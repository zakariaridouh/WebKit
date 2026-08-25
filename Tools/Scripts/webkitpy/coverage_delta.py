#!/usr/bin/env python3
#
# Copyright (C) 2026 Apple Inc. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY APPLE INC. AND ITS CONTRIBUTORS ``AS IS''
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL APPLE INC. OR ITS CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF
# THE POSSIBILITY OF SUCH DAMAGE.

"""Differential coverage: what a change did to the numbers.

A coverage report answers "what is covered". The question asked in review is "what did
this change do to coverage", and "is the code I just wrote tested". llvm-cov cannot
answer either, because it has no notion of a baseline. This module compares two parsed
lcov datasets and answers them.

The subtle part is that the denominator moves. If a file grows by a hundred uncovered
lines then a hundred lines are uncovered that were not before, but nothing regressed --
the author added untested code. That is a different problem from a line that used to be
executed and no longer is, and it wants a different fix, so the two are counted
separately throughout and never added together:

  * regressed lines      -- present in BOTH traces, executed in baseline, not in current.
                            An existing test stopped reaching existing code. A regression.
  * new uncovered lines  -- present ONLY in current, never executed. Untested new code.
                            A gap, not a regression.
  * recovered lines      -- present in both, newly executed. The mirror of a regression.
  * new covered lines    -- only in current, executed. New code that is tested.
  * removed lines        -- only in baseline. Left the denominator entirely.

LIMITATION, and it is not a small one: lcov identifies a line by its number, so this
compares line NUMBERS across two runs. That is exact for a file whose source did not
change between them and only approximate for one that did -- inserting a line near the
top shifts every line below it, and the shifted numbers then look like a mixture of
regressions and new lines even though nothing about the coverage changed. So for the
files a change actually touched, the honest primary metric is not the line-level
attribution but the coverage of the lines present in current, which is well defined
either way; the attribution is a hint about where to look. Function-level regressions
are keyed by mangled name rather than by line, so those do survive an edit, and are
reported separately for that reason.
"""

import html
import logging
import os

from webkitpy.coverage_directory_index import SORT_SCRIPT, REPORT_STYLE, format_percent, meter_html

logger = logging.getLogger(__name__)

# The three metrics FileCoverage.totals() reports. lcov carries no region data.
METRICS = ('lines', 'functions', 'branches')

NEW = 'new'
DELETED = 'deleted'
REGRESSED = 'regressed'
IMPROVED = 'improved'
UNCHANGED = 'unchanged'

# Only used to order the report, worst first.
_STATUS_RANK = {REGRESSED: 0, NEW: 1, DELETED: 2, IMPROVED: 3, UNCHANGED: 4}

# Source kinds llvm-cov can have data for. A change also touches tests, expectations and
# build files, and listing those as "no coverage data" would bury the real answer.
SOURCE_EXTENSIONS = ('.c', '.cc', '.cpp', '.cxx', '.m', '.mm', '.h', '.hh', '.hpp', '.hxx')


def _percent(count, covered):
    return (100.0 * covered / count) if count else None


def _side(totals, metric):
    """(count, covered) for a metric, or zeros when the file is absent on that side."""
    return totals[metric] if totals else (0, 0)


def _difference(before, after):
    """after - before, or None when either side is undefined."""
    if before is None or after is None:
        return None
    return after - before


class FileDelta:
    """What changed for one file, at both the summary and the individual-line level."""
    __slots__ = ('path', 'status', 'baseline_totals', 'current_totals', 'regressed_lines',
                 'recovered_lines', 'new_uncovered_lines', 'new_covered_lines',
                 'removed_lines', 'regressed_functions')

    def __init__(self, path, baseline_coverage, current_coverage):
        self.path = path
        # None, not zeros, when the file is absent from that trace: a file with no
        # instrumented lines and a file that does not exist are not the same thing.
        self.baseline_totals = baseline_coverage.totals() if baseline_coverage else None
        self.current_totals = current_coverage.totals() if current_coverage else None

        baseline_lines = baseline_coverage.lines if baseline_coverage else {}
        current_lines = current_coverage.lines if current_coverage else {}
        both = baseline_lines.keys() & current_lines.keys()

        # Lines in both traces: the only place a real regression can be observed, because
        # it is the only place the same line exists on both sides to compare.
        self.regressed_lines = sorted(n for n in both if baseline_lines[n] and not current_lines[n])
        self.recovered_lines = sorted(n for n in both if not baseline_lines[n] and current_lines[n])

        # Lines that entered the denominator. An uncovered one is a gap in new code, which
        # is a separate finding from a regression and must not be summed with one.
        entered = current_lines.keys() - baseline_lines.keys()
        self.new_uncovered_lines = sorted(n for n in entered if not current_lines[n])
        self.new_covered_lines = sorted(n for n in entered if current_lines[n])
        self.removed_lines = sorted(baseline_lines.keys() - current_lines.keys())

        # Mangled names survive a source edit, so unlike every line number above this
        # stays meaningful for a file whose contents moved.
        baseline_functions = baseline_coverage.functions if baseline_coverage else {}
        current_functions = current_coverage.functions if current_coverage else {}
        self.regressed_functions = sorted(
            name for name in baseline_functions.keys() & current_functions.keys()
            if baseline_functions[name] and not current_functions[name])

        self.status = self._classify()

    def _classify(self):
        if self.baseline_totals is None:
            return NEW
        if self.current_totals is None:
            return DELETED
        # A specific line stopped being executed. That outranks the percentage, which can
        # sit still or even rise while individual lines regress -- and the line is what a
        # reviewer has to go and look at. Both counts are kept either way, so the report
        # can still show that this file gained coverage in one place and lost it in
        # another; only the one-word headline has to pick.
        if self.regressed_lines:
            return REGRESSED
        if self.recovered_lines:
            return IMPROVED
        change = self.percent_delta()
        if change is None:
            # No instrumented lines on one side or the other; fall back to raw counts.
            return IMPROVED if self.covered_delta > 0 else UNCHANGED
        if change < 0:
            # Nothing lost coverage line for line, so the percentage can only have fallen
            # because the file grew by lines that are not covered.
            return REGRESSED
        if change > 0:
            return IMPROVED
        return UNCHANGED

    def baseline_percent(self, metric='lines'):
        return _percent(*_side(self.baseline_totals, metric))

    def current_percent(self, metric='lines'):
        return _percent(*_side(self.current_totals, metric))

    def percent_delta(self, metric='lines'):
        return _difference(self.baseline_percent(metric), self.current_percent(metric))

    @property
    def denominator_delta(self):
        return _side(self.current_totals, 'lines')[0] - _side(self.baseline_totals, 'lines')[0]

    @property
    def covered_delta(self):
        return _side(self.current_totals, 'lines')[1] - _side(self.baseline_totals, 'lines')[1]

    @property
    def uncovered_delta(self):
        """Change in the number of uncovered lines, from any cause.

        One number that captures both a regression and newly added untested code, which
        makes it the right thing to sort a report by even though it deliberately blurs a
        distinction the rest of this module is careful to keep.
        """
        return self.denominator_delta - self.covered_delta


class DeltaTotals:
    """Baseline and current totals plus line-level counters, for a directory or a report."""
    __slots__ = ('baseline', 'current', 'file_count', 'statuses', 'regressed_line_count',
                 'recovered_line_count', 'new_uncovered_line_count', 'new_covered_line_count',
                 'removed_line_count', 'regressed_function_count', 'files_with_regressions')

    def __init__(self):
        self.baseline = {metric: [0, 0] for metric in METRICS}
        self.current = {metric: [0, 0] for metric in METRICS}
        self.file_count = 0
        self.statuses = {status: 0 for status in _STATUS_RANK}
        self.regressed_line_count = 0
        self.recovered_line_count = 0
        self.new_uncovered_line_count = 0
        self.new_covered_line_count = 0
        self.removed_line_count = 0
        self.regressed_function_count = 0
        self.files_with_regressions = 0

    def add(self, file_delta):
        self.file_count += 1
        self.statuses[file_delta.status] += 1
        for metric in METRICS:
            for accumulator, totals in ((self.baseline, file_delta.baseline_totals),
                                        (self.current, file_delta.current_totals)):
                count, covered = _side(totals, metric)
                accumulator[metric][0] += count
                accumulator[metric][1] += covered
        self.regressed_line_count += len(file_delta.regressed_lines)
        self.recovered_line_count += len(file_delta.recovered_lines)
        self.new_uncovered_line_count += len(file_delta.new_uncovered_lines)
        self.new_covered_line_count += len(file_delta.new_covered_lines)
        self.removed_line_count += len(file_delta.removed_lines)
        self.regressed_function_count += len(file_delta.regressed_functions)
        if file_delta.regressed_lines:
            self.files_with_regressions += 1

    def baseline_percent(self, metric='lines'):
        return _percent(*self.baseline[metric])

    def current_percent(self, metric='lines'):
        return _percent(*self.current[metric])

    def percent_delta(self, metric='lines'):
        return _difference(self.baseline_percent(metric), self.current_percent(metric))

    @property
    def denominator_delta(self):
        return self.current['lines'][0] - self.baseline['lines'][0]

    @property
    def covered_delta(self):
        return self.current['lines'][1] - self.baseline['lines'][1]

    @property
    def uncovered_delta(self):
        return self.denominator_delta - self.covered_delta


class CoverageDelta:
    """The comparison of two traces, optionally focused on a set of changed files.

    `overall` always covers every file in either trace, so even a focused run can show
    what happened to the project as a whole. `scope` covers the focused set, and is the
    same object as `overall` when there is no focus.
    """

    def __init__(self, file_deltas, requested_paths=None, missing_paths=(), ignored_path_count=0):
        self.file_deltas = file_deltas
        self.missing_paths = sorted(missing_paths)
        self.ignored_path_count = ignored_path_count
        self.overall = DeltaTotals()
        for file_delta in file_deltas.values():
            self.overall.add(file_delta)

        if requested_paths is None:
            self.scope_paths = sorted(file_deltas)
            self.scope = self.overall
        else:
            self.scope_paths = sorted(path for path in requested_paths if path in file_deltas)
            self.scope = DeltaTotals()
            for path in self.scope_paths:
                self.scope.add(file_deltas[path])

    @property
    def focused(self):
        return self.scope is not self.overall

    def files_to_report(self):
        """The file deltas worth showing, worst first.

        A focused run lists every file asked about, including ones whose coverage did not
        move, because "the file you touched is unchanged" is an answer. An unfocused run
        cannot: WebKit has ~18,000 covered files, and listing the unchanged ones would
        bury the handful that moved.
        """
        deltas = [self.file_deltas[path] for path in self.scope_paths]
        if not self.focused:
            deltas = [delta for delta in deltas if delta.status != UNCHANGED]
        return sorted(deltas, key=lambda delta: (-len(delta.regressed_lines),
                                                 -delta.uncovered_delta,
                                                 _STATUS_RANK[delta.status],
                                                 delta.path))

    def directory_totals(self):
        """{directory: DeltaTotals} over the reported files, keyed by immediate parent.

        Parents are not rolled up into their ancestors. A delta touches few files, so a
        flat list of the directories involved is more skimmable than a drill-down tree,
        and rolling up would report the same lines again at every level above them.
        """
        totals = {}
        for file_delta in self.files_to_report():
            totals.setdefault(os.path.dirname(file_delta.path), DeltaTotals()).add(file_delta)
        return totals


def compare(baseline_files, current_files, changed_files=None):
    """Compare {path: FileCoverage} datasets. Both must already be canonicalized alike.

    Two traces from different builds disagree about where a copied header lives, so run
    both through one PathCanonicalizer with the same checkout root before calling this, or
    every such header looks like a deleted file plus an unrelated new one.
    """
    paths = set(baseline_files) | set(current_files)
    file_deltas = {path: FileDelta(path, baseline_files.get(path), current_files.get(path))
                   for path in paths}

    if changed_files is None:
        return CoverageDelta(file_deltas)

    requested = set(changed_files)
    interesting = {path for path in requested if path.endswith(SOURCE_EXTENSIONS)}
    return CoverageDelta(file_deltas,
                         requested_paths=interesting,
                         missing_paths=interesting - paths,
                         ignored_path_count=len(requested) - len(interesting))


class TracePaths:
    """One trace's paths on the way in: rebased onto this checkout, and checked against it.

    Two jobs, both about a trace whose paths were not produced by this checkout, and both
    outside PathCanonicalizer because they are about which checkout a trace came from rather
    than about where a copied header lives.

    Rebasing makes a foreign trace usable at all. A baseline needs no binaries -- 54 MB
    gzipped for a full-suite run, against the 45 GB build tree -- so the whole point of one
    is to keep it after the tree is gone, or to take it from a bot or a colleague. Its paths
    are then rooted somewhere else, and without rebasing every file in it is a file the
    current trace has never heard of.

    Counting how many paths were under the expected root catches a wrong --source-root,
    which is otherwise silent and does move the numbers. The copied-header rules rewrite
    <build>/usr/local/include/wtf/X.h to <source root>/Source/WTF/wtf/X.h, and that record
    only unions with the in-tree Source/WTF/wtf/X.h record when the root matches; measured
    over the same two traces, three different --source-root values gave 3,680 against 3,981
    files and 752,036 against 767,366 lines, a 0.17pp swing, with nothing said about it.
    """

    def __init__(self, canonicalizer=None, source_root=None, foreign_root=None):
        self._canonicalizer = canonicalizer
        self._source_root = source_root.rstrip('/') if source_root else None
        self._foreign_root = foreign_root.rstrip('/') if foreign_root else None
        self.path_count = 0
        self.rebased_count = 0
        self.under_source_root_count = 0

    def canonicalize(self, path):
        self.path_count += 1
        if self._foreign_root and self._source_root and \
                path.startswith(self._foreign_root + '/'):
            path = self._source_root + path[len(self._foreign_root):]
            self.rebased_count += 1
        if self._source_root and path.startswith(self._source_root + '/'):
            self.under_source_root_count += 1
        return self._canonicalizer.canonicalize(path) if self._canonicalizer else path

    def log_findings(self, label, lcov_path):
        if self._foreign_root:
            if self.rebased_count:
                logger.info('Rebased %d of the %d paths in the %s trace from %s onto %s',
                            self.rebased_count, self.path_count, label, self._foreign_root,
                            self._source_root)
            else:
                logger.warning('--baseline-source-root %s is not a prefix of any of the %d '
                               'paths in %s, so nothing was rebased. Name the checkout that '
                               'trace was produced in.',
                               self._foreign_root, self.path_count, lcov_path)
        if self.path_count and self._source_root and not self.under_source_root_count:
            logger.warning(
                'None of the %d paths in the %s trace %s are under --source-root %s. That '
                'root decides where a copied header is placed -- '
                '<build>/usr/local/include/wtf/X.h becomes <root>/Source/WTF/wtf/X.h -- and '
                'a record placed under the wrong root does not union with the in-tree one, '
                'which moves the totals silently: measured over one pair of traces, three '
                'different --source-root values gave 3,680 against 3,981 files and 752,036 '
                'against 767,366 lines, a 0.17pp swing.',
                self.path_count, label, lcov_path, self._source_root)


def check_traces_fit(baseline_files, current_files, baseline_path, current_path):
    """Raise unless the two traces have at least one source path in common.

    Zero overlap is not a comparison with a lot of new files in it, it is two traces about
    different things, and everything downstream of it is meaningless: measured with a
    baseline whose root was rewritten to look like a bot artifact, every file reported as
    `new`, the baseline contributed nothing to any total, --fail-under-delta could not be
    evaluated -- and the tool exited 0. A gate that passes because it could not find the
    baseline is worse than no gate.
    """
    if baseline_files.keys() & current_files.keys():
        return
    raise RuntimeError(
        'The baseline and current traces have no source file in common, so there is nothing '
        'to compare: {} has {} path(s) such as {}, and {} has {} path(s) such as {}. Every '
        'file would be reported as new and the baseline would contribute nothing to any '
        'total. If the baseline was produced in another checkout, pass '
        '--baseline-source-root=<that checkout>.'.format(
            baseline_path, len(baseline_files), min(baseline_files),
            current_path, len(current_files), min(current_files)))


def compare_lcov_files(baseline_path, current_path, source_root=None, changed_files=None,
                       baseline_source_root=None):
    """Parse and compare two lcov traces, canonicalizing both against one checkout."""
    from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov

    canonicalizer = PathCanonicalizer(source_root) if source_root else None

    baseline_paths = TracePaths(canonicalizer, source_root, baseline_source_root)
    baseline_files = parse_lcov(baseline_path, baseline_paths)
    if not baseline_files:
        raise RuntimeError('{} contained no coverage records'.format(baseline_path))
    baseline_paths.log_findings('baseline', baseline_path)

    current_paths = TracePaths(canonicalizer, source_root)
    current_files = parse_lcov(current_path, current_paths)
    if not current_files:
        raise RuntimeError('{} contained no coverage records'.format(current_path))
    current_paths.log_findings('current', current_path)

    if canonicalizer:
        canonicalizer.log_summary()
    check_traces_fit(baseline_files, current_files, baseline_path, current_path)
    return compare(baseline_files, current_files, changed_files)


def absolute_paths(paths, source_root):
    """Resolve a list of possibly-repository-relative paths against the checkout root."""
    resolved = set()
    for path in paths:
        path = path.strip()
        if not path:
            continue
        resolved.add(path if os.path.isabs(path)
                     else os.path.normpath(os.path.join(source_root, path)))
    return resolved


def format_line_numbers(numbers, limit=12):
    """[1, 2, 3, 7] -> '1-3, 7'. Nobody reads two hundred comma-separated integers."""
    if not numbers:
        return ''
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append((start, previous))
        start = previous = number
    ranges.append((start, previous))

    shown = ['{}'.format(low) if low == high else '{}-{}'.format(low, high)
             for low, high in ranges[:limit]]
    if len(ranges) > limit:
        shown.append('and {} more'.format(len(ranges) - limit))
    return ', '.join(shown)


def _column_percent(value):
    """A percentage in a fixed-width text column, or a dash of the same width."""
    return '     -' if value is None else '{:6.2f}%'.format(value)


def _column_delta(value):
    return '       -' if value is None else '{:+6.2f}pp'.format(value)


def _display_path(path, source_root):
    if source_root and path.startswith(source_root.rstrip('/') + '/'):
        return os.path.relpath(path, source_root)
    return path


def format_summary(delta, source_root=None, max_files=25):
    """A text summary, sized and shaped to be pasted into a review comment."""
    lines = []
    scope = delta.scope
    count, covered = scope.current['lines']

    if delta.focused:
        lines.append('Coverage delta for {} changed file{} with coverage data'.format(
            scope.file_count, '' if scope.file_count == 1 else 's'))
        # The metric that stays honest when the source itself changed: no line-number
        # matching goes into it, so an edit that shifted every line cannot distort it.
        lines.append('  coverage of lines present in current: {} ({:,} of {:,} lines)'.format(
            _column_percent(_percent(count, covered)).strip(), covered, count))
    else:
        lines.append('Coverage delta over {:,} files'.format(scope.file_count))

    lines.append('  lines {} -> {}  ({})'.format(
        _column_percent(scope.baseline_percent()).strip(),
        _column_percent(scope.current_percent()).strip(),
        _column_delta(scope.percent_delta()).strip()))
    lines.append('  denominator {:,} -> {:,} ({:+,})   covered {:,} -> {:,} ({:+,})'.format(
        scope.baseline['lines'][0], scope.current['lines'][0], scope.denominator_delta,
        scope.baseline['lines'][1], scope.current['lines'][1], scope.covered_delta))
    if delta.focused:
        lines.append('  whole project: {} -> {} ({})'.format(
            _column_percent(delta.overall.baseline_percent()).strip(),
            _column_percent(delta.overall.current_percent()).strip(),
            _column_delta(delta.overall.percent_delta()).strip()))

    lines.append('')
    lines.append('Regressions, lines that were covered and are not any more: {:,} in {:,} file{}'.format(
        scope.regressed_line_count, scope.files_with_regressions,
        '' if scope.files_with_regressions == 1 else 's'))
    lines.append('Gaps in new code, lines only in current and never executed: {:,}'.format(
        scope.new_uncovered_line_count))
    lines.append('Newly covered: {:,} existing lines, {:,} new lines'.format(
        scope.recovered_line_count, scope.new_covered_line_count))
    if scope.regressed_function_count:
        lines.append('Functions that lost coverage: {:,} (matched by name, so unaffected by '
                     'line shifts)'.format(scope.regressed_function_count))

    reported = delta.files_to_report()
    if reported:
        lines.append('')
        lines.append('{:<9} {:>7} {:>7} {:>8}  {:>5} {:>5}  {}'.format(
            'STATUS', 'BEFORE', 'AFTER', 'DELTA', 'REGR', 'NEW-U', 'FILE'))
        for file_delta in reported[:max_files]:
            lines.append('{:<9} {} {} {}  {:>5} {:>5}  {}'.format(
                file_delta.status,
                _column_percent(file_delta.baseline_percent()),
                _column_percent(file_delta.current_percent()),
                _column_delta(file_delta.percent_delta()),
                len(file_delta.regressed_lines),
                len(file_delta.new_uncovered_lines),
                _display_path(file_delta.path, source_root)))
            if file_delta.regressed_lines:
                lines.append('{:<9} covered -> uncovered at {}'.format(
                    '', format_line_numbers(file_delta.regressed_lines, limit=6)))
        remaining = len(reported) - max_files
        if remaining > 0:
            lines.append('... and {:,} more file{}'.format(remaining, '' if remaining == 1 else 's'))

    if delta.missing_paths:
        lines.append('')
        one = len(delta.missing_paths) == 1
        lines.append('{} changed source file{} had no coverage data in either trace, so nothing '
                     'instrumented includes {}:'.format(len(delta.missing_paths),
                                                        '' if one else 's', 'it' if one else 'them'))
        for path in delta.missing_paths[:max_files]:
            lines.append('  {}'.format(_display_path(path, source_root)))
        if len(delta.missing_paths) > max_files:
            lines.append('  ... and {} more'.format(len(delta.missing_paths) - max_files))

    if scope.regressed_line_count or scope.new_uncovered_line_count:
        lines.append('')
        lines.append('Line numbers are compared as numbers, so for a file whose source changed '
                     'between the two runs the per-line attribution is approximate. The coverage '
                     'of the lines present in current is not.')
    return '\n'.join(lines) + '\n'


# --- HTML report -----------------------------------------------------------------------
#
# REPORT_STYLE, SORT_SCRIPT, meter_html and format_percent are imported from
# coverage_directory_index rather than copied, so the delta report and the directory index
# cannot drift apart. Only what a delta needs and an absolute report does not is added here.

# One new color role: the diverging pair from the reference palette. Blue is the positive
# arm, and is already --meter-fill, so improvement reuses the existing token exactly; red is
# the negative one. The pair validates in both modes -- worst CVD deltaE 21.6 light and 19.2
# dark against a >= 8 target -- where the intuitive green/red pair measures 4.1 and is
# unusable for a deuteranopic reader. light-dark() picks the mode's step off the
# color-scheme the imported style already sets in all three of its scopes, so the OS
# setting and the theme override are both handled without restating either.
_DELTA_STYLE = """
:root {
  --delta-positive: var(--meter-fill);
  --delta-negative: #e34948;
  --delta-negative: light-dark(#e34948, #e66767);
}
.wrap { max-width: 1280px; }
.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 16px; }
.tile {
  flex: 1 1 180px; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 11px 13px;
}
.tile .k {
  font-size: 11px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em; margin: 0;
}
.tile .v { font-size: 21px; font-weight: 600; font-variant-numeric: tabular-nums; margin: 3px 0 1px; }
.tile .s { font-size: 11px; color: var(--text-secondary); margin: 0; }
.up { color: var(--delta-positive); }
.down { color: var(--delta-negative); }
h2 { font-size: 13px; font-weight: 600; margin: 22px 0 8px; }
.dbar {
  position: relative; width: 120px; height: 8px;
  background: var(--meter-track); border-radius: 4px; overflow: hidden;
}
.dbar::before {
  content: ""; position: absolute; left: 50%; top: 0; bottom: 0;
  width: 1px; background: var(--muted); opacity: .55;
}
.dbar > i { position: absolute; top: 0; bottom: 0; display: block; border-radius: 4px; }
.dbar > i.up { background: var(--delta-positive); }
.dbar > i.down { background: var(--delta-negative); }
.tag {
  display: inline-block; font-size: 11px; font-weight: 600;
  padding: 1px 6px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--page); white-space: nowrap;
}
.detail { padding: 9px 12px; border-bottom: 1px solid var(--gridline); }
.detail:last-child { border-bottom: 0; }
.detail p { margin: 0; color: var(--text-secondary); font-size: 12px; }
.detail p.p { font-weight: 600; color: var(--text-primary); margin-bottom: 3px; }
.detail code { font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--text-primary); }
.legend { color: var(--text-secondary); font-size: 11px; margin: 0 0 8px; }
.legend i {
  display: inline-block; width: 22px; height: 8px; border-radius: 4px;
  vertical-align: middle; margin: 0 3px 0 8px;
}
.empty { padding: 14px 12px; color: var(--text-secondary); margin: 0; }
"""

# (label, css class, sorts numerically). Column indices are assigned in order.
_LEADING_FILE_HEADERS = (('File', '', False), ('Status', '', False))
_LEADING_DIRECTORY_HEADERS = (('Directory', '', False), ('Files', 'n', True))
_SHARED_HEADERS = (
    ('Change', '', True),
    ('Delta', 'n', True),
    ('Before', 'n', True),
    ('After', 'n', True),
    ('Coverage now', '', True),
    ('Lines', 'n', True),
    ('Covered -> uncovered', 'n', True),
    ('New uncovered', 'n', True),
)


def _percent_cell(value):
    return '<td class="n pct" data-v="{}">{}</td>'.format(
        -1 if value is None else '{:.4f}'.format(value), format_percent(value))


def _delta_bar_cell(value, scale, before, after):
    """A diverging bar around a zero rule, scaled to the largest change on the page.

    A percentage-point delta is almost always a fraction of a point, so a bar drawn
    against the full -100..100 range would be invisible on every row. The scale is stated
    in the page footnote, and every bar is labelled with its own signed number in the next
    column, so the sign is never carried by color alone.
    """
    tooltip = html.escape('{} -> {}'.format(format_percent(before), format_percent(after)))
    if not value:
        return '<td data-v="0"><div class="dbar" title="{}"></div></td>'.format(tooltip)
    # A floor, so a small but real change is never drawn as no change at all. One outlier
    # otherwise squashes every other bar on the page to nothing.
    width = max(3.0, min(50.0, 50.0 * abs(value) / scale)) if scale else 0.0
    edge, css = ('left', 'up') if value > 0 else ('right', 'down')
    return ('<td data-v="{:.4f}"><div class="dbar" title="{}">'
            '<i class="{}" style="{}:50%;width:{:.2f}%"></i></div></td>'.format(
                value, tooltip, css, edge, width))


def _shared_cells(totals, scale):
    before = totals.baseline_percent()
    after = totals.current_percent()
    change = totals.percent_delta()
    direction = '' if not change else ('up' if change > 0 else 'down')
    return [
        _delta_bar_cell(change, scale, before, after),
        '<td class="{}" data-v="{}">{}</td>'.format(
            ('n pct ' + direction).strip(), -1000 if change is None else '{:.4f}'.format(change),
            '-' if change is None else '{:+.2f}pp'.format(change)),
        _percent_cell(before),
        _percent_cell(after),
        '<td data-v="{}">{}</td>'.format(
            -1 if after is None else '{:.4f}'.format(after), meter_html(after)),
        '<td class="n" data-v="{}">{:,} ({:+,})</td>'.format(
            totals.denominator_delta, totals.current['lines'][0], totals.denominator_delta),
        '<td class="{}" data-v="{}">{:,}</td>'.format(
            'n down' if totals.regressed_line_count else 'n',
            totals.regressed_line_count, totals.regressed_line_count),
        '<td class="n" data-v="{}">{:,}</td>'.format(
            totals.new_uncovered_line_count, totals.new_uncovered_line_count),
    ]


def _file_row(file_delta, scale, source_root):
    # A FileDelta exposes the same percentage accessors DeltaTotals does but none of the
    # aggregate counters the shared cells need, so wrap it in a DeltaTotals of one.
    totals = DeltaTotals()
    totals.add(file_delta)
    label = _display_path(file_delta.path, source_root)
    status_css = ('down' if file_delta.status in (REGRESSED, DELETED)
                  else ('up' if file_delta.status == IMPROVED else ''))
    cells = ['<td data-v="{0}">{0}</td>'.format(html.escape(label)),
             '<td data-v="{0}"><span class="{1}">{0}</span></td>'.format(
                 file_delta.status, ('tag ' + status_css).strip())]
    return '<tr>' + ''.join(cells + _shared_cells(totals, scale)) + '</tr>'


def _directory_row(label, totals, scale):
    cells = ['<td class="dir" data-v="{0}">{0}</td>'.format(html.escape(label)),
             '<td class="n" data-v="{0}">{0:,}</td>'.format(totals.file_count)]
    return '<tr>' + ''.join(cells + _shared_cells(totals, scale)) + '</tr>'


def _table(leading_headers, rows, empty_message):
    if not rows:
        return '<div class="card"><p class="empty">{}</p></div>'.format(html.escape(empty_message))
    headers = leading_headers + _SHARED_HEADERS
    header_cells = ['<th class="{}" data-col="{}" data-numeric="{}">{}</th>'.format(
        css, index, '1' if numeric else '0', html.escape(label))
        for index, (label, css, numeric) in enumerate(headers)]
    return ('<div class="card"><table><thead><tr>{}</tr></thead><tbody>\n{}\n'
            '</tbody></table></div>'.format(''.join(header_cells), '\n'.join(rows)))


def _tile(key, value, subtitle, css=''):
    return ('<div class="tile"><p class="k">{}</p><p class="{}">{}</p>'
            '<p class="s">{}</p></div>'.format(html.escape(key), ('v ' + css).strip(),
                                               html.escape(value), html.escape(subtitle)))


def _tiles(scope):
    change = scope.percent_delta()
    if change is None:
        headline, css = '-', ''
    else:
        # The glyph, not the color, is what carries the sign for a reader who cannot
        # tell the two arms of the bars apart.
        headline = '{}{:+.2f}pp'.format('▲ ' if change > 0 else
                                        ('▼ ' if change < 0 else ''), change)
        css = '' if not change else ('up' if change > 0 else 'down')

    newly_covered = scope.recovered_line_count + scope.new_covered_line_count
    return '<div class="tiles">' + ''.join((
        _tile('Line coverage', headline, '{} to {}'.format(
            format_percent(scope.baseline_percent()), format_percent(scope.current_percent())),
            css),
        _tile('Regressed lines', '{:,}'.format(scope.regressed_line_count),
              'were covered, now are not, in {:,} file{}'.format(
                  scope.files_with_regressions, '' if scope.files_with_regressions == 1 else 's'),
              'down' if scope.regressed_line_count else ''),
        _tile('Uncovered new lines', '{:,}'.format(scope.new_uncovered_line_count),
              'only in current, never executed'),
        _tile('Newly covered', '{:,}'.format(newly_covered),
              '{:,} existing, {:,} new'.format(scope.recovered_line_count,
                                               scope.new_covered_line_count),
              'up' if newly_covered else ''),
        _tile('Files compared', '{:,}'.format(scope.file_count),
              '{:,} regressed, {:,} improved, {:,} new, {:,} deleted, {:,} unchanged'.format(
                  scope.statuses[REGRESSED], scope.statuses[IMPROVED], scope.statuses[NEW],
                  scope.statuses[DELETED], scope.statuses[UNCHANGED])),
    )) + '</div>'


def _details(reported, source_root, line_limit):
    """Per-file line numbers. The specific lines are the point of the whole report."""
    blocks = []
    for file_delta in reported:
        if not file_delta.regressed_lines and not file_delta.regressed_functions:
            continue
        parts = ['<p class="p">{}</p>'.format(
            html.escape(_display_path(file_delta.path, source_root)))]
        if file_delta.regressed_lines:
            parts.append('<p><span class="down">covered &rarr; uncovered</span> '
                         '<code>{}</code></p>'.format(html.escape(
                             format_line_numbers(file_delta.regressed_lines, limit=line_limit))))
        if file_delta.new_uncovered_lines:
            parts.append('<p>new, never executed <code>{}</code></p>'.format(
                html.escape(format_line_numbers(file_delta.new_uncovered_lines,
                                                limit=line_limit))))
        if file_delta.regressed_functions:
            parts.append('<p>functions that lost coverage <code>{}</code></p>'.format(
                html.escape(', '.join(file_delta.regressed_functions[:8]))))
        blocks.append('<div class="detail">' + ''.join(parts) + '</div>')
    if not blocks:
        return '<div class="card"><p class="empty">No line went from covered to uncovered.</p></div>'
    return '<div class="card">' + ''.join(blocks) + '</div>'


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}{delta_style}</style>
</head>
<body>
<div class="wrap">
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
{tiles}
<p class="legend">Change bars diverge from a zero rule, scaled to the largest change on this
page ({scale}); each is labelled with its own value.
<i style="background:var(--delta-negative)"></i>coverage fell
<i style="background:var(--delta-positive)"></i>coverage rose. Click a column heading to sort.</p>
<h2>Directories</h2>
{directories}
<h2>Files</h2>
{files}
<h2>Lines that stopped being covered</h2>
{details}
<p class="hint">{note}</p>
</div>
<script>{script}</script>
</body>
</html>
"""

_NOTE = (
    'A regressed line was present in both traces, executed in the baseline and not in the '
    'current run: an existing test stopped reaching existing code. An uncovered new line is '
    'only in the current trace and was never executed: new code without a test. They are '
    'counted separately and never added together, because they call for different fixes. '
    'lcov identifies a line by its number, so for a file whose source changed between the two '
    'runs the per-line attribution is approximate -- an inserted line shifts everything below '
    'it. The After column, the coverage of the lines present in the current run, does not '
    'depend on matching line numbers and is exact. Function-level regressions are matched by '
    'mangled name and survive an edit.')


def write_delta_report(delta, output_directory, source_root=None, line_limit=24):
    """Write the HTML delta report. Returns the path to the page."""
    reported = delta.files_to_report()
    directories = sorted(delta.directory_totals().items(),
                         key=lambda entry: (-entry[1].uncovered_delta, entry[0]))

    # One scale for both tables, so a row in the directory table is directly comparable
    # with a row in the file table. Floored at a point, or a report whose largest change
    # is a hundredth of a point would draw that change as a full-width bar.
    changes = [totals.percent_delta() for _, totals in directories]
    changes += [file_delta.percent_delta() for file_delta in reported]
    scale = max([abs(change) for change in changes if change is not None] + [1.0])

    title = ('Coverage delta: {:,} changed files'.format(delta.scope.file_count)
             if delta.focused else 'Coverage delta')
    subtitle = ('{:,} lines in the current trace ({:+,}), {:,} of them uncovered ({:+,})'.format(
        delta.scope.current['lines'][0], delta.scope.denominator_delta,
        delta.scope.current['lines'][0] - delta.scope.current['lines'][1],
        delta.scope.uncovered_delta))
    note = _NOTE
    if delta.missing_paths:
        note += (' {} changed source file(s) had no coverage data in either trace and are not '
                 'listed: nothing instrumented includes them.'.format(len(delta.missing_paths)))

    page = _PAGE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        style=REPORT_STYLE,
        delta_style=_DELTA_STYLE,
        tiles=_tiles(delta.scope),
        scale=html.escape('±{:.2f}pp'.format(scale)),
        directories=_table(_LEADING_DIRECTORY_HEADERS,
                           [_directory_row(_display_path(path, source_root) or '/', totals, scale)
                            for path, totals in directories],
                           'No directory changed.'),
        files=_table(_LEADING_FILE_HEADERS,
                     [_file_row(file_delta, scale, source_root) for file_delta in reported],
                     'No file changed coverage.'),
        details=_details(reported, source_root, line_limit),
        note=html.escape(note),
        script=SORT_SCRIPT)

    os.makedirs(output_directory, exist_ok=True)
    page_path = os.path.join(output_directory, 'index.html')
    with open(page_path, 'w') as handle:
        handle.write(page)
    return page_path

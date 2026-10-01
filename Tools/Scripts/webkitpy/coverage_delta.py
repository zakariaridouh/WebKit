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

"""Differential coverage between two parsed lcov datasets.

Regressed lines and new uncovered lines are counted separately; lines are matched by number."""

import html
import logging
import os
from urllib.parse import quote

from webkitpy.coverage_directory_index import (
    REPORT_STYLE, SORT_SCRIPT, format_percent, headers_html, meter_html)

logger = logging.getLogger(__name__)

# Not index.html or summary.txt: a report directory already holds both.
DELTA_REPORT_NAME = 'delta-coverage.html'
DELTA_SUMMARY_NAME = 'delta-summary.txt'

METRICS = ('lines', 'functions', 'branches')

NEW = 'new'
DELETED = 'deleted'
REGRESSED = 'regressed'
IMPROVED = 'improved'
UNCHANGED = 'unchanged'

_STATUS_RANK = {REGRESSED: 0, NEW: 1, DELETED: 2, IMPROVED: 3, UNCHANGED: 4}

# Kinds llvm-cov has data for; listing tests and build files as "no data" would bury the answer.
SOURCE_EXTENSIONS = ('.c', '.cc', '.cpp', '.cxx', '.m', '.mm', '.h', '.hh', '.hpp', '.hxx')


def _percent(count, covered):
    return (100.0 * covered / count) if count else None


def _side(totals, metric):
    return totals[metric] if totals else (0, 0)


def _difference(before, after):
    if before is None or after is None:
        return None
    return after - before


class FileDelta:
    __slots__ = ('path', 'status', 'baseline_totals', 'current_totals', 'regressed_lines',
                 'recovered_lines', 'new_uncovered_lines', 'new_covered_lines',
                 'removed_lines', 'regressed_functions')

    def __init__(self, path, baseline_coverage, current_coverage):
        self.path = path
        # None, not zeros, when absent: a file with no instrumented lines still exists.
        self.baseline_totals = baseline_coverage.totals() if baseline_coverage else None
        self.current_totals = current_coverage.totals() if current_coverage else None

        baseline_lines = baseline_coverage.lines if baseline_coverage else {}
        current_lines = current_coverage.lines if current_coverage else {}
        both = baseline_lines.keys() & current_lines.keys()

        self.regressed_lines = sorted(n for n in both if baseline_lines[n] and not current_lines[n])
        self.recovered_lines = sorted(n for n in both if not baseline_lines[n] and current_lines[n])

        entered = current_lines.keys() - baseline_lines.keys()
        self.new_uncovered_lines = sorted(n for n in entered if not current_lines[n])
        self.new_covered_lines = sorted(n for n in entered if current_lines[n])
        self.removed_lines = sorted(baseline_lines.keys() - current_lines.keys())

        # Mangled names survive a source edit, unlike line numbers.
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
        # A regressed line outranks the percentage, which can hold or rise while lines regress.
        if self.regressed_lines:
            return REGRESSED
        if self.recovered_lines:
            return IMPROVED
        change = self.percent_delta()
        if change is None:
            return IMPROVED if self.covered_delta > 0 else UNCHANGED
        if change < 0:
            # No line regressed, so the fall comes from new uncovered lines.
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
        """Change in uncovered lines from any cause; regressions and new untested code together."""
        return self.denominator_delta - self.covered_delta


class DeltaTotals:
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
    """`overall` covers every file; `scope` the focused set, or `overall` when unfocused."""

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
        """Worst first. A focused run keeps unchanged files; an unfocused run drops them."""
        deltas = [self.file_deltas[path] for path in self.scope_paths]
        if not self.focused:
            deltas = [delta for delta in deltas if delta.status != UNCHANGED]
        return sorted(deltas, key=lambda delta: (-len(delta.regressed_lines),
                                                 -delta.uncovered_delta,
                                                 _STATUS_RANK[delta.status],
                                                 delta.path))

    def directory_totals(self):
        """{directory: DeltaTotals} keyed by immediate parent, not rolled up into ancestors."""
        totals = {}
        for file_delta in self.files_to_report():
            totals.setdefault(os.path.dirname(file_delta.path), DeltaTotals()).add(file_delta)
        return totals


def compare(baseline_files, current_files, changed_files=None):
    """Compare {path: FileCoverage} datasets canonicalized with one PathCanonicalizer and root."""
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
    """Rebases a foreign trace's paths onto this checkout and counts those under --source-root.
    A wrong root misplaces copied headers, which then fail to union with their in-tree records."""

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
    """Raise unless the traces share a source path; with none, every file reads as new
    and --fail-under-delta passes vacuously."""
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


def load_traces(current_path, baseline_path=None, source_root=None, baseline_source_root=None):
    """(baseline files or None, current files), canonicalized together."""
    from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov

    canonicalizer = PathCanonicalizer(source_root) if source_root else None

    current_paths = TracePaths(canonicalizer, source_root)
    current_files = parse_lcov(current_path, current_paths)
    if not current_files:
        raise RuntimeError('{} contained no coverage records'.format(current_path))

    baseline_files = None
    if baseline_path:
        baseline_paths = TracePaths(canonicalizer, source_root, baseline_source_root)
        baseline_files = parse_lcov(baseline_path, baseline_paths)
        if not baseline_files:
            raise RuntimeError('{} contained no coverage records'.format(baseline_path))
        baseline_paths.log_findings('baseline', baseline_path)
    current_paths.log_findings('current', current_path)

    if canonicalizer:
        canonicalizer.log_summary()
    if baseline_files is not None:
        check_traces_fit(baseline_files, current_files, baseline_path, current_path)
    return baseline_files, current_files


def compare_lcov_files(baseline_path, current_path, source_root=None, changed_files=None,
                       baseline_source_root=None):
    baseline_files, current_files = load_traces(current_path, baseline_path, source_root,
                                                baseline_source_root)
    return compare(baseline_files, current_files, changed_files)


def absolute_paths(paths, source_root):
    resolved = set()
    for path in paths:
        path = path.strip()
        if not path:
            continue
        resolved.add(path if os.path.isabs(path)
                     else os.path.normpath(os.path.join(source_root, path)))
    return resolved


def line_ranges(numbers):
    """[1, 2, 3, 7] -> [(1, 3), (7, 7)]"""
    if not numbers:
        return []
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append((start, previous))
        start = previous = number
    ranges.append((start, previous))
    return ranges


def format_line_numbers(numbers, limit=12):
    """[1, 2, 3, 7] -> '1-3, 7'"""
    ranges = line_ranges(numbers)
    if not ranges:
        return ''
    shown = ['{}'.format(low) if low == high else '{}-{}'.format(low, high)
             for low, high in ranges[:limit]]
    if len(ranges) > limit:
        shown.append('and {} more'.format(len(ranges) - limit))
    return ', '.join(shown)


class LineViews:
    """Links into the per-file line views generate-coverage-report writes (`#L<n>`, `#L<a>-L<b>`).

    A link is made only when the page exists on disk: a scoped report has pages for some files only.
    """

    def __init__(self, directory, source_root, prefix=''):
        # Deferred: run-webkit-tests imports this module on every run, and only
        # link-writing pages need coverage_source_view.
        from webkitpy.coverage_report_assets import source_page_path
        from webkitpy.coverage_source_view import relative_source_path
        self._relative_source_path = relative_source_path
        self._source_page_path = source_page_path
        self._directory = directory
        self._source_root = source_root
        self._prefix = prefix
        self._found = {}

    def page(self, path):
        """The href of path's line view, or None when there is no such page."""
        if self._directory is None:
            return None
        page = self._source_page_path(self._relative_source_path(path, self._source_root))
        found = self._found.get(page)
        if found is None:
            found = os.path.isfile(os.path.join(self._directory, page))
            self._found[page] = found
        return '{}{}'.format(self._prefix, quote(page)) if found else None

    def lines(self, path, first, last=None):
        page = self.page(path)
        if page is None:
            return None
        if last is None or last == first:
            return '{}#L{}'.format(page, first)
        return '{}#L{}-L{}'.format(page, first, last)


def link_html(href, label):
    if href is None:
        return html.escape(label)
    return '<a href="{}">{}</a>'.format(html.escape(href), html.escape(label))


def format_line_links(numbers, href=None, limit=12):
    """format_line_numbers() as HTML, one link per run to href(first, last) when href is given."""
    ranges = line_ranges(numbers)
    pieces = []
    for low, high in ranges[:limit]:
        label = '{}'.format(low) if low == high else '{}-{}'.format(low, high)
        pieces.append(link_html(href(low, high) if href else None, label))
    if len(ranges) > limit:
        pieces.append(html.escape('and {} more'.format(len(ranges) - limit)))
    return ', '.join(pieces)


def _column_percent(value):
    return '     -' if value is None else '{:6.2f}%'.format(value)


def _column_delta(value):
    return '       -' if value is None else '{:+6.2f}pp'.format(value)


def display_path(path, source_root):
    if source_root and path.startswith(source_root.rstrip('/') + '/'):
        return os.path.relpath(path, source_root)
    return path


def format_summary(delta, source_root=None, max_files=25):
    lines = []
    scope = delta.scope
    count, covered = scope.current['lines']

    if delta.focused:
        lines.append('Coverage delta for {} changed file{} with coverage data'.format(
            scope.file_count, '' if scope.file_count == 1 else 's'))
        # Needs no line matching, so a line shift cannot distort it.
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
                display_path(file_delta.path, source_root)))
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
            lines.append('  {}'.format(display_path(path, source_root)))
        if len(delta.missing_paths) > max_files:
            lines.append('  ... and {} more'.format(len(delta.missing_paths) - max_files))

    if scope.regressed_line_count or scope.new_uncovered_line_count:
        lines.append('')
        lines.append('Line numbers are compared as numbers, so for a file whose source changed '
                     'between the two runs the per-line attribution is approximate. The coverage '
                     'of the lines present in current is not.')
    return '\n'.join(lines) + '\n'


# Blue/red diverging pair, not green/red, which deuteranopic readers cannot tell apart.
# light-dark() follows the color-scheme the imported REPORT_STYLE already sets.
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
.detail p.p a { color: var(--text-primary); text-decoration: none; }
.detail p.p a:hover, .detail code a:hover { color: var(--meter-fill); text-decoration: underline; }
.detail code a { color: var(--meter-fill); text-decoration: none; }
.legend { color: var(--text-secondary); font-size: 11px; margin: 0 0 8px; }
.legend i {
  display: inline-block; width: 22px; height: 8px; border-radius: 4px;
  vertical-align: middle; margin: 0 3px 0 8px;
}
.empty { padding: 14px 12px; color: var(--text-secondary); margin: 0; }
"""

# (label, css class, sorts numerically)
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
    """Diverging bar around a zero rule, scaled to the page's largest change.
    A fixed -100..100 scale would make every bar invisible."""
    tooltip = html.escape('{} -> {}'.format(format_percent(before), format_percent(after)))
    if not value:
        return '<td data-v="0"><div class="dbar" title="{}"></div></td>'.format(tooltip)
    # Minimum width keeps a small real change visible.
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


def _file_row(file_delta, scale, source_root, line_views):
    # The shared cells need DeltaTotals' aggregate counters.
    totals = DeltaTotals()
    totals.add(file_delta)
    label = display_path(file_delta.path, source_root)
    status_css = ('down' if file_delta.status in (REGRESSED, DELETED)
                  else ('up' if file_delta.status == IMPROVED else ''))
    cells = [
        '<td data-v="{}">{}</td>'.format(
            html.escape(label), link_html(line_views.page(file_delta.path), label)),
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
    return ('<div class="card"><table><thead><tr>{}</tr></thead><tbody>\n{}\n'
            '</tbody></table></div>'.format(headers_html(leading_headers + _SHARED_HEADERS),
                                            '\n'.join(rows)))


def _tile(key, value, subtitle, css=''):
    return ('<div class="tile"><p class="k">{}</p><p class="{}">{}</p>'
            '<p class="s">{}</p></div>'.format(html.escape(key), ('v ' + css).strip(),
                                               html.escape(value), html.escape(subtitle)))


def _tiles(scope):
    change = scope.percent_delta()
    if change is None:
        headline, css = '-', ''
    else:
        # The glyph carries the sign for readers who cannot tell the colors apart.
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


def _details(reported, source_root, line_limit, line_views):
    """Line numbers are in the current trace's numbering, which the line views are rendered from."""
    blocks = []
    for file_delta in reported:
        if not file_delta.regressed_lines and not file_delta.regressed_functions:
            continue

        def lines_href(first, last, path=file_delta.path):
            return line_views.lines(path, first, last)

        parts = ['<p class="p">{}</p>'.format(link_html(
            line_views.page(file_delta.path), display_path(file_delta.path, source_root)))]
        if file_delta.regressed_lines:
            parts.append('<p><span class="down">covered &rarr; uncovered</span> '
                         '<code>{}</code></p>'.format(format_line_links(
                             file_delta.regressed_lines, lines_href, limit=line_limit)))
        if file_delta.new_uncovered_lines:
            parts.append('<p>new, never executed <code>{}</code></p>'.format(
                format_line_links(file_delta.new_uncovered_lines, lines_href, limit=line_limit)))
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
    """Write DELTA_REPORT_NAME into output_directory and return its path.
    Names and line numbers link into line views already in that directory, where the pages exist."""
    reported = delta.files_to_report()
    line_views = LineViews(output_directory, source_root)
    directories = sorted(delta.directory_totals().items(),
                         key=lambda entry: (-entry[1].uncovered_delta, entry[0]))

    # One scale for both tables, floored at a point so a tiny change is not drawn full width.
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
                           [_directory_row(display_path(path, source_root) or '/', totals, scale)
                            for path, totals in directories],
                           'No directory changed.'),
        files=_table(_LEADING_FILE_HEADERS,
                     [_file_row(file_delta, scale, source_root, line_views)
                      for file_delta in reported],
                     'No file changed coverage.'),
        details=_details(reported, source_root, line_limit, line_views),
        note=html.escape(note),
        script=SORT_SCRIPT)

    os.makedirs(output_directory, exist_ok=True)
    page_path = os.path.join(output_directory, DELTA_REPORT_NAME)
    with open(page_path, 'w', encoding='utf-8') as handle:
        handle.write(page)
    return page_path

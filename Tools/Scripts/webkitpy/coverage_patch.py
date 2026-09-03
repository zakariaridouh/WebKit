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

"""Patch coverage: of the lines this change added, how many does a test execute?

This is a different question from the delta coverage_delta computes, and it is the one
review actually asks. Delta coverage asks whether the project total moved, and the answer
is dominated by the size of the project: appending twenty never-executed lines to the real
Source/WebCore/dom/Document.cpp record -- 8,051 instrumented lines at 92.88%, a perfectly
untested new function -- moves that file from 92.88% to 92.65%, so a 0%-covered addition
reads as -0.23pp and passes --fail-under-delta=0.5. Patch coverage reports the same
addition as 0% of 20 and names all twenty line numbers.

Two consequences beyond the number being the right one.

It needs ONE trace, not two. A baseline comparison costs a full build, an incremental
build and two full test runs, and the two runs cannot overlap on one
machine, so the loop is most of a working day. Patch coverage needs the run you were going
to do anyway.

And it cannot be distorted by a line shift, by construction rather than by correction.
coverage_delta compares two sets of line numbers, so inserting a line near the top of a
file makes every line below it look like a mixture of a regression and a new line: three
real records shifted by twelve lines, coverage byte-identical, produce 400 "regressed" and
239 "uncovered new" lines. Patch coverage compares the diff's line numbers against one
trace, and the diff and the trace were made from the same text, so there is no second set
of numbers to shift against.

The denominator is the added lines that CARRY A COVERAGE RECORD, not all of them. A
comment, a blank line, a closing brace and a declaration have no DA: record because there
is no code there to execute, and counting them uncovered would make every patch look worse
than it is -- a twenty-line function with a five-line comment above it would read as 0 of
25 rather than 0 of 20. They are counted and reported separately instead, so the exclusion
is visible rather than silent.

The one thing it does assume is that the trace was produced from the source the diff
describes. Nothing in an lcov trace records which revision it was built from -- see PLAN
8.2 S11 -- so a trace from an older tree reads the right line numbers of the wrong text,
and nothing here can detect that. That is the same gap the line views have, and it wants
provenance in the artifact rather than a check here.
"""

import html
import logging
import os
import subprocess

from webkitpy.common.checkout.diff_parser import DiffParser
from webkitpy.coverage_delta import (
    SOURCE_EXTENSIONS, display_path, format_line_numbers, line_ranges)
from webkitpy.coverage_directory_index import (
    REPORT_STYLE, SORT_SCRIPT, format_percent, headers_html, meter_html)

logger = logging.getLogger(__name__)

# Arguments every git diff this module runs needs, whatever the checkout is configured to do.
#
# -U0 is the whole point: with context lines, a hunk contains lines the patch did not touch,
# and there is no way to tell them apart afterwards.
#
# The rest defend the format the parser reads. diff_parser turns `diff --git a/X b/Y` into a
# path, so the prefixes have to be the defaults whatever diff.noprefix and diff.mnemonicPrefix
# say; the paths have to be repository-relative whatever diff.relative says; and neither an
# external diff driver nor a textconv filter nor color may replace the body. core.fsmonitor is
# off because it is on in this checkout and it has been observed reporting a clean tree that
# was not clean, and a patch-coverage report that silently omits a file the author
# just edited is the failure this whole tool exists to prevent.
_GIT_DIFF_ARGUMENTS = ('-U0', '--src-prefix=a/', '--dst-prefix=b/', '--no-relative',
                       '--no-ext-diff', '--no-textconv', '--no-color')

_GIT_CONFIGURATION = ('-c', 'core.fsmonitor=false')


def _absolute(path, source_root):
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(source_root, path))


def added_lines_from_diff(diff_text, source_root):
    """{absolute path: [added or modified line numbers]} for a unified diff.

    Parsed by webkitpy.common.checkout.diff_parser, which already understands git's output
    through git_diff_to_svn_diff and is already unit-tested, rather than by a second @@
    parser written here. A path with no added lines -- a pure deletion, a rename with no
    edit, a mode change -- is kept with an empty list, because it is still a path the change
    touched and so still belongs in a file-level scope.
    """
    added = {}
    for name, diff_file in DiffParser(diff_text.splitlines()).files.items():
        # add_new_line() records (0, new_line_number, text), so a zero here would mean a
        # deleted line and never an added one; filter it out rather than trust that.
        numbers = sorted({number for number in diff_file.added_or_modified_line_numbers()
                          if number})
        added[_absolute(name, source_root)] = numbers
    return added


def added_lines_from_untracked_files(paths, source_root):
    """{absolute path: [1..line count]}, since every line of a brand-new file is added.

    git diff does not mention an untracked file at all, so --git-diff's file list silently
    dropped brand-new files: verified against a scratch repository, a new New.cpp never
    appeared in scope. A file nobody has committed yet is exactly the case patch coverage
    exists for, so the line numbers have to come from the file itself.
    """
    added = {}
    for path in paths:
        # Restricted to sources here rather than downstream: an untracked path can be
        # anything at all, including a large binary, and there is no reason to read one.
        if not path.endswith(SOURCE_EXTENSIONS):
            continue
        absolute = _absolute(path, source_root)
        try:
            with open(absolute, 'r', encoding='utf-8', errors='replace') as handle:
                count = sum(1 for _ in handle)
        except OSError as error:
            logger.warning('Could not read the untracked file %s: %s', absolute, error)
            continue
        if count:
            added[absolute] = list(range(1, count + 1))
    return added


def _git(source_root, *arguments):
    completed = subprocess.run(('git',) + _GIT_CONFIGURATION + arguments,
                               cwd=source_root, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError('git {} failed: {}'.format(' '.join(arguments),
                                                      completed.stderr.strip()))
    return completed.stdout


def git_diff_added_lines(source_root, ref):
    """{absolute path: [added or modified line numbers]} for everything REF names.

    The ref is passed through verbatim, so `main` means "everything since main, including my
    working tree" and `main...HEAD` means "only my commits". A ref cannot contain `..`, so
    the presence of one is an exact test for the second form rather than a guess -- and it
    decides whether untracked files belong: they are in the working tree and in no commit, so
    a commit range must not include them and a working-tree comparison must.
    """
    diff = _git(source_root, 'diff', *(_GIT_DIFF_ARGUMENTS + (ref,)))
    added = added_lines_from_diff(diff, source_root)
    if '..' in ref:
        logger.info('%s names a commit range, so untracked files are not in it', ref)
        return added

    untracked = added_lines_from_untracked_files(
        _git(source_root, 'ls-files', '--others', '--exclude-standard',
             '--full-name').splitlines(), source_root)
    if untracked:
        logger.info('Including %d untracked source file(s), which git diff never mentions',
                    len(untracked))
    # Untracked wins on a collision, which cannot happen -- git diff does not report an
    # untracked path -- but if it ever did, the file on disk is the better answer.
    added.update(untracked)
    return added


def _sortable(percent):
    """A percentage as a sort key, with "no percentage at all" sorting last."""
    return 101.0 if percent is None else percent


class FilePatchCoverage:
    """One file's added lines, split by whether the trace has a record for them."""
    __slots__ = ('path', 'added_line_count', 'covered_lines', 'uncovered_lines',
                 'file_totals')

    def __init__(self, path, added_lines, coverage):
        self.path = path
        self.added_line_count = len(added_lines)
        counts = coverage.lines
        self.covered_lines = sorted(number for number in added_lines
                                    if counts.get(number))
        # `is not None` and not truthiness: a DA: record of 0 is an instrumented line that
        # nothing executed, which is the finding, while no record at all is not a line the
        # compiler emitted code for.
        self.uncovered_lines = sorted(number for number in added_lines
                                      if counts.get(number) is not None
                                      and not counts[number])
        self.file_totals = coverage.totals()['lines']

    @property
    def instrumented_line_count(self):
        return len(self.covered_lines) + len(self.uncovered_lines)

    @property
    def excluded_line_count(self):
        """Added lines with no coverage record: comments, blanks, braces, declarations."""
        return self.added_line_count - self.instrumented_line_count

    def percent(self):
        count = self.instrumented_line_count
        return (100.0 * len(self.covered_lines) / count) if count else None

    def file_percent(self):
        count, covered = self.file_totals
        return (100.0 * covered / count) if count else None


class PatchCoverage:
    """Coverage of the added and modified lines of one change, against one trace.

    line_numbers is False when the change was named by a file list rather than by a diff.
    A file list cannot say which lines were added, so the per-line half is simply absent and
    what is left is the file-level view: how well tested are the files you touched. That is
    the weaker question -- it is dominated by the 8,000 lines that were already there -- but
    it is what `--baseline=X --current=X --git-diff=REF` produced before this existed, and it
    is all a list of paths can answer.
    """

    def __init__(self, added_by_path, coverage_by_path, line_numbers=True):
        self.line_numbers = line_numbers
        self.files = []
        self.missing_paths = []
        self.ignored_path_count = 0
        for path, added_lines in sorted(added_by_path.items()):
            if not path.endswith(SOURCE_EXTENSIONS):
                self.ignored_path_count += 1
                continue
            coverage = coverage_by_path.get(path)
            if coverage is None:
                self.missing_paths.append(path)
                continue
            self.files.append(FilePatchCoverage(path, added_lines, coverage))
        # Worst first, and "worst" is the count of uncovered added lines rather than the
        # percentage: one uncovered line in a two-line change is 50% and one uncovered line
        # in a two-hundred-line change is 99.5%, and the second is the one to go and look at.
        # With no line numbers there is nothing to rank by but the file's own coverage.
        if line_numbers:
            self.files.sort(key=lambda entry: (-len(entry.uncovered_lines),
                                               _sortable(entry.percent()), entry.path))
        else:
            self.files.sort(key=lambda entry: (_sortable(entry.file_percent()), entry.path))

    @property
    def instrumented_line_count(self):
        return sum(entry.instrumented_line_count for entry in self.files)

    @property
    def covered_line_count(self):
        return sum(len(entry.covered_lines) for entry in self.files)

    @property
    def uncovered_line_count(self):
        return sum(len(entry.uncovered_lines) for entry in self.files)

    @property
    def added_line_count(self):
        return sum(entry.added_line_count for entry in self.files)

    @property
    def excluded_line_count(self):
        return self.added_line_count - self.instrumented_line_count

    @property
    def file_totals(self):
        """(count, covered) over the whole of every file the change touched."""
        count = sum(entry.file_totals[0] for entry in self.files)
        covered = sum(entry.file_totals[1] for entry in self.files)
        return count, covered

    def percent(self):
        count = self.instrumented_line_count
        return (100.0 * self.covered_line_count / count) if count else None

    def file_percent(self):
        count, covered = self.file_totals
        return (100.0 * covered / count) if count else None


def _percent(value):
    return '-' if value is None else '{:.2f}%'.format(value)


_PATCH_NOTE = (
    'Patch coverage compares the diff against the current trace alone, so unlike a baseline '
    'comparison it cannot be distorted by a line shift: the diff and the trace describe the '
    'same text, so there is no second set of line numbers to shift against. Added lines with '
    'no coverage record are excluded from the denominator rather than counted uncovered, '
    'because a comment, a blank line, a brace or a declaration is not a line any test could '
    'execute. What it does assume is that the trace was produced from the source the diff '
    'describes -- nothing in an lcov trace records which revision it was built from, so a '
    'trace from an older tree reads the right line numbers of the wrong text.')

_FILE_LEVEL_NOTE = (
    'This is file level. It is the coverage of the whole of every file the change touched, so '
    'it is dominated by the code that was already there and it cannot say whether the lines '
    'you added are tested: a wholly untested twenty-line addition to a 8,000-line file at '
    '92.88% reads as 92.65%. Pass --git-diff=REF instead of a file list for that.')


def _line_level_rows(patch, source_root, max_files, line_limit):
    rows = ['{:>7} {:>9} {:>7} {:>8}  {}'.format('PATCH', 'COVERED', 'ADDED', 'IN FILE', 'FILE')]
    for entry in patch.files[:max_files]:
        rows.append('{:>7} {:>9} {:>7} {:>8}  {}'.format(
            _percent(entry.percent()),
            '{:,}/{:,}'.format(len(entry.covered_lines), entry.instrumented_line_count),
            '{:,}'.format(entry.added_line_count),
            _percent(entry.file_percent()),
            display_path(entry.path, source_root)))
        if entry.uncovered_lines:
            # The product: a reviewer needs the line numbers, not a percentage.
            rows.append('{:>7} uncovered added lines {}'.format(
                '', format_line_numbers(entry.uncovered_lines, limit=line_limit)))
    return rows


def _file_level_rows(patch, source_root, max_files):
    rows = ['{:>8} {:>9}  {}'.format('IN FILE', 'LINES', 'FILE')]
    for entry in patch.files[:max_files]:
        rows.append('{:>8} {:>9}  {}'.format(
            _percent(entry.file_percent()), '{:,}'.format(entry.file_totals[0]),
            display_path(entry.path, source_root)))
    return rows


def format_patch_summary(patch, source_root=None, max_files=25, line_limit=12):
    """A text summary, sized and shaped to be pasted into a review comment."""
    count, covered = patch.file_totals
    file_level = 'coverage of the whole of {:,} changed file{}: {} ({:,} of {:,} lines)'.format(
        len(patch.files), '' if len(patch.files) == 1 else 's',
        _percent(patch.file_percent()), covered, count)

    lines = []
    if patch.line_numbers:
        lines.append('Patch coverage: {} ({:,} of {:,} added lines with coverage data '
                     'covered)'.format(_percent(patch.percent()), patch.covered_line_count,
                                       patch.instrumented_line_count))
        lines.append('  {:,} added line{} in total'.format(
            patch.added_line_count, '' if patch.added_line_count == 1 else 's'))
        if patch.excluded_line_count:
            lines.append('  {:,} added line{} no coverage record -- a comment, a blank line, a '
                         'brace, a declaration -- and {} excluded from the denominator rather '
                         'than counted uncovered'.format(
                             patch.excluded_line_count,
                             ' carries' if patch.excluded_line_count == 1 else 's carry',
                             'is' if patch.excluded_line_count == 1 else 'are'))
        # The file-level number is stated beside the patch number, never instead of it: it
        # answers the weaker question, and it is the one that hides an untested addition.
        lines.append('  ' + file_level)
        rows = _line_level_rows(patch, source_root, max_files, line_limit)
        note = _PATCH_NOTE
    else:
        lines.append('File-level ' + file_level)
        rows = _file_level_rows(patch, source_root, max_files)
        note = _FILE_LEVEL_NOTE

    if patch.files:
        lines.append('')
        lines.extend(rows)
        remaining = len(patch.files) - max_files
        if remaining > 0:
            lines.append('... and {:,} more file{}'.format(remaining,
                                                           '' if remaining == 1 else 's'))

    if patch.missing_paths:
        lines.append('')
        one = len(patch.missing_paths) == 1
        # Loudest for a brand-new file, which is the case where this is most likely to mean
        # the build never compiled it rather than that it holds no executable code.
        lines.append('{} changed source file{} no coverage data in the trace, so nothing '
                     'instrumented compiled {}, and nothing here measures {}:'.format(
                         len(patch.missing_paths), ' has' if one else 's have',
                         'it' if one else 'them', 'it' if one else 'them'))
        for path in patch.missing_paths[:max_files]:
            lines.append('  {}'.format(display_path(path, source_root)))
        if len(patch.missing_paths) > max_files:
            lines.append('  ... and {} more'.format(len(patch.missing_paths) - max_files))

    lines.append('')
    lines.append(note)
    return '\n'.join(lines) + '\n'


# --- HTML report -----------------------------------------------------------------------
#
# Patch coverage is the question webkit-coverage is built around, and until now the only way to
# read the answer was a text file: compare-coverage-reports wrote patch-summary.txt while
# printing the whole-tree index as the headline artifact one line above it. The tiles, the
# palette, the meter and the sort script are imported from the two modules that already define
# them, so this is a third view of the same report and not a second tool.
#
# The page is patch-coverage.html and deliberately not index.html. Both generate-coverage-report
# and coverage_delta write an index.html, and webkit-coverage already points
# compare-coverage-reports --output-dir at the report directory, so a third index.html there
# would silently destroy the coverage index.
PATCH_REPORT_NAME = 'patch-coverage.html'

_PATCH_STYLE = """
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
.tile.bad .v { color: var(--patch-bad); }
h2 { font-size: 13px; font-weight: 600; margin: 22px 0 8px; }
.detail { padding: 9px 12px; border-bottom: 1px solid var(--gridline); }
.detail:last-child { border-bottom: 0; }
.detail p { margin: 0; color: var(--text-secondary); font-size: 12px; }
.detail p.p { font-weight: 600; color: var(--text-primary); margin-bottom: 3px; }
.detail p.p a { color: var(--text-primary); text-decoration: none; }
.detail p.p a:hover { color: var(--meter-fill); text-decoration: underline; }
.detail code { font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
.detail code a { color: var(--patch-bad); text-decoration: none; }
.detail code a:hover { text-decoration: underline; }
.empty { padding: 14px 12px; color: var(--text-secondary); margin: 0; }
td.file { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
:root {
  --patch-bad: #c0392b;
  --patch-bad: light-dark(#c0392b, #f0776a);
}
"""

# (label, css class, sorts numerically), in order.
_PATCH_HEADERS = (
    ('File', '', False),
    ('Patch coverage', '', True),
    ('Patch %', 'n', True),
    ('Covered', 'n', True),
    ('Added', 'n', True),
    ('No record', 'n', True),
    ('Whole file %', 'n', True),
)

_FILE_LEVEL_HEADERS = (
    ('File', '', False),
    ('Coverage', '', True),
    ('Whole file %', 'n', True),
    ('Lines', 'n', True),
    ('Uncovered', 'n', True),
)


def _tile(key, value, subtitle, css=''):
    return ('<div class="{}"><p class="k">{}</p><p class="v">{}</p>'
            '<p class="s">{}</p></div>'.format(('tile ' + css).strip(), html.escape(key),
                                               html.escape(value), html.escape(subtitle)))


def _patch_tiles(patch):
    percent = patch.percent()
    uncovered = patch.uncovered_line_count
    return '<div class="tiles">' + ''.join((
        _tile('Patch coverage', _percent(percent),
              '{:,} of {:,} added lines with coverage data'.format(
                  patch.covered_line_count, patch.instrumented_line_count),
              'bad' if percent is not None and uncovered else ''),
        _tile('Uncovered added lines', '{:,}'.format(uncovered),
              'instrumented, and no test executed them',
              'bad' if uncovered else ''),
        _tile('Added lines', '{:,}'.format(patch.added_line_count),
              '{:,} {} no coverage record'.format(
                  patch.excluded_line_count,
                  'carries' if patch.excluded_line_count == 1 else 'carry')),
        _tile('Files changed', '{:,}'.format(len(patch.files)),
              '{:,} with uncovered added lines'.format(
                  sum(1 for entry in patch.files if entry.uncovered_lines))),
        _tile('Whole-file coverage', _percent(patch.file_percent()),
              'the files this change touched, all of their lines'),
    )) + '</div>'


def _file_level_tiles(patch):
    count, covered = patch.file_totals
    return '<div class="tiles">' + ''.join((
        _tile('Whole-file coverage', _percent(patch.file_percent()),
              '{:,} of {:,} lines'.format(covered, count)),
        _tile('Files changed', '{:,}'.format(len(patch.files)),
              'measured over all of their lines'),
        _tile('Uncovered lines', '{:,}'.format(count - covered),
              'in the files the change touched', 'bad' if count - covered else ''),
    )) + '</div>'


def _file_cell(entry, source_root, report_root):
    relative = display_path(entry.path, source_root)
    if report_root is None:
        return '<td class="file" data-v="{v}">{v}</td>'.format(v=html.escape(relative))
    return '<td class="file" data-v="{v}"><a href="{href}">{v}</a></td>'.format(
        v=html.escape(relative), href=html.escape('{}{}.html'.format(report_root, relative)))


def _patch_row(entry, source_root, report_root):
    percent = entry.percent()
    return '<tr>{}{}{}{}{}{}{}</tr>'.format(
        _file_cell(entry, source_root, report_root),
        '<td data-v="{}">{}</td>'.format(
            -1 if percent is None else '{:.4f}'.format(percent), meter_html(percent)),
        '<td class="n pct" data-v="{}">{}</td>'.format(
            -1 if percent is None else '{:.4f}'.format(percent), format_percent(percent)),
        '<td class="n" data-v="{}">{:,}/{:,}</td>'.format(
            len(entry.covered_lines), len(entry.covered_lines), entry.instrumented_line_count),
        '<td class="n" data-v="{c}">{c:,}</td>'.format(c=entry.added_line_count),
        '<td class="n" data-v="{c}">{c:,}</td>'.format(c=entry.excluded_line_count),
        '<td class="n pct" data-v="{}">{}</td>'.format(
            -1 if entry.file_percent() is None else '{:.4f}'.format(entry.file_percent()),
            format_percent(entry.file_percent())))


def _file_level_row(entry, source_root, report_root):
    percent = entry.file_percent()
    count, covered = entry.file_totals
    return '<tr>{}{}{}{}{}</tr>'.format(
        _file_cell(entry, source_root, report_root),
        '<td data-v="{}">{}</td>'.format(
            -1 if percent is None else '{:.4f}'.format(percent), meter_html(percent)),
        '<td class="n pct" data-v="{}">{}</td>'.format(
            -1 if percent is None else '{:.4f}'.format(percent), format_percent(percent)),
        '<td class="n" data-v="{c}">{c:,}</td>'.format(c=count),
        '<td class="n" data-v="{c}">{c:,}</td>'.format(c=count - covered))


def _table(headers, rows, empty):
    if not rows:
        return '<div class="card"><p class="empty">{}</p></div>'.format(html.escape(empty))
    return ('<div class="card"><table><thead><tr>{}</tr></thead><tbody>{}</tbody>'
            '</table></div>'.format(headers_html(headers), ''.join(rows)))


def _line_links(path, numbers, report_root, limit):
    """Uncovered line numbers, as links into the annotated source view when there is one.

    One link per contiguous run rather than one per line: a 200-line untested block is one
    thing to go and look at, and 200 anchors is not a list anybody reads. The link target is
    the run's first line, which is where you want the cursor.
    """
    ranges = line_ranges(numbers)
    if not ranges:
        return ''
    pieces = []
    for low, high in ranges[:limit]:
        label = '{}'.format(low) if low == high else '{}-{}'.format(low, high)
        if report_root is None:
            pieces.append(html.escape(label))
            continue
        target = '{}{}.html#L{}'.format(report_root, path, low)
        pieces.append('<a href="{}">{}</a>'.format(html.escape(target), html.escape(label)))
    if len(ranges) > limit:
        pieces.append(html.escape('and {} more'.format(len(ranges) - limit)))
    return ', '.join(pieces)


def _patch_details(patch, source_root, report_root, line_limit):
    """The uncovered added lines, per file. This is the product; everything else is context."""
    blocks = []
    for entry in patch.files:
        if not entry.uncovered_lines:
            continue
        relative = display_path(entry.path, source_root)
        heading = html.escape(relative) if report_root is None else (
            '<a href="{}">{}</a>'.format(
                html.escape('{}{}.html'.format(report_root, relative)), html.escape(relative)))
        blocks.append(
            '<div class="detail"><p class="p">{name}</p>'
            '<p>{count} uncovered added line{plural} <code>{links}</code></p></div>'.format(
                name=heading, count='{:,}'.format(len(entry.uncovered_lines)),
                plural='' if len(entry.uncovered_lines) == 1 else 's',
                links=_line_links(relative, entry.uncovered_lines, report_root, line_limit)))
    if not blocks:
        return ('<div class="card"><p class="empty">Every added line with coverage data was '
                'executed.</p></div>')
    return '<div class="card">' + ''.join(blocks) + '</div>'


def _missing_card(patch, source_root, max_files):
    if not patch.missing_paths:
        return ''
    one = len(patch.missing_paths) == 1
    shown = patch.missing_paths[:max_files]
    items = ''.join('<div class="detail"><p class="p">{}</p></div>'.format(
        html.escape(display_path(path, source_root))) for path in shown)
    if len(patch.missing_paths) > max_files:
        items += '<div class="detail"><p>and {:,} more</p></div>'.format(
            len(patch.missing_paths) - max_files)
    heading = ('{} changed source file{} no coverage data in the trace, so nothing '
               'instrumented compiled {}'.format(len(patch.missing_paths),
                                                 ' has' if one else 's have',
                                                 'it' if one else 'them'))
    return '<h2>{}</h2><div class="card">{}</div>'.format(html.escape(heading), items)


_PATCH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}{patch_style}</style>
</head>
<body>
<div class="wrap">
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
{caveat}{tiles}
<h2>{table_heading}</h2>
{table}
{details_section}{missing}<p class="hint">{note}</p>
</div>
<script>{script}</script>
</body>
</html>
"""


def write_patch_report(patch, output_directory, source_root=None, report_root=None,
                       line_limit=24, max_files=200, scope=None):
    """Write the patch-coverage HTML page. Returns the path to it.

    report_root, when given, is the relative path from this page to the root of a coverage
    report written by generate-coverage-report, and it is what turns each uncovered line number
    into a link into the annotated source view. '' means the report is in this same directory,
    which is what webkit-coverage arranges. Without it the numbers are plain text rather than
    links to pages that may not exist.

    scope is a coverage_scope.CoverageScope, and puts the lower-bound banner above the number
    for a selective run -- the uncovered line list is still exactly the thing to act on, because
    it is a superset of the truly untested lines and never a subset.
    """
    os.makedirs(output_directory, exist_ok=True)
    if patch.line_numbers:
        title = 'Patch coverage'
        subtitle = ('{} of the {:,} added lines with coverage data are covered, over {:,} '
                    'changed file{}'.format(_percent(patch.percent()),
                                            patch.instrumented_line_count, len(patch.files),
                                            '' if len(patch.files) == 1 else 's'))
        tiles = _patch_tiles(patch)
        table_heading = 'Changed files, worst first'
        table = _table(_PATCH_HEADERS,
                       [_patch_row(entry, source_root, report_root)
                        for entry in patch.files[:max_files]],
                       'No changed file has coverage data.')
        details_section = '<h2>Uncovered added lines</h2>' + _patch_details(
            patch, source_root, report_root, line_limit)
        note = _PATCH_NOTE
    else:
        count, covered = patch.file_totals
        title = 'Coverage of the files this change touched'
        subtitle = '{} over {:,} file{}, {:,} of {:,} lines'.format(
            _percent(patch.file_percent()), len(patch.files),
            '' if len(patch.files) == 1 else 's', covered, count)
        tiles = _file_level_tiles(patch)
        table_heading = 'Changed files, least covered first'
        table = _table(_FILE_LEVEL_HEADERS,
                       [_file_level_row(entry, source_root, report_root)
                        for entry in patch.files[:max_files]],
                       'No changed file has coverage data.')
        # No per-line section at all rather than an empty one: a file list cannot say which
        # lines were added, so there is no such thing as an uncovered added line here.
        details_section = ''
        note = _FILE_LEVEL_NOTE

    if len(patch.files) > max_files:
        note = ('The table lists the first {:,} of {:,} changed files, worst first. '.format(
            max_files, len(patch.files)) + note)

    caveat = ''
    if scope is not None and scope.is_selective:
        caveat = '<p class="caveat">{}</p>\n'.format(html.escape(' '.join(scope.banner_lines())))
        title = scope.qualify_title(title)

    page = _PATCH_PAGE.format(
        title=html.escape(title), subtitle=html.escape(subtitle), caveat=caveat, tiles=tiles,
        table_heading=html.escape(table_heading), table=table,
        details_section=details_section,
        missing=_missing_card(patch, source_root, max_files),
        note=html.escape(note), style=REPORT_STYLE, patch_style=_PATCH_STYLE, script=SORT_SCRIPT)
    path = os.path.join(output_directory, PATCH_REPORT_NAME)
    with open(path, 'w') as handle:
        handle.write(page)
    return path

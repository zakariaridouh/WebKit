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

"""Patch coverage: of the lines a change added, how many does a test execute?

Needs one trace and no baseline. Added lines with no DA: record are excluded from the count."""

import html
import logging
import os
import subprocess

from webkitpy.common.checkout.diff_parser import DiffParser
from webkitpy.coverage_delta import (
    SOURCE_EXTENSIONS, LineViews, display_path, format_line_links, format_line_numbers,
    line_ranges, link_html)
from webkitpy.coverage_directory_index import (
    REPORT_STYLE, SORT_SCRIPT, format_percent, headers_html, meter_html)

logger = logging.getLogger(__name__)

# -U0: context lines would be indistinguishable from added ones. The rest pin the format
# diff_parser reads against user git config; fsmonitor is off as it can report a stale clean tree.
_GIT_DIFF_ARGUMENTS = ('-U0', '--src-prefix=a/', '--dst-prefix=b/', '--no-relative',
                       '--no-ext-diff', '--no-textconv', '--no-color')

_GIT_CONFIGURATION = ('-c', 'core.fsmonitor=false')


def _absolute(path, source_root):
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(source_root, path))


def added_lines_from_diff(diff_text, source_root):
    """{absolute path: [added or modified line numbers]}; a pure deletion keeps an empty list."""
    added = {}
    for name, diff_file in DiffParser(diff_text.splitlines()).files.items():
        # Drop 0, which would denote a deleted line.
        numbers = sorted({number for number in diff_file.added_or_modified_line_numbers()
                          if number})
        added[_absolute(name, source_root)] = numbers
    return added


def added_lines_from_untracked_files(paths, source_root):
    """{absolute path: [1..line count]}: git diff never mentions untracked files, so read them."""
    added = {}
    for path in paths:
        # Skip non-sources early so a large untracked binary is never read.
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
    A ref containing `..` is a commit range, which excludes untracked files; other refs include them."""
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
    added.update(untracked)
    return added


def _sortable(percent):
    """Sort key; None sorts last."""
    return 101.0 if percent is None else percent


class FilePatchCoverage:
    """One file's added lines, split by whether the trace has a record for them.
    line_counts is the trace's {line: count} for the whole file, used for excerpt context."""
    __slots__ = ('path', 'added_lines', 'added_line_count', 'covered_lines', 'uncovered_lines',
                 'file_totals', 'line_counts')

    def __init__(self, path, added_lines, coverage):
        self.path = path
        self.added_lines = sorted(set(added_lines))
        self.added_line_count = len(added_lines)
        counts = coverage.lines
        self.line_counts = counts
        self.covered_lines = sorted(number for number in added_lines
                                    if counts.get(number))
        # `is not None`, not truthiness: a DA: record of 0 is the finding; no record is no code.
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
    line_numbers is False for a file list, which cannot say which lines were added."""

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
        # Worst first by uncovered added lines, not percentage; a file list ranks by file coverage.
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


# Not index.html: the report directory already holds generate-coverage-report's.
PATCH_REPORT_NAME = 'patch-coverage.html'

# Excerpt sizes bound the page: a 5,000-line untested file is one folded excerpt, not 5,000 rows.
EXCERPT_CONTEXT = 3
EXCERPT_MERGE_GAP = 2
# Longer excerpts fold the middle behind a <details>, keeping HEAD and TAIL lines in view.
EXCERPT_FOLD_AT = 24
EXCERPT_FOLD_HEAD = 10
EXCERPT_FOLD_TAIL = 5
EXCERPT_MAX_LINES = 400
# Per file: EXCERPTS_SHOWN open, then up to EXCERPTS_PER_FILE behind "show more".
EXCERPTS_SHOWN = 3
EXCERPTS_PER_FILE = 20

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
.detail p.nosrc { margin-top: 6px; font-style: italic; }
.detail p.nosrc a { color: var(--meter-fill); }
.empty { padding: 14px 12px; color: var(--text-secondary); margin: 0; }
td.file { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
:root {
  --patch-bad: #c0392b;
  --patch-bad: light-dark(#c0392b, #f0776a);
  --patch-good: var(--meter-fill);
}
.legend { color: var(--text-secondary); font-size: 11px; margin: 0 0 8px; }
.key {
  display: inline-block; min-width: 18px; margin: 0 3px 0 8px; padding: 0 4px;
  text-align: center; font: 600 11px/16px ui-monospace, SFMono-Regular, Menlo, monospace;
  border-radius: 3px; border: 1px solid var(--border);
}
.excerpt {
  margin: 8px 0 2px; border: 1px solid var(--border); border-radius: 6px;
  overflow: hidden; background: var(--page);
}
.eh {
  display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline;
  padding: 4px 10px; font-size: 11px; color: var(--text-secondary);
  background: var(--surface-1); border-bottom: 1px solid var(--gridline);
  font-variant-numeric: tabular-nums;
}
.eh .r { font-weight: 600; color: var(--text-primary); }
.eh .bad { font-weight: 600; color: var(--patch-bad); }
.eh a { margin-left: auto; color: var(--meter-fill); text-decoration: none; }
.eh a:hover { text-decoration: underline; }
.code { overflow-x: auto; font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace; }
.code > .in { display: inline-block; min-width: 100%; }
.ln { display: flex; white-space: pre; }
.ln > span { flex: none; }
.ln .no {
  width: calc(var(--nw) + 16px); padding: 0 8px; text-align: right;
  color: var(--muted); user-select: none; border-right: 1px solid var(--gridline);
}
.ln .no a { color: inherit; text-decoration: none; }
.ln .no a:hover { color: var(--meter-fill); text-decoration: underline; }
.ln .ct {
  width: calc(5ch + 16px); padding: 0 8px; text-align: right;
  color: var(--muted); user-select: none;
}
.ln .mk { width: 2ch; text-align: center; user-select: none; color: var(--muted); }
.ln .tx { flex: 1 0 auto; padding-right: 14px; tab-size: 4; color: var(--text-secondary); }
.ln.a .mk { font-weight: 600; color: var(--text-primary); }
.ln.a .tx { color: var(--text-primary); }
.ln.u, .key.u { background: color-mix(in oklab, var(--patch-bad) 13%, transparent); }
.ln.u .no { box-shadow: inset 3px 0 0 var(--patch-bad); }
.ln.u .ct, .ln.u .mk, .key.u { color: var(--patch-bad); font-weight: 600; }
.ln.c, .key.c { background: color-mix(in oklab, var(--patch-good) 10%, transparent); }
.ln.c .no { box-shadow: inset 3px 0 0 var(--patch-good); }
.ln.c .mk, .key.c { color: var(--patch-good); }
.ln.a:not(.u):not(.c), .key.n { background: color-mix(in oklab, var(--muted) 9%, transparent); }
.fold > summary, .gap {
  display: block; padding: 2px 10px 2px calc(var(--nw) + 16px + 5ch + 16px + 2ch);
  font: 11px/1.7 system-ui, -apple-system, sans-serif; color: var(--text-secondary);
  background: color-mix(in oklab, var(--muted) 12%, transparent);
  border-top: 1px dashed var(--gridline); border-bottom: 1px dashed var(--gridline);
}
.fold > summary { cursor: pointer; list-style: none; }
.fold > summary::-webkit-details-marker { display: none; }
.fold > summary::before { content: "\\25B8  "; }
.fold[open] > summary::before { content: "\\25BE  "; }
.fold > summary .bad { color: var(--patch-bad); font-weight: 600; }
.gap a { color: var(--meter-fill); }
.more { margin-top: 8px; }
.more > summary {
  cursor: pointer; font-size: 12px; color: var(--meter-fill); padding: 2px 0;
}
"""

# (label, css class, sorts numerically)
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


def _file_cell(entry, source_root, line_views):
    relative = display_path(entry.path, source_root)
    return '<td class="file" data-v="{}">{}</td>'.format(
        html.escape(relative), link_html(line_views.page(entry.path), relative))


def _patch_row(entry, source_root, line_views):
    percent = entry.percent()
    return '<tr>{}{}{}{}{}{}{}</tr>'.format(
        _file_cell(entry, source_root, line_views),
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


def _file_level_row(entry, source_root, line_views):
    percent = entry.file_percent()
    count, covered = entry.file_totals
    return '<tr>{}{}{}{}{}</tr>'.format(
        _file_cell(entry, source_root, line_views),
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


def read_source_lines(path):
    """The file's lines, numbered as the line views number them. Raises OSError.
    Split on newlines only: str.splitlines() also splits on the form feeds in bison output."""
    with open(path, 'r', encoding='utf-8', errors='replace') as handle:
        lines = handle.read().split('\n')
    if lines and not lines[-1]:
        lines.pop()
    return lines


def excerpt_ranges(uncovered_lines, line_count, context=EXCERPT_CONTEXT):
    """[(first, last)] per cluster of uncovered lines, with context, clamped to the file.
    A run starting past the end gets no range: the checkout is not the text that was measured."""
    ranges = []
    for low, high in line_ranges(uncovered_lines):
        if low > line_count:
            break
        first = max(1, low - context)
        last = min(line_count, high + context)
        if ranges and first <= ranges[-1][1] + EXCERPT_MERGE_GAP + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], last))
        else:
            ranges.append((first, last))
    return ranges


def _compact_count(count):
    """An execution count in at most five characters: 7, 1234, 12.3k, 40.9M."""
    if count < 10000:
        return '{}'.format(count)
    for divisor, suffix in ((10 ** 12, 'T'), (10 ** 9, 'G'), (10 ** 6, 'M'), (10 ** 3, 'k')):
        if count >= divisor:
            value = count / divisor
            return ('{:.1f}{}' if value < 100 else '{:.0f}{}').format(value, suffix)
    return '{}'.format(count)


# (css class, marker, tooltip) of one excerpt row
_UNCOVERED_ROW = ('a u', '+', 'added, and no test executed it')
_COVERED_ROW = ('a c', '+', 'added, and executed')
_UNRECORDED_ROW = ('a', '+', 'added, with no coverage record: no code a test could execute')
_CONTEXT_ROW = ('', '', '')


def _row_state(number, added, counts):
    if number not in added:
        return _CONTEXT_ROW
    count = counts.get(number)
    if count is None:
        return _UNRECORDED_ROW
    return _COVERED_ROW if count else _UNCOVERED_ROW


def _excerpt_row(number, text, state, count, page):
    css, marker, tooltip = state
    label = '{}'.format(number)
    number_html = label if page is None else '<a href="{}#L{}">{}</a>'.format(
        html.escape(page), number, label)
    return ('<div class="ln{}"><span class="no">{}</span><span class="ct">{}</span>'
            '<span class="mk"{}>{}</span><span class="tx">{}</span></div>'.format(
                ' ' + css if css else '', number_html,
                '' if count is None else _compact_count(count),
                ' title="{}"'.format(tooltip) if tooltip else '', marker,
                html.escape(text, quote=False)))


def _range_label(first, last):
    # No thousands separator, to match the line list's 8052-8071 style.
    return 'line {}'.format(first) if first == last else 'lines {}-{}'.format(first, last)


def _excerpt(entry, source, first, last, added, line_views, number_width):
    """One excerpt: a counts header, then rows, folded in the middle if long.
    number_width is shared by a file's excerpts so their code columns line up."""
    page = line_views.page(entry.path)
    counts = entry.line_counts
    states = [_row_state(number, added, counts) for number in range(first, last + 1)]

    def row(number):
        return _excerpt_row(number, source[number - 1], states[number - first],
                            counts.get(number), page)

    def uncovered_in(low, high):
        return sum(1 for number in range(low, high + 1)
                   if states[number - first] is _UNCOVERED_ROW)

    uncovered = uncovered_in(first, last)
    covered = sum(1 for state in states if state is _COVERED_ROW)
    unrecorded = sum(1 for state in states if state is _UNRECORDED_ROW)
    header = [
        '<span class="r">{}</span>'.format(html.escape(_range_label(first, last).capitalize())),
        '<span class="bad">{:,} uncovered</span>'.format(uncovered)]
    if covered:
        header.append('<span>{:,} covered</span>'.format(covered))
    if unrecorded:
        header.append('<span>{:,} added with no record</span>'.format(unrecorded))
    target = line_views.lines(entry.path, first, last)
    if target is not None:
        header.append('<a href="{}">open in the line view</a>'.format(html.escape(target)))

    length = last - first + 1
    if length <= EXCERPT_FOLD_AT:
        body = ''.join(row(number) for number in range(first, last + 1))
    else:
        # Past EXCERPT_MAX_LINES the folded middle is not rendered; the line view has the rest.
        fold_first = first + EXCERPT_FOLD_HEAD
        fold_last = last - EXCERPT_FOLD_TAIL
        rendered_last = min(fold_last, first + EXCERPT_MAX_LINES - EXCERPT_FOLD_TAIL - 1)
        folded = ''.join(row(number) for number in range(fold_first, rendered_last + 1))
        if rendered_last < fold_last:
            skipped = line_views.lines(entry.path, rendered_last + 1, fold_last)
            folded += '<div class="gap">{} not shown here{}</div>'.format(
                html.escape(_range_label(rendered_last + 1, fold_last).capitalize()),
                '' if skipped is None else
                ': <a href="{}">open them in the line view</a>'.format(html.escape(skipped)))
        hidden_uncovered = uncovered_in(fold_first, fold_last)
        body = ''.join((
            ''.join(row(number) for number in range(first, fold_first)),
            '<details class="fold"><summary>{:,} more lines, {}{}</summary>{}</details>'.format(
                fold_last - fold_first + 1, html.escape(_range_label(fold_first, fold_last)),
                ', <span class="bad">{:,} uncovered</span>'.format(hidden_uncovered)
                if hidden_uncovered else '', folded),
            ''.join(row(number) for number in range(fold_last + 1, last + 1))))

    return ('<div class="excerpt"><div class="eh">{}</div><div class="code" style="--nw:{}ch">'
            '<div class="in">{}</div></div></div>'.format(''.join(header), number_width, body))


def _file_excerpts(entry, line_views):
    try:
        source = read_source_lines(entry.path)
    except OSError as error:
        return ('<p class="nosrc">No excerpt: the source could not be read from the checkout '
                '({}).</p>'.format(html.escape(error.strerror or str(error))))

    notes = []
    past_end = [number for number in entry.uncovered_lines if number > len(source)]
    if past_end:
        notes.append('<p class="nosrc">{:,} uncovered added line{} past line {:,}, the end of '
                     'this file in the checkout, so the checkout is not the text the trace '
                     'measured and {} no excerpt.</p>'.format(
                         len(past_end), ' is' if len(past_end) == 1 else 's are', len(source),
                         'it has' if len(past_end) == 1 else 'they have'))

    added = set(entry.added_lines)
    ranges = excerpt_ranges(entry.uncovered_lines, len(source))
    shown = ranges[:EXCERPTS_SHOWN]
    more = ranges[EXCERPTS_SHOWN:EXCERPTS_PER_FILE]
    left_out = ranges[EXCERPTS_PER_FILE:]
    width = len(str((more or shown)[-1][1])) if shown else 1
    parts = [_excerpt(entry, source, first, last, added, line_views, width)
             for first, last in shown]
    if more:
        hidden = sum(1 for number in entry.uncovered_lines
                     if more[0][0] <= number <= more[-1][1])
        parts.append('<details class="more"><summary>Show {:,} more excerpt{}, {:,} uncovered '
                     'added line{}</summary>{}</details>'.format(
                         len(more), '' if len(more) == 1 else 's', hidden,
                         '' if hidden == 1 else 's',
                         ''.join(_excerpt(entry, source, first, last, added, line_views, width)
                                 for first, last in more)))
    if left_out:
        page = line_views.page(entry.path)
        parts.append('<p class="nosrc">{:,} more excerpt{}, from {}, {} not shown{}.</p>'.format(
            len(left_out), '' if len(left_out) == 1 else 's',
            html.escape(_range_label(left_out[0][0], left_out[-1][1])),
            'is' if len(left_out) == 1 else 'are',
            '' if page is None else
            ': <a href="{}">the line view has all of them</a>'.format(html.escape(page))))
    return ''.join(parts + notes)


def _patch_details(patch, source_root, line_views, line_limit):
    blocks = []
    for entry in patch.files:
        if not entry.uncovered_lines:
            continue
        relative = display_path(entry.path, source_root)

        def lines_href(first, last, path=entry.path):
            return line_views.lines(path, first, last)

        blocks.append(
            '<div class="detail"><p class="p">{name}</p>'
            '<p>{count} uncovered added line{plural} <code>{links}</code></p>{excerpts}'
            '</div>'.format(
                name=link_html(line_views.page(entry.path), relative),
                count='{:,}'.format(len(entry.uncovered_lines)),
                plural='' if len(entry.uncovered_lines) == 1 else 's',
                links=format_line_links(entry.uncovered_lines, lines_href, limit=line_limit),
                excerpts=_file_excerpts(entry, line_views)))
    if not blocks:
        return ('<div class="card"><p class="empty">Every added line with coverage data was '
                'executed.</p></div>')
    return '<div class="card">' + ''.join(blocks) + '</div>'


_EXCERPT_LEGEND = (
    '<p class="legend">Each run of uncovered added lines, with {context} lines of the '
    'checkout\'s source either side. The second column is the execution count.'
    '<span class="key u">+</span>added, never executed'
    '<span class="key c">+</span>added, executed'
    '<span class="key n">+</span>added, no coverage record'
    '<span class="key">&nbsp;</span>unchanged context</p>').format(context=EXCERPT_CONTEXT)


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


def write_patch_report(patch, output_directory, source_root=None, report_root='',
                       line_limit=24, max_files=200, scope=None):
    """Write the patch-coverage page and return its path.
    report_root: URL to the line views ('' = this directory, None = no links).
    source_root: where excerpts are read."""
    os.makedirs(output_directory, exist_ok=True)
    line_views = LineViews(None if report_root is None
                           else os.path.join(output_directory, report_root),
                           source_root, report_root or '')
    if patch.line_numbers:
        title = 'Patch coverage'
        subtitle = ('{} of the {:,} added lines with coverage data are covered, over {:,} '
                    'changed file{}'.format(_percent(patch.percent()),
                                            patch.instrumented_line_count, len(patch.files),
                                            '' if len(patch.files) == 1 else 's'))
        tiles = _patch_tiles(patch)
        table_heading = 'Changed files, worst first'
        table = _table(_PATCH_HEADERS,
                       [_patch_row(entry, source_root, line_views)
                        for entry in patch.files[:max_files]],
                       'No changed file has coverage data.')
        details_section = '<h2>Uncovered added lines</h2>'
        if patch.uncovered_line_count:
            details_section += _EXCERPT_LEGEND
        details_section += _patch_details(patch, source_root, line_views, line_limit)
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
                       [_file_level_row(entry, source_root, line_views)
                        for entry in patch.files[:max_files]],
                       'No changed file has coverage data.')
        # A file list cannot say which lines were added, so there is no per-line section.
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
    # UTF-8 whatever the locale: the excerpts are the checkout's own text, which is not ASCII.
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(page)
    return path

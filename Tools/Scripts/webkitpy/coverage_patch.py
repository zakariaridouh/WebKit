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

import logging
import os
import subprocess

from webkitpy.common.checkout.diff_parser import DiffParser
from webkitpy.coverage_delta import SOURCE_EXTENSIONS, display_path, format_line_numbers

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

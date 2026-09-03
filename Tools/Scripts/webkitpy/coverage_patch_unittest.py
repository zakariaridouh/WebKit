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

import contextlib
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from webkitpy.coverage_lcov import FileCoverage
from webkitpy.coverage_patch import (PatchCoverage, added_lines_from_diff,
                                     added_lines_from_untracked_files, format_patch_summary,
                                     git_diff_added_lines, write_patch_report)
from webkitpy.coverage_thresholds import COVERAGE_GATE_EXIT_CODE

# Everything a scratch repository needs so that the developer's own configuration cannot
# change the answer. commit.gpgsign is not hypothetical: it is on in this checkout, and
# without it `git commit` fails with "gpg failed to sign the data". core.fsmonitor is off for
# the reason coverage_patch turns it off, and the template directory is empty so that a
# personal hook cannot run inside a unit test.
_GIT_CONFIGURATION = ('-c', 'user.name=Coverage Test', '-c', 'user.email=test@example.com',
                      '-c', 'commit.gpgsign=false', '-c', 'core.fsmonitor=false',
                      '-c', 'init.defaultBranch=main', '-c', 'init.templateDir=')


def coverage(lines):
    """A FileCoverage from (line, execution count) pairs."""
    result = FileCoverage()
    result.lines = dict(lines)
    return result


class AddedLineParsingTest(unittest.TestCase):
    """The @@ hunks, which is the half of a diff --name-only threw away."""

    def _added(self, diff):
        return added_lines_from_diff(diff, '/co')

    def test_the_added_lines_of_a_hunk_are_the_ones_the_patch_wrote(self):
        added = self._added(
            'diff --git a/Source/a.cpp b/Source/a.cpp\n'
            'index 1111111..2222222 100644\n'
            '--- a/Source/a.cpp\n'
            '+++ b/Source/a.cpp\n'
            '@@ -40,0 +41,3 @@ void existing()\n'
            '+    int added = 1;\n'
            '+    int alsoAdded = 2;\n'
            '+    return added + alsoAdded;\n')
        self.assertEqual(added, {'/co/Source/a.cpp': [41, 42, 43]})

    def test_a_modified_line_counts_as_added_at_its_new_number(self):
        # git -U0 writes a modification as a deletion plus an insertion. The new line is code
        # the author is responsible for, so it belongs in the denominator; the old one is gone.
        added = self._added(
            'diff --git a/a.cpp b/a.cpp\n'
            'index 1111111..2222222 100644\n'
            '--- a/a.cpp\n'
            '+++ b/a.cpp\n'
            '@@ -10 +10 @@\n'
            '-    int wasThis = 1;\n'
            '+    int isNowThis = 2;\n')
        self.assertEqual(added, {'/co/a.cpp': [10]})

    def test_a_pure_deletion_adds_nothing_but_is_still_in_scope(self):
        # Kept with an empty list rather than dropped: it is still a path the change touched,
        # so a file-level scope needs it, and patch coverage has nothing to say about it.
        added = self._added(
            'diff --git a/a.cpp b/a.cpp\n'
            'index 1111111..2222222 100644\n'
            '--- a/a.cpp\n'
            '+++ b/a.cpp\n'
            '@@ -10,2 +9,0 @@\n'
            '-    int gone = 1;\n'
            '-    int alsoGone = 2;\n')
        self.assertEqual(added, {'/co/a.cpp': []})

    def test_hunks_in_several_files_are_kept_apart(self):
        added = self._added(
            'diff --git a/a.cpp b/a.cpp\n'
            'index 1111111..2222222 100644\n'
            '--- a/a.cpp\n'
            '+++ b/a.cpp\n'
            '@@ -1,0 +2 @@\n'
            '+    int one = 1;\n'
            'diff --git a/b.cpp b/b.cpp\n'
            'index 3333333..4444444 100644\n'
            '--- a/b.cpp\n'
            '+++ b/b.cpp\n'
            '@@ -100,0 +101,2 @@\n'
            '+    int two = 2;\n'
            '+    int three = 3;\n')
        self.assertEqual(added, {'/co/a.cpp': [2], '/co/b.cpp': [101, 102]})

    def test_several_hunks_in_one_file_accumulate(self):
        added = self._added(
            'diff --git a/a.cpp b/a.cpp\n'
            'index 1111111..2222222 100644\n'
            '--- a/a.cpp\n'
            '+++ b/a.cpp\n'
            '@@ -1,0 +2 @@\n'
            '+    int one = 1;\n'
            '@@ -50,0 +52,2 @@\n'
            '+    int two = 2;\n'
            '+    int three = 3;\n')
        self.assertEqual(added, {'/co/a.cpp': [2, 52, 53]})

    def test_a_new_file_is_added_from_its_first_line(self):
        added = self._added(
            'diff --git a/New.cpp b/New.cpp\n'
            'new file mode 100644\n'
            'index 0000000..1111111\n'
            '--- /dev/null\n'
            '+++ b/New.cpp\n'
            '@@ -0,0 +1,2 @@\n'
            '+int first();\n'
            '+int second();\n')
        self.assertEqual(added, {'/co/New.cpp': [1, 2]})

    def test_a_rename_with_no_edit_is_named_by_its_new_path_and_adds_nothing(self):
        added = self._added(
            'diff --git a/Old.cpp b/Renamed.cpp\n'
            'similarity index 100%\n'
            'rename from Old.cpp\n'
            'rename to Renamed.cpp\n')
        self.assertEqual(added, {'/co/Renamed.cpp': []})

    def test_an_empty_diff_is_an_empty_result_and_not_an_error(self):
        self.assertEqual(self._added(''), {})


class UntrackedFileTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.mkdtemp()

    def _write(self, name, text):
        path = os.path.join(self._directory, name)
        with open(path, 'w') as handle:
            handle.write(text)
        return path

    def test_every_line_of_an_untracked_file_is_an_added_line(self):
        # A brand-new file is the case patch coverage matters most for, and git diff does not
        # mention it at all, so the numbers have to come from the file.
        self._write('New.cpp', 'one\ntwo\nthree\n')
        self.assertEqual(added_lines_from_untracked_files(['New.cpp'], self._directory),
                         {os.path.join(self._directory, 'New.cpp'): [1, 2, 3]})

    def test_a_file_with_no_trailing_newline_still_counts_its_last_line(self):
        self._write('New.cpp', 'one\ntwo')
        self.assertEqual(added_lines_from_untracked_files(['New.cpp'], self._directory),
                         {os.path.join(self._directory, 'New.cpp'): [1, 2]})

    def test_an_untracked_non_source_file_is_never_even_opened(self):
        # An untracked path can be anything, including a large binary; there is no reason to
        # read one, and no coverage record could exist for it.
        self.assertEqual(added_lines_from_untracked_files(
            ['LayoutTests/fast/a-expected.txt', 'a.png', 'CMakeLists.txt'], self._directory), {})

    def test_an_empty_untracked_source_file_contributes_nothing(self):
        self._write('Empty.cpp', '')
        self.assertEqual(added_lines_from_untracked_files(['Empty.cpp'], self._directory), {})


class GitDiffTest(unittest.TestCase):
    """git_diff_added_lines against a real repository, since the plumbing is the risk."""

    def setUp(self):
        self._directory = tempfile.mkdtemp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self._git('init', '-q', self._directory)
        self._write('Committed.cpp', 'int one();\nint two();\n')
        self._git('add', 'Committed.cpp')
        self._git('commit', '-q', '-m', 'base')

    def _git(self, *arguments):
        subprocess.run(('git',) + _GIT_CONFIGURATION + arguments, cwd=self._directory,
                       check=True, capture_output=True, text=True)

    def _write(self, name, text):
        with open(os.path.join(self._directory, name), 'w') as handle:
            handle.write(text)

    def _path(self, name):
        return os.path.join(self._directory, name)

    def test_a_working_tree_comparison_sees_edits_and_untracked_files(self):
        # git diff --name-only, which this replaced, silently omits Untracked.cpp: verified
        # against a scratch repository, a brand-new file never appeared in scope at all.
        self._write('Committed.cpp', 'int one();\nint two();\nint three();\n')
        self._write('Untracked.cpp', 'int four();\nint five();\n')
        added = git_diff_added_lines(self._directory, 'HEAD')
        self.assertEqual(added, {self._path('Committed.cpp'): [3],
                                 self._path('Untracked.cpp'): [1, 2]})

    def test_a_commit_range_excludes_the_working_tree_and_therefore_untracked_files(self):
        # `main...HEAD` asks about commits, and an untracked file is in no commit. A ref name
        # cannot contain `..`, so this is an exact test of which form was asked for.
        self._write('Committed.cpp', 'int one();\nint two();\nint committed();\n')
        self._git('add', 'Committed.cpp')
        self._git('commit', '-q', '-m', 'second')
        self._write('Untracked.cpp', 'int four();\n')
        self._write('Committed.cpp', 'int one();\nint two();\nint committed();\nint dirty();\n')
        self.assertEqual(git_diff_added_lines(self._directory, 'HEAD~1...HEAD'),
                         {self._path('Committed.cpp'): [3]})
        # The same repository through the working-tree form sees all three.
        self.assertEqual(git_diff_added_lines(self._directory, 'HEAD~1'),
                         {self._path('Committed.cpp'): [3, 4],
                          self._path('Untracked.cpp'): [1]})

    def test_a_bad_ref_raises_with_the_message_git_produced(self):
        with self.assertRaises(RuntimeError) as raised:
            git_diff_added_lines(self._directory, 'no-such-ref-exists-anywhere')
        self.assertIn('no-such-ref-exists-anywhere', str(raised.exception))


class PatchCoverageTest(unittest.TestCase):
    def test_an_added_line_with_no_coverage_record_is_excluded_not_counted_uncovered(self):
        # The comment, the blank line and the brace have no DA: record because there is no
        # code there. Counting them uncovered would make every patch look worse than it is.
        patch = PatchCoverage({'/co/a.cpp': [10, 11, 12, 13]},
                              {'/co/a.cpp': coverage([(12, 1), (13, 0)])})
        entry = patch.files[0]
        self.assertEqual(entry.added_line_count, 4)
        self.assertEqual(entry.instrumented_line_count, 2)
        self.assertEqual(entry.excluded_line_count, 2)
        self.assertEqual(entry.percent(), 50.0)
        self.assertEqual(patch.excluded_line_count, 2)

    def test_an_added_line_recorded_as_zero_is_uncovered_and_not_excluded(self):
        # The distinction the whole denominator rests on: no record at all means no code, a
        # record of 0 means code nothing ran.
        patch = PatchCoverage({'/co/a.cpp': [1, 2]}, {'/co/a.cpp': coverage([(2, 0)])})
        entry = patch.files[0]
        self.assertEqual(entry.uncovered_lines, [2])
        self.assertEqual(entry.excluded_line_count, 1)
        self.assertEqual(entry.percent(), 0.0)

    def test_the_uncovered_added_line_numbers_are_the_product(self):
        patch = PatchCoverage({'/co/a.cpp': [4, 5, 6, 7, 8]},
                              {'/co/a.cpp': coverage([(4, 2), (5, 0), (6, 0), (7, 0), (8, 3)])})
        self.assertEqual(patch.files[0].uncovered_lines, [5, 6, 7])
        self.assertEqual(patch.files[0].covered_lines, [4, 8])

    def test_only_the_added_lines_are_measured_and_not_the_rest_of_the_file(self):
        # A twenty-line untested addition to a large well-covered file is what delta coverage
        # cannot see: 8,051 lines at 92.88% become 8,071 at 92.65%, a drop of 0.23pp.
        lines = [(number, 1) for number in range(1, 8052)]
        lines += [(number, 0) for number in range(8052, 8072)]
        patch = PatchCoverage({'/co/a.cpp': list(range(8052, 8072))},
                              {'/co/a.cpp': coverage(lines)})
        self.assertEqual(patch.percent(), 0.0)
        self.assertEqual(patch.instrumented_line_count, 20)
        self.assertAlmostEqual(patch.file_percent(), 100.0 * 8051 / 8071)

    def test_a_changed_file_with_no_record_at_all_is_reported_missing(self):
        # The most alarming case there is: a source file nothing instrumented compiled.
        patch = PatchCoverage({'/co/a.cpp': [1], '/co/New.cpp': [1, 2]},
                              {'/co/a.cpp': coverage([(1, 1)])})
        self.assertEqual(patch.missing_paths, ['/co/New.cpp'])
        self.assertEqual([entry.path for entry in patch.files], ['/co/a.cpp'])
        # Its lines are in no denominator, rather than being counted as uncovered, because
        # nothing measured them either way.
        self.assertEqual(patch.instrumented_line_count, 1)

    def test_paths_that_cannot_have_coverage_are_ignored_not_reported_missing(self):
        patch = PatchCoverage({'/co/a.cpp': [1], '/co/LayoutTests/a-expected.txt': [1],
                               '/co/Source/CMakeLists.txt': [1]},
                              {'/co/a.cpp': coverage([(1, 1)])})
        self.assertEqual(patch.ignored_path_count, 2)
        self.assertEqual(patch.missing_paths, [])

    def test_the_overall_percentage_is_a_ratio_of_sums_not_an_average_of_ratios(self):
        # One fully covered one-line addition and one uncovered ninety-nine-line addition is
        # 1%, not 50%. Averaging the per-file percentages is the classic way to get this wrong.
        patch = PatchCoverage(
            {'/co/small.cpp': [1], '/co/big.cpp': list(range(1, 100))},
            {'/co/small.cpp': coverage([(1, 1)]),
             '/co/big.cpp': coverage([(number, 0) for number in range(1, 100)])})
        self.assertEqual(patch.percent(), 1.0)

    def test_a_patch_that_added_no_instrumented_line_has_no_percentage(self):
        # A comment-only change. Zero of zero is not 0%, and reporting it as 0% would fail
        # every gate for no reason.
        patch = PatchCoverage({'/co/a.cpp': [1, 2]}, {'/co/a.cpp': coverage([(50, 1)])})
        self.assertIsNone(patch.percent())
        self.assertEqual(patch.instrumented_line_count, 0)

    def test_files_are_ordered_by_how_many_added_lines_are_uncovered(self):
        # Not by percentage: one uncovered line in a two-line change is 50% and one uncovered
        # line in a two-hundred-line change is 99.5%, and the second is the one to look at.
        patch = PatchCoverage(
            {'/co/half.cpp': [1, 2], '/co/many.cpp': list(range(1, 21))},
            {'/co/half.cpp': coverage([(1, 1), (2, 0)]),
             '/co/many.cpp': coverage([(number, 0) for number in range(1, 21)])})
        self.assertEqual([entry.path for entry in patch.files], ['/co/many.cpp', '/co/half.cpp'])

    def test_a_line_shift_cannot_change_the_answer(self):
        # The property that makes this worth having before drift correction. Delta coverage
        # compares two sets of line numbers, so shifting three real records by twelve lines
        # with byte-identical coverage fabricated 400 "regressed" and 239 "uncovered new"
        # lines. Here the diff and the trace come from the same text, so shifting both -- as
        # inserting twelve lines above does -- is the same patch and the same answer.
        def measure(shift):
            lines = [(number + shift, 1) for number in (10, 11)]
            lines += [(number + shift, 0) for number in (12, 13)]
            return PatchCoverage({'/co/a.cpp': [number + shift for number in (10, 11, 12, 13)]},
                                 {'/co/a.cpp': coverage(lines)})

        unshifted, shifted = measure(0), measure(12)
        self.assertEqual(unshifted.percent(), shifted.percent())
        self.assertEqual([number + 12 for number in unshifted.files[0].uncovered_lines],
                         shifted.files[0].uncovered_lines)


class PatchSummaryTest(unittest.TestCase):
    def test_the_summary_leads_with_the_ratio_and_names_the_uncovered_lines(self):
        patch = PatchCoverage(
            {'/co/Source/a.cpp': [8052, 8053, 8054]},
            {'/co/Source/a.cpp': coverage([(number, 1) for number in range(1, 8052)]
                                          + [(8052, 0), (8054, 0)])})
        summary = format_patch_summary(patch, source_root='/co')
        self.assertIn('Patch coverage: 0.00% (0 of 2 added lines with coverage data covered)',
                      summary)
        self.assertIn('uncovered added lines 8052, 8054', summary)
        self.assertIn('1 added line carries no coverage record', summary)
        # Paths relative to the checkout, or the table is unreadable.
        self.assertIn('Source/a.cpp', summary)
        self.assertNotIn('/co/Source/a.cpp', summary)

    def test_the_summary_states_the_file_level_number_beside_the_patch_number(self):
        # Beside, never instead: the file-level number is the one that hides an untested
        # addition, so showing it alone would be the defect this replaces.
        patch = PatchCoverage({'/co/a.cpp': [3]},
                              {'/co/a.cpp': coverage([(1, 1), (2, 1), (3, 0)])})
        summary = format_patch_summary(patch, source_root='/co')
        self.assertIn('Patch coverage: 0.00%', summary)
        self.assertIn('coverage of the whole of 1 changed file: 66.67%', summary)

    def test_a_missing_file_is_called_out_rather_than_left_out(self):
        patch = PatchCoverage({'/co/a.cpp': [1], '/co/New.cpp': [1]},
                              {'/co/a.cpp': coverage([(1, 1)])})
        summary = format_patch_summary(patch, source_root='/co')
        self.assertIn('nothing instrumented compiled it', summary)
        self.assertIn('New.cpp', summary)

    def test_the_file_level_summary_says_it_cannot_see_the_added_lines(self):
        # --changed-files carries no line numbers, so the report has to say what it is not
        # answering rather than present a file percentage as if it were a patch percentage.
        patch = PatchCoverage({'/co/a.cpp': []}, {'/co/a.cpp': coverage([(1, 1), (2, 0)])},
                              line_numbers=False)
        summary = format_patch_summary(patch, source_root='/co')
        self.assertIn('File-level coverage of the whole of 1 changed file: 50.00%', summary)
        self.assertNotIn('Patch coverage:', summary)
        self.assertIn('--git-diff=REF', summary)

    def test_the_file_list_is_truncated(self):
        added = {'/co/f{}.cpp'.format(number): [1] for number in range(10)}
        files = {path: coverage([(1, 0)]) for path in added}
        summary = format_patch_summary(PatchCoverage(added, files), max_files=3)
        self.assertIn('... and 7 more files', summary)


class PatchReportTest(unittest.TestCase):
    """The HTML page. Until it existed, the answer this whole tool produces was a text file."""

    def setUp(self):
        self._directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._directory, ignore_errors=True)

    def write(self, patch, **keywords):
        path = write_patch_report(patch, self._directory, source_root='/co', **keywords)
        with open(path) as handle:
            return path, handle.read()

    def test_it_is_not_called_index_html(self):
        # generate-coverage-report and coverage_delta both write an index.html, and
        # webkit-coverage points compare-coverage-reports --output-dir at the report directory,
        # so a third one there would destroy the coverage index.
        patch = PatchCoverage({'/co/a.cpp': [2]}, {'/co/a.cpp': coverage([(1, 1), (2, 0)])})
        path, _ = self.write(patch)
        self.assertEqual(os.path.basename(path), 'patch-coverage.html')
        self.assertFalse(os.path.exists(os.path.join(self._directory, 'index.html')))

    def test_the_headline_is_the_patch_number_and_the_uncovered_lines_are_named(self):
        patch = PatchCoverage(
            {'/co/Source/a.cpp': [8052, 8053, 8054]},
            {'/co/Source/a.cpp': coverage([(number, 1) for number in range(1, 8052)]
                                          + [(8052, 0), (8054, 0)])})
        _, page = self.write(patch)
        self.assertIn('<h1>Patch coverage</h1>', page)
        self.assertIn('>0.00%<', page)
        self.assertIn('Uncovered added lines', page)
        self.assertIn('<code>8052, 8054</code>', page)
        self.assertIn('Source/a.cpp', page)
        self.assertNotIn('/co/Source/a.cpp', page)

    def test_line_numbers_link_into_the_source_view_when_there_is_one(self):
        patch = PatchCoverage({'/co/Source/a.cpp': [2, 3, 4]},
                              {'/co/Source/a.cpp': coverage([(1, 1), (2, 0), (3, 0), (4, 0)])})
        _, page = self.write(patch, report_root='')
        # One link per contiguous run, to the run's first line, which is where the cursor wants
        # to land. Not one anchor per line: a 200-line block is one thing to go and look at.
        self.assertIn('<a href="Source/a.cpp.html#L2">2-4</a>', page)

    def test_the_numbers_are_plain_text_when_there_is_nothing_to_link_into(self):
        # A link to a page that was never written is a 404, and 477 dead links in a shipped
        # report is a bug this branch has already had once.
        patch = PatchCoverage({'/co/Source/a.cpp': [2]},
                              {'/co/Source/a.cpp': coverage([(1, 1), (2, 0)])})
        _, page = self.write(patch)
        self.assertNotIn('.cpp.html', page)
        self.assertIn('<code>2</code>', page)

    def test_a_fully_covered_patch_says_so_instead_of_showing_an_empty_card(self):
        patch = PatchCoverage({'/co/a.cpp': [1, 2]},
                              {'/co/a.cpp': coverage([(1, 1), (2, 1)])})
        _, page = self.write(patch)
        self.assertIn('Every added line with coverage data was executed.', page)

    def test_a_file_with_no_coverage_data_gets_its_own_section(self):
        patch = PatchCoverage({'/co/a.cpp': [1], '/co/New.cpp': [1]},
                              {'/co/a.cpp': coverage([(1, 1)])})
        _, page = self.write(patch)
        self.assertIn('nothing instrumented compiled it', page)
        self.assertIn('New.cpp', page)

    def test_the_file_level_page_does_not_claim_to_measure_added_lines(self):
        patch = PatchCoverage({'/co/a.cpp': []}, {'/co/a.cpp': coverage([(1, 1), (2, 0)])},
                              line_numbers=False)
        _, page = self.write(patch)
        self.assertIn('<h1>Coverage of the files this change touched</h1>', page)
        self.assertNotIn('Uncovered added lines', page)
        self.assertIn('--git-diff=REF', page)

    def test_the_truncation_is_stated_rather_than_silent(self):
        added = {'/co/f{}.cpp'.format(number): [1] for number in range(10)}
        files = {path: coverage([(1, 0)]) for path in added}
        _, page = self.write(PatchCoverage(added, files), max_files=3)
        self.assertIn('first 3 of 10 changed files', page)

    def test_every_link_resolves(self):
        patch = PatchCoverage({'/co/Source/a.cpp': [2]},
                              {'/co/Source/a.cpp': coverage([(1, 1), (2, 0)])})
        path, page = self.write(patch, report_root='')
        # The source views live beside the page, so make the one it links to exist and then
        # check nothing else is dangling.
        target = os.path.join(self._directory, 'Source')
        os.makedirs(target, exist_ok=True)
        open(os.path.join(target, 'a.cpp.html'), 'w').close()
        broken = [link for link in re.findall(r'href="([^"#]+)', page)
                  if not os.path.exists(os.path.join(self._directory, link))]
        self.assertEqual(broken, [])


if __name__ == '__main__':
    unittest.main()


class FailUnderPatchTest(unittest.TestCase):
    """--fail-under-patch, exercised through the script's main() the way CI calls it."""

    @classmethod
    def setUpClass(cls):
        # The script has no .py extension, so it cannot simply be imported by name.
        import importlib.machinery
        import importlib.util
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'compare-coverage-reports')
        loader = importlib.machinery.SourceFileLoader('compare_coverage_reports', script)
        specification = importlib.util.spec_from_loader(loader.name, loader)
        cls.script = importlib.util.module_from_spec(specification)
        loader.exec_module(cls.script)

    def setUp(self):
        self._directory = tempfile.mkdtemp()
        # main() prints a summary and logs its verdict; neither belongs in test output.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self._git('init', '-q', self._directory)

    def _git(self, *arguments):
        subprocess.run(('git',) + _GIT_CONFIGURATION + arguments, cwd=self._directory,
                       check=True, capture_output=True, text=True)

    def _commit_then_append(self, name, committed_lines, added_lines):
        """Commit a file of committed_lines lines, then append added_lines more."""
        path = os.path.join(self._directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(''.join('int line{}();\n'.format(n)
                                 for n in range(1, committed_lines + 1)))
        self._git('add', name)
        self._git('commit', '-q', '-m', 'base')
        with open(path, 'a') as handle:
            handle.write(''.join('int added{}();\n'.format(n)
                                 for n in range(1, added_lines + 1)))
        return path

    def _lcov(self, name, files):
        """files: {absolute path: [(line, count)]}."""
        path = os.path.join(self._directory, name)
        with open(path, 'w') as handle:
            for source, lines in files.items():
                handle.write('SF:{}\n'.format(source))
                for number, count in lines:
                    handle.write('DA:{},{}\n'.format(number, count))
                handle.write('end_of_record\n')
        return path

    def _run(self, *extra):
        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout):
            return self.script.main(['--source-root', self._directory] + list(extra))

    def test_a_twenty_line_untested_addition_fails_the_patch_gate(self):
        # The case the delta gate cannot see. 8,051 covered lines plus 20 uncovered ones is
        # -0.23pp, which passes --fail-under-delta=0.5; as patch coverage it is 0 of 20.
        source = self._commit_then_append('Source/a.cpp', 8051, 20)
        lines = [(number, 1) for number in range(1, 8052)]
        lines += [(number, 0) for number in range(8052, 8072)]
        current = self._lcov('current.lcov', {source: lines})

        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--fail-under-patch', '70'), COVERAGE_GATE_EXIT_CODE)
        self.assertIn('0 of 20 added lines', self.stdout.getvalue())
        self.assertIn('uncovered added lines 8052-8071', self.stdout.getvalue())

        # And the same addition through --fail-under-delta PASSES, which is why this exists:
        # 8,051 of 8,051 lines becomes 8,051 of 8,071, a drop of 0.25pp, well inside the
        # 0.50pp the recommended gate allows.
        baseline = self._lcov('baseline.lcov', {source: lines[:8051]})
        self.assertEqual(self._run('--baseline', baseline, '--current', current,
                                   '--git-diff', 'HEAD', '--fail-under-delta', '0.5'), 0)
        self.assertIn('lines 100.00% -> 99.75%', self.stdout.getvalue())
        # The patch section of the same run still names all twenty lines.
        self.assertIn('0 of 20 added lines', self.stdout.getvalue())

    def test_a_fully_covered_addition_passes(self):
        source = self._commit_then_append('Source/a.cpp', 2, 3)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 1), (3, 4), (4, 4), (5, 4)]})
        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--fail-under-patch', '100'), 0)

    def test_a_patch_exactly_at_the_threshold_passes(self):
        # Float noise must not turn 50.0 against 50 into a build break.
        source = self._commit_then_append('Source/a.cpp', 1, 2)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 3), (3, 0)]})
        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--fail-under-patch', '50'), 0)

    def test_a_patch_with_no_instrumented_added_line_passes(self):
        # A comment-only or declaration-only change adds nothing a test could execute. Failing
        # that would make the flag unusable on a real branch -- unlike a gate that cannot be
        # computed, there is genuinely nothing to require here.
        source = self._commit_then_append('Source/a.cpp', 2, 2)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 1)]})
        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--fail-under-patch', '100'), 0)

    def test_an_untracked_new_file_reaches_the_gate(self):
        source = self._commit_then_append('Source/a.cpp', 2, 0)
        untracked = os.path.join(self._directory, 'Source', 'New.cpp')
        with open(untracked, 'w') as handle:
            handle.write('int one();\nint two();\nint three();\n')
        current = self._lcov('current.lcov', {
            source: [(1, 1), (2, 1)], untracked: [(1, 0), (2, 0), (3, 0)]})
        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--fail-under-patch', '50'), COVERAGE_GATE_EXIT_CODE)
        self.assertIn('New.cpp', self.stdout.getvalue())

    def test_a_current_only_run_needs_the_change_named(self):
        # Without a baseline and without a diff there is nothing here that
        # generate-coverage-report does not already answer.
        source = self._commit_then_append('Source/a.cpp', 2, 0)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 0)]})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._run('--current', current)

    def test_fail_under_patch_without_a_diff_is_rejected_rather_than_ignored(self):
        source = self._commit_then_append('Source/a.cpp', 2, 0)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 0)]})
        changed = os.path.join(self._directory, 'changed.txt')
        with open(changed, 'w') as handle:
            handle.write('Source/a.cpp\n')
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            self._run('--current', current, '--changed-files', changed,
                      '--fail-under-patch', '50')
        self.assertIn('--git-diff', stderr.getvalue())

    def test_fail_under_delta_without_a_baseline_is_rejected(self):
        source = self._commit_then_append('Source/a.cpp', 2, 0)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 0)]})
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            self._run('--current', current, '--git-diff', 'HEAD', '--fail-under-delta', '0.5')
        self.assertIn('--baseline', stderr.getvalue())

    def test_a_percentage_outside_zero_to_a_hundred_is_rejected(self):
        source = self._commit_then_append('Source/a.cpp', 2, 0)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 0)]})
        for value in ('-1', '101'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self._run('--current', current, '--git-diff', 'HEAD',
                          '--fail-under-patch', value)

    def test_a_file_level_run_reports_without_a_baseline(self):
        # The documented replacement for passing the same trace as both baseline and current.
        source = self._commit_then_append('Source/a.cpp', 2, 0)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 0)]})
        changed = os.path.join(self._directory, 'changed.txt')
        with open(changed, 'w') as handle:
            handle.write('Source/a.cpp\n')
        self.assertEqual(self._run('--current', current, '--changed-files', changed), 0)
        self.assertIn('File-level coverage', self.stdout.getvalue())

    def test_the_output_directory_gets_the_patch_summary(self):
        source = self._commit_then_append('Source/a.cpp', 2, 1)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 1), (3, 0)]})
        output = os.path.join(self._directory, 'report')
        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--output-dir', output), 0)
        with open(os.path.join(output, 'patch-summary.txt')) as handle:
            self.assertIn('Patch coverage:', handle.read())

    def test_the_output_directory_also_gets_the_patch_page(self):
        # Through the script, not the module: the wiring is the part that was missing, not the
        # renderer. The answer used to exist only as text.
        from webkitpy.coverage_patch import PATCH_REPORT_NAME
        source = self._commit_then_append('Source/a.cpp', 2, 1)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 1), (3, 0)]})
        output = os.path.join(self._directory, 'report')
        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--output-dir', output), 0)
        with open(os.path.join(output, PATCH_REPORT_NAME)) as handle:
            page = handle.read()
        self.assertIn('<h1>Patch coverage</h1>', page)
        self.assertIn('Uncovered added lines', page)
        # No coverage report in that directory, so there is nothing to link into and the line
        # numbers stay text rather than becoming links to pages that do not exist.
        self.assertNotIn('a.cpp.html', page)

    def test_the_line_numbers_link_when_a_report_is_in_the_output_directory(self):
        from webkitpy.coverage_patch import PATCH_REPORT_NAME
        source = self._commit_then_append('Source/a.cpp', 2, 1)
        current = self._lcov('current.lcov', {source: [(1, 1), (2, 1), (3, 0)]})
        output = os.path.join(self._directory, 'report')
        os.makedirs(output, exist_ok=True)
        # What generate-coverage-report leaves behind, and what webkit-coverage arranges by
        # pointing both tools at one directory.
        open(os.path.join(output, 'index.html'), 'w').close()
        self.assertEqual(self._run('--current', current, '--git-diff', 'HEAD',
                                   '--output-dir', output), 0)
        with open(os.path.join(output, PATCH_REPORT_NAME)) as handle:
            self.assertIn('Source/a.cpp.html#L3', handle.read())


if __name__ == '__main__':
    unittest.main()

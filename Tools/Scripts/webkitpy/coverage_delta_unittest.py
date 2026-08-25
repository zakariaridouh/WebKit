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
import tempfile
import unittest

from webkitpy.coverage_delta import (DELETED, IMPROVED, NEW, REGRESSED, UNCHANGED,
                                     absolute_paths, compare, compare_lcov_files,
                                     format_line_numbers, format_summary, write_delta_report)
from webkitpy.coverage_lcov import FileCoverage
from webkitpy.coverage_thresholds import COVERAGE_GATE_EXIT_CODE


def coverage(lines=(), functions=()):
    """A FileCoverage from (line, count) and (name, count) pairs."""
    result = FileCoverage()
    result.lines = dict(lines)
    result.functions = dict(functions)
    return result


def dataset(**files):
    """{'/a.cpp': [(1, 1), (2, 0)]} keyword form, since every test needs two of these."""
    return {path: coverage(lines) for path, lines in files.items()}


class LineLevelAttributionTest(unittest.TestCase):
    def test_a_line_that_goes_covered_to_uncovered_is_reported_with_its_line_number(self):
        # The whole point of the tool: the percentage is not enough, a reviewer needs to
        # be told which line to go and look at.
        delta = compare(dataset(a=[(10, 3), (11, 1), (12, 0)]),
                        dataset(a=[(10, 3), (11, 0), (12, 0)]))
        self.assertEqual(delta.file_deltas['a'].regressed_lines, [11])
        self.assertEqual(delta.file_deltas['a'].status, REGRESSED)

    def test_a_percentage_that_did_not_move_still_reports_the_regressed_line(self):
        # One line lost coverage and another gained it, so 50% before and 50% after. A
        # report that only compared percentages would call this file unchanged.
        delta = compare(dataset(a=[(1, 5), (2, 0)]), dataset(a=[(1, 0), (2, 5)]))
        file_delta = delta.file_deltas['a']
        self.assertEqual(file_delta.percent_delta(), 0.0)
        self.assertEqual(file_delta.regressed_lines, [1])
        self.assertEqual(file_delta.recovered_lines, [2])
        self.assertEqual(file_delta.status, REGRESSED)

    def test_a_new_uncovered_line_is_not_counted_as_a_regression(self):
        # The denominator grew by untested code. That is a gap in new code, a different
        # finding from an existing test no longer reaching existing code, and the two must
        # never be added together.
        delta = compare(dataset(a=[(1, 5)]), dataset(a=[(1, 5), (2, 0), (3, 0)]))
        file_delta = delta.file_deltas['a']
        self.assertEqual(file_delta.regressed_lines, [])
        self.assertEqual(file_delta.new_uncovered_lines, [2, 3])
        self.assertEqual(file_delta.new_covered_lines, [])
        self.assertEqual(delta.overall.regressed_line_count, 0)
        self.assertEqual(delta.overall.new_uncovered_line_count, 2)

    def test_lines_that_left_the_denominator_are_reported_separately(self):
        delta = compare(dataset(a=[(1, 5), (2, 5), (3, 0)]), dataset(a=[(1, 5)]))
        file_delta = delta.file_deltas['a']
        self.assertEqual(file_delta.removed_lines, [2, 3])
        self.assertEqual(file_delta.regressed_lines, [])
        self.assertEqual(file_delta.denominator_delta, -2)

    def test_functions_are_matched_by_name_so_they_survive_a_line_shift(self):
        # Every line number moved, which makes the line-level comparison meaningless here,
        # but the mangled names still line up and still show that one function is dead.
        baseline = {'a': coverage(lines=[(1, 4), (2, 4)],
                                  functions=[('_ZN3Foo3barEv', 4), ('_ZN3Foo4bazEv', 2)])}
        current = {'a': coverage(lines=[(31, 4), (32, 4)],
                                 functions=[('_ZN3Foo3barEv', 4), ('_ZN3Foo4bazEv', 0)])}
        file_delta = compare(baseline, current).file_deltas['a']
        self.assertEqual(file_delta.regressed_functions, ['_ZN3Foo4bazEv'])


class FileClassificationTest(unittest.TestCase):
    def test_a_file_only_in_current_is_new(self):
        delta = compare(dataset(a=[(1, 1)]), dataset(a=[(1, 1)], b=[(1, 0), (2, 0)]))
        self.assertEqual(delta.file_deltas['b'].status, NEW)
        self.assertIsNone(delta.file_deltas['b'].baseline_totals)
        # A new file has no baseline percentage, so its delta is undefined rather than a
        # drop from zero.
        self.assertIsNone(delta.file_deltas['b'].percent_delta())
        self.assertEqual(delta.file_deltas['b'].new_uncovered_lines, [1, 2])

    def test_a_file_only_in_baseline_is_deleted(self):
        delta = compare(dataset(a=[(1, 1)], b=[(1, 1)]), dataset(a=[(1, 1)]))
        self.assertEqual(delta.file_deltas['b'].status, DELETED)
        self.assertIsNone(delta.file_deltas['b'].current_totals)
        self.assertEqual(delta.file_deltas['b'].denominator_delta, -1)

    def test_an_unchanged_file_is_not_reported_as_changed(self):
        delta = compare(dataset(a=[(1, 1), (2, 0)], b=[(7, 3)]),
                        dataset(a=[(1, 9), (2, 0)], b=[(7, 3)]))
        # The execution count of line 1 rose, but coverage is a boolean, so nothing
        # changed about what is covered.
        self.assertEqual(delta.file_deltas['a'].status, UNCHANGED)
        self.assertEqual(delta.file_deltas['b'].status, UNCHANGED)
        self.assertEqual(delta.files_to_report(), [])

    def test_a_file_that_gained_coverage_is_improved(self):
        delta = compare(dataset(a=[(1, 0), (2, 0)]), dataset(a=[(1, 4), (2, 0)]))
        self.assertEqual(delta.file_deltas['a'].status, IMPROVED)
        self.assertEqual(delta.file_deltas['a'].recovered_lines, [1])
        self.assertEqual(delta.file_deltas['a'].percent_delta(), 50.0)

    def test_only_the_files_that_moved_are_reported_when_unfocused(self):
        delta = compare(dataset(a=[(1, 1)], b=[(1, 1)]), dataset(a=[(1, 0)], b=[(1, 1)]))
        self.assertEqual([f.path for f in delta.files_to_report()], ['a'])

    def test_regressions_sort_ahead_of_everything_else(self):
        delta = compare(dataset(regressed=[(1, 1), (2, 1)], grew=[(1, 1)]),
                        dataset(regressed=[(1, 1), (2, 0)],
                                grew=[(1, 1)] + [(n, 0) for n in range(2, 60)]))
        # `grew` gained far more uncovered lines, but `regressed` is the one where a test
        # stopped reaching code that it used to, so it has to come first.
        self.assertEqual([f.path for f in delta.files_to_report()], ['regressed', 'grew'])


class DenominatorChangeTest(unittest.TestCase):
    def test_a_file_can_gain_covered_lines_and_still_lose_percentage(self):
        # 1 of 2 covered, so 50%. Then 6 lines are added, 2 of them covered: 3 of 8, 37.5%.
        # More covered lines than before, and a percentage that fell by 12.5pp.
        delta = compare(dataset(a=[(1, 1), (2, 0)]),
                        dataset(a=[(1, 1), (2, 0), (3, 7), (4, 7), (5, 0), (6, 0), (7, 0), (8, 0)]))
        file_delta = delta.file_deltas['a']
        self.assertEqual(file_delta.baseline_percent(), 50.0)
        self.assertEqual(file_delta.current_percent(), 37.5)
        self.assertAlmostEqual(file_delta.percent_delta(), -12.5)
        self.assertEqual(file_delta.covered_delta, 2)
        self.assertEqual(file_delta.denominator_delta, 6)
        self.assertEqual(file_delta.uncovered_delta, 4)
        # Nothing regressed. The author added code that is mostly untested.
        self.assertEqual(file_delta.regressed_lines, [])
        self.assertEqual(file_delta.new_covered_lines, [3, 4])
        self.assertEqual(file_delta.new_uncovered_lines, [5, 6, 7, 8])
        # The one-word headline still has to say the percentage fell.
        self.assertEqual(file_delta.status, REGRESSED)

    def test_deleting_an_uncovered_file_raises_the_percentage_without_covering_anything(self):
        delta = compare(dataset(kept=[(1, 1)], dead=[(1, 0), (2, 0), (3, 0)]),
                        dataset(kept=[(1, 1)]))
        self.assertEqual(delta.overall.baseline_percent(), 25.0)
        self.assertEqual(delta.overall.current_percent(), 100.0)
        self.assertEqual(delta.overall.percent_delta(), 75.0)
        self.assertEqual(delta.overall.recovered_line_count, 0)

    def test_project_totals_are_ratios_of_sums_not_averages_of_ratios(self):
        # One 100%-covered 1-line file and one 0%-covered 99-line file is 1% overall, not
        # 50%. Averaging the per-file percentages is the classic way to get this wrong.
        delta = compare(dataset(small=[(1, 1)], big=[(n, 0) for n in range(1, 100)]),
                        dataset(small=[(1, 1)], big=[(n, 0) for n in range(1, 100)]))
        self.assertEqual(delta.overall.current_percent(), 1.0)


class ChangedFilesTest(unittest.TestCase):
    def _delta(self, changed):
        baseline = dataset(**{'/co/Source/a.cpp': [(1, 1), (2, 1)],
                              '/co/Source/b.cpp': [(1, 1), (2, 1)]})
        current = dataset(**{'/co/Source/a.cpp': [(1, 1), (2, 0)],
                             '/co/Source/b.cpp': [(1, 0), (2, 0)]})
        return compare(baseline, current, changed_files=changed)

    def test_the_scope_covers_only_the_changed_files_but_overall_still_covers_all(self):
        delta = self._delta(['/co/Source/a.cpp'])
        self.assertTrue(delta.focused)
        self.assertEqual(delta.scope_paths, ['/co/Source/a.cpp'])
        self.assertEqual(delta.scope.percent_delta(), -50.0)
        # a.cpp lost one line and b.cpp lost two, so the project as a whole lost three of
        # four; the focused number must not be contaminated by b.cpp.
        self.assertEqual(delta.overall.percent_delta(), -75.0)

    def test_a_changed_file_whose_coverage_did_not_move_is_still_listed(self):
        # "The file you touched is unchanged" is an answer, so a focused report lists it,
        # unlike an unfocused one which would drown in 18,000 unchanged files.
        baseline = dataset(**{'/co/a.cpp': [(1, 1)], '/co/b.cpp': [(1, 1)]})
        current = dataset(**{'/co/a.cpp': [(1, 1)], '/co/b.cpp': [(1, 0)]})
        self.assertEqual([f.path for f in compare(baseline, current).files_to_report()],
                         ['/co/b.cpp'])
        focused = compare(baseline, current, changed_files=['/co/a.cpp'])
        self.assertEqual([f.path for f in focused.files_to_report()], ['/co/a.cpp'])
        self.assertEqual(focused.scope.statuses[UNCHANGED], 1)

    def test_a_changed_source_file_with_no_coverage_data_is_reported_as_missing(self):
        # Silently dropping it would hide the most alarming case there is: a new source
        # file that nothing instrumented even compiled.
        delta = self._delta(['/co/Source/a.cpp', '/co/Source/untested.cpp'])
        self.assertEqual(delta.missing_paths, ['/co/Source/untested.cpp'])
        self.assertEqual(delta.scope_paths, ['/co/Source/a.cpp'])

    def test_changed_paths_that_cannot_have_coverage_are_ignored_not_reported_missing(self):
        delta = self._delta(['/co/Source/a.cpp', '/co/LayoutTests/fast/a-expected.txt',
                             '/co/Source/CMakeLists.txt'])
        self.assertEqual(delta.missing_paths, [])
        self.assertEqual(delta.ignored_path_count, 2)

    def test_relative_changed_paths_resolve_against_the_checkout(self):
        self.assertEqual(absolute_paths(['Source/WebCore/dom/Node.cpp', '', '  \n'], '/co'),
                         {'/co/Source/WebCore/dom/Node.cpp'})

    def test_absolute_changed_paths_are_left_alone(self):
        self.assertEqual(absolute_paths(['/elsewhere/a.cpp\n'], '/co'), {'/elsewhere/a.cpp'})


class DirectoryTotalsTest(unittest.TestCase):
    def test_files_aggregate_into_their_immediate_directory(self):
        delta = compare(dataset(**{'/co/dom/a.cpp': [(1, 1), (2, 1)],
                                   '/co/dom/b.cpp': [(1, 1)],
                                   '/co/css/c.cpp': [(1, 1)]}),
                        dataset(**{'/co/dom/a.cpp': [(1, 1), (2, 0)],
                                   '/co/dom/b.cpp': [(1, 0)],
                                   '/co/css/c.cpp': [(1, 1)]}))
        totals = delta.directory_totals()
        # css did not change, so it is not in the report and not in the directory table.
        self.assertEqual(sorted(totals), ['/co/dom'])
        self.assertEqual(totals['/co/dom'].file_count, 2)
        self.assertEqual(totals['/co/dom'].regressed_line_count, 2)
        self.assertEqual(totals['/co/dom'].current['lines'], [3, 1])


class LineNumberFormattingTest(unittest.TestCase):
    def test_consecutive_lines_collapse_into_ranges(self):
        self.assertEqual(format_line_numbers([1, 2, 3, 7, 9, 10]), '1-3, 7, 9-10')

    def test_a_long_run_of_ranges_is_truncated_with_a_count(self):
        self.assertEqual(format_line_numbers([1, 3, 5, 7], limit=2), '1, 3, and 2 more')

    def test_no_lines_is_empty(self):
        self.assertEqual(format_line_numbers([]), '')


class TextSummaryTest(unittest.TestCase):
    def test_the_summary_names_the_regressed_lines_and_separates_the_two_findings(self):
        delta = compare(dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 1), (12, 1)]}),
                        dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 0), (12, 0), (40, 0)]}))
        summary = format_summary(delta, source_root='/co')
        self.assertIn('covered -> uncovered at 11-12', summary)
        self.assertIn('Regressions, lines that were covered and are not any more: 2 in 1 file',
                      summary)
        self.assertIn('Gaps in new code, lines only in current and never executed: 1', summary)
        # Paths are shown relative to the checkout, or the table is unreadable.
        self.assertIn('Source/a.cpp', summary)
        self.assertNotIn('/co/Source/a.cpp', summary)

    def test_a_focused_summary_leads_with_the_metric_that_survives_a_source_edit(self):
        delta = compare(dataset(**{'/co/a.cpp': [(1, 1), (2, 1)], '/co/b.cpp': [(1, 1)]}),
                        dataset(**{'/co/a.cpp': [(1, 1), (2, 0)], '/co/b.cpp': [(1, 1)]}),
                        changed_files=['/co/a.cpp'])
        summary = format_summary(delta, source_root='/co')
        self.assertIn('coverage of lines present in current: 50.00% (1 of 2 lines)', summary)
        self.assertIn('whole project:', summary)

    def test_the_file_list_is_truncated(self):
        baseline = dataset(**{'f{}.cpp'.format(n): [(1, 1)] for n in range(10)})
        current = dataset(**{'f{}.cpp'.format(n): [(1, 0)] for n in range(10)})
        summary = format_summary(compare(baseline, current), max_files=3)
        self.assertIn('... and 7 more files', summary)


class HTMLReportTest(unittest.TestCase):
    def _write(self, delta, **kwargs):
        output = os.path.join(tempfile.mkdtemp(), 'delta')
        path = write_delta_report(delta, output, **kwargs)
        with open(path) as handle:
            return handle.read()

    def test_the_report_carries_the_line_numbers_and_the_shared_visual_language(self):
        from webkitpy.coverage_directory_index import REPORT_STYLE
        delta = compare(dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 1)]}),
                        dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 0), (12, 0)]}))
        page = self._write(delta, source_root='/co')
        self.assertIn('11', page)
        self.assertIn('covered &rarr; uncovered', page)
        # Imported, not copied, so the delta report cannot drift from the directory index.
        self.assertIn(REPORT_STYLE, page)
        self.assertIn('--delta-negative', page)
        self.assertIn('Source/a.cpp', page)

    def test_an_unchanged_report_still_renders(self):
        delta = compare(dataset(a=[(1, 1)]), dataset(a=[(1, 1)]))
        page = self._write(delta)
        self.assertIn('No file changed coverage.', page)
        self.assertIn('No line went from covered to uncovered.', page)

    def test_the_bar_scale_is_floored_so_a_tiny_change_is_not_drawn_as_a_huge_one(self):
        # 1 of 1000 lines lost, a delta of a tenth of a point. Against a scale of its own
        # magnitude it would draw as a full-width bar.
        baseline = dataset(a=[(n, 1) for n in range(1, 1001)])
        current = dataset(a=[(n, 1 if n != 500 else 0) for n in range(1, 1001)])
        page = self._write(compare(baseline, current))
        self.assertIn('±1.00pp', page)


class LcovRoundTripTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.mkdtemp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _lcov(self, name, records):
        path = os.path.join(self._directory, name)
        with open(path, 'w') as handle:
            for filename, lines in records:
                handle.write('SF:{}\n'.format(filename))
                for number, count in lines:
                    handle.write('DA:{},{}\n'.format(number, count))
                handle.write('end_of_record\n')
        return path

    def test_both_traces_are_canonicalized_so_a_copied_header_is_not_a_rename(self):
        # The baseline saw wtf/Vector.h through the source tree and the current run saw it
        # through the build directory's copy. Without canonicalizing both against one
        # checkout root this would read as a deleted file plus an unrelated new one.
        baseline = self._lcov('baseline.lcov', [
            ('/checkout/Source/WTF/wtf/Vector.h', [(1, 5), (2, 1)])])
        current = self._lcov('current.lcov', [
            ('/checkout/WebKitBuild/Release/usr/local/include/wtf/Vector.h', [(1, 5), (2, 0)])])

        delta = compare_lcov_files(baseline, current, source_root='/checkout')

        self.assertEqual(sorted(delta.file_deltas), ['/checkout/Source/WTF/wtf/Vector.h'])
        file_delta = delta.file_deltas['/checkout/Source/WTF/wtf/Vector.h']
        self.assertEqual(file_delta.status, REGRESSED)
        self.assertEqual(file_delta.regressed_lines, [2])

    def test_an_empty_trace_is_an_error_rather_than_a_report_of_total_loss(self):
        # Reporting an empty current trace as "everything got deleted" would turn a broken
        # run into a wall of false regressions.
        baseline = self._lcov('baseline.lcov', [('/checkout/a.cpp', [(1, 1)])])
        empty = self._lcov('empty.lcov', [])
        with self.assertRaises(RuntimeError):
            compare_lcov_files(baseline, empty, source_root='/checkout')
        with self.assertRaises(RuntimeError):
            compare_lcov_files(empty, baseline, source_root='/checkout')


class FailUnderDeltaTest(unittest.TestCase):
    """--fail-under-delta, exercised through the script's main() the way CI calls it."""

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

    def _lcov(self, name, files):
        """files: {repository-relative path: [(line, count)]}."""
        path = os.path.join(self._directory, name)
        with open(path, 'w') as handle:
            for relative, lines in files.items():
                handle.write('SF:{}\n'.format(os.path.join(self._directory, relative)))
                for number, count in lines:
                    handle.write('DA:{},{}\n'.format(number, count))
                handle.write('end_of_record\n')
        return path

    def _run(self, baseline_files, current_files, *extra):
        arguments = ['--baseline', self._lcov('baseline.lcov', baseline_files),
                     '--current', self._lcov('current.lcov', current_files),
                     '--source-root', self._directory] + list(extra)
        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout):
            return self.script.main(arguments)

    # 4 lines, 3 covered (75%) against 4 lines, 2 covered (50%): a drop of 25pp.
    _BEFORE = {'Source/a.cpp': [(1, 1), (2, 1), (3, 1), (4, 0)]}
    _AFTER = {'Source/a.cpp': [(1, 1), (2, 1), (3, 0), (4, 0)]}

    def test_a_drop_larger_than_the_allowance_fails(self):
        # COVERAGE_GATE_EXIT_CODE and not 1: 1 means the comparison could not be made at all,
        # which is somebody else's problem to fix.
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '10'),
                         COVERAGE_GATE_EXIT_CODE)

    def test_a_drop_within_the_allowance_passes(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '30'), 0)

    def test_a_drop_exactly_at_the_allowance_passes(self):
        # The flag reads as "fail if it regressed by MORE than this", so the boundary is
        # not a failure. Float noise must not turn 25.0 against 25 into a build break.
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '25'), 0)

    def test_zero_allows_no_drop_at_all_but_still_tolerates_no_change(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '0'),
                         COVERAGE_GATE_EXIT_CODE)
        self.assertEqual(self._run(self._BEFORE, self._BEFORE, '--fail-under-delta', '0'), 0)

    def test_an_improvement_never_fails(self):
        self.assertEqual(self._run(self._AFTER, self._BEFORE, '--fail-under-delta', '0'), 0)

    def test_without_the_flag_a_regression_is_reported_but_does_not_fail(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER), 0)
        # The summary goes to stdout so it can be piped into a review comment.
        self.assertIn('covered -> uncovered at 3', self.stdout.getvalue())

    def test_a_negative_allowance_is_rejected(self):
        # It is a size, not a signed threshold, and reading it as one would silently
        # invert the gate.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '-5')

    def test_the_gate_follows_the_changed_file_restriction(self):
        # b.cpp regressed and a.cpp did not, so a run focused on a.cpp must not fail on
        # somebody else's regression, and one focused on b.cpp must.
        baseline = {'Source/a.cpp': [(1, 1)], 'Source/b.cpp': [(1, 1), (2, 1)]}
        current = {'Source/a.cpp': [(1, 1)], 'Source/b.cpp': [(1, 1), (2, 0)]}
        for relative, expected in (('Source/a.cpp', 0),
                                   ('Source/b.cpp', COVERAGE_GATE_EXIT_CODE)):
            changed = os.path.join(self._directory, 'changed.txt')
            with open(changed, 'w') as handle:
                handle.write(relative + '\n')
            self.assertEqual(self._run(baseline, current, '--changed-files', changed,
                                       '--fail-under-delta', '0'), expected, relative)

    def test_changed_files_that_are_in_neither_trace_are_an_error_not_a_silent_pass(self):
        # Reporting "no regression" for a set of files nothing knows about would make the
        # gate pass whenever --source-root is wrong.
        changed = os.path.join(self._directory, 'changed.txt')
        with open(changed, 'w') as handle:
            handle.write('Source/never-built.cpp\n')
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--changed-files', changed,
                                   '--fail-under-delta', '0'), 1)

    def test_the_report_directory_gets_a_page_and_a_summary_but_no_browser(self):
        output = os.path.join(self._directory, 'report')
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--output-dir', output), 0)
        self.assertTrue(os.path.isfile(os.path.join(output, 'index.html')))
        self.assertTrue(os.path.isfile(os.path.join(output, 'summary.txt')))

    def test_a_bad_git_ref_is_a_message_not_a_traceback(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER,
                                   '--git-diff', 'no-such-ref-exists-anywhere'), 1)


if __name__ == '__main__':
    unittest.main()

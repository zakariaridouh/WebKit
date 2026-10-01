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
import tempfile
import unittest

from webkitpy.coverage_delta import (DELETED, DELTA_REPORT_NAME, DELTA_SUMMARY_NAME, IMPROVED,
                                     NEW, REGRESSED, UNCHANGED, LineViews, absolute_paths,
                                     compare, compare_lcov_files, format_line_links,
                                     format_line_numbers, format_summary, load_traces,
                                     write_delta_report)
from webkitpy.coverage_lcov import FileCoverage
from webkitpy.coverage_thresholds import COVERAGE_GATE_EXIT_CODE


def coverage(lines=(), functions=()):
    result = FileCoverage()
    result.lines = dict(lines)
    result.functions = dict(functions)
    return result


def dataset(**files):
    """{'/a.cpp': [(1, 1), (2, 0)]} keyword form, since every test needs two of these."""
    return {path: coverage(lines) for path, lines in files.items()}


class LineLevelAttributionTest(unittest.TestCase):
    def test_a_line_that_goes_covered_to_uncovered_is_reported_with_its_line_number(self):
        delta = compare(dataset(a=[(10, 3), (11, 1), (12, 0)]),
                        dataset(a=[(10, 3), (11, 0), (12, 0)]))
        self.assertEqual(delta.file_deltas['a'].regressed_lines, [11])
        self.assertEqual(delta.file_deltas['a'].status, REGRESSED)

    def test_a_percentage_that_did_not_move_still_reports_the_regressed_line(self):
        delta = compare(dataset(a=[(1, 5), (2, 0)]), dataset(a=[(1, 0), (2, 5)]))
        file_delta = delta.file_deltas['a']
        self.assertEqual(file_delta.percent_delta(), 0.0)
        self.assertEqual(file_delta.regressed_lines, [1])
        self.assertEqual(file_delta.recovered_lines, [2])
        self.assertEqual(file_delta.status, REGRESSED)

    def test_a_new_uncovered_line_is_not_counted_as_a_regression(self):
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
        # Undefined, not a drop from zero.
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
        # Line 1's execution count rose, but coverage is a boolean.
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
        self.assertEqual([f.path for f in delta.files_to_report()], ['regressed', 'grew'])


class DenominatorChangeTest(unittest.TestCase):
    def test_a_file_can_gain_covered_lines_and_still_lose_percentage(self):
        # 1 of 2 covered (50%); then 6 lines are added, 2 covered: 3 of 8 (37.5%).
        delta = compare(dataset(a=[(1, 1), (2, 0)]),
                        dataset(a=[(1, 1), (2, 0), (3, 7), (4, 7), (5, 0), (6, 0), (7, 0), (8, 0)]))
        file_delta = delta.file_deltas['a']
        self.assertEqual(file_delta.baseline_percent(), 50.0)
        self.assertEqual(file_delta.current_percent(), 37.5)
        self.assertAlmostEqual(file_delta.percent_delta(), -12.5)
        self.assertEqual(file_delta.covered_delta, 2)
        self.assertEqual(file_delta.denominator_delta, 6)
        self.assertEqual(file_delta.uncovered_delta, 4)
        self.assertEqual(file_delta.regressed_lines, [])
        self.assertEqual(file_delta.new_covered_lines, [3, 4])
        self.assertEqual(file_delta.new_uncovered_lines, [5, 6, 7, 8])
        self.assertEqual(file_delta.status, REGRESSED)

    def test_deleting_an_uncovered_file_raises_the_percentage_without_covering_anything(self):
        delta = compare(dataset(kept=[(1, 1)], dead=[(1, 0), (2, 0), (3, 0)]),
                        dataset(kept=[(1, 1)]))
        self.assertEqual(delta.overall.baseline_percent(), 25.0)
        self.assertEqual(delta.overall.current_percent(), 100.0)
        self.assertEqual(delta.overall.percent_delta(), 75.0)
        self.assertEqual(delta.overall.recovered_line_count, 0)

    def test_project_totals_are_ratios_of_sums_not_averages_of_ratios(self):
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
        self.assertEqual(delta.overall.percent_delta(), -75.0)

    def test_a_changed_file_whose_coverage_did_not_move_is_still_listed(self):
        baseline = dataset(**{'/co/a.cpp': [(1, 1)], '/co/b.cpp': [(1, 1)]})
        current = dataset(**{'/co/a.cpp': [(1, 1)], '/co/b.cpp': [(1, 0)]})
        self.assertEqual([f.path for f in compare(baseline, current).files_to_report()],
                         ['/co/b.cpp'])
        focused = compare(baseline, current, changed_files=['/co/a.cpp'])
        self.assertEqual([f.path for f in focused.files_to_report()], ['/co/a.cpp'])
        self.assertEqual(focused.scope.statuses[UNCHANGED], 1)

    def test_a_changed_source_file_with_no_coverage_data_is_reported_as_missing(self):
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

    def test_it_is_not_called_index_html(self):
        output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, output, ignore_errors=True)
        path = write_delta_report(compare(dataset(a=[(1, 1)]), dataset(a=[(1, 0)])), output)
        self.assertEqual(os.path.basename(path), 'delta-coverage.html')
        self.assertEqual(DELTA_REPORT_NAME, 'delta-coverage.html')
        self.assertEqual(os.listdir(output), [DELTA_REPORT_NAME])

    def test_the_report_carries_the_line_numbers_and_the_shared_visual_language(self):
        from webkitpy.coverage_directory_index import REPORT_STYLE
        delta = compare(dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 1)]}),
                        dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 0), (12, 0)]}))
        page = self._write(delta, source_root='/co')
        self.assertIn('11', page)
        self.assertIn('covered &rarr; uncovered', page)
        self.assertIn(REPORT_STYLE, page)
        self.assertIn('--delta-negative', page)
        self.assertIn('Source/a.cpp', page)

    def test_an_unchanged_report_still_renders(self):
        delta = compare(dataset(a=[(1, 1)]), dataset(a=[(1, 1)]))
        page = self._write(delta)
        self.assertIn('No file changed coverage.', page)
        self.assertIn('No line went from covered to uncovered.', page)

    def test_the_bar_scale_is_floored_so_a_tiny_change_is_not_drawn_as_a_huge_one(self):
        baseline = dataset(a=[(n, 1) for n in range(1, 1001)])
        current = dataset(a=[(n, 1 if n != 500 else 0) for n in range(1, 1001)])
        page = self._write(compare(baseline, current))
        self.assertIn('±1.00pp', page)


class LineViewLinkTest(unittest.TestCase):
    def setUp(self):
        self._output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._output, ignore_errors=True)

    def _line_view(self, relative):
        path = os.path.join(self._output, relative + '.html')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()

    def _page(self, delta):
        with open(write_delta_report(delta, self._output, source_root='/co')) as handle:
            return handle.read()

    def _delta(self):
        # a.cpp: lines 11 and 20-21 regressed, 12 is new and uncovered. b.cpp: line 5 regressed.
        return compare(
            dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 1), (20, 1), (21, 2)],
                       '/co/Source/b.cpp': [(5, 1), (6, 1)]}),
            dataset(**{'/co/Source/a.cpp': [(10, 1), (11, 0), (12, 0), (20, 0), (21, 0)],
                       '/co/Source/b.cpp': [(5, 0), (6, 1)]}))

    def test_file_names_and_regressed_lines_link_to_the_line_view(self):
        self._line_view('Source/a.cpp')
        page = self._page(self._delta())
        self.assertIn('<a href="Source/a.cpp.html">Source/a.cpp</a>', page)
        self.assertIn('<a href="Source/a.cpp.html#L11">11</a>', page)
        self.assertIn('<a href="Source/a.cpp.html#L20-L21">20-21</a>', page)
        self.assertIn('new, never executed <code><a href="Source/a.cpp.html#L12">12</a>', page)

    def test_without_line_views_everything_stays_text(self):
        page = self._page(self._delta())
        self.assertNotIn('.cpp.html', page)
        self.assertIn('<code>11, 20-21</code>', page)

    def test_each_file_is_checked_for_its_own_line_view(self):
        self._line_view('Source/a.cpp')
        page = self._page(self._delta())
        self.assertIn('Source/a.cpp.html', page)
        self.assertNotIn('Source/b.cpp.html', page)
        self.assertIn('<td data-v="Source/b.cpp">Source/b.cpp</td>', page)

    def test_every_link_resolves(self):
        self._line_view('Source/a.cpp')
        self._line_view('Source/b.cpp')
        page = self._page(self._delta())
        links = re.findall(r'href="([^"#]+)', page)
        self.assertTrue(links)
        self.assertEqual([link for link in links
                          if not os.path.isfile(os.path.join(self._output, link))], [])

    def test_a_path_is_escaped_in_the_link_and_in_the_text(self):
        name = '/co/Source/a&b <c>.cpp'
        self._line_view('Source/a&b <c>.cpp')
        page = self._page(compare(dataset(**{name: [(1, 1)]}), dataset(**{name: [(1, 0)]})))
        self.assertIn('<a href="Source/a%26b%20%3Cc%3E.cpp.html#L1">1</a>', page)
        self.assertIn('>Source/a&amp;b &lt;c&gt;.cpp</a>', page)
        self.assertNotIn('<c>', page)

    def test_links_agree_with_the_line_views_generate_coverage_report_writes(self):
        # Against the real writer, including a path outside the checkout, which gets its own directory.
        from webkitpy.coverage_lcov import FileCoverage
        from webkitpy.coverage_source_view import write_source_views
        checkout = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, checkout, ignore_errors=True)
        source = os.path.join(checkout, 'Source', 'WTF', 'wtf', 'Thing.cpp')
        os.makedirs(os.path.dirname(source))
        with open(source, 'w') as handle:
            handle.write('int a;\nint b;\nint c;\n')
        current = FileCoverage()
        current.lines = {1: 1, 2: 0, 3: 0}
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        write_source_views({source: current}, self._output, checkout, workers=1)

        baseline = FileCoverage()
        baseline.lines = {1: 1, 2: 1, 3: 1}
        path = write_delta_report(compare({source: baseline}, {source: current}), self._output,
                                  source_root=checkout)
        with open(path) as handle:
            page = handle.read()
        self.assertIn('<a href="Source/WTF/wtf/Thing.cpp.html#L2-L3">2-3</a>', page)
        with open(os.path.join(self._output, 'Source/WTF/wtf/Thing.cpp.html')) as handle:
            self.assertIn('id=L2', handle.read())


class LineViewsTest(unittest.TestCase):
    def setUp(self):
        self._output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._output, ignore_errors=True)
        os.makedirs(os.path.join(self._output, 'Source'))
        open(os.path.join(self._output, 'Source', 'a.cpp.html'), 'w').close()

    def test_a_line_and_a_range(self):
        views = LineViews(self._output, '/co')
        self.assertEqual(views.page('/co/Source/a.cpp'), 'Source/a.cpp.html')
        self.assertEqual(views.lines('/co/Source/a.cpp', 7), 'Source/a.cpp.html#L7')
        self.assertEqual(views.lines('/co/Source/a.cpp', 7, 7), 'Source/a.cpp.html#L7')
        self.assertEqual(views.lines('/co/Source/a.cpp', 7, 9), 'Source/a.cpp.html#L7-L9')
        self.assertIsNone(views.page('/co/Source/b.cpp'))
        self.assertIsNone(views.lines('/co/Source/b.cpp', 7))

    def test_a_file_named_index_links_to_its_renamed_page(self):
        open(os.path.join(self._output, 'Source', 'index.html.html'), 'w').close()
        self.assertEqual(LineViews(self._output, '/co').page('/co/Source/index'), 'Source/index.html.html')

    def test_a_prefix_is_how_the_page_reaches_the_report_root(self):
        views = LineViews(self._output, '/co', prefix='../report/')
        self.assertEqual(views.page('/co/Source/a.cpp'), '../report/Source/a.cpp.html')

    def test_no_directory_means_no_links(self):
        self.assertIsNone(LineViews(None, '/co').page('/co/Source/a.cpp'))

    def test_line_links_fall_back_to_text(self):
        self.assertEqual(format_line_links([1, 2, 3, 7]), '1-3, 7')
        self.assertEqual(format_line_links([1, 2, 3, 7], lambda low, high: '#L{}'.format(low)),
                         '<a href="#L1">1-3</a>, <a href="#L7">7</a>')
        self.assertEqual(format_line_links([1, 3, 5], limit=2), '1, 3, and 1 more')


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
        baseline = self._lcov('baseline.lcov', [('/checkout/a.cpp', [(1, 1)])])
        empty = self._lcov('empty.lcov', [])
        with self.assertRaises(RuntimeError):
            compare_lcov_files(baseline, empty, source_root='/checkout')
        with self.assertRaises(RuntimeError):
            compare_lcov_files(empty, baseline, source_root='/checkout')


class TraceFitTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.mkdtemp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _capturing_logs(self, level='WARNING'):
        """assertLogs needs logging on, which setUp turns off for the quiet tests."""
        logging.disable(logging.NOTSET)
        return self.assertLogs('webkitpy', level=level)

    def _lcov(self, name, records):
        path = os.path.join(self._directory, name)
        with open(path, 'w') as handle:
            for filename, lines in records:
                handle.write('SF:{}\n'.format(filename))
                for number, count in lines:
                    handle.write('DA:{},{}\n'.format(number, count))
                handle.write('end_of_record\n')
        return path

    def _traces(self):
        baseline = self._lcov('baseline.lcov', [('/bot/WebKit/Source/a.cpp', [(1, 1), (2, 1)])])
        current = self._lcov('current.lcov', [('/checkout/Source/a.cpp', [(1, 1), (2, 0)])])
        return baseline, current

    def test_traces_with_no_source_path_in_common_are_an_error(self):
        baseline, current = self._traces()
        with self.assertRaises(RuntimeError) as raised:
            compare_lcov_files(baseline, current, source_root='/checkout')
        self.assertIn('no source file in common', str(raised.exception))
        self.assertIn('--baseline-source-root', str(raised.exception))

    def test_one_shared_path_is_enough_to_be_a_comparison(self):
        baseline = self._lcov('baseline.lcov', [('/checkout/Source/a.cpp', [(1, 1)]),
                                                ('/checkout/Source/gone.cpp', [(1, 1)])])
        current = self._lcov('current.lcov', [('/checkout/Source/a.cpp', [(1, 0)]),
                                              ('/checkout/Source/new.cpp', [(1, 0)])])
        delta = compare_lcov_files(baseline, current, source_root='/checkout')
        self.assertEqual(delta.overall.file_count, 3)

    def test_a_baseline_from_another_checkout_is_rebased_onto_this_one(self):
        baseline, current = self._traces()
        delta = compare_lcov_files(baseline, current, source_root='/checkout',
                                   baseline_source_root='/bot/WebKit')
        self.assertEqual(sorted(delta.file_deltas), ['/checkout/Source/a.cpp'])
        self.assertEqual(delta.file_deltas['/checkout/Source/a.cpp'].regressed_lines, [2])

    def test_a_baseline_source_root_that_matches_nothing_warns(self):
        baseline, current = self._traces()
        with self._capturing_logs() as logged:
            with self.assertRaises(RuntimeError):
                compare_lcov_files(baseline, current, source_root='/checkout',
                                   baseline_source_root='/somewhere/else')
        self.assertTrue(any('--baseline-source-root' in message for message in logged.output),
                        logged.output)

    def test_a_source_root_that_is_a_prefix_of_nothing_in_the_trace_warns(self):
        baseline = self._lcov('baseline.lcov', [('/checkout/Source/a.cpp', [(1, 1)])])
        current = self._lcov('current.lcov', [('/checkout/Source/a.cpp', [(1, 0)])])
        with self._capturing_logs() as logged:
            compare_lcov_files(baseline, current, source_root='/somewhere/else')
        self.assertTrue(any('--source-root' in message for message in logged.output),
                        logged.output)

    def test_a_matching_source_root_says_nothing(self):
        baseline = self._lcov('baseline.lcov', [('/checkout/Source/a.cpp', [(1, 1)])])
        current = self._lcov('current.lcov', [('/checkout/Source/a.cpp', [(1, 0)])])
        with self._capturing_logs(level='INFO') as logged:
            compare_lcov_files(baseline, current, source_root='/checkout')
        self.assertEqual([record.getMessage() for record in logged.records
                          if record.levelno >= logging.WARNING], [])

    def test_only_the_current_trace_is_required(self):
        current = self._lcov('current.lcov', [('/checkout/Source/a.cpp', [(1, 1)])])
        baseline_files, current_files = load_traces(current, source_root='/checkout')
        self.assertIsNone(baseline_files)
        self.assertEqual(sorted(current_files), ['/checkout/Source/a.cpp'])


class FailUnderDeltaTest(unittest.TestCase):
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
        # Not 1, which means the comparison could not be made at all.
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '10'),
                         COVERAGE_GATE_EXIT_CODE)

    def test_a_drop_within_the_allowance_passes(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '30'), 0)

    def test_a_drop_exactly_at_the_allowance_passes(self):
        # The boundary is not a failure; float noise must not break the build.
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '25'), 0)

    def test_zero_allows_no_drop_at_all_but_still_tolerates_no_change(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '0'),
                         COVERAGE_GATE_EXIT_CODE)
        self.assertEqual(self._run(self._BEFORE, self._BEFORE, '--fail-under-delta', '0'), 0)

    def test_an_improvement_never_fails(self):
        self.assertEqual(self._run(self._AFTER, self._BEFORE, '--fail-under-delta', '0'), 0)

    def test_without_the_flag_a_regression_is_reported_but_does_not_fail(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER), 0)
        self.assertIn('covered -> uncovered at 3', self.stdout.getvalue())

    def test_a_negative_allowance_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self._run(self._BEFORE, self._AFTER, '--fail-under-delta', '-5')

    def test_the_gate_follows_the_changed_file_restriction(self):
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
        changed = os.path.join(self._directory, 'changed.txt')
        with open(changed, 'w') as handle:
            handle.write('Source/never-built.cpp\n')
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--changed-files', changed,
                                   '--fail-under-delta', '0'), 1)

    def test_the_report_directory_gets_a_page_and_a_summary_but_no_browser(self):
        output = os.path.join(self._directory, 'report')
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--output-dir', output), 0)
        self.assertEqual(sorted(os.listdir(output)),
                         sorted([DELTA_REPORT_NAME, DELTA_SUMMARY_NAME]))
        with open(os.path.join(output, DELTA_SUMMARY_NAME)) as handle:
            self.assertIn('covered -> uncovered at 3', handle.read())

    def test_a_delta_into_a_report_directory_leaves_the_report_alone(self):
        output = os.path.join(self._directory, 'report')
        os.makedirs(output)
        for name in ('index.html', 'summary.txt'):
            with open(os.path.join(output, name), 'w') as handle:
                handle.write('the report\'s own ' + name)
        self.assertEqual(self._run(self._BEFORE, self._AFTER, '--output-dir', output), 0)
        for name in ('index.html', 'summary.txt'):
            with open(os.path.join(output, name)) as handle:
                self.assertEqual(handle.read(), 'the report\'s own ' + name)
        self.assertTrue(os.path.isfile(os.path.join(output, DELTA_REPORT_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(output, DELTA_SUMMARY_NAME)))

    def test_the_help_names_the_files_it_writes(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, self.assertRaises(SystemExit):
            self.script.parse_args(['--help'])
        # optparse wraps at a hyphen, as it already does to --source-root.
        help_text = re.sub(r'-\s+', '-', stdout.getvalue())
        self.assertIn(DELTA_REPORT_NAME, help_text)
        self.assertIn(DELTA_SUMMARY_NAME, help_text)

    def test_a_bad_git_ref_is_a_message_not_a_traceback(self):
        self.assertEqual(self._run(self._BEFORE, self._AFTER,
                                   '--git-diff', 'no-such-ref-exists-anywhere'), 1)

    def test_a_gate_that_cannot_be_evaluated_fails_rather_than_passing(self):
        baseline = {'Source/a.cpp': [(1, 1)]}
        current = {'Source/a.cpp': [(1, 1)], 'Source/new.cpp': [(1, 0), (2, 0)]}
        changed = os.path.join(self._directory, 'changed.txt')
        with open(changed, 'w') as handle:
            handle.write('Source/new.cpp\n')
        self.assertEqual(self._run(baseline, current, '--changed-files', changed,
                                   '--fail-under-delta', '0.5'), COVERAGE_GATE_EXIT_CODE)
        self.assertEqual(self._run(baseline, current, '--changed-files', changed), 0)

    def test_a_baseline_rooted_in_another_checkout_is_an_error_not_a_silent_pass(self):
        baseline = os.path.join(self._directory, 'baseline.lcov')
        with open(baseline, 'w') as handle:
            handle.write('SF:/bot/WebKit/Source/a.cpp\nDA:1,1\nDA:2,1\nend_of_record\n')
        current = self._lcov('current.lcov', self._AFTER)
        arguments = ['--baseline', baseline, '--current', current,
                     '--source-root', self._directory, '--fail-under-delta', '0.5']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.script.main(arguments), 1)
            self.assertEqual(self.script.main(
                arguments + ['--baseline-source-root', '/bot/WebKit']),
                COVERAGE_GATE_EXIT_CODE)

    def test_a_baseline_source_root_without_a_baseline_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.script.main(['--current', self._lcov('current.lcov', self._AFTER),
                              '--baseline-source-root', '/bot/WebKit', '--git-diff', 'HEAD'])


if __name__ == '__main__':
    unittest.main()

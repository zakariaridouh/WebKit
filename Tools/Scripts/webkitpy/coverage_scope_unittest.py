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

import json
import logging
import os
import shutil
import tempfile
import unittest

from webkitpy.coverage_scope import (
    CoverageScope, LOWER_BOUND_PREFIX, SELECTIVE_INFIX, digest_test_names, scope_from_provenance)


class TwoCasesTest(unittest.TestCase):
    """The value has two cases and no third one, and neither is a string."""

    def test_full_suite_is_not_selective(self):
        scope = CoverageScope.full_suite()
        self.assertTrue(scope.is_full_suite)
        self.assertFalse(scope.is_selective)

    def test_selective_is_not_full_suite(self):
        scope = CoverageScope.selective(['svg'])
        self.assertTrue(scope.is_selective)
        self.assertFalse(scope.is_full_suite)

    def test_the_argv_is_kept_verbatim(self):
        self.assertEqual(CoverageScope.selective(['svg', 'fast/css']).argv, ('svg', 'fast/css'))

    def test_equal_scopes_compare_equal(self):
        self.assertEqual(CoverageScope.full_suite(), CoverageScope.full_suite())
        self.assertEqual(CoverageScope.selective(['svg'], tests_run=3),
                         CoverageScope.selective(['svg'], tests_run=3))

    def test_the_two_cases_are_never_equal(self):
        self.assertNotEqual(CoverageScope.full_suite(), CoverageScope.selective(['svg']))

    def test_a_different_subset_is_a_different_scope(self):
        self.assertNotEqual(CoverageScope.selective(['svg']),
                            CoverageScope.selective(['fast/css']))


class LowerBoundRenderingTest(unittest.TestCase):
    """`41.30%` and `>= 41.30%` are different claims, and only one of them is true of a subset."""

    def test_a_full_suite_percentage_is_exact(self):
        self.assertEqual(CoverageScope.full_suite().format_percent(41.3), '41.30%')

    def test_a_selective_percentage_is_a_lower_bound(self):
        self.assertEqual(CoverageScope.selective(['svg']).format_percent(41.3),
                         LOWER_BOUND_PREFIX + '41.30%')

    def test_no_data_is_neither(self):
        self.assertEqual(CoverageScope.selective(['svg']).format_percent(None), '-')
        self.assertEqual(CoverageScope.full_suite().format_percent(None), '-')

    def test_qualifying_twice_does_not_double_the_marker(self):
        scope = CoverageScope.selective(['svg'])
        self.assertEqual(scope.qualify(scope.qualify('41.30%')), LOWER_BOUND_PREFIX + '41.30%')

    def test_a_title_says_which_it_is(self):
        self.assertEqual(CoverageScope.full_suite().qualify_title('Coverage: dom'),
                         'Coverage: dom')
        self.assertIn('lower bound',
                      CoverageScope.selective(['svg']).qualify_title('Coverage: dom'))

    def test_percentages_in_someone_elses_output_are_marked(self):
        # llvm-cov's own TOTAL line, restated. Integers must not be touched: the columns beside
        # the percentages are line counts, and those are exact.
        line = 'TOTAL   2098175   683970  67.41%   255297   71203  72.09%'
        marked = CoverageScope.selective(['svg']).qualify_percentages(line)
        self.assertIn(LOWER_BOUND_PREFIX + '67.41%', marked)
        self.assertIn(LOWER_BOUND_PREFIX + '72.09%', marked)
        self.assertIn('2098175', marked)
        self.assertNotIn(LOWER_BOUND_PREFIX + '2098175', marked)

    def test_a_full_suite_line_is_untouched(self):
        line = 'TOTAL   2098175   683970  67.41%'
        self.assertEqual(CoverageScope.full_suite().qualify_percentages(line), line)


class ArtifactNamingTest(unittest.TestCase):
    """An artifact is moved, and then its name is most of what it has to say what it is."""

    def test_a_full_suite_trace_keeps_its_name(self):
        self.assertEqual(CoverageScope.full_suite().filename('coverage.lcov.gz'),
                         'coverage.lcov.gz')

    def test_a_selective_trace_is_infixed_before_the_extensions(self):
        # Before .lcov.gz and not before .gz, so that everything which sniffs gzip by extension
        # still recognises it.
        self.assertEqual(CoverageScope.selective(['svg']).filename('coverage.lcov.gz'),
                         'coverage' + SELECTIVE_INFIX + '.lcov.gz')

    def test_a_suite_trace_is_infixed_too(self):
        self.assertEqual(CoverageScope.selective(['svg']).filename('coverage-layout.lcov.gz'),
                         'coverage-layout' + SELECTIVE_INFIX + '.lcov.gz')

    def test_infixing_is_idempotent(self):
        scope = CoverageScope.selective(['svg'])
        self.assertEqual(scope.filename(scope.filename('coverage.lcov.gz')),
                         'coverage' + SELECTIVE_INFIX + '.lcov.gz')

    def test_a_full_suite_directory_keeps_its_name(self):
        self.assertEqual(CoverageScope.full_suite().directory('/tmp/report'), '/tmp/report')

    def test_a_selective_directory_is_infixed(self):
        self.assertEqual(CoverageScope.selective(['svg']).directory('/tmp/report'),
                         '/tmp/report' + SELECTIVE_INFIX)

    def test_a_trailing_slash_does_not_produce_an_empty_component(self):
        self.assertEqual(CoverageScope.selective(['svg']).directory('/tmp/report/'),
                         '/tmp/report' + SELECTIVE_INFIX)

    def test_infixing_a_directory_is_idempotent(self):
        scope = CoverageScope.selective(['svg'])
        self.assertEqual(scope.directory(scope.directory('/tmp/report')),
                         '/tmp/report' + SELECTIVE_INFIX)


class ShortfallBannerTest(unittest.TestCase):
    """In test counts. Never as a percentage of the suite."""

    def test_a_full_suite_scope_has_no_banner(self):
        self.assertEqual(CoverageScope.full_suite().banner_lines(), [])

    def test_the_banner_names_both_counts_and_the_difference(self):
        banner = ' '.join(CoverageScope.selective(
            ['svg'], tests_run=2893, tests_in_suite=106172).banner_lines())
        self.assertIn('2,893', banner)
        self.assertIn('106,172', banner)
        self.assertIn('103,279', banner)

    def test_the_banner_carries_no_percentage_of_the_suite(self):
        # "3% of the suite ran" invites the reader to scale the coverage number by it, and the
        # relationship between the two is not linear and not knowable from here.
        banner = ' '.join(CoverageScope.selective(
            ['svg'], tests_run=2893, tests_in_suite=106172).banner_lines())
        self.assertNotIn('2.7%', banner)
        self.assertNotIn('3%', banner)

    def test_the_banner_states_the_monotonicity_it_all_rests_on(self):
        banner = ' '.join(CoverageScope.selective(['svg']).banner_lines())
        self.assertIn('covered line is exact', banner)
        self.assertIn('uncovered line is unknown', banner)

    def test_an_unmeasured_suite_size_says_so_rather_than_guessing(self):
        banner = ' '.join(CoverageScope.selective(['svg'], tests_run=10).banner_lines())
        self.assertIn('not measured', banner)

    def test_tests_not_run_is_none_when_it_cannot_be_computed(self):
        self.assertIsNone(CoverageScope.selective(['svg']).tests_not_run)
        self.assertEqual(CoverageScope.selective(['svg'], tests_run=1,
                                                 tests_in_suite=10).tests_not_run, 9)

    def test_a_run_of_more_tests_than_the_suite_does_not_go_negative(self):
        self.assertEqual(CoverageScope.selective(['svg'], tests_run=11,
                                                 tests_in_suite=10).tests_not_run, 0)

    def test_a_counted_suite_that_ran_in_full_does_not_read_as_a_full_run(self):
        # webkit-coverage --full-suite --api-tests=WTF: the layout suite ran in full and the API
        # subset is what makes it selective. "0 tests were not asked" would read as a full run.
        banner = ' '.join(CoverageScope.selective(
            ['(api) WTF'], tests_run=106172, tests_in_suite=106172,
            suite_name='layout').banner_lines())
        self.assertIn('it ran all 106,172 layout tests', banner)
        self.assertIn('(api) WTF', banner)
        self.assertNotIn('0 test(s) were not asked', banner)


class DigestTest(unittest.TestCase):
    """Without this, two subset traces compare happily and produce garbage."""

    def test_the_same_tests_digest_the_same(self):
        self.assertEqual(digest_test_names(['a.html', 'b.html']),
                         digest_test_names(['a.html', 'b.html']))

    def test_order_does_not_matter(self):
        self.assertEqual(digest_test_names(['a.html', 'b.html']),
                         digest_test_names(['b.html', 'a.html']))

    def test_duplicates_do_not_matter(self):
        self.assertEqual(digest_test_names(['a.html', 'a.html']), digest_test_names(['a.html']))

    def test_a_different_test_is_a_different_digest(self):
        self.assertNotEqual(digest_test_names(['a.html']), digest_test_names(['c.html']))

    def test_no_list_is_not_a_digest_of_nothing(self):
        # "we do not know which tests ran" and "no tests ran" are different facts, and the first
        # is what makes a comparison refuse.
        self.assertIsNone(digest_test_names(None))
        self.assertIsNotNone(digest_test_names([]))

    def test_a_scope_built_from_names_counts_and_digests_them(self):
        scope = CoverageScope.selective(['svg'], test_names=['a.html', 'b.html', 'a.html'])
        self.assertEqual(scope.tests_run, 3)
        self.assertEqual(scope.test_names_digest, digest_test_names(['a.html', 'b.html']))


class GateTest(unittest.TestCase):
    def test_a_full_suite_scope_refuses_nothing(self):
        self.assertIsNone(CoverageScope.full_suite().gate_refusal('--fail-under-lines'))

    def test_a_selective_scope_refuses_an_absolute_gate(self):
        refusal = CoverageScope.selective(['svg']).gate_refusal('--fail-under-lines')
        self.assertIn('--fail-under-lines', refusal)
        self.assertIn('lower bound', refusal)
        # And points at the gate that is sound under selection, or the reader has nothing to do.
        self.assertIn('--fail-under-patch', refusal)


class ComparisonRefusalTest(unittest.TestCase):
    """Delta coverage is not sound under selection at all."""

    def test_full_against_full_is_allowed(self):
        self.assertIsNone(CoverageScope.full_suite().comparison_refusal(
            CoverageScope.full_suite()))

    def test_selective_against_full_is_refused(self):
        refusal = CoverageScope.selective(['svg']).comparison_refusal(CoverageScope.full_suite())
        self.assertIn('fabricated regression', refusal)

    def test_full_against_selective_is_refused_too(self):
        # The other direction is just as wrong, and reads as a huge improvement rather than a
        # huge regression, which nobody investigates.
        refusal = CoverageScope.full_suite().comparison_refusal(CoverageScope.selective(['svg']))
        self.assertIn('selective', refusal)

    def test_the_refusal_names_which_side_is_selective(self):
        refusal = CoverageScope.selective(['svg']).comparison_refusal(CoverageScope.full_suite())
        self.assertIn('current trace is from a selective run', refusal)

    def test_two_selective_runs_of_the_same_tests_are_allowed(self):
        names = ['svg/a.html', 'svg/b.html']
        self.assertIsNone(
            CoverageScope.selective(['svg'], test_names=names).comparison_refusal(
                CoverageScope.selective(['svg'], test_names=names)))

    def test_two_selective_runs_of_different_tests_are_refused(self):
        refusal = CoverageScope.selective(['svg'], test_names=['svg/a.html']).comparison_refusal(
            CoverageScope.selective(['svg'], test_names=['svg/b.html']))
        self.assertIn('ran different tests', refusal)

    def test_two_selective_runs_with_no_digest_are_refused(self):
        refusal = CoverageScope.selective(['svg']).comparison_refusal(
            CoverageScope.selective(['svg']))
        self.assertIn('does not record which tests it ran', refusal)


class SerializationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='coverage-scope-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def roundtrip(self, scope):
        path = os.path.join(self.directory, 'scope.json')
        scope.write(path)
        return CoverageScope.read(path)

    def test_a_full_suite_scope_survives_a_roundtrip(self):
        self.assertEqual(self.roundtrip(CoverageScope.full_suite()), CoverageScope.full_suite())

    def test_a_selective_scope_survives_a_roundtrip(self):
        scope = CoverageScope.selective(['svg'], tests_in_suite=106172,
                                        test_names=['svg/a.html'], suite_name='layout')
        restored = self.roundtrip(scope)
        self.assertEqual(restored, scope)
        self.assertEqual(restored.tests_run, 1)
        self.assertEqual(restored.tests_in_suite, 106172)
        self.assertEqual(restored.test_names_digest, scope.test_names_digest)
        self.assertEqual(restored.suite_name, 'layout')

    def test_the_record_is_flat_json_so_it_greps_and_diffs(self):
        record = CoverageScope.selective(['svg']).to_json()
        json.dumps(record)
        self.assertEqual(sorted(record), ['argv', 'kind', 'schema', 'suite_name',
                                          'test_names_digest', 'tests_in_suite', 'tests_run'])

    def test_no_record_at_all_is_full_suite(self):
        # Which is the safe reading: it makes the tooling stricter, not looser.
        self.assertEqual(CoverageScope.from_json(None), CoverageScope.full_suite())
        self.assertEqual(CoverageScope.from_json({}), CoverageScope.full_suite())

    def test_an_unknown_kind_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            CoverageScope.from_json({'kind': 'per-shard'})

    def test_a_provenance_record_without_a_scope_is_full_suite(self):
        self.assertEqual(scope_from_provenance({'schema': 'x'}), CoverageScope.full_suite())

    def test_a_provenance_record_with_a_scope_is_read_from_it(self):
        record = {'test_scope': CoverageScope.selective(['svg'], tests_run=7).to_json()}
        self.assertEqual(scope_from_provenance(record),
                         CoverageScope.selective(['svg'], tests_run=7))


class ThresholdGateTest(unittest.TestCase):
    """--fail-under-lines on a selective trace must exit non-zero: it is not evaluable."""

    def setUp(self):
        self.totals = {'lines': (100, 90), 'functions': (10, 9), 'branches': (10, 9)}

    def check(self, scope, thresholds=None):
        from webkitpy.coverage_thresholds import check_absolute_thresholds

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        return check_absolute_thresholds(thresholds or {'lines': 60.0}, self.totals, scope=scope)

    def test_a_full_suite_trace_passes_a_threshold_it_meets(self):
        from webkitpy.coverage_thresholds import COVERAGE_GATE_EXIT_CODE

        self.assertEqual(self.check(CoverageScope.full_suite()), 0)
        self.assertEqual(self.check(CoverageScope.full_suite(), {'lines': 95.0}),
                         COVERAGE_GATE_EXIT_CODE)

    def test_a_selective_trace_fails_a_threshold_it_would_have_met(self):
        # 90% against a threshold of 60 -- and it still fails, because 90% is a lower bound over
        # a whole-tree denominator and there is no number here for a threshold to compare with.
        from webkitpy.coverage_thresholds import COVERAGE_GATE_EXIT_CODE

        self.assertEqual(self.check(CoverageScope.selective(['svg'])), COVERAGE_GATE_EXIT_CODE)

    def test_a_selective_trace_with_no_threshold_asked_for_is_not_a_failure(self):
        from webkitpy.coverage_thresholds import check_absolute_thresholds

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.assertEqual(check_absolute_thresholds(
            {}, self.totals, scope=CoverageScope.selective(['svg'])), 0)

    def test_no_scope_at_all_behaves_as_it_did_before(self):
        # Every existing caller passes no scope, and none of them should start failing.
        from webkitpy.coverage_thresholds import check_absolute_thresholds

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.assertEqual(check_absolute_thresholds({'lines': 60.0}, self.totals), 0)


class ReportRenderingTest(unittest.TestCase):
    """The >= has to reach the page, not just the value object."""

    def setUp(self):
        from webkitpy.coverage_directory_index import write_directory_index

        self.write_directory_index = write_directory_index
        self.root = tempfile.mkdtemp(prefix='coverage-scope-source-')
        self.output = tempfile.mkdtemp(prefix='coverage-scope-report-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.output, ignore_errors=True)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def index(self, scope):
        relative = os.path.join('Source', 'WebCore', 'svg', 'SVGElement.cpp')
        source = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with open(source, 'w') as handle:
            handle.write('int f();\nint g();\n')
        trace = os.path.join(self.root, 'coverage.lcov')
        with open(trace, 'w') as handle:
            handle.write('SF:{}\nFN:1,_Z1fv\nFNDA:1,_Z1fv\nDA:1,1\nDA:2,0\n'
                         'end_of_record\n'.format(source))
        self.write_directory_index(trace, self.output, source_root=self.root, scope=scope)
        with open(os.path.join(self.output, 'index.html')) as handle:
            return handle.read()

    def test_a_full_suite_page_carries_no_marker(self):
        page = self.index(CoverageScope.full_suite())
        self.assertIn('50.00%', page)
        self.assertNotIn(LOWER_BOUND_PREFIX, page)

    def test_a_selective_page_marks_its_total(self):
        page = self.index(CoverageScope.selective(['svg'], tests_run=3, tests_in_suite=100))
        self.assertIn(LOWER_BOUND_PREFIX + '50.00%', page)

    def test_a_selective_page_marks_its_title(self):
        page = self.index(CoverageScope.selective(['svg']))
        self.assertIn('<title>Coverage: All source (lower bound, selective run)</title>', page)

    def test_a_selective_page_states_the_shortfall_in_test_counts(self):
        page = self.index(CoverageScope.selective(['svg'], tests_run=3, tests_in_suite=100))
        self.assertIn('it ran 3 of the 100', page)
        self.assertIn('97 test(s) were not asked', page)

    def test_the_percentage_columns_still_sort_numerically(self):
        # The marker is in the cell's text and never in its data-v, which is what the sort reads.
        page = self.index(CoverageScope.selective(['svg']))
        self.assertIn('data-v="50.0000"', page)

    def test_a_selective_page_does_not_call_unrun_lines_uncovered(self):
        page = self.index(CoverageScope.selective(['svg']))
        self.assertIn('not executed by the tests that ran', page)
        self.assertNotIn('1 uncovered', page)


if __name__ == '__main__':
    unittest.main()

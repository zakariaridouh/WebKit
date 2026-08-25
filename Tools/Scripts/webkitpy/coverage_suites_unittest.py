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

import os
import shutil
import tempfile
import unittest

from webkitpy.coverage_lcov import FileCoverage, parse_lcov
from webkitpy.coverage_suites import (
    SuiteSpecError, check_union_equals_combined, line_totals, parse_suite_spec,
    parse_suite_specs, resolve_suite, shared_coverage_directory_warning)


class ParseSuiteSpecTest(unittest.TestCase):
    def test_a_label_and_a_path(self):
        self.assertEqual(parse_suite_spec('layout:/tmp/cov-layout'),
                         ('layout', '/tmp/cov-layout'))

    def test_the_path_may_contain_a_colon(self):
        # Split on the first colon only: a path is allowed one on this platform, and a label
        # is not, so the first colon is unambiguous.
        self.assertEqual(parse_suite_spec('api:/tmp/a:b/cov'), ('api', '/tmp/a:b/cov'))

    def test_no_colon_is_an_error_rather_than_a_derived_label(self):
        # A label derived from the basename would put "cov2" at the top of a column somebody
        # else has to read.
        with self.assertRaises(SuiteSpecError):
            parse_suite_spec('/tmp/cov-layout')

    def test_an_empty_path_is_an_error(self):
        with self.assertRaises(SuiteSpecError):
            parse_suite_spec('layout:')

    def test_a_label_with_a_slash_is_an_error(self):
        # The label becomes part of a filename as well as a column heading.
        with self.assertRaises(SuiteSpecError):
            parse_suite_spec('a/b:/tmp/cov')

    def test_labels_that_are_useful_are_allowed(self):
        for label in ('layout', 'api', 'jsc-stress', 'wk2.debug', 'v1+v2', 'run_1'):
            self.assertEqual(parse_suite_spec(label + ':/tmp/cov')[0], label)

    def test_a_repeated_label_is_an_error(self):
        with self.assertRaises(SuiteSpecError):
            parse_suite_specs(['layout:/tmp/one', 'layout:/tmp/two'])

    def test_order_is_kept_because_it_is_the_column_order(self):
        self.assertEqual(parse_suite_specs(['layout:/tmp/one', 'api:/tmp/two']),
                         [('layout', '/tmp/one'), ('api', '/tmp/two')])


class SharedCoverageDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def touch(self, name):
        with open(os.path.join(self.directory, name), 'w'):
            pass

    def test_one_runs_profiles_are_not_a_warning(self):
        # %4m gives one profile per framework per pool slot and no collisions inside a run.
        for name in ('WebCore_0.profraw', 'WebCore_1.profraw', 'WebKit_0.profraw'):
            self.touch(name)
        self.assertIsNone(shared_coverage_directory_warning(self.directory))

    def test_a_de_collided_name_means_two_runs(self):
        self.touch('WebCore_0.profraw')
        self.touch('WebCore_0-1.profraw')
        warning = shared_coverage_directory_warning(self.directory)
        self.assertIsNotNone(warning)
        self.assertIn('WebCore_0-1.profraw', warning)

    def test_a_missing_directory_is_not_a_warning(self):
        self.assertIsNone(shared_coverage_directory_warning(
            os.path.join(self.directory, 'nothing-here')))


class ResolveSuiteTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_an_indexed_profile_is_used_as_it_is(self):
        # No merge, and nothing copied: a sharded run folds its shards into one indexed
        # profile as it goes, and that profile is the suite.
        profile = os.path.join(self.directory, 'running.profdata')
        with open(profile, 'w'):
            pass
        suite = resolve_suite('layout', profile, self.directory)
        self.assertEqual((suite.name, suite.profdata, suite.raw_profiles),
                         ('layout', profile, []))

    def test_a_path_that_is_neither_is_an_error(self):
        with self.assertRaises(SuiteSpecError):
            resolve_suite('layout', os.path.join(self.directory, 'nothing-here'), self.directory)


class LineTotalsTest(unittest.TestCase):
    def coverage(self, lines):
        coverage = FileCoverage()
        coverage.lines = lines
        return coverage

    def test_instrumented_and_executed_lines_per_file(self):
        totals = line_totals({'a.cpp': self.coverage({1: 3, 2: 0, 3: 1}),
                              'b.cpp': self.coverage({1: 0})})
        self.assertEqual(totals, {'a.cpp': (3, 2), 'b.cpp': (1, 0)})

    def test_a_file_with_no_instrumented_lines_is_zero_over_zero(self):
        self.assertEqual(line_totals({'a.cpp': self.coverage({})}), {'a.cpp': (0, 0)})


class UnionCheckTest(unittest.TestCase):
    def coverage(self, lines):
        coverage = FileCoverage()
        coverage.lines = lines
        return coverage

    def test_the_union_of_two_suites_agrees_with_their_merge(self):
        # The whole claim the combined column makes. Note that it is a union and not a sum:
        # line 1 is executed by both suites and is one covered line, not two.
        combined = {'a.cpp': self.coverage({1: 5, 2: 3, 3: 2, 4: 0})}
        suites = {'one': {'a.cpp': self.coverage({1: 4, 2: 3, 3: 0, 4: 0})},
                  'two': {'a.cpp': self.coverage({1: 1, 2: 0, 3: 2, 4: 0})}}
        check = check_union_equals_combined(combined, suites)
        self.assertEqual((check.lines, check.disagreeing_lines, check.denominator_files),
                         (4, 0, 0))

    def test_a_line_covered_in_the_merge_and_in_no_suite_is_reported(self):
        combined = {'a.cpp': self.coverage({1: 1})}
        suites = {'one': {'a.cpp': self.coverage({1: 0})}}
        check = check_union_equals_combined(combined, suites)
        self.assertEqual(check.disagreeing_lines, 1)
        self.assertEqual(check.examples, [('a.cpp', 1, 1, {'one': 0})])

    def test_a_line_covered_in_a_suite_and_not_in_the_merge_is_reported(self):
        combined = {'a.cpp': self.coverage({1: 0})}
        suites = {'one': {'a.cpp': self.coverage({1: 7})}}
        self.assertEqual(check_union_equals_combined(combined, suites).disagreeing_lines, 1)

    def test_a_different_set_of_instrumented_lines_is_reported(self):
        # Which means a suite's profile does not belong to the same build as the rest, and
        # every percentage in its column is over a different denominator.
        combined = {'a.cpp': self.coverage({1: 1, 2: 0})}
        suites = {'one': {'a.cpp': self.coverage({1: 1})}}
        check = check_union_equals_combined(combined, suites)
        self.assertEqual(check.denominator_files, 1)

    def test_a_file_no_suite_mentions_is_a_denominator_difference(self):
        combined = {'a.cpp': self.coverage({1: 0})}
        self.assertEqual(
            check_union_equals_combined(combined, {'one': {}}).denominator_files, 1)

    def test_examples_are_capped(self):
        combined = {'a.cpp': self.coverage({number: 1 for number in range(1, 21)})}
        suites = {'one': {'a.cpp': self.coverage({number: 0 for number in range(1, 21)})}}
        check = check_union_equals_combined(combined, suites, max_examples=3)
        self.assertEqual(check.disagreeing_lines, 20)
        self.assertEqual(len(check.examples), 3)


class LinesOnlyParseTest(unittest.TestCase):
    TRACE = ('SF:/checkout/a.cpp\n'
             'FN:1,_Z1fv\n'
             'FNDA:2,_Z1fv\n'
             'DA:1,2\n'
             'DA:2,0\n'
             'BRDA:1,0,0,2\n'
             'BRDA:1,0,1,-\n'
             'end_of_record\n')

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.path = os.path.join(self.directory, 'trace.lcov')
        with open(self.path, 'w') as handle:
            handle.write(self.TRACE)

    def test_lines_are_the_same_either_way(self):
        # The per-suite parse has to agree with the combined one line for line, since the
        # union check compares them.
        self.assertEqual(parse_lcov(self.path, lines_only=True)['/checkout/a.cpp'].lines,
                         parse_lcov(self.path)['/checkout/a.cpp'].lines)

    def test_functions_and_branches_are_skipped(self):
        coverage = parse_lcov(self.path, lines_only=True)['/checkout/a.cpp']
        self.assertEqual(coverage.functions, {})
        self.assertEqual(coverage.branches, {})

    def test_duplicate_records_are_still_unioned(self):
        with open(self.path, 'w') as handle:
            handle.write(self.TRACE + self.TRACE.replace('DA:2,0', 'DA:2,4'))
        self.assertEqual(parse_lcov(self.path, lines_only=True)['/checkout/a.cpp'].lines,
                         {1: 2, 2: 4})


if __name__ == '__main__':
    unittest.main()

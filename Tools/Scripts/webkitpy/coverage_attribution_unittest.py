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

"""Tests for per-test coverage attribution.

llvm-profdata merge and llvm-cov export are stubbed to write a canned trace.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from webkitpy import coverage_attribution
from webkitpy.coverage_profile_environment import current_profile_environment
from webkitpy.coverage_attribution import (
    PER_TEST_INDEX_DIRECTORY_NAME, PER_TEST_RUN_PROFDATA_NAME, PROFILE_ENVIRONMENT_VARIABLES,
    PerTestCollector, PerTestIndex, PerTestIndexWriter, TestCoverage, accumulate_profdata,
    decode_lines, encode_lines, instrumented_objects,
    parse_location, per_test_profile_directory, prepare_per_test_options,
    prepare_per_test_profile_root, profile_environment, reduce_raw_profiles,
    relative_source_path, scope_includes, set_current_profile_environment,
    slug_for_test_name)


def _lcov(records):
    """An lcov trace from {path: {line: count}}, in the shape --skip-functions produces."""
    text = ''
    for path, lines in records.items():
        text += 'SF:{}\n'.format(path)
        for number, count in sorted(lines.items()):
            text += 'DA:{},{}\n'.format(number, count)
        text += 'LF:{}\nLH:{}\nend_of_record\n'.format(
            len(lines), sum(1 for count in lines.values() if count))
    return text


class _StubbedTools:
    """llvm-profdata merge and llvm-cov export, replaced by writes to disk. Records commands."""

    def __init__(self, trace, merge_returncode=0, export_returncode=0):
        self.trace = trace
        self.merge_returncode = merge_returncode
        self.export_returncode = export_returncode
        self.merge_commands = []
        self.export_commands = []

    def merge(self, output_file, unweighted_profiles=(), weighted_profiles=(),
              failure_mode=None, num_threads=None, executable=None):
        self.merge_commands.append((output_file, list(unweighted_profiles), failure_mode))
        if not self.merge_returncode:
            with open(output_file, 'wb') as handle:
                handle.write(b'indexed profile')
        return subprocess.CompletedProcess(['llvm-profdata'], self.merge_returncode, '', '')

    def run(self, command, stdout=None, stderr=None, text=None, **kwargs):
        self.export_commands.append(list(command))
        if not self.export_returncode and stdout is not None:
            stdout.write(self.trace)
        return subprocess.CompletedProcess(['llvm-cov'], self.export_returncode, '', '')

    def patches(self):
        return (mock.patch.object(coverage_attribution.LLVMProfileData, 'merge', self.merge),
                mock.patch.object(coverage_attribution.LLVMCovExecutable, 'run', self.run))


class _TemporaryDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='coverage-attribution-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.addCleanup(set_current_profile_environment, {})

    def path(self, *components):
        return os.path.join(self.directory, *components)

    def make(self, *components):
        path = self.path(*components)
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, relative, contents=''):
        path = self.path(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path


class TestSlugTest(unittest.TestCase):
    def test_a_layout_test_name_becomes_one_directory_component(self):
        self.assertNotIn('/', slug_for_test_name('fast/dom/Element/attribute.html'))

    def test_two_names_that_sanitize_alike_get_different_directories(self):
        self.assertNotEqual(slug_for_test_name('fast/dom/a-b.html'),
                            slug_for_test_name('fast/dom/a_b.html'))

    def test_it_is_stable_across_calls(self):
        # begin() and finish() each derive the directory from the name.
        self.assertEqual(slug_for_test_name('TestWTF.WTF_Vector.Reverse'),
                         slug_for_test_name('TestWTF.WTF_Vector.Reverse'))

    def test_a_very_long_name_stays_inside_a_filename(self):
        self.assertLessEqual(
            len(slug_for_test_name('imported/w3c/web-platform-tests/' + 'x' * 400)), 255)

    def test_the_readable_part_survives(self):
        self.assertTrue(slug_for_test_name('fast/dom/a.html').startswith('fast_dom_a.html-'))


class ProfileEnvironmentTest(unittest.TestCase):
    def test_both_variables_are_set(self):
        # __XPC_ copies are what libxpc forwards to the WebContent, GPU and Networking services.
        environment = profile_environment('/private/tmp/WebKitCoverage/per-test/x')
        self.assertEqual(sorted(environment), sorted(PROFILE_ENVIRONMENT_VARIABLES))
        self.assertEqual(len(set(environment.values())), 1)

    def test_the_pattern_is_one_file_per_image_in_continuous_mode(self):
        value = profile_environment('/tmp/x')['LLVM_PROFILE_FILE']
        self.assertTrue(value.endswith('%1m%c.profraw'), value)

    def test_the_directory_is_inside_the_one_the_sandbox_allows(self):
        self.assertTrue(per_test_profile_directory('fast/dom/a.html').startswith(
            '/private/tmp/WebKitCoverage/'))


class LineEncodingTest(unittest.TestCase):
    def test_contiguous_lines_become_a_range(self):
        self.assertEqual(encode_lines({1, 2, 3, 7}), '1-3,7')

    def test_an_empty_set_encodes_to_nothing(self):
        self.assertEqual(encode_lines(set()), '')

    def test_a_round_trip_preserves_the_set(self):
        lines = {1, 2, 3, 10, 11, 40, 41, 42, 43, 99}
        self.assertEqual(decode_lines(encode_lines(lines)), lines)

    def test_a_single_line_needs_no_range(self):
        self.assertEqual(encode_lines({512}), '512')
        self.assertEqual(decode_lines('512'), {512})

    def test_an_unreadable_range_costs_that_range_and_not_the_record(self):
        self.assertEqual(decode_lines('1-3,not-a-range,7'), {1, 2, 3, 7})

    def test_whitespace_and_empty_pieces_are_tolerated(self):
        self.assertEqual(decode_lines(' 1-2 , ,4 '), {1, 2, 4})


class SourcePathTest(unittest.TestCase):
    def test_a_path_under_the_checkout_is_stored_relative(self):
        self.assertEqual(relative_source_path('/checkout/Source/WTF/wtf/Vector.h', '/checkout'),
                         'Source/WTF/wtf/Vector.h')

    def test_a_path_outside_the_checkout_is_kept_as_it_is(self):
        self.assertEqual(relative_source_path('/build/WTF/Headers/wtf/Vector.h', '/checkout'),
                         '/build/WTF/Headers/wtf/Vector.h')

    def test_a_scope_prefix_matches_whole_components_only(self):
        self.assertTrue(scope_includes('Source/WTF/wtf/Vector.h', ['Source/WTF']))
        self.assertTrue(scope_includes('Source/WTF', ['Source/WTF']))
        self.assertFalse(scope_includes('Source/WTFCopy/x.cpp', ['Source/WTF']))

    def test_no_scope_at_all_includes_everything(self):
        self.assertTrue(scope_includes('Source/WebCore/dom/Document.cpp', []))


class ParseLocationTest(unittest.TestCase):
    def test_a_bare_path_means_every_line_of_it(self):
        self.assertEqual(parse_location('Source/WTF/wtf/Vector.h'),
                         ('Source/WTF/wtf/Vector.h', None, None))

    def test_a_line_is_a_range_of_one(self):
        self.assertEqual(parse_location('a/b.cpp:42'), ('a/b.cpp', 42, 42))

    def test_a_range_is_accepted(self):
        self.assertEqual(parse_location('a/b.cpp:40-44'), ('a/b.cpp', 40, 44))

    def test_a_backwards_range_is_refused(self):
        with self.assertRaises(ValueError):
            parse_location('a/b.cpp:44-40')

    def test_something_that_is_not_a_line_number_is_refused_rather_than_read_as_a_path(self):
        with self.assertRaises(ValueError):
            parse_location('a/b.cpp:middle')


class ExportCommandTest(unittest.TestCase):
    def _command(self, sources=('/checkout/Source/WTF',)):
        return coverage_attribution._export_arguments(
            ['/build/WebCore', '/build/TestWTF'], '/tmp/test.profdata', list(sources))

    def test_the_first_object_is_positional_and_the_rest_are_repeated_flags(self):
        command = self._command()
        self.assertEqual(command[1], '/build/WebCore')
        self.assertIn('-object=/build/TestWTF', command)

    def test_sources_come_last_and_as_separate_arguments(self):
        # --sources=PATH is accepted, ignored, and produces the whole report.
        command = self._command()
        self.assertEqual(command[-2:], ['--sources', '/checkout/Source/WTF'])
        self.assertNotIn('--sources=/checkout/Source/WTF', command)

    def test_function_records_are_skipped(self):
        # One FN:/FNDA: pair per template instantiation dominates a scoped export.
        command = self._command()
        for flag in ('--skip-functions', '--skip-branches', '--skip-expansions'):
            self.assertIn(flag, command)

    def test_an_empty_scope_produces_no_sources_flag(self):
        self.assertNotIn('--sources', self._command(sources=()))


class ReduceRawProfilesTest(_TemporaryDirectoryTest):
    def setUp(self):
        super().setUp()
        self.profiles = self.make('profiles')
        self.checkout = self.make('checkout')
        with open(os.path.join(self.profiles, '123_0.profraw'), 'wb') as handle:
            handle.write(b'x' * 2048)

    def _reduce(self, trace, **kwargs):
        tools = _StubbedTools(trace, **kwargs)
        merge_patch, run_patch = tools.patches()
        with merge_patch, run_patch:
            return tools, reduce_raw_profiles(
                self.profiles, ['/build/WebCore'], [os.path.join(self.checkout, 'Source/WTF')],
                self.checkout)

    def test_only_executed_lines_are_kept(self):
        _, reduced = self._reduce(_lcov({
            os.path.join(self.checkout, 'Source/WTF/wtf/Vector.h'): {10: 3, 11: 0, 12: 1},
        }))
        self.assertEqual(reduced.files, {'Source/WTF/wtf/Vector.h': {10, 12}})

    def test_a_file_with_no_executed_line_is_absent_rather_than_empty(self):
        _, reduced = self._reduce(_lcov({
            os.path.join(self.checkout, 'Source/WTF/wtf/Vector.h'): {10: 0, 11: 0},
        }))
        self.assertEqual(reduced.files, {})

    def test_the_raw_profiles_are_measured_even_when_nothing_was_executed(self):
        _, reduced = self._reduce(_lcov({}))
        self.assertEqual(reduced.raw_bytes, 2048)

    def test_a_directory_with_no_raw_profile_reduces_to_nothing_without_running_llvm_cov(self):
        shutil.rmtree(self.profiles)
        os.makedirs(self.profiles)
        tools, reduced = self._reduce(_lcov({}))
        self.assertEqual((reduced.files, reduced.raw_bytes), ({}, 0))
        self.assertEqual(tools.export_commands, [])

    def test_a_failed_merge_does_not_lose_the_measurement_of_what_it_cost(self):
        tools, reduced = self._reduce(_lcov({}), merge_returncode=1)
        self.assertEqual(reduced.files, {})
        self.assertEqual(reduced.raw_bytes, 2048)
        self.assertEqual(tools.export_commands, [])

    def test_a_failed_export_records_no_coverage_rather_than_partial_coverage(self):
        _, reduced = self._reduce(_lcov({
            os.path.join(self.checkout, 'Source/WTF/wtf/Vector.h'): {10: 3},
        }), export_returncode=1)
        self.assertEqual(reduced.files, {})

    def test_the_merge_tolerates_one_unreadable_profile_out_of_several(self):
        tools, _ = self._reduce(_lcov({}))
        self.assertEqual(tools.merge_commands[0][2], 'all')


class IndexRoundTripTest(_TemporaryDirectoryTest):
    def _write(self, tests, shard='worker/0', sources=('Source/WTF',), suite='api'):
        writer = PerTestIndexWriter(self.path('index'), shard, suite=suite, sources=sources,
                                    source_root=self.directory)
        for name, files in tests.items():
            writer.add(TestCoverage(name, files, suite=suite, sources=sources))
        writer.close()
        return writer

    def test_a_record_survives_being_written_and_read(self):
        self._write({'a': {'Source/WTF/wtf/Vector.h': {1, 2, 3, 9}}})
        index = PerTestIndex.read(self.path('index'))
        self.assertEqual(index.for_test('a').files, {'Source/WTF/wtf/Vector.h': {1, 2, 3, 9}})

    def test_the_shard_declares_the_scope_its_records_were_measured_over(self):
        self._write({'a': {}}, sources=('Source/WTF',))
        index = PerTestIndex.read(self.path('index'))
        self.assertEqual(index.for_test('a').sources, ('Source/WTF',))

    def test_a_scope_is_stored_relative_to_the_checkout(self):
        writer = PerTestIndexWriter(self.path('index'), 'worker/0',
                                    sources=[os.path.join(self.directory, 'Source/WTF')],
                                    source_root=self.directory)
        writer.close()
        with open(writer.path) as handle:
            self.assertEqual(json.loads(handle.readline())['sources'], ['Source/WTF'])

    def test_a_truncated_last_line_costs_one_test_and_not_the_index(self):
        writer = self._write({'a': {'x.cpp': {1}}, 'b': {'x.cpp': {2}}})
        with open(writer.path) as handle:
            text = handle.read()
        with open(writer.path, 'w') as handle:
            handle.write(text + '{"record": "test", "test": "c", "fil')
        index = PerTestIndex.read(self.path('index'))
        self.assertEqual([coverage.test for coverage in index.tests], ['a', 'b'])

    def test_two_shards_are_read_as_one_index(self):
        self._write({'a': {'x.cpp': {1}}}, shard='worker/0')
        self._write({'b': {'x.cpp': {2}}}, shard='worker/1')
        index = PerTestIndex.read(self.path('index'))
        self.assertEqual([coverage.test for coverage in index.tests], ['a', 'b'])

    def test_a_second_run_of_a_test_replaces_the_first_rather_than_appearing_twice(self):
        self._write({'a': {'x.cpp': {1}}}, shard='worker/0')
        self._write({'a': {'x.cpp': {5, 6}}}, shard='worker/1')
        index = PerTestIndex.read(self.path('index'))
        self.assertEqual(len(index.tests), 1)
        self.assertEqual(index.for_test('a').files, {'x.cpp': {5, 6}})

    def test_a_directory_that_is_not_an_index_is_refused(self):
        os.makedirs(self.path('empty'))
        with self.assertRaises(OSError):
            PerTestIndex.read(self.path('empty'))

    def test_the_record_is_kilobytes_and_not_megabytes(self):
        self._write({'a': {'Source/WTF/wtf/{}.h'.format(index): set(range(1, 200))
                           for index in range(30)}})
        writer_path = os.path.join(self.path('index'),
                                   slug_for_test_name('worker/0') + coverage_attribution.SHARD_SUFFIX)
        self.assertLess(os.path.getsize(writer_path), 8 * 1024)


class IndexQueryTest(unittest.TestCase):
    def setUp(self):
        self.index = PerTestIndex([
            TestCoverage('wtf-a', {'Source/WTF/wtf/Vector.h': {10, 11, 12}},
                         suite='api', sources=('Source/WTF',)),
            TestCoverage('wtf-b', {'Source/WTF/wtf/Vector.h': {10, 11},
                                   'Source/WTF/wtf/HashMap.h': {5}},
                         suite='api', sources=('Source/WTF',)),
            TestCoverage('webcore', {'Source/WebCore/dom/Document.cpp': {100}},
                         suite='layout', sources=('Source/WebCore',)),
        ], source_root='/checkout')

    def test_it_names_the_tests_that_execute_a_line(self):
        covering, measured, unmeasured = self.index.covering('Source/WTF/wtf/Vector.h', 12)
        self.assertEqual([coverage.test for coverage in covering], ['wtf-a'])
        self.assertEqual((measured, unmeasured), (2, 1))

    def test_a_line_no_test_executes_is_reported_with_the_number_that_could_have(self):
        covering, measured, unmeasured = self.index.covering('Source/WTF/wtf/Vector.h', 99)
        self.assertEqual((covering, measured, unmeasured), ([], 2, 1))

    def test_a_query_outside_every_scope_is_unanswerable_and_says_so(self):
        covering, measured, unmeasured = self.index.covering('Source/JavaScriptCore/jit/JIT.cpp')
        self.assertEqual((covering, measured, unmeasured), ([], 0, 3))

    def test_an_absolute_path_is_normalized_into_the_stored_form(self):
        self.assertEqual(self.index.normalize('/checkout/Source/WTF/wtf/Vector.h'),
                         'Source/WTF/wtf/Vector.h')

    def test_it_counts_how_many_tests_reach_each_line(self):
        self.assertEqual(self.index.line_test_counts('Source/WTF/wtf/Vector.h'),
                         {10: 2, 11: 2, 12: 1})

    def test_a_partial_name_matches_and_an_exact_name_wins(self):
        self.assertEqual(self.index.matching_tests('wtf'), ['wtf-a', 'wtf-b'])
        self.assertEqual(self.index.matching_tests('wtf-a'), ['wtf-a'])

    def test_the_statistics_count_pairs_and_singly_covered_lines(self):
        statistics = self.index.statistics()
        self.assertEqual((statistics.test_count, statistics.file_count), (3, 3))
        # Pairs: a line covered by two tests counts once.
        self.assertEqual(statistics.line_count, 5)
        self.assertEqual(statistics.singly_covered_line_count, 3)
        self.assertEqual(statistics.suites, ['api', 'layout'])
        self.assertEqual(statistics.sources, ['Source/WTF', 'Source/WebCore'])


class TestsForDiffTest(unittest.TestCase):
    def setUp(self):
        self.index = PerTestIndex([
            TestCoverage('a', {'Source/WTF/wtf/Vector.h': {10, 11}}, suite='api',
                         sources=('Source/WTF',)),
            TestCoverage('b', {'Source/WTF/wtf/Vector.h': {11, 12}}, suite='api',
                         sources=('Source/WTF',)),
        ], source_root='/checkout')

    def test_it_selects_every_test_that_reaches_any_changed_line(self):
        selection = self.index.for_added_lines({'Source/WTF/wtf/Vector.h': [10, 12]})
        self.assertEqual([coverage.test for coverage in selection.tests], ['a', 'b'])
        self.assertEqual(selection.covered_lines, {'Source/WTF/wtf/Vector.h': [10, 12]})

    def test_a_changed_line_nothing_reaches_is_reported_separately(self):
        selection = self.index.for_added_lines({'Source/WTF/wtf/Vector.h': [10, 500]})
        self.assertEqual(selection.uncovered_lines, {'Source/WTF/wtf/Vector.h': [500]})

    def test_a_changed_file_outside_the_scope_is_neither_covered_nor_uncovered(self):
        selection = self.index.for_added_lines({'Source/WebCore/dom/Document.cpp': [1, 2]})
        self.assertEqual(selection.out_of_scope, ['Source/WebCore/dom/Document.cpp'])
        self.assertEqual((selection.covered_lines, selection.uncovered_lines), ({}, {}))

    def test_a_file_inside_the_scope_that_no_test_has_a_line_of_is_named(self):
        selection = self.index.for_added_lines({'Source/WTF/wtf/New.cpp': [1]})
        self.assertEqual(selection.unknown_files, ['Source/WTF/wtf/New.cpp'])

    def test_an_absolute_path_from_a_diff_is_matched_against_the_relative_index(self):
        selection = self.index.for_added_lines({'/checkout/Source/WTF/wtf/Vector.h': [10]})
        self.assertEqual([coverage.test for coverage in selection.tests], ['a'])


class RedundantTestsTest(unittest.TestCase):
    def test_a_test_whose_lines_another_test_also_covers_is_redundant(self):
        index = PerTestIndex([
            TestCoverage('wide', {'x.cpp': {1, 2, 3}}, sources=('.',)),
            TestCoverage('narrow', {'x.cpp': {1, 2}}, sources=('.',)),
        ])
        self.assertEqual([coverage.test for coverage in index.redundant_tests()], ['narrow'])

    def test_a_test_with_one_line_of_its_own_is_not_redundant(self):
        index = PerTestIndex([
            TestCoverage('a', {'x.cpp': {1, 2, 3}}, sources=('.',)),
            TestCoverage('b', {'x.cpp': {3, 4}}, sources=('.',)),
        ])
        self.assertEqual(index.redundant_tests(), [])

    def test_two_identical_tests_are_both_redundant(self):
        index = PerTestIndex([
            TestCoverage('a', {'x.cpp': {1}}, sources=('.',)),
            TestCoverage('b', {'x.cpp': {1}}, sources=('.',)),
        ])
        self.assertEqual(len(index.redundant_tests()), 2)

    def test_the_answer_is_about_the_rest_of_the_index_and_not_about_a_single_other_test(self):
        # {1,2} is covered by {2} and {1} together; each of the three is redundant on its own.
        tests = [TestCoverage('a', {'x.cpp': {1, 2}}, sources=('.',)),
                 TestCoverage('b', {'x.cpp': {2}}, sources=('.',)),
                 TestCoverage('c', {'x.cpp': {1}}, sources=('.',))]
        forwards = [coverage.test for coverage in PerTestIndex(tests).redundant_tests()]
        backwards = [coverage.test for coverage in
                     PerTestIndex(list(reversed(tests))).redundant_tests()]
        self.assertEqual(forwards, backwards)
        self.assertEqual(forwards, ['a', 'b', 'c'])

    def test_a_test_that_covered_nothing_is_not_called_redundant(self):
        index = PerTestIndex([
            TestCoverage('a', {'x.cpp': {1}}, sources=('.',)),
            TestCoverage('empty', {}, sources=('.',)),
        ])
        self.assertEqual(index.redundant_tests(), [])


class CollectorTest(_TemporaryDirectoryTest):
    def _collector(self, **kwargs):
        arguments = dict(
            index_directory=self.path('index'), shard='worker/0', objects=['/build/WebCore'],
            sources=[os.path.join(self.directory, 'Source/WTF')], source_root=self.directory,
            suite='api', profile_root=self.path('per-test'))
        arguments.update(kwargs)
        collector = PerTestCollector(**arguments)
        self.addCleanup(collector.close)
        return collector

    def _raw_profile(self, collector, test):
        directory = per_test_profile_directory(test, collector.profile_root)
        with open(os.path.join(directory, '99_0.profraw'), 'wb') as handle:
            handle.write(b'x' * 1024)

    def test_begin_creates_the_directory_and_publishes_the_environment(self):
        # Published as well as returned: the layout-test driver builds its own environment.
        collector = self._collector()
        environment = collector.begin('a/b.html')
        self.assertTrue(os.path.isdir(per_test_profile_directory('a/b.html',
                                                                 collector.profile_root)))
        self.assertEqual(current_profile_environment(), environment)

    def test_begin_empties_a_directory_that_is_already_there(self):
        collector = self._collector()
        collector.begin('a/b.html')
        self._raw_profile(collector, 'a/b.html')
        collector.begin('a/b.html')
        self.assertEqual(os.listdir(per_test_profile_directory('a/b.html',
                                                               collector.profile_root)), [])

    def test_finish_records_the_test_and_deletes_the_raw_profiles(self):
        tools = _StubbedTools(_lcov({
            os.path.join(self.directory, 'Source/WTF/wtf/Vector.h'): {10: 1, 11: 0},
        }))
        collector = self._collector()
        collector.begin('a/b.html')
        self._raw_profile(collector, 'a/b.html')
        merge_patch, run_patch = tools.patches()
        with merge_patch, run_patch:
            coverage = collector.finish('a/b.html')
        self.assertEqual(coverage.files, {'Source/WTF/wtf/Vector.h': {10}})
        self.assertFalse(os.path.exists(per_test_profile_directory('a/b.html',
                                                                   collector.profile_root)))

    def test_finish_clears_the_published_environment(self):
        collector = self._collector()
        collector.begin('a/b.html')
        with mock.patch.object(coverage_attribution, 'reduce_raw_profiles',
                               return_value=coverage_attribution.ReducedProfile({}, 0, 0, 0.0)):
            collector.finish('a/b.html')
        self.assertEqual(current_profile_environment(), {})

    def test_a_test_that_wrote_no_profile_is_not_recorded_at_all(self):
        collector = self._collector()
        collector.begin('a/b.html')
        tools = _StubbedTools(_lcov({}))
        merge_patch, run_patch = tools.patches()
        with merge_patch, run_patch:
            self.assertIsNone(collector.finish('a/b.html'))
        collector.close()
        self.assertEqual(PerTestIndex.read(self.path('index')).tests, [])

    def test_the_directory_is_deleted_even_when_the_reduction_raises(self):
        collector = self._collector()
        collector.begin('a/b.html')
        self._raw_profile(collector, 'a/b.html')
        with mock.patch.object(coverage_attribution, 'reduce_raw_profiles',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                collector.finish('a/b.html')
        self.assertFalse(os.path.exists(per_test_profile_directory('a/b.html',
                                                                   collector.profile_root)))

    def test_a_shard_file_that_cannot_be_written_costs_the_record_and_not_the_run(self):
        tools = _StubbedTools(_lcov({
            os.path.join(self.directory, 'Source/WTF/wtf/Vector.h'): {10: 1},
        }))
        collector = self._collector()
        collector.begin('a/b.html')
        self._raw_profile(collector, 'a/b.html')
        merge_patch, run_patch = tools.patches()
        with merge_patch, run_patch, mock.patch.object(
                collector.writer, 'add', side_effect=OSError('No space left on device')):
            coverage = collector.finish('a/b.html')
        self.assertEqual(coverage.files, {'Source/WTF/wtf/Vector.h': {10}})

    def test_the_export_names_the_staged_headers_and_the_record_names_only_the_scope(self):
        staged = self.make('build/WTF/Headers/wtf')
        tools = _StubbedTools(_lcov({}))
        collector = self._collector(staged_sources=[staged])
        collector.begin('a/b.html')
        self._raw_profile(collector, 'a/b.html')
        merge_patch, run_patch = tools.patches()
        with merge_patch, run_patch:
            collector.finish('a/b.html')
        self.assertIn(staged, tools.export_commands[0])
        with open(collector.writer.path) as handle:
            self.assertEqual(json.loads(handle.readline())['sources'], ['Source/WTF'])

    def test_the_test_binary_can_be_added_to_the_object_list_for_one_test(self):
        tools = _StubbedTools(_lcov({}))
        collector = self._collector()
        collector.begin('a')
        self._raw_profile(collector, 'a')
        merge_patch, run_patch = tools.patches()
        with merge_patch, run_patch:
            collector.finish('a', extra_objects=['/build/TestWTF'])
        self.assertIn('-object=/build/TestWTF', tools.export_commands[0])
        self.assertEqual(collector.objects, ['/build/WebCore'])


class AccumulatedProfileTest(_TemporaryDirectoryTest):
    def test_the_first_test_creates_the_accumulated_profile(self):
        tools = _StubbedTools('')
        addition = self.write('test.profdata', 'x')
        with tools.patches()[0]:
            accumulate_profdata(self.path('run.profdata'), addition)
        self.assertTrue(os.path.exists(self.path('run.profdata')))
        self.assertEqual(tools.merge_commands[0][1], [addition])

    def test_later_tests_merge_into_it(self):
        tools = _StubbedTools('')
        addition = self.write('test.profdata', 'x')
        self.write('run.profdata', 'old')
        with tools.patches()[0]:
            accumulate_profdata(self.path('run.profdata'), addition)
        self.assertEqual(tools.merge_commands[0][1], [self.path('run.profdata'), addition])

    def test_a_failed_merge_leaves_the_previous_profile_intact(self):
        tools = _StubbedTools('', merge_returncode=1)
        addition = self.write('test.profdata', 'x')
        self.write('run.profdata', 'old')
        with tools.patches()[0]:
            accumulate_profdata(self.path('run.profdata'), addition)
        with open(self.path('run.profdata')) as handle:
            self.assertEqual(handle.read(), 'old')
        self.assertFalse(os.path.exists(self.path('run.profdata.partial')))


class PrepareProfileRootTest(_TemporaryDirectoryTest):
    def test_an_abandoned_per_test_directory_is_removed_and_measured(self):
        root = self.make('per-test')
        os.makedirs(os.path.join(root, 'a-1234'))
        with open(os.path.join(root, 'a-1234', '1_0.profraw'), 'wb') as handle:
            handle.write(b'x' * 4096)
        stale = prepare_per_test_profile_root(root)
        self.assertEqual((stale.count, stale.total_bytes), (1, 4096))
        self.assertEqual(os.listdir(root), [])

    def test_a_missing_root_is_not_an_error(self):
        self.assertEqual(prepare_per_test_profile_root(self.path('nothing')).count, 0)

    def test_a_file_beside_the_directories_is_left_alone(self):
        root = self.make('per-test')
        with open(os.path.join(root, 'note.txt'), 'w') as handle:
            handle.write('x')
        prepare_per_test_profile_root(root)
        self.assertEqual(os.listdir(root), ['note.txt'])


class _Options:
    def __init__(self, **kwargs):
        self.coverage = True
        self.coverage_dir = None
        self.per_test_coverage = True
        self.per_test_coverage_sources = []
        self.per_test_coverage_index = None
        self.per_test_coverage_profdata = None
        for name, value in kwargs.items():
            setattr(self, name, value)


class PrepareOptionsTest(_TemporaryDirectoryTest):
    def setUp(self):
        super().setUp()
        self.checkout = self.directory
        self.make('Source/WTF')

    def _options(self, **kwargs):
        arguments = dict(coverage_dir=self.path('cov'),
                         per_test_coverage_sources=['Source/WTF'])
        arguments.update(kwargs)
        return _Options(**arguments)

    def test_it_refuses_without_coverage(self):
        with self.assertRaises(ValueError) as raised:
            prepare_per_test_options(self._options(coverage=False), self.checkout)
        self.assertIn('needs --coverage', str(raised.exception))

    def test_it_refuses_without_a_scope(self):
        with self.assertRaises(ValueError) as raised:
            prepare_per_test_options(self._options(per_test_coverage_sources=[]), self.checkout)
        self.assertIn('--per-test-coverage-sources', str(raised.exception))

    def test_it_refuses_a_scope_that_does_not_exist(self):
        with self.assertRaises(ValueError) as raised:
            prepare_per_test_options(self._options(per_test_coverage_sources=['Source/Nope']),
                                     self.checkout)
        self.assertIn('does not exist', str(raised.exception))

    def test_a_relative_scope_becomes_absolute(self):
        options = prepare_per_test_options(self._options(), self.checkout)
        self.assertEqual(options.per_test_coverage_sources,
                         [os.path.join(self.checkout, 'Source/WTF')])

    def test_the_index_and_the_run_profile_default_under_the_coverage_directory(self):
        options = prepare_per_test_options(self._options(), self.checkout)
        self.assertEqual(options.per_test_coverage_index,
                         os.path.join(self.path('cov'), PER_TEST_INDEX_DIRECTORY_NAME))
        self.assertEqual(options.per_test_coverage_profdata,
                         os.path.join(self.path('cov'), PER_TEST_RUN_PROFDATA_NAME))
        self.assertTrue(os.path.isdir(options.per_test_coverage_index))

    def test_the_run_profile_can_be_turned_off(self):
        options = prepare_per_test_options(
            self._options(per_test_coverage_profdata=False), self.checkout)
        self.assertIsNone(options.per_test_coverage_profdata)


class InstrumentedObjectsTest(_TemporaryDirectoryTest):
    def setUp(self):
        super().setUp()
        for name in ('JavaScriptCore', 'WebCore', 'WebKit', 'WebKitLegacy', 'WebGPU'):
            self.write('{}.framework/Versions/A/{}'.format(name, name), 'mach-o')

    def test_all_five_frameworks_by_default(self):
        self.assertEqual(len(instrumented_objects(self.directory)), 5)

    def test_products_restricts_them_by_name(self):
        objects = instrumented_objects(self.directory, products=['WebCore'])
        self.assertEqual([os.path.basename(path) for path in objects], ['WebCore'])

    def test_an_empty_product_list_selects_none_of_them(self):
        self.assertEqual(instrumented_objects(self.directory, products=[]), [])

    def test_extra_objects_are_appended_and_not_duplicated(self):
        objects = instrumented_objects(
            self.directory, extra_objects=['/build/TestWTF', '/build/TestWTF'], products=[])
        self.assertEqual(objects, ['/build/TestWTF'])


def _read(*components):
    scripts = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(scripts, *components)) as handle:
        return handle.read()


class HarnessWiringTest(unittest.TestCase):
    """The harness plumbing, asserted against the source: the harnesses cannot be run from here."""

    def test_the_driver_applies_the_profile_environment_last(self):
        # Anything after it could overwrite LLVM_PROFILE_FILE.
        source = _read('webkitpy', 'port', 'driver.py')
        body = source[source.index('def _setup_environ_for_driver'):]
        body = body[:body.index('def _setup_environ_for_test')]
        self.assertIn('environment.update(current_profile_environment())', body)
        self.assertLess(body.index('current_profile_environment()'), body.rindex('return'))

    def test_the_layout_worker_restarts_the_driver_around_every_test(self):
        # A live driver keeps writing its counter file: kill it before begin() and finish().
        source = _read('webkitpy', 'layout_tests', 'controllers', 'layout_test_runner.py')
        body = source[source.index('    def run_test(self, test_input, shard_name):'):]
        body = body[:body.index('    def _do_post_tests_work')]
        self.assertLess(body.index('_kill_driver'), body.index('_coverage_collector.begin'))
        self.assertLess(body.index('_coverage_collector.begin'),
                        body.index('_coverage_collector.finish'))
        self.assertLess(body.rindex('_kill_driver'), body.index('_coverage_collector.finish'))

    def test_the_api_worker_puts_the_environment_into_the_process_it_starts(self):
        # setup_environ_for_server() copies an allow-list from os.environ; exporting does nothing.
        source = _read('webkitpy', 'api_tests', 'runner.py')
        body = source[source.index('    def _run_single_test(self, binary_name, test):'):]
        self.assertIn('environment.update(self._coverage_collector.begin(full_test_name))', body)
        self.assertLess(body.index('_coverage_collector.begin'), body.index('ServerProcess('))
        self.assertLess(body.index('server_process.stop()'),
                        body.index('_coverage_collector.finish'))

    def test_neither_harness_lets_the_summary_discard_the_runs_result(self):
        # A raise inside finally would replace the run's result.
        for path in (('run-api-tests',),
                     ('webkitpy', 'layout_tests', 'run_webkit_tests.py')):
            source = _read(*path)
            call = source.index('summarize_per_test_run(')
            preceding = source[:call]
            self.assertIn('try:', preceding[preceding.rindex('finally:'):], path)

    def test_both_harnesses_check_the_options_before_the_run_starts(self):
        for path in (('run-api-tests',),
                     ('webkitpy', 'layout_tests', 'run_webkit_tests.py')):
            source = _read(*path)
            self.assertLess(source.index('prepare_per_test_options('),
                            source.index('prepare_per_test_profile_root('), path)

    def test_the_api_harness_forces_one_process_per_test(self):
        # The profile path is read once per process, so a batched shard would be one test's.
        source = _read('run-api-tests')
        self.assertIn('options.run_singly = True', source)


if __name__ == '__main__':
    unittest.main()

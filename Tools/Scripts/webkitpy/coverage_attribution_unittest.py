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

"""Tests for per-test coverage attribution.

The one thing that cannot be tested here is a real .profraw: writing one by hand is not the
same kind of exercise as llvm_profile_utils_unittest's Mach-O fixture, because the raw profile
format has no compatibility guarantees and a hand-built one would be testing this checkout's
llvm-profdata rather than this code. What is exercised instead is everything on either side of
those two subprocesses -- the command lines they are given, the trace they hand back, the
directory they write into, and the index that comes out -- with the two calls replaced by stubs
that write a canned lcov trace. The subprocesses themselves were verified by running the real
thing twice -- six TestWTF tests through run-api-tests --per-test-coverage and one layout test
through run-webkit-tests --per-test-coverage -- which is where the measurements quoted here and
throughout coverage_attribution.py come from.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from webkitpy import coverage_attribution
from webkitpy.coverage_attribution import (
    PER_TEST_INDEX_DIRECTORY_NAME, PER_TEST_RUN_PROFDATA_NAME, PROFILE_ENVIRONMENT_VARIABLES,
    PerTestCollector, PerTestIndex, PerTestIndexWriter, TestCoverage, accumulate_profdata,
    current_profile_environment, decode_lines, encode_lines, instrumented_objects,
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
    """llvm-profdata merge and llvm-cov export, replaced by writes to disk.

    The merge writes a plausible indexed profile so that the accumulation step has something to
    read, and the export writes whatever trace the test asked for. Both record their command
    lines, because the command line is the part of this that has been wrong: --sources=PATH
    instead of --sources PATH is accepted, ignored, and silently produces the whole tree.
    """

    def __init__(self, trace, merge_returncode=0, export_returncode=0):
        self.trace = trace
        self.merge_returncode = merge_returncode
        self.export_returncode = export_returncode
        self.merge_commands = []
        self.export_commands = []

    def merge(self, output_file, unweighted_profiles=(), weighted_profiles=(),
              failure_mode=None, num_threads=None):
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
        # The name is a path, so it cannot be used as a directory name as it stands.
        self.assertNotIn('/', slug_for_test_name('fast/dom/Element/attribute.html'))

    def test_two_names_that_sanitize_alike_get_different_directories(self):
        # This is the whole reason for the digest. Replacing every unsafe character with '_'
        # maps these two onto one directory, and one test's counters would then be attributed
        # to the other -- silently, and in whichever order they ran.
        self.assertNotEqual(slug_for_test_name('fast/dom/a-b.html'),
                            slug_for_test_name('fast/dom/a_b.html'))

    def test_it_is_stable_across_calls(self):
        # begin() and finish() each derive the directory from the name rather than passing it
        # between them, so an unstable slug would reduce an empty directory.
        self.assertEqual(slug_for_test_name('TestWTF.WTF_Vector.Reverse'),
                         slug_for_test_name('TestWTF.WTF_Vector.Reverse'))

    def test_a_very_long_name_stays_inside_a_filename(self):
        # HFS+ and APFS both stop at 255 bytes, and a WPT test name can be longer than that.
        self.assertLessEqual(
            len(slug_for_test_name('imported/w3c/web-platform-tests/' + 'x' * 400)), 255)

    def test_the_readable_part_survives(self):
        # A directory listing during a run is how somebody sees which test is being measured.
        self.assertTrue(slug_for_test_name('fast/dom/a.html').startswith('fast_dom_a.html-'))


class ProfileEnvironmentTest(unittest.TestCase):
    def test_both_variables_are_set(self):
        # LLVM_PROFILE_FILE covers the process itself; __XPC_LLVM_PROFILE_FILE is what libxpc
        # forwards to the WebContent, GPU and Networking services. Without the second one a
        # layout test's record is the UI process's coverage and nothing else.
        environment = profile_environment('/private/tmp/WebKitCoverage/per-test/x')
        self.assertEqual(sorted(environment), sorted(PROFILE_ENVIRONMENT_VARIABLES))
        self.assertEqual(len(set(environment.values())), 1)

    def test_the_pattern_is_one_file_per_image_in_continuous_mode(self):
        # %1m and not the baked-in %8m: one pool slot is 142 MB across the five frameworks, and
        # a per-test directory holds a whole slot per image for the length of one test. %c is
        # continuous mode, which the counters-section rename in a coverage build requires.
        value = profile_environment('/tmp/x')['LLVM_PROFILE_FILE']
        self.assertTrue(value.endswith('%1m%c.profraw'), value)

    def test_the_directory_is_inside_the_one_the_sandbox_allows(self):
        # `(allow file-write* (subpath "/private/tmp/WebKitCoverage"))` in the WebContent, GPU
        # and Networking profiles is the only reason LLVM_PROFILE_FILE can be redirected at all.
        # A per-test directory anywhere else has its writes denied for those processes, and the
        # counters vanish with no diagnostic of any kind.
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
        # A shard file is appended to per test and read back later; one bad piece must not throw
        # away the lines around it.
        self.assertEqual(decode_lines('1-3,not-a-range,7'), {1, 2, 3, 7})

    def test_whitespace_and_empty_pieces_are_tolerated(self):
        self.assertEqual(decode_lines(' 1-2 , ,4 '), {1, 2, 4})


class SourcePathTest(unittest.TestCase):
    def test_a_path_under_the_checkout_is_stored_relative(self):
        self.assertEqual(relative_source_path('/checkout/Source/WTF/wtf/Vector.h', '/checkout'),
                         'Source/WTF/wtf/Vector.h')

    def test_a_path_outside_the_checkout_is_kept_as_it_is(self):
        # A copied header coverage_lcov could not place is still real coverage and still belongs
        # to the test that executed it.
        self.assertEqual(relative_source_path('/build/WTF/Headers/wtf/Vector.h', '/checkout'),
                         '/build/WTF/Headers/wtf/Vector.h')

    def test_a_scope_prefix_matches_whole_components_only(self):
        # Source/WTF must not match Source/WTFCopy, or a query about the second reads as
        # measured when it was not.
        self.assertTrue(scope_includes('Source/WTF/wtf/Vector.h', ['Source/WTF']))
        self.assertTrue(scope_includes('Source/WTF', ['Source/WTF']))
        self.assertFalse(scope_includes('Source/WTFCopy/x.cpp', ['Source/WTF']))

    def test_no_scope_at_all_includes_everything(self):
        # An index recorded without a declared scope has to answer from what it holds, rather
        # than answer "out of scope" for every query.
        self.assertTrue(scope_includes('Source/WebCore/dom/Document.cpp', []))


class ParseLocationTest(unittest.TestCase):
    def test_a_bare_path_means_every_line_of_it(self):
        self.assertEqual(parse_location('Source/WTF/wtf/Vector.h'),
                         ('Source/WTF/wtf/Vector.h', None, None))

    def test_a_line_is_a_range_of_one(self):
        self.assertEqual(parse_location('a/b.cpp:42'), ('a/b.cpp', 42, 42))

    def test_a_range_is_accepted(self):
        # "What covers the function I just changed" is a question about a run of lines, and
        # asking it twenty times is not the same tool.
        self.assertEqual(parse_location('a/b.cpp:40-44'), ('a/b.cpp', 40, 44))

    def test_a_backwards_range_is_refused(self):
        with self.assertRaises(ValueError):
            parse_location('a/b.cpp:44-40')

    def test_something_that_is_not_a_line_number_is_refused_rather_than_read_as_a_path(self):
        # Silently treating it as a path would answer a question nobody asked.
        with self.assertRaises(ValueError):
            parse_location('a/b.cpp:middle')


class ExportCommandTest(unittest.TestCase):
    """The llvm-cov command line, which is where the silent failures are."""

    def _command(self, sources=('/checkout/Source/WTF',)):
        return coverage_attribution._export_arguments(
            ['/build/WebCore', '/build/TestWTF'], '/tmp/test.profdata', list(sources))

    def test_the_first_object_is_positional_and_the_rest_are_repeated_flags(self):
        command = self._command()
        self.assertEqual(command[1], '/build/WebCore')
        self.assertIn('-object=/build/TestWTF', command)

    def test_sources_come_last_and_as_separate_arguments(self):
        # Verified against the current Apple LLVM in llvm_profile_utils: --sources=PATH is
        # accepted, silently ignored, and produces the whole report.
        command = self._command()
        self.assertEqual(command[-2:], ['--sources', '/checkout/Source/WTF'])
        self.assertNotIn('--sources=/checkout/Source/WTF', command)

    def test_function_records_are_skipped(self):
        # Not an optimization. lcov emits one FN:/FNDA: pair per template instantiation:
        # measured, --sources Source/WTF/wtf/Vector.h against JavaScriptCore alone is 52.8 MB of
        # which 52.3 MB is mangled names, and the whole of Source/WTF is 1.69 MB with function
        # records and 488 KB without.
        command = self._command()
        for flag in ('--skip-functions', '--skip-branches', '--skip-expansions'):
            self.assertIn(flag, command)

    def test_an_empty_scope_produces_no_sources_flag(self):
        # llvm-cov's response to --sources with nothing after it is not scoping to nothing.
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
        # An unexecuted instrumented line is what the whole-run report answers exactly, and
        # storing it would nearly double the artifact: 21,509 covered lines against 37,891
        # instrumented ones in the measured Source/WTF scope.
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
        # The cost is the point of the measurement: it is what a run reports having paid.
        _, reduced = self._reduce(_lcov({}))
        self.assertEqual(reduced.raw_bytes, 2048)

    def test_a_directory_with_no_raw_profile_reduces_to_nothing_without_running_llvm_cov(self):
        # What a skipped test, or one whose driver never started, looks like. Recording it as
        # covering nothing would make it look like a test that runs and touches no code.
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
        # --failure-mode=all for the same reason the whole-run merge uses it: the harness
        # hard-kills drivers, so one unreadable profile must not lose the test.
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
        # Per shard and not per index: two runs can append to one index with different scopes,
        # and without this a query cannot tell "this test does not execute that line" from
        # "that file was never in this test's export".
        self._write({'a': {}}, sources=('Source/WTF',))
        index = PerTestIndex.read(self.path('index'))
        self.assertEqual(index.for_test('a').sources, ('Source/WTF',))

    def test_a_scope_is_stored_relative_to_the_checkout(self):
        # The harness resolves --per-test-coverage-sources to absolute paths for llvm-cov; an
        # index that recorded those could not be compared with a diff's paths.
        writer = PerTestIndexWriter(self.path('index'), 'worker/0',
                                    sources=[os.path.join(self.directory, 'Source/WTF')],
                                    source_root=self.directory)
        writer.close()
        with open(writer.path) as handle:
            self.assertEqual(json.loads(handle.readline())['sources'], ['Source/WTF'])

    def test_a_truncated_last_line_costs_one_test_and_not_the_index(self):
        # A run measured per test is normally killed rather than finished, so this is the
        # ordinary way a shard file ends. Appending per test exists to keep the rest.
        writer = self._write({'a': {'x.cpp': {1}}, 'b': {'x.cpp': {2}}})
        with open(writer.path) as handle:
            text = handle.read()
        with open(writer.path, 'w') as handle:
            handle.write(text + '{"record": "test", "test": "c", "fil')
        index = PerTestIndex.read(self.path('index'))
        self.assertEqual([coverage.test for coverage in index.tests], ['a', 'b'])

    def test_two_shards_are_read_as_one_index(self):
        # One file per worker, because two processes appending JSON lines longer than PIPE_BUF
        # to one descriptor can interleave a line.
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
        # The premise of the whole design. A per-test profdata is around 200 MB; the measured
        # six-test index for a Source/WTF scope is 15,024 bytes, about 2.4 KB per test.
        self._write({'a': {'Source/WTF/wtf/{}.h'.format(index): set(range(1, 200))
                           for index in range(30)}})
        writer_path = os.path.join(self.path('index'),
                                   slug_for_test_name('worker/0') + coverage_attribution.SHARD_SUFFIX)
        self.assertLess(os.path.getsize(writer_path), 8 * 1024)


class IndexQueryTest(unittest.TestCase):
    def setUp(self):
        # Two tests measured over Source/WTF and one over Source/WebCore, so that the
        # scope-awareness of every query is exercised rather than assumed.
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
        # 2 of 3 measured and nothing covering is evidence about the line; 0 of 3 measured is
        # not evidence about anything, and the two must not print the same way.
        covering, measured, unmeasured = self.index.covering('Source/WTF/wtf/Vector.h', 99)
        self.assertEqual((covering, measured, unmeasured), ([], 2, 1))

    def test_a_query_outside_every_scope_is_unanswerable_and_says_so(self):
        covering, measured, unmeasured = self.index.covering('Source/JavaScriptCore/jit/JIT.cpp')
        self.assertEqual((covering, measured, unmeasured), ([], 0, 3))

    def test_an_absolute_path_is_normalized_into_the_stored_form(self):
        self.assertEqual(self.index.normalize('/checkout/Source/WTF/wtf/Vector.h'),
                         'Source/WTF/wtf/Vector.h')

    def test_it_counts_how_many_tests_reach_each_line(self):
        # The singly-covered lines are the finding: "this line is covered, by one test, and that
        # test is flaky" is invisible in a merged profile.
        self.assertEqual(self.index.line_test_counts('Source/WTF/wtf/Vector.h'),
                         {10: 2, 11: 2, 12: 1})

    def test_a_partial_name_matches_and_an_exact_name_wins(self):
        self.assertEqual(self.index.matching_tests('wtf'), ['wtf-a', 'wtf-b'])
        self.assertEqual(self.index.matching_tests('wtf-a'), ['wtf-a'])

    def test_the_statistics_count_pairs_and_singly_covered_lines(self):
        statistics = self.index.statistics()
        self.assertEqual((statistics.test_count, statistics.file_count), (3, 3))
        # Pairs, not the sum of the tests' line counts: Vector.h:10 and :11 are each covered
        # twice and counted once. 3 + 1 + 1.
        self.assertEqual(statistics.line_count, 5)
        # Vector.h:12, HashMap.h:5 and Document.cpp:100.
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
        # Which is where a new test would go, and is the more useful half of the answer.
        selection = self.index.for_added_lines({'Source/WTF/wtf/Vector.h': [10, 500]})
        self.assertEqual(selection.uncovered_lines, {'Source/WTF/wtf/Vector.h': [500]})

    def test_a_changed_file_outside_the_scope_is_neither_covered_nor_uncovered(self):
        # Counting it as uncovered would claim the change is untested when the truth is that
        # this index never looked; it is the same distinction as the report's "not built here".
        selection = self.index.for_added_lines({'Source/WebCore/dom/Document.cpp': [1, 2]})
        self.assertEqual(selection.out_of_scope, ['Source/WebCore/dom/Document.cpp'])
        self.assertEqual((selection.covered_lines, selection.uncovered_lines), ({}, {}))

    def test_a_file_inside_the_scope_that_no_test_has_a_line_of_is_named(self):
        # For a brand-new file this usually means nothing instrumented compiled it, which is a
        # different thing from a file whose lines are simply not reached.
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
        # Which is the correct answer to the question asked -- each of them is covered by the
        # other -- and is why the output says dropping ANY ONE cannot reduce the coverage.
        index = PerTestIndex([
            TestCoverage('a', {'x.cpp': {1}}, sources=('.',)),
            TestCoverage('b', {'x.cpp': {1}}, sources=('.',)),
        ])
        self.assertEqual(len(index.redundant_tests()), 2)

    def test_the_answer_is_about_the_rest_of_the_index_and_not_about_a_single_other_test(self):
        # {1,2} is covered by {2} and {1} together and by neither alone, and all three are
        # listed: dropping any one of them keeps both lines, and dropping all three loses both.
        # A greedy minimal-cover answer would instead name a different set depending on the
        # order it walked; this question is exact, so the order cannot matter.
        tests = [TestCoverage('a', {'x.cpp': {1, 2}}, sources=('.',)),
                 TestCoverage('b', {'x.cpp': {2}}, sources=('.',)),
                 TestCoverage('c', {'x.cpp': {1}}, sources=('.',))]
        forwards = [coverage.test for coverage in PerTestIndex(tests).redundant_tests()]
        backwards = [coverage.test for coverage in
                     PerTestIndex(list(reversed(tests))).redundant_tests()]
        self.assertEqual(forwards, backwards)
        self.assertEqual(forwards, ['a', 'b', 'c'])

    def test_a_test_that_covered_nothing_is_not_called_redundant(self):
        # It is a test whose profile held nothing in the scope, which is a different finding.
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
        # Published as well as returned, because the layout-test driver assembles its own
        # environment out of reach of the worker that knows which test is next.
        collector = self._collector()
        environment = collector.begin('a/b.html')
        self.assertTrue(os.path.isdir(per_test_profile_directory('a/b.html',
                                                                 collector.profile_root)))
        self.assertEqual(current_profile_environment(), environment)

    def test_begin_empties_a_directory_that_is_already_there(self):
        # --repeat-each, --iterations and a retry after a failure all run one test twice, and
        # continuous mode would merge the second run into the first run's preallocated file.
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
        # The 142 MB that must not still be there when the next test starts.
        self.assertFalse(os.path.exists(per_test_profile_directory('a/b.html',
                                                                   collector.profile_root)))

    def test_finish_clears_the_published_environment(self):
        # Otherwise a driver started between tests -- for a retry, or by the leak checker --
        # would write into the directory of a test that has already been reduced and deleted.
        collector = self._collector()
        collector.begin('a/b.html')
        with mock.patch.object(coverage_attribution, 'reduce_raw_profiles',
                               return_value=coverage_attribution.ReducedProfile({}, 0, 0, 0.0)):
            collector.finish('a/b.html')
        self.assertEqual(current_profile_environment(), {})

    def test_a_test_that_wrote_no_profile_is_not_recorded_at_all(self):
        # A skipped test, or one whose driver never started. Recording it as covering nothing
        # would make it look like a test that runs and touches no code, which is a finding.
        collector = self._collector()
        collector.begin('a/b.html')
        tools = _StubbedTools(_lcov({}))
        merge_patch, run_patch = tools.patches()
        with merge_patch, run_patch:
            self.assertIsNone(collector.finish('a/b.html'))
        collector.close()
        # The shard file exists -- it has a header -- and holds no test record.
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
        # The last I/O operation that can fail at runtime -- a full disk, hours into a layout
        # run. The raw profiles are gone by then either way, so failing the run buys nothing.
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
        # Measured: without the staged copy in the export, a Source/WTF scope records not one
        # line of wtf/Vector.h, because the mapping records the path the translation unit
        # included -- <build>/WTF/Headers/wtf/Vector.h -- and llvm-cov's --sources filter runs
        # against that, long before the canonicalization that maps it back. With the frameworks
        # as objects that path has 1,159 instrumented lines, 1,085 of them covered.
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
        # An API-test shard adds the binary it is running: a statically linked WTF function
        # instantiated only by TestWTF has its coverage mapping there and nowhere else.
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
        # llvm-profdata reads the file it is writing here, so writing in place would leave a
        # half-written profile on top of everything the run had accumulated.
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
        # prepare_coverage_profile_directory() unlinks *.profraw at the top level and does not
        # recurse, so ten abandoned layout tests is 1.4 GB nothing else will clean up.
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
        # Only directories are per-test profile directories. Deleting anything else in the
        # machine-global tree is not this function's business.
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
        # --coverage is what takes the machine-global profile-directory lock and proves
        # --coverage-dir can be written to.
        with self.assertRaises(ValueError) as raised:
            prepare_per_test_options(self._options(coverage=False), self.checkout)
        self.assertIn('needs --coverage', str(raised.exception))

    def test_it_refuses_without_a_scope(self):
        # Not a default. An unscoped export is 23 s and 751 MB per test, so a per-test run
        # without a scope is not a slow run, it is one that will not finish.
        with self.assertRaises(ValueError) as raised:
            prepare_per_test_options(self._options(per_test_coverage_sources=[]), self.checkout)
        self.assertIn('--per-test-coverage-sources', str(raised.exception))

    def test_it_refuses_a_scope_that_does_not_exist(self):
        # llvm-cov's response to a --sources filter that matches nothing is not an error, so
        # this would be a full-length run whose every record is empty.
        with self.assertRaises(ValueError) as raised:
            prepare_per_test_options(self._options(per_test_coverage_sources=['Source/Nope']),
                                     self.checkout)
        self.assertIn('does not exist', str(raised.exception))

    def test_a_relative_scope_becomes_absolute(self):
        # llvm-cov resolves a relative --sources against its own working directory, so a
        # relative scope works only when the tool happens to have been run from the checkout.
        options = prepare_per_test_options(self._options(), self.checkout)
        self.assertEqual(options.per_test_coverage_sources,
                         [os.path.join(self.checkout, 'Source/WTF')])

    def test_the_index_and_the_run_profile_default_under_the_coverage_directory(self):
        # So that one --coverage-dir describes one run completely and there is no second path
        # to remember.
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
        # The per-test cost knob: 8.3 s against the five frameworks, 0.48 s against one binary,
        # almost all of it spent loading coverage mappings rather than on the scope.
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
    """The harness plumbing, asserted against the source.

    The same approach as api_tests/coverage_collection_unittest.py, and for the same reason: the
    harnesses cannot be run from here, and each of these shapes is both easy to break and
    silent when broken.
    """

    def test_the_driver_applies_the_profile_environment_last(self):
        # Anything after it could overwrite LLVM_PROFILE_FILE, and the profile runtime reads the
        # variable once, at process start, so a lost value is a test measured against the
        # previous test's directory.
        source = _read('webkitpy', 'port', 'driver.py')
        body = source[source.index('def _setup_environ_for_driver'):]
        body = body[:body.index('def _setup_environ_for_test')]
        self.assertIn('environment.update(current_profile_environment())', body)
        self.assertLess(body.index('current_profile_environment()'), body.rindex('return'))

    def test_the_layout_worker_restarts_the_driver_around_every_test(self):
        # Before the test, because a driver that is already running is already writing into the
        # previous test's file; after it, because continuous mode keeps the counter file mmapped
        # for the life of the process and indexing it while any process is alive reads a file
        # that is still being written.
        source = _read('webkitpy', 'layout_tests', 'controllers', 'layout_test_runner.py')
        body = source[source.index('    def run_test(self, test_input, shard_name):'):]
        body = body[:body.index('    def _do_post_tests_work')]
        self.assertLess(body.index('_kill_driver'), body.index('_coverage_collector.begin'))
        self.assertLess(body.index('_coverage_collector.begin'),
                        body.index('_coverage_collector.finish'))
        self.assertLess(body.rindex('_kill_driver'), body.index('_coverage_collector.finish'))

    def test_the_api_worker_puts_the_environment_into_the_process_it_starts(self):
        # setup_environ_for_server() copies a fixed allow-list out of os.environ and
        # LLVM_PROFILE_FILE is not on it, so exporting it would do nothing at all.
        source = _read('webkitpy', 'api_tests', 'runner.py')
        body = source[source.index('    def _run_single_test(self, binary_name, test):'):]
        self.assertIn('environment.update(self._coverage_collector.begin(full_test_name))', body)
        self.assertLess(body.index('_coverage_collector.begin'), body.index('ServerProcess('))
        self.assertLess(body.index('server_process.stop()'),
                        body.index('_coverage_collector.finish'))

    def test_neither_harness_lets_the_summary_discard_the_runs_result(self):
        # A raise inside a finally block replaces whatever the try block produced, which is how
        # a coverage failure once turned a passing run into exit 254 with a traceback and no
        # test results at all.
        for path in (('run-api-tests',),
                     ('webkitpy', 'layout_tests', 'run_webkit_tests.py')):
            source = _read(*path)
            call = source.index('summarize_per_test_run(')
            preceding = source[:call]
            self.assertIn('try:', preceding[preceding.rindex('finally:'):], path)

    def test_both_harnesses_check_the_options_before_the_run_starts(self):
        # A per-test run deletes each test's raw profiles as it goes, so a scope that names
        # nothing produces a full-length run with nothing left to re-reduce.
        for path in (('run-api-tests',),
                     ('webkitpy', 'layout_tests', 'run_webkit_tests.py')):
            source = _read(*path)
            self.assertLess(source.index('prepare_per_test_options('),
                            source.index('prepare_per_test_profile_root('), path)

    def test_the_api_harness_forces_one_process_per_test(self):
        # A batched shard runs several tests in one process, and the profile path is read once
        # at process start, so every test in such a shard would be attributed the whole shard's
        # counters under the first test's name.
        source = _read('run-api-tests')
        self.assertIn('options.run_singly = True', source)


if __name__ == '__main__':
    unittest.main()

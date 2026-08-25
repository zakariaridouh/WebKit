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

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from webkitpy.coverage_provenance import (
    PROVENANCE_FILENAME, SCHEMA, TRACE_COMMENT_PREFIX, add_trace_measurements, checkout_state,
    count_lcov_records, dirty_digest, dirty_files, object_states, provenance_record,
    read_trace_comment, summary_lines, trace_comment, write_provenance)


class _TemporaryDirectory(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def write(self, relative, contents):
        path = os.path.join(self.directory, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path


class _TemporaryCheckout(_TemporaryDirectory):
    """A real one-commit git repository, because the point is what git actually reports.

    Mocking git here would test the parsing of a string this test wrote, which is the half that
    does not break. What breaks is the shape of --porcelain output and whether a rename, an
    untracked file or a nested repository comes back as something digestible.
    """

    def setUp(self):
        super().setUp()
        if not shutil.which('git'):
            self.skipTest('no git')
        self.git('init', '-q', '-b', 'coverage-test')
        self.write('Source/WebCore/dom/Node.cpp', 'int main() { return 0; }\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'first')
        self.revision = self.git('rev-parse', 'HEAD').strip()

    def git(self, *arguments):
        environment = dict(os.environ,
                           GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.com',
                           GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.com')
        return subprocess.run(['git', '-c', 'commit.gpgsign=false', *arguments],
                              cwd=self.directory, check=True, text=True, capture_output=True,
                              env=environment).stdout


class CheckoutStateTest(_TemporaryCheckout):
    def test_a_clean_tree_records_its_revision_and_no_dirty_files(self):
        state = checkout_state(self.directory)
        self.assertEqual(state['source_revision'], self.revision)
        self.assertEqual(state['source_branch'], 'coverage-test')
        self.assertEqual(state['source_root'], self.directory)
        self.assertEqual(state['source_dirty_file_count'], 0)
        self.assertEqual(state['source_dirty_files'], [])
        # Present and well defined rather than null, so a clean report is comparable against
        # another clean report by the digest alone.
        self.assertTrue(state['source_dirty_digest'].startswith('sha256:'))

    def test_a_modified_file_is_recorded_with_its_own_digest(self):
        self.write('Source/WebCore/dom/Node.cpp', 'int main() { return 1; }\n')
        state = checkout_state(self.directory)
        self.assertEqual(state['source_dirty_file_count'], 1)
        entry = state['source_dirty_files'][0]
        self.assertEqual(entry['path'], 'Source/WebCore/dom/Node.cpp')
        self.assertEqual(entry['status'], 'M')
        self.assertTrue(entry['digest'].startswith('sha256:'))
        self.assertEqual(entry['size_bytes'], 25)

    def test_an_untracked_file_counts_as_dirty(self):
        # An untracked .cpp gets compiled like any other, so a report over it is not reproducible
        # from the revision alone.
        self.write('Source/WebCore/dom/New.cpp', 'int f() { return 0; }\n')
        state = checkout_state(self.directory)
        self.assertEqual([entry['status'] for entry in state['source_dirty_files']], ['??'])

    def test_editing_a_file_changes_the_digest_and_editing_it_back_restores_it(self):
        original = checkout_state(self.directory)['source_dirty_digest']
        self.write('Source/WebCore/dom/Node.cpp', 'int main() { return 1; }\n')
        edited = checkout_state(self.directory)['source_dirty_digest']
        self.assertNotEqual(original, edited)
        self.write('Source/WebCore/dom/Node.cpp', 'int main() { return 0; }\n')
        self.assertEqual(checkout_state(self.directory)['source_dirty_digest'], original)

    def test_two_files_with_swapped_contents_do_not_collide(self):
        # A digest over contents alone would be identical for both trees, and the pair of files
        # is exactly the case where a line view renders against the wrong text.
        self.write('a.cpp', 'AAA\n')
        self.write('b.cpp', 'BBB\n')
        first = checkout_state(self.directory)['source_dirty_digest']
        self.write('a.cpp', 'BBB\n')
        self.write('b.cpp', 'AAA\n')
        self.assertNotEqual(checkout_state(self.directory)['source_dirty_digest'], first)

    def test_a_directory_entry_is_recorded_without_a_digest_rather_than_walked(self):
        # A nested repository or submodule comes back from git as one entry ending in '/'.
        os.makedirs(os.path.join(self.directory, 'nested'))
        self.git('-C', 'nested', 'init', '-q')
        self.write('nested/thing.cpp', 'int f();\n')
        entries = [entry for entry in dirty_files(self.directory)
                   if entry['path'].startswith('nested')]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]['digest'])

    def test_a_directory_that_is_not_a_checkout_answers_null_and_not_zero(self):
        # "git could not tell us" and "nothing is dirty" are different facts. A 0 here would be
        # read as a reproducible report.
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        state = checkout_state(outside)
        self.assertIsNone(state['source_revision'])
        self.assertIsNone(state['source_dirty_file_count'])
        self.assertIsNone(state['source_dirty_digest'])


class DirtyDigestTest(unittest.TestCase):
    ENTRIES = [{'path': 'a.cpp', 'status': 'M', 'digest': 'sha256:aa', 'size_bytes': 1,
                'modified_at': 'x'},
               {'path': 'b.cpp', 'status': '??', 'digest': 'sha256:bb', 'size_bytes': 2,
                'modified_at': 'y'}]

    def test_the_order_of_the_list_does_not_matter_but_the_contents_do(self):
        self.assertEqual(dirty_digest(self.ENTRIES), dirty_digest(self.ENTRIES))
        self.assertNotEqual(dirty_digest(self.ENTRIES), dirty_digest(self.ENTRIES[:1]))

    def test_a_status_change_alone_changes_the_digest(self):
        staged = [dict(self.ENTRIES[0], status='A'), self.ENTRIES[1]]
        self.assertNotEqual(dirty_digest(staged), dirty_digest(self.ENTRIES))

    def test_a_file_too_large_to_hash_contributes_its_size_and_mtime(self):
        unhashed = [dict(self.ENTRIES[0], digest=None)]
        self.assertNotEqual(dirty_digest(unhashed),
                            dirty_digest([dict(unhashed[0], size_bytes=999)]))

    def test_no_dirty_files_is_a_well_defined_digest_rather_than_an_error(self):
        self.assertTrue(dirty_digest([]).startswith('sha256:'))
        self.assertEqual(dirty_digest([]), dirty_digest(None))


class ObjectStatesTest(_TemporaryDirectory):
    def test_each_object_records_what_it_says_about_its_own_instrumentation(self):
        from webkitpy.llvm_profile_utils_unittest import _mach_o
        path = os.path.join(self.directory, 'WebKit')
        with open(path, 'wb') as handle:
            handle.write(_mach_o('/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw'))
        state = object_states([path])[0]
        self.assertEqual(state['path'], path)
        self.assertTrue(state['instrumented'])
        self.assertEqual(state['profile_filename'],
                         '/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw')
        self.assertEqual(state['size_bytes'], os.path.getsize(path))
        # The mtime is the other half of "is the source newer than the binaries", which
        # --check-binary-ids cannot answer.
        self.assertTrue(state['modified_at'].endswith('Z'))

    def test_a_missing_object_is_recorded_as_unknown_rather_than_raising(self):
        state = object_states([os.path.join(self.directory, 'gone')])[0]
        self.assertIsNone(state['size_bytes'])
        self.assertIsNone(state['instrumented'])


class RecordTest(_TemporaryDirectory):
    def record(self, **overrides):
        arguments = dict(checkout_root=self.directory,
                         build_directory=os.path.join(self.directory, 'build'),
                         port_name='mac-test-wk2', configuration='Release', objects=[],
                         profile_path=os.path.join(self.directory, 'coverage.profdata'),
                         command_line=['--release'], products=None, suites=[],
                         raw_profile_count=20, unreadable_raw_profile_count=0,
                         ignore_filename_regexes=['Source/ThirdParty/'], sources_scope=[],
                         generator='generate-coverage-report')
        arguments.update(overrides)
        return provenance_record(**arguments)

    def test_every_documented_field_is_present(self):
        # The record is the contract; a field that quietly stops being written is a field a
        # consumer starts guessing at.
        record = self.record()
        for field in ('schema', 'generated_at', 'generator', 'command_line', 'source_root',
                      'source_revision', 'source_branch', 'source_dirty_file_count',
                      'source_dirty_digest', 'source_dirty_files',
                      'source_dirty_files_truncated', 'port', 'configuration',
                      'build_directory', 'products', 'objects', 'suites', 'profile_path',
                      'profile_size_bytes', 'raw_profile_count',
                      'unreadable_raw_profile_count', 'ignore_filename_regexes',
                      'sources_scope', 'include_third_party', 'include_test_support', 'tools',
                      'trace_path', 'trace_size_bytes', 'trace_record_count'):
            self.assertIn(field, record)
        self.assertEqual(record['schema'], SCHEMA)

    def test_the_exclusion_set_is_recorded_in_the_order_it_was_applied(self):
        # Two traces built with different exclusions compare as file deletions, so this is the
        # field that makes a comparison refusable.
        record = self.record(ignore_filename_regexes=['/DerivedSources/', 'Source/ThirdParty/'])
        self.assertEqual(record['ignore_filename_regexes'],
                         ['/DerivedSources/', 'Source/ThirdParty/'])

    def test_the_toolchain_records_every_candidate_and_not_only_the_chosen_one(self):
        # The runner rotates through candidates on failure, so a report can have come from a
        # different binary than 'path' names; hiding the list would make the record confident
        # about something it does not know.
        tools = self.record()['tools']
        for name in ('llvm-cov', 'llvm-profdata'):
            self.assertIn('candidates', tools[name])
            self.assertIsInstance(tools[name]['candidates'], list)

    def test_the_suite_names_and_sources_are_recorded(self):
        record = self.record(suites=[('layout', '/tmp/cov-layout'), ('api', '/tmp/cov-api')])
        self.assertEqual(record['suites'], [{'name': 'layout', 'source': '/tmp/cov-layout'},
                                            {'name': 'api', 'source': '/tmp/cov-api'}])

    def test_writing_and_reading_the_record_round_trips(self):
        path = write_provenance(self.record(), self.directory)
        self.assertEqual(os.path.basename(path), PROVENANCE_FILENAME)
        with open(path) as handle:
            self.assertEqual(json.load(handle)['schema'], SCHEMA)

    def test_summary_lines_say_when_the_report_is_only_part_of_the_tree(self):
        scoped = summary_lines(self.record(sources_scope=['/checkout/Source/WebCore/dom']))
        self.assertTrue(any('NOT the project total' in line for line in scoped))
        self.assertFalse(any('NOT the project total' in line
                             for line in summary_lines(self.record())))


class TraceCommentTest(_TemporaryDirectory):
    RECORD = {'schema': SCHEMA, 'source_revision': 'a' * 40, 'source_dirty_digest': 'sha256:bb',
              'source_dirty_files': [{'path': 'a.cpp'}], 'source_dirty_files_truncated': False,
              'ignore_filename_regexes': ['Source/ThirdParty/'], 'trace_record_count': 42,
              'trace_size_bytes': 99, 'trace_path': 'coverage.lcov.gz'}

    def test_the_comment_carries_what_decides_whether_two_traces_are_comparable(self):
        carried = json.loads(trace_comment(self.RECORD)[len(TRACE_COMMENT_PREFIX):])
        self.assertEqual(carried['source_revision'], 'a' * 40)
        self.assertEqual(carried['source_dirty_digest'], 'sha256:bb')
        self.assertEqual(carried['ignore_filename_regexes'], ['Source/ThirdParty/'])

    def test_the_comment_omits_what_cannot_exist_yet_and_what_is_unbounded(self):
        # The trace has to be written before anything in it can be counted, and the per-file
        # dirty list has no bound.
        carried = json.loads(trace_comment(self.RECORD)[len(TRACE_COMMENT_PREFIX):])
        for field in ('trace_record_count', 'trace_size_bytes', 'source_dirty_files'):
            self.assertNotIn(field, carried)

    def test_it_is_a_single_line(self):
        self.assertEqual(trace_comment(self.RECORD).count('\n'), 1)
        self.assertTrue(trace_comment(self.RECORD).endswith('\n'))

    def _trace(self, name, header, compress=False):
        path = os.path.join(self.directory, name)
        body = 'SF:/checkout/a.cpp\nDA:1,1\nend_of_record\nSF:/checkout/b.cpp\nDA:1,0\nend_of_record\n'
        opener = gzip.open if compress else open
        with opener(path, 'wt') as handle:
            handle.write((header or '') + body)
        return path

    def test_a_trace_describes_itself_and_a_gzipped_one_does_too(self):
        for compress in (False, True):
            path = self._trace('t{}.lcov'.format(int(compress)),
                               trace_comment(self.RECORD), compress=compress)
            self.assertEqual(read_trace_comment(path)['source_revision'], 'a' * 40)

    def test_a_trace_with_no_provenance_reads_as_none_rather_than_raising(self):
        self.assertIsNone(read_trace_comment(self._trace('bare.lcov', None)))

    def test_the_comment_does_not_disturb_the_parser(self):
        from webkitpy.coverage_lcov import parse_lcov, parse_lcov_source_files
        annotated = self._trace('annotated.lcov', trace_comment(self.RECORD))
        self.assertEqual(sorted(parse_lcov(annotated)), ['/checkout/a.cpp', '/checkout/b.cpp'])
        self.assertEqual(parse_lcov_source_files(annotated),
                         {'/checkout/a.cpp', '/checkout/b.cpp'})

    def test_records_are_counted_and_the_comment_is_not_one(self):
        self.assertEqual(count_lcov_records(self._trace('c.lcov', trace_comment(self.RECORD))), 2)

    def test_the_traces_own_measurements_are_added_once_it_exists(self):
        path = self._trace('m.lcov', trace_comment(self.RECORD))
        record = add_trace_measurements(dict(self.RECORD, trace_record_count=None), path)
        self.assertEqual(record['trace_record_count'], 2)
        self.assertEqual(record['trace_size_bytes'], os.path.getsize(path))
        self.assertEqual(record['trace_path'], 'm.lcov')


if __name__ == '__main__':
    unittest.main()

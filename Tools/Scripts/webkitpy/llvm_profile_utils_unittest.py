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

import fcntl
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from webkitpy import llvm_profile_utils
from webkitpy.coverage_requirements import UNREPORTED_PROFILE_WRITERS
from webkitpy.llvm_profile_utils import (
    COVERAGE_PROFILE_DIRECTORY, CoverageProfileDirectoryInUse, LLVMCov, LLVMCovExecutable,
    acquire_coverage_profile_directory_lock, collect_coverage_profiles,
    collected_profiles_with_no_object, coverage_profile_directory_holder,
    coverage_profile_lock_path, objects_with_no_profile_data, partition_unclaimed_profiles,
    prepare_coverage_profile_directory, profile_name_prefix, read_instrumentation,
    release_coverage_profile_directory_lock, survey_instrumentation,
    unreadable_profiles_from_stderr)


def _mach_o(profile_filename=None, instrumented=True, sections=(), fat=False,
            preceded_by_stab=False, preceded_by_undefined=False, duplicate_name_string=False):
    """A 64-bit Mach-O with one segment, an optional __llvm_prf_cnts, and a symbol table.

    Small enough to write by hand and real enough that the reader has to be right: the
    instrumentation reader has to walk load commands, find a symbol by name in the string
    table, map its address through a section, and read a NUL-terminated string. There is no
    other way to test that against an unbaked framework without building one.

    preceded_by_stab puts a GSYM debug stab with the same n_strx and n_value 0 ahead of the
    real symbol, which is what an unstripped framework actually contains.

    preceded_by_undefined puts an N_UNDF entry with the same name ahead of it, whose n_value
    is a size rather than an address. duplicate_name_string gives the string table a second
    copy of the name and points the real entry at the second copy, which is what a partly
    deduplicated string table looks like.
    """
    LC_SEGMENT_64, LC_SYMTAB = 0x19, 0x02
    names = list(sections)
    if instrumented:
        names.append('__llvm_prf_cnts')
    names.append('__data')

    data = b'' if profile_filename is None else profile_filename.encode() + b'\0'
    # A binary with no such symbol at all, which is also what a stripped one looks like.
    name = b'___llvm_profile_filename\0' if profile_filename is not None else b'_some_other_symbol\0'
    strings = b'\0' + (name * 2 if duplicate_name_string else name)
    real_strx = 1 + len(name) if duplicate_name_string else 1
    # Layout: header, load commands, then the sections' contents, the symbol table and the
    # string table, in that order. Addresses are arbitrary; only addr-to-offset must agree.
    header_size = 32
    commands_size = (72 + 80 * len(names)) + 24
    data_offset = header_size + commands_size
    symbol_offset = data_offset + len(data)
    number_of_symbols = 1 + int(preceded_by_stab) + int(preceded_by_undefined)
    string_offset = symbol_offset + 16 * number_of_symbols
    base_address = 0x100000000

    section_commands = b''
    address = base_address
    for name in names:
        is_data = name == '__data'
        section_commands += (
            name.encode().ljust(16, b'\0') + b'__DATA'.ljust(16, b'\0')
            + struct.pack('<QQ', address, len(data) if is_data else 8)
            + struct.pack('<IIIIIIII', data_offset if is_data else header_size,
                          0, 0, 0, 0, 0, 0, 0))
        if is_data:
            data_address = address
        address += 0x1000

    segment = (struct.pack('<II', LC_SEGMENT_64, 72 + len(section_commands))
               + b'__DATA'.ljust(16, b'\0')
               + struct.pack('<QQQQ', base_address, 0x10000, 0, 0)
               + struct.pack('<IIII', 7, 3, len(names), 0)
               + section_commands)
    symtab = struct.pack('<IIIIII', LC_SYMTAB, 24, symbol_offset, number_of_symbols,
                         string_offset, len(strings))
    commands = segment + symtab

    symbol_table = b''
    if preceded_by_stab:
        symbol_table += struct.pack('<IBBHQ', real_strx, 0x20, 0, 0, 0)
    if preceded_by_undefined:
        # N_UNDF | N_EXT. n_value is a requested size for a common symbol, never an address.
        symbol_table += struct.pack('<IBBHQ', real_strx, 0x01, 0, 0, 0x4000)
    symbol_table += struct.pack('<IBBHQ', real_strx, 0x0E, 1, 0,
                                data_address if profile_filename is not None else 0)

    slice_bytes = (
        b'\xcf\xfa\xed\xfe'
        + struct.pack('<IIIIIII', 0x0100000C, 0, 6, 2, len(commands), 0, 0)
        + commands + data
        + symbol_table
        + strings)
    if not fat:
        return slice_bytes
    offset = 4096
    return (b'\xca\xfe\xba\xbe' + struct.pack('>I', 1)
            + struct.pack('>IIIII', 0x0100000C, 0, offset, len(slice_bytes), 14)
            + b'\0' * (offset - 28) + slice_bytes)


class _MachOFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def write(self, name, contents):
        path = os.path.join(self.directory, name)
        with open(path, 'wb') as handle:
            handle.write(contents)
        return path


class MachOInstrumentationTest(_MachOFixture):
    def test_reads_a_baked_in_profile_filename(self):
        path = self.write('WebKit', _mach_o('/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw'))
        self.assertEqual(read_instrumentation(path),
                         (True, '/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw'))

    def test_an_empty_profile_filename_is_read_as_empty_and_not_as_absent(self):
        # The compiler-rt runtime defines the symbol weakly as an empty string, so this is what
        # a framework whose project never defined ENABLE_LLVM_COVERAGE actually looks like.
        path = self.write('WebGPU', _mach_o(''))
        self.assertEqual(read_instrumentation(path), (True, ''))

    def test_a_binary_with_no_such_symbol_reads_as_cannot_tell(self):
        # Also what a stripped binary looks like, so it must not read as broken.
        path = self.write('Stripped', _mach_o(None))
        self.assertEqual(read_instrumentation(path), (True, None))

    def test_a_debug_stab_for_the_same_symbol_does_not_win(self):
        # Every unstripped framework carries a GSYM stab for ___llvm_profile_filename with the
        # same n_strx and n_value 0. Taking the first matching entry reported "cannot tell" for
        # WebGPU, WebKit and WebKitLegacy -- three of the five instrumented frameworks -- and
        # made the collected-but-unclaimed guard warn about exactly what it exists to check.
        path = self.write('WebGPU', _mach_o('/private/tmp/WebKitCoverage/WebGPU_%4m%c.profraw',
                                            preceded_by_stab=True))
        self.assertEqual(read_instrumentation(path),
                         (True, '/private/tmp/WebKitCoverage/WebGPU_%4m%c.profraw'))

    def test_an_undefined_entry_for_the_same_name_is_not_an_address(self):
        # N_UNDF is not a stab, and its n_value is a size. Only N_SECT entries have addresses.
        path = self.write('WebKit', _mach_o('/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw',
                                            preceded_by_undefined=True))
        self.assertEqual(read_instrumentation(path),
                         (True, '/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw'))

    def test_a_second_copy_of_the_name_does_not_hide_the_definition(self):
        # Mach-O string tables are not fully deduplicated -- 280 names in WebGPU alone occupy
        # more than one index -- so matching a single index is not the same as matching a name.
        path = self.write('WebKit', _mach_o('/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw',
                                            duplicate_name_string=True))
        self.assertEqual(read_instrumentation(path),
                         (True, '/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw'))

    def test_all_three_confounders_at_once(self):
        path = self.write('WebGPU', _mach_o('/private/tmp/WebKitCoverage/WebGPU_%4m%c.profraw',
                                            preceded_by_stab=True, preceded_by_undefined=True,
                                            duplicate_name_string=True))
        self.assertEqual(read_instrumentation(path),
                         (True, '/private/tmp/WebKitCoverage/WebGPU_%4m%c.profraw'))

    def test_an_uninstrumented_binary_has_no_counters_section(self):
        path = self.write('Plain', _mach_o('/private/tmp/WebKitCoverage/x.profraw',
                                           instrumented=False))
        self.assertFalse(read_instrumentation(path).instrumented)

    def test_the_counters_section_is_found_whatever_its_segment_is_called(self):
        # Coverage builds rename the segment to __MMAP_DATA for continuous mode.
        path = self.write('WebKit', _mach_o('/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw',
                                            sections=('__llvm_prf_data', '__llvm_prf_names')))
        self.assertTrue(read_instrumentation(path).instrumented)

    def test_a_fat_binary_is_read_through_its_first_architecture(self):
        path = self.write('Fat', _mach_o('/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw',
                                         fat=True))
        self.assertEqual(read_instrumentation(path),
                         (True, '/private/tmp/WebKitCoverage/WebKit_%4m%c.profraw'))

    def test_something_that_is_not_a_mach_o_reads_as_uninstrumented(self):
        path = self.write('script', b'#!/bin/sh\necho hello\n')
        self.assertEqual(read_instrumentation(path), (False, None))

    def test_profile_name_prefix_stops_at_the_first_pattern(self):
        self.assertEqual(profile_name_prefix('/private/tmp/WebKitCoverage/WebGPU_%4m%c.profraw'),
                         'WebGPU_')
        self.assertEqual(profile_name_prefix('/tmp/plain.profraw'), 'plain.profraw')


class ObjectsWithNoProfileDataTest(_MachOFixture):
    GOOD = COVERAGE_PROFILE_DIRECTORY + '/WebKit_%4m%c.profraw'

    def test_an_instrumented_binary_with_no_baked_in_path_is_reported(self):
        broken = self.write('WebGPU', _mach_o(''))
        findings = objects_with_no_profile_data([self.write('WebKit', _mach_o(self.GOOD)), broken])
        self.assertEqual([path for path, _ in findings], [broken])
        self.assertIn('ENABLE_LLVM_COVERAGE', findings[0][1])

    def test_a_path_outside_the_collection_directory_is_reported(self):
        # Not collected, and denied outright for a sandboxed WebContent process.
        stray = self.write('Stray', _mach_o('/tmp/somewhere-else/Stray_%m.profraw'))
        findings = objects_with_no_profile_data([stray])
        self.assertEqual([path for path, _ in findings], [stray])
        self.assertIn('outside', findings[0][1])

    def test_a_binary_whose_profile_was_never_collected_is_reported(self):
        path = self.write('WebKit', _mach_o(self.GOOD))
        findings = objects_with_no_profile_data([path], ['/tmp/cov/WebCore_1234.profraw'])
        self.assertEqual([path for path, _ in findings], [path])
        self.assertIn('WebKit_', findings[0][1])

    def test_a_binary_whose_profile_was_collected_is_not_reported(self):
        path = self.write('WebKit', _mach_o(self.GOOD))
        self.assertEqual(objects_with_no_profile_data(
            [path], ['/tmp/cov/WebKit_1234.profraw', '/tmp/cov/WebCore_1.profraw']), [])

    def test_a_uniquified_collected_name_still_matches(self):
        # collect_coverage_profiles appends -N when two runs accumulate into one directory.
        path = self.write('WebKit', _mach_o(self.GOOD))
        self.assertEqual(objects_with_no_profile_data(
            [path], ['/tmp/cov/WebKit_1234-2.profraw']), [])

    def test_nothing_is_claimed_about_collection_when_there_are_no_raw_profiles(self):
        # Reporting from an already-indexed profile: the raw profiles are long gone, so the
        # second rule cannot be applied and must not be guessed at.
        path = self.write('WebKit', _mach_o(self.GOOD))
        self.assertEqual(objects_with_no_profile_data([path], []), [])

    def test_a_binary_that_cannot_be_read_is_skipped_rather_than_raising(self):
        self.assertEqual(objects_with_no_profile_data(
            [os.path.join(self.directory, 'does-not-exist')]), [])

    def test_a_truncated_mach_o_is_skipped_rather_than_raising(self):
        path = self.write('Truncated', _mach_o('')[:40])
        self.assertEqual(objects_with_no_profile_data([path]), [])


class CollectedProfilesWithNoObjectTest(_MachOFixture):
    """The inverse guard: profile data was collected and nothing in the report claims it.

    This is the direction that catches the 84,332-line bug rather than one instance of it --
    WebKitLegacy was missing from INSTRUMENTED_PRODUCTS, and an object that is not in the report
    cannot be asked whether its profile was collected.
    """

    def webkit(self):
        return self.write('WebKit', _mach_o(COVERAGE_PROFILE_DIRECTORY + '/WebKit_%4m%c.profraw'))

    def test_a_collected_product_no_object_claims_is_reported(self):
        orphans = collected_profiles_with_no_object(
            [self.webkit()], ['/tmp/cov/WebKit_1_0.profraw', '/tmp/cov/WebKitLegacy_2_0.profraw',
                              '/tmp/cov/WebKitLegacy_2_1.profraw'])
        self.assertEqual(orphans, [('WebKitLegacy_', ['/tmp/cov/WebKitLegacy_2_0.profraw',
                                                      '/tmp/cov/WebKitLegacy_2_1.profraw'])])

    def test_a_claimed_prefix_that_is_a_prefix_of_another_product_does_not_claim_it(self):
        # 'WebKitLegacy_1_0.profraw'.startswith('WebKit') is true and would hide the bug; the
        # trailing underscore is what makes the two names distinguishable.
        orphans = collected_profiles_with_no_object(
            [self.webkit()], ['/tmp/cov/WebKitLegacy_1_0.profraw'])
        self.assertEqual([group for group, _ in orphans], ['WebKitLegacy_'])

    def test_everything_claimed_reports_nothing(self):
        self.assertEqual(collected_profiles_with_no_object(
            [self.webkit()], ['/tmp/cov/WebKit_1_0.profraw', '/tmp/cov/WebKit_1_1.profraw']), [])

    def test_a_uniquified_collected_name_is_still_claimed(self):
        # collect_coverage_profiles appends -N when two runs accumulate into one directory.
        self.assertEqual(collected_profiles_with_no_object(
            [self.webkit()], ['/tmp/cov/WebKit_1_0-2.profraw']), [])

    def test_an_unbaked_binary_claims_nothing_so_its_profiles_are_orphans(self):
        # The other half of the same bug: WebGPU baked no path, so it claims no prefix at all,
        # and a WebGPU profile from a fixed build reported against this binary is unclaimed.
        orphans = collected_profiles_with_no_object(
            [self.write('WebGPU', _mach_o(''))], ['/tmp/cov/WebGPU_1_0.profraw'])
        self.assertEqual([group for group, _ in orphans], ['WebGPU_'])

    def test_the_runtime_fallback_profile_is_reported_under_its_own_name(self):
        # An unbaked binary writes default.profraw into its working directory. There is no
        # underscore to split on, so the group is the whole name.
        orphans = collected_profiles_with_no_object([self.webkit()], ['/tmp/cov/default.profraw'])
        self.assertEqual(orphans, [('default.profraw', ['/tmp/cov/default.profraw'])])

    def test_no_collected_profiles_reports_nothing(self):
        # Reporting from an already-indexed profile: there is nothing to compare against.
        self.assertEqual(collected_profiles_with_no_object([self.webkit()], []), [])

    def test_a_binary_that_cannot_be_read_claims_nothing_rather_than_raising(self):
        orphans = collected_profiles_with_no_object(
            [os.path.join(self.directory, 'does-not-exist')], ['/tmp/cov/WebKit_1_0.profraw'])
        self.assertEqual([group for group, _ in orphans], ['WebKit_'])


class PartitionUnclaimedProfilesTest(unittest.TestCase):
    ORPHANS = [
        ('WebKitTestRunner_', ['/tmp/cov/WebKitTestRunner_1_0.profraw']),
        ('WebProcess_', ['/tmp/cov/WebProcess_1_0.profraw', '/tmp/cov/WebProcess_1_1.profraw']),
        ('WebKitLegacy_', ['/tmp/cov/WebKitLegacy_1_0.profraw']),
    ]

    def test_a_known_non_report_writer_is_expected_and_a_framework_is_not(self):
        # The whole point: WebKitLegacy missing from INSTRUMENTED_PRODUCTS is the bug the guard
        # exists for, and it must survive the same call that quietens the driver and the service.
        unexplained, expected = partition_unclaimed_profiles(
            self.ORPHANS, ('WebKitTestRunner', 'WebProcess'))
        self.assertEqual([group for group, _ in unexplained], ['WebKitLegacy_'])
        self.assertEqual([group for group, _ in expected], ['WebKitTestRunner_', 'WebProcess_'])

    def test_matching_is_on_the_whole_group_and_not_a_prefix(self):
        # 'WebKit' in the list must not swallow WebKitLegacy_ or WebKitTestRunner_.
        unexplained, expected = partition_unclaimed_profiles(self.ORPHANS, ('WebKit',))
        self.assertEqual(len(unexplained), 3)
        self.assertEqual(expected, [])

    def test_an_empty_list_leaves_everything_unexplained(self):
        unexplained, expected = partition_unclaimed_profiles(self.ORPHANS, ())
        self.assertEqual(unexplained, self.ORPHANS)
        self.assertEqual(expected, [])

    def test_the_runtime_fallback_profile_is_never_expected(self):
        # default.profraw has no underscore, so no '<name>_' entry can match it, which is right:
        # nothing bakes that name, so something wrote it by accident.
        orphans = [('default.profraw', ['/tmp/cov/default.profraw'])]
        unexplained, expected = partition_unclaimed_profiles(orphans, UNREPORTED_PROFILE_WRITERS)
        self.assertEqual(unexplained, orphans)
        self.assertEqual(expected, [])

    def test_the_shipped_list_covers_the_drivers_and_services_a_run_collects(self):
        collected = [(name + '_', ['/tmp/cov/{}_1_0.profraw'.format(name)]) for name in
                     ('WebKitTestRunner', 'TestRunnerInjectedBundle', 'WebProcess', 'GPUProcess',
                      'NetworkProcess', 'TestWTF')]
        unexplained, expected = partition_unclaimed_profiles(collected, UNREPORTED_PROFILE_WRITERS)
        self.assertEqual(unexplained, [])
        self.assertEqual(len(expected), len(collected))


class SurveyInstrumentationTest(_MachOFixture):
    GOOD = COVERAGE_PROFILE_DIRECTORY + '/WebKit_%4m%c.profraw'

    def test_an_uninstrumented_binary_is_reported_rather_than_skipped(self):
        # The single most likely first-run mistake is pointing the report at a tree that was not
        # built with --coverage. A real uninstrumented WebCore reads exactly like this.
        plain = self.write('WebCore', _mach_o(None, instrumented=False))
        survey = survey_instrumentation([self.write('WebKit', _mach_o(self.GOOD)), plain])
        self.assertEqual([path for path, _ in survey.uninstrumented], [plain])
        self.assertEqual(survey.unverifiable, [])
        self.assertIn('__llvm_prf_cnts', survey.uninstrumented[0][1])

    def test_a_stripped_binary_is_reported_separately_from_a_broken_one(self):
        # "Cannot tell" must never read as "broken": a stripped binary is fine.
        stripped = self.write('Stripped', _mach_o(None))
        survey = survey_instrumentation([stripped])
        self.assertEqual(survey.uninstrumented, [])
        self.assertEqual([path for path, _ in survey.unverifiable], [stripped])
        self.assertIn('llvm_profile_filename', survey.unverifiable[0][1])

    def test_a_correctly_instrumented_binary_is_in_neither_bucket(self):
        survey = survey_instrumentation([self.write('WebKit', _mach_o(self.GOOD))])
        self.assertEqual(survey, ([], []))

    def test_an_unbaked_but_instrumented_binary_is_in_neither_bucket(self):
        # That is objects_with_no_profile_data()'s finding, not this one's; reporting it twice
        # would make the two guards disagree about the same binary.
        survey = survey_instrumentation([self.write('WebGPU', _mach_o(''))])
        self.assertEqual(survey, ([], []))

    def test_something_that_is_not_a_mach_o_reads_as_uninstrumented(self):
        script = self.write('script', b'#!/bin/sh\n')
        self.assertEqual([path for path, _ in survey_instrumentation([script]).uninstrumented],
                         [script])


class UnreadableProfileAccountingTest(unittest.TestCase):
    """llvm-profdata --failure-mode=all fails only if *every* input fails.

    Verified against the current Apple LLVM: merging one good profile with two garbage ones prints
    'warning: <path>: truncated profile data' for each and exits 0. So the exit status says
    nothing, and the number of profiles that contributed is only knowable from these warnings.
    """

    INPUTS = ['/tmp/cov/A_1_0.profraw', '/tmp/cov/B_2_0.profraw', '/tmp/cov/C_3_0.profraw']
    STDERR = ('warning: /tmp/cov/B_2_0.profraw: truncated profile data\n'
              'warning: /tmp/cov/C_3_0.profraw: invalid instrumentation profile data '
              '(file header is corrupt)\n')

    def test_each_unreadable_input_is_counted_with_its_reason(self):
        self.assertEqual(unreadable_profiles_from_stderr(self.STDERR, self.INPUTS), [
            ('/tmp/cov/B_2_0.profraw', 'truncated profile data'),
            ('/tmp/cov/C_3_0.profraw',
             'invalid instrumentation profile data (file header is corrupt)')])

    def test_a_warning_about_something_that_is_not_an_input_is_not_a_lost_profile(self):
        # Otherwise any future diagnostic llvm-profdata adds inflates the count, and the
        # threshold starts refusing healthy runs.
        self.assertEqual(unreadable_profiles_from_stderr(
            'warning: 9418 functions have mismatched data\n', self.INPUTS), [])

    def test_no_stderr_at_all_is_no_lost_profiles(self):
        self.assertEqual(unreadable_profiles_from_stderr('', self.INPUTS), [])
        self.assertEqual(unreadable_profiles_from_stderr(None, self.INPUTS), [])

    def test_the_reported_order_is_the_input_order(self):
        reversed_stderr = '\n'.join(reversed(self.STDERR.splitlines()))
        self.assertEqual([path for path, _ in
                          unreadable_profiles_from_stderr(reversed_stderr, self.INPUTS)],
                         ['/tmp/cov/B_2_0.profraw', '/tmp/cov/C_3_0.profraw'])


class MergeRawProfilesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def write(self, name):
        path = os.path.join(self.directory, name)
        with open(path, 'w') as handle:
            handle.write('not a profile')
        return path

    def merge(self, stderr, returncode=0, **kwargs):
        """merge_raw_profiles_in_directory over the directory, with llvm-profdata's answer given.

        The merge itself is llvm-profdata's business and is exercised by the tool; what has to be
        right here is what this concludes from the answer.
        """
        completed = subprocess.CompletedProcess([], returncode, stdout='', stderr=stderr)
        with mock.patch.object(llvm_profile_utils.LLVMProfileData, 'merge',
                               return_value=completed):
            return llvm_profile_utils.merge_raw_profiles_in_directory(
                self.directory, os.path.join(self.directory, 'out.profdata'), **kwargs)

    def test_only_the_readable_profiles_are_returned_as_having_contributed(self):
        first, second = self.write('A_1_0.profraw'), self.write('B_2_0.profraw')
        for _ in range(8):
            self.write('C_3_{}.profraw'.format(_))
        merge = self.merge('warning: {}: truncated profile data\n'.format(second))
        self.assertNotIn(second, merge.merged)
        self.assertIn(first, merge.merged)
        self.assertEqual(merge.unreadable, [(second, 'truncated profile data')])

    def test_a_healthy_merge_returns_every_input(self):
        paths = sorted(self.write('A_1_{}.profraw'.format(index)) for index in range(3))
        self.assertEqual(self.merge('').merged, paths)
        self.assertEqual(self.merge('').unreadable, [])

    def test_too_many_unreadable_profiles_is_refused_rather_than_reported(self):
        # A run in which 99 of 100 profiles were unreadable merges, exits 0, and produces a
        # confidently low report. This is the only place that can tell.
        first, second = self.write('A_1_0.profraw'), self.write('B_2_0.profraw')
        stderr = ''.join('warning: {}: truncated profile data\n'.format(path)
                         for path in (first, second))
        with self.assertRaises(RuntimeError) as raised:
            self.merge(stderr)
        self.assertIn('2 of 2', str(raised.exception))

    def test_the_threshold_can_be_lifted(self):
        first, second = self.write('A_1_0.profraw'), self.write('B_2_0.profraw')
        stderr = 'warning: {}: truncated profile data\n'.format(second)
        self.assertEqual(self.merge(stderr, unreadable_limit=1.0).merged, [first])

    def test_a_merge_that_failed_outright_still_raises(self):
        self.write('A_1_0.profraw')
        with self.assertRaises(subprocess.CalledProcessError):
            self.merge('error: something else entirely\n', returncode=1)

    def test_an_empty_directory_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.merge('')

    def test_the_compatibility_wrapper_returns_just_the_readable_ones(self):
        first, second = self.write('A_1_0.profraw'), self.write('B_2_0.profraw')
        for index in range(8):
            self.write('C_3_{}.profraw'.format(index))
        completed = subprocess.CompletedProcess(
            [], 0, stdout='', stderr='warning: {}: truncated profile data\n'.format(second))
        with mock.patch.object(llvm_profile_utils.LLVMProfileData, 'merge',
                               return_value=completed):
            merged = llvm_profile_utils.merge_all_raw_profiles_in_directory(
                self.directory, os.path.join(self.directory, 'out.profdata'))
        self.assertIn(first, merged)
        self.assertNotIn(second, merged)


class LLVMCovArgumentsTest(unittest.TestCase):
    def test_first_object_is_positional_and_the_rest_are_repeated(self):
        # llvm-cov takes the first binary positionally and each additional one as a
        # repeated -object=. Getting this wrong either omits a binary (under-reporting
        # the files only it contains) or fails outright.
        self.assertEqual(
            LLVMCov._object_arguments(['/WebCore', '/WebKit', '/JavaScriptCore']),
            ['/WebCore', '-object=/WebKit', '-object=/JavaScriptCore'])

    def test_single_object_has_no_object_flag(self):
        self.assertEqual(LLVMCov._object_arguments(['/WebCore']), ['/WebCore'])

    def test_common_arguments_include_every_exclusion_and_equivalence(self):
        arguments = LLVMCov._common_arguments(
            ['/WebCore', '/WebKit'], '/tmp/coverage.profdata',
            ignore_filename_regexes=('Source/ThirdParty/', '/DerivedSources/'),
            path_equivalences=('/build,/src',))
        self.assertEqual(arguments, [
            '/WebCore',
            '-object=/WebKit',
            '-instr-profile=/tmp/coverage.profdata',
            '--ignore-filename-regex=Source/ThirdParty/',
            '--ignore-filename-regex=/DerivedSources/',
            '-path-equivalence=/build,/src',
        ])

    def test_sources_are_passed_positionally_and_never_as_sources_equals(self):
        # Verified against the current Apple LLVM: --sources=PATH is accepted, silently ignored and
        # produces the whole report -- 1,022,546 lines for WebCore against 35,978 for the same
        # scope passed this way -- with nothing on stderr. Getting this wrong is a scoped report
        # that is quietly the unscoped one.
        self.assertEqual(LLVMCov._sources_arguments(['/checkout/Source/WebCore/dom']),
                         ['--sources', '/checkout/Source/WebCore/dom'])
        self.assertEqual(LLVMCov._sources_arguments(['/a', '/b']), ['--sources', '/a', '/b'])

    def test_no_sources_adds_no_arguments(self):
        self.assertEqual(LLVMCov._sources_arguments([]), [])


class ToolchainSelectionTest(unittest.TestCase):
    """Which llvm-cov runs, in what order, and which are refused.

    There was no test for this at all, and the defect it hides is silent in both directions:
    preference_ordered_paths() advanced a class attribute *before* yielding, so the first
    candidate run() tried was index 1 and the intended binary was tried last, while
    preferred_path() read the same mutated index -- so report and export, which run
    concurrently, could be served by different binaries within one report.
    """

    VERSIONS = {
        # A synthetic major, well above 3.2, so the comparison under test is meaningful
        # without naming a toolchain.
        '/xcode/OSX/llvm-cov': 'Apple LLVM version 99.0.0',
        '/xcode/iOS/llvm-cov': 'Apple LLVM version 99.0.0',
        '/xcode/Default/llvm-cov': 'Apple LLVM version 99.0.0',
        '/usr/local/bin/llvm-cov': 'LLVM (http://llvm.org/):\n  LLVM version 3.2svn Apple '
                                   'Build #3425-36',
    }

    def executable_class(self, detected, versions=None, returncode=1):
        versions = self.VERSIONS if versions is None else versions

        class Fake(llvm_profile_utils.ExecutablesFromEnvAndXcode):
            EXECUTABLE_NAME = 'llvm-cov'

            @classmethod
            def detect_binaries(cls):
                return list(detected)

            @classmethod
            def version_of(cls, path):
                # Deliberately routed through the real parser rather than returning a number,
                # because tolerating a non-zero exit is half of what is being tested.
                completed = subprocess.CompletedProcess([path], returncode,
                                                        stdout=versions.get(path, ''))
                with mock.patch('subprocess.run', return_value=completed):
                    return llvm_profile_utils.ExecutablesFromEnvAndXcode.version_of.__wrapped__(
                        cls, path)

        return Fake

    def test_the_detected_order_is_the_order_run_tries(self):
        detected = ['/xcode/OSX/llvm-cov', '/xcode/iOS/llvm-cov', '/xcode/Default/llvm-cov']
        executable = self.executable_class(detected)
        self.assertEqual(executable.preference_ordered_paths(), detected)
        # Repeatedly, which is the property the old rotation broke.
        self.assertEqual(executable.preference_ordered_paths(), detected)
        self.assertEqual(executable.preferred_path(), '/xcode/OSX/llvm-cov')
        self.assertEqual(executable.preferred_path(), '/xcode/OSX/llvm-cov')

    def test_preferred_path_is_the_first_binary_run_tries(self):
        # These have to agree: report() goes through run() and export_lcov(compress=True) has
        # to name a binary up front, so a disagreement means one report's summary and its
        # trace came from different toolchains.
        executable = self.executable_class(['/xcode/OSX/llvm-cov', '/xcode/iOS/llvm-cov'])
        self.assertEqual(executable.preferred_path(),
                         executable.preference_ordered_paths()[0])

    def test_a_binary_older_than_the_toolchain_is_refused(self):
        executable = self.executable_class(['/xcode/OSX/llvm-cov', '/usr/local/bin/llvm-cov'])
        self.assertEqual(executable.preference_ordered_paths(), ['/xcode/OSX/llvm-cov'])

    def test_a_version_is_read_from_a_command_that_exits_non_zero(self):
        # /usr/local/bin/llvm-cov on this machine prints its banner and exits 1. Requiring exit
        # 0 reads it as having no version, which is why 3.2svn was never refused.
        executable = self.executable_class(['/usr/local/bin/llvm-cov'], returncode=1)
        self.assertEqual(executable.version_of('/usr/local/bin/llvm-cov')[0], 3)

    def test_the_only_binary_is_kept_even_when_its_version_is_unreadable(self):
        executable = self.executable_class(['/xcode/OSX/llvm-cov', '/somewhere/llvm-cov'],
                                           versions={'/xcode/OSX/llvm-cov': '', '/somewhere/llvm-cov': ''})
        self.assertEqual(executable.preference_ordered_paths(),
                         ['/xcode/OSX/llvm-cov', '/somewhere/llvm-cov'])
        executable = self.executable_class(['/usr/local/bin/llvm-cov'])
        self.assertEqual(executable.preference_ordered_paths(), ['/usr/local/bin/llvm-cov'])

    def test_no_binary_at_all_is_an_error_rather_than_a_silent_nothing(self):
        executable = self.executable_class([])
        self.assertEqual(executable.preference_ordered_paths(), [])
        with self.assertRaises(RuntimeError):
            executable.preferred_path()
        with self.assertRaises(RuntimeError):
            executable.run(['report'])

    def test_this_machines_real_order_puts_the_toolchain_first(self):
        # Regression cover for the measured defect: detection found the macOS SDK's copy first
        # and run() tried it fourth, after /usr/local/bin's LLVM 3.2svn.
        detected = LLVMCovExecutable.detect_binaries()
        if not detected:
            self.skipTest('No llvm-cov on this machine')
        self.assertEqual(LLVMCovExecutable.preference_ordered_paths()[0], detected[0])
        self.assertEqual(LLVMCovExecutable.preferred_path(), detected[0])


class AtomicOutputTest(unittest.TestCase):
    """A failed export must not leave a well-formed truncated trace at the final filename.

    That is the worst shape a coverage artifact can take: parse_lcov() reads a truncated trace as
    a perfectly valid smaller one, so the report is over whichever files llvm-cov reached before
    it died, and nothing downstream can tell the difference.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.output = os.path.join(self.directory, 'coverage.lcov')

    def write(self, returncode):
        def writer(path):
            with open(path, 'w') as handle:
                handle.write('SF:/checkout/a.cpp\nDA:1,1\n')
            return subprocess.CompletedProcess([], returncode)
        return LLVMCov._write_atomically(self.output, writer)

    def test_a_successful_write_lands_at_the_final_path(self):
        self.assertEqual(self.write(0).returncode, 0)
        self.assertEqual(sorted(os.listdir(self.directory)), ['coverage.lcov'])

    def test_a_failed_write_leaves_nothing_behind(self):
        self.assertEqual(self.write(1).returncode, 1)
        self.assertEqual(os.listdir(self.directory), [])

    def test_a_failed_write_does_not_replace_an_earlier_good_one(self):
        # An incremental workflow re-reports into the same directory, so the previous trace being
        # replaced by a truncated one is a real way to lose a good artifact.
        self.write(0)
        self.write(1)
        with open(self.output) as handle:
            self.assertIn('SF:', handle.read())

    def test_an_exception_partway_through_leaves_nothing_behind(self):
        def explode(path):
            with open(path, 'w') as handle:
                handle.write('SF:/checkout/a.cpp\n')
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            LLVMCov._write_atomically(self.output, explode)
        self.assertEqual(os.listdir(self.directory), [])


class CollectCoverageProfilesTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.mkdtemp()
        self._profile_directory = os.path.join(self._directory, 'WebKitCoverage')
        self._destination = os.path.join(self._directory, 'collected')
        os.makedirs(self._profile_directory)
        self._patch = mock.patch.object(llvm_profile_utils, 'COVERAGE_PROFILE_DIRECTORY',
                                        self._profile_directory)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _write_profile(self, name, contents='profile'):
        with open(os.path.join(self._profile_directory, name), 'w') as profile:
            profile.write(contents)

    def test_moves_profiles_and_ignores_other_files(self):
        self._write_profile('WebCore_1234_0.profraw')
        self._write_profile('WebKit_5678_0.profraw')
        with open(os.path.join(self._profile_directory, 'notes.txt'), 'w') as other:
            other.write('not a profile')

        collected = collect_coverage_profiles(self._destination)

        self.assertEqual(sorted(os.path.basename(path) for path in collected),
                         ['WebCore_1234_0.profraw', 'WebKit_5678_0.profraw'])
        # Moved, not copied, so a following run starts from an empty directory.
        self.assertEqual(os.listdir(self._profile_directory), ['notes.txt'])

    def test_successive_runs_accumulate_without_clobbering(self):
        # A layout-test run followed by an API-test run into one directory must keep both
        # sets, even though %Nm pooling produces the same filenames each time.
        self._write_profile('WebCore_1234_0.profraw', 'first')
        collect_coverage_profiles(self._destination)
        self._write_profile('WebCore_1234_0.profraw', 'second')
        collect_coverage_profiles(self._destination)

        self.assertEqual(sorted(os.listdir(self._destination)),
                         ['WebCore_1234_0-1.profraw', 'WebCore_1234_0.profraw'])

    def test_no_profiles_is_not_an_error(self):
        # A run that produced nothing should warn, not raise: the caller collects from a
        # finally block and must not mask the real test failure.
        self.assertEqual(collect_coverage_profiles(self._destination), [])

    def test_missing_profile_directory_is_not_an_error(self):
        self._patch.stop()
        self._patch = mock.patch.object(llvm_profile_utils, 'COVERAGE_PROFILE_DIRECTORY',
                                        os.path.join(self._directory, 'does-not-exist'))
        self._patch.start()
        self.assertEqual(collect_coverage_profiles(self._destination), [])


class PrepareCoverageProfileDirectoryTest(unittest.TestCase):
    """The directory is machine-global, so clearing it is the one place a run can destroy another.

    The clearing itself is load-bearing and must stay: %Nm merges into an existing profile rather
    than replacing it, so a leftover from an earlier run or a rebuild would be folded into this
    run's counters. What it cannot do on its own is tell an abandoned profile from one a
    concurrent run is still writing to.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.profile_directory = os.path.join(self.directory, 'WebKitCoverage')
        self._patch = mock.patch.object(llvm_profile_utils, 'COVERAGE_PROFILE_DIRECTORY',
                                        self.profile_directory)
        self._patch.start()
        release_coverage_profile_directory_lock()
        self.addCleanup(self._patch.stop)
        self.addCleanup(release_coverage_profile_directory_lock)

    def write_profile(self, name, contents='profile'):
        os.makedirs(self.profile_directory, exist_ok=True)
        with open(os.path.join(self.profile_directory, name), 'w') as handle:
            handle.write(contents)

    def test_a_missing_directory_is_created(self):
        prepare_coverage_profile_directory()
        self.assertTrue(os.path.isdir(self.profile_directory))

    def test_stale_profiles_are_removed_and_counted(self):
        # Counted because this is data being discarded: an interrupted run whose profiles were
        # never collected looks exactly like a rebuild's leftovers from in here.
        self.write_profile('WebCore_1_0.profraw', 'x' * 100)
        self.write_profile('WebKit_2_0.profraw', 'y' * 50)
        stale = prepare_coverage_profile_directory()
        self.assertEqual((stale.count, stale.total_bytes), (2, 150))
        self.assertEqual([name for name in os.listdir(self.profile_directory)
                          if name.endswith('.profraw')], [])

    def test_nothing_stale_is_counted_as_nothing(self):
        self.assertEqual(prepare_coverage_profile_directory(), (0, 0))

    def test_only_profiles_are_removed(self):
        self.write_profile('WebCore_1_0.profraw')
        self.write_profile('notes.txt')
        prepare_coverage_profile_directory()
        self.assertEqual(sorted(os.listdir(self.profile_directory)),
                         sorted([llvm_profile_utils.COVERAGE_PROFILE_LOCK_FILENAME, 'notes.txt']))

    def test_the_lock_is_held_afterwards_and_names_this_process(self):
        prepare_coverage_profile_directory()
        with open(coverage_profile_lock_path()) as handle:
            self.assertIn('pid {}'.format(os.getpid()), handle.read())

    def test_preparing_twice_in_one_process_is_not_a_deadlock(self):
        # The harness calls this once, but a tool that calls it again must not lock itself out.
        prepare_coverage_profile_directory()
        prepare_coverage_profile_directory()

    def test_a_run_already_holding_the_lock_is_refused_and_named(self):
        # An flock is per open file description, so a second descriptor stands in for another
        # process here -- and a real other process is what this is for: it would otherwise delete
        # this run's live mmapped profiles and then collect whatever survived into its own
        # --coverage-dir.
        os.makedirs(self.profile_directory, exist_ok=True)
        other = open(coverage_profile_lock_path(), 'a+')
        self.addCleanup(other.close)
        other.write('pid 4242 (run-webkit-tests) since now\n')
        other.flush()
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        with self.assertRaises(CoverageProfileDirectoryInUse) as raised:
            prepare_coverage_profile_directory()
        self.assertIn('pid 4242', str(raised.exception))
        self.assertIn('run-webkit-tests', str(raised.exception))

    def test_a_refused_run_does_not_delete_the_other_runs_profiles(self):
        os.makedirs(self.profile_directory, exist_ok=True)
        other = open(coverage_profile_lock_path(), 'a+')
        self.addCleanup(other.close)
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.write_profile('WebCore_1_0.profraw')

        with self.assertRaises(CoverageProfileDirectoryInUse):
            prepare_coverage_profile_directory()
        self.assertIn('WebCore_1_0.profraw', os.listdir(self.profile_directory))

    def test_the_lock_is_released_when_the_holder_goes_away(self):
        # Which is why there is no stale-lock problem to solve: a SIGKILLed run closes its
        # descriptor, so the next run takes the lock without having to decide whether a recorded
        # pid is still alive.
        os.makedirs(self.profile_directory, exist_ok=True)
        other = open(coverage_profile_lock_path(), 'a+')
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        other.close()
        prepare_coverage_profile_directory()

    def test_a_lock_file_that_cannot_be_opened_warns_rather_than_refusing(self):
        # The directory is mode 1777 so any user's sandboxed process can write a profile there,
        # which means the lock file can belong to somebody else.
        os.makedirs(self.profile_directory, exist_ok=True)
        with mock.patch('builtins.open', side_effect=PermissionError('denied')):
            self.assertIsNone(acquire_coverage_profile_directory_lock())

    def test_nobody_is_holding_it_when_there_is_no_lock_file(self):
        self.assertIsNone(coverage_profile_directory_holder())
        os.makedirs(self.profile_directory, exist_ok=True)
        self.assertIsNone(coverage_profile_directory_holder())

    def test_a_lock_file_left_by_a_dead_run_names_nobody(self):
        # The stale-file case, which is the whole reason this asks the kernel and not the file:
        # flock is released when the holder dies but nothing truncates the text, so the real
        # /private/tmp/WebKitCoverage/.webkit-coverage-run.lock routinely names a dead pid.
        os.makedirs(self.profile_directory, exist_ok=True)
        with open(coverage_profile_lock_path(), 'w') as handle:
            handle.write('pid 20947 (run-webkit-tests) since 2026-08-22T11:04:00-0700\n')
        self.assertIsNone(coverage_profile_directory_holder())
        # And the directory is still claimable, which is the consequence that matters.
        prepare_coverage_profile_directory()

    def test_a_live_holder_is_named_without_taking_the_lock_from_it(self):
        os.makedirs(self.profile_directory, exist_ok=True)
        other = open(coverage_profile_lock_path(), 'a+')
        self.addCleanup(other.close)
        other.write('pid 4242 (run-webkit-tests) since now\n')
        other.flush()
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.assertIn('pid 4242', coverage_profile_directory_holder())
        # Asking twice gives the same answer, and the holder still holds it.
        self.assertIn('pid 4242', coverage_profile_directory_holder())
        with self.assertRaises(CoverageProfileDirectoryInUse):
            prepare_coverage_profile_directory()

    def test_this_process_holding_it_reports_itself(self):
        prepare_coverage_profile_directory()
        self.assertIn('pid {}'.format(os.getpid()), coverage_profile_directory_holder())


class LcovCanonicalizationTest(unittest.TestCase):
    def test_installed_headers_map_back_to_source(self):
        from webkitpy.coverage_lcov import PathCanonicalizer
        c = PathCanonicalizer('/checkout')
        self.assertEqual(c.canonicalize('/checkout/WebKitBuild/Release/usr/local/include/wtf/Vector.h'),
                         '/checkout/Source/WTF/wtf/Vector.h')
        self.assertEqual(c.canonicalize('/checkout/WebKitBuild/Release/usr/local/include/bmalloc/bmalloc.h'),
                         '/checkout/Source/bmalloc/bmalloc/bmalloc.h')
        self.assertEqual(c.installed_header_count, 2)

    def test_source_paths_are_left_alone(self):
        from webkitpy.coverage_lcov import PathCanonicalizer
        c = PathCanonicalizer('/checkout')
        for path in ('/checkout/Source/WebCore/dom/Node.cpp', '/checkout/Source/WTF/wtf/Vector.h'):
            self.assertEqual(c.canonicalize(path), path)
        self.assertEqual(c.installed_header_count, 0)

    def test_duplicate_entries_union_per_line_rather_than_summing(self):
        # The same header seen through two paths: WTF's own TUs instantiate lines 1-2, a
        # different framework's TUs instantiate lines 2-3. Summing would report 4 lines for a
        # 3-line file and double-count line 2; taking the max per line is correct.
        from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov
        directory = tempfile.mkdtemp()
        lcov = os.path.join(directory, 'coverage.lcov')
        with open(lcov, 'w') as handle:
            handle.write(
                'SF:/checkout/Source/WTF/wtf/Vector.h\n'
                'DA:1,5\nDA:2,0\nend_of_record\n'
                'SF:/checkout/WebKitBuild/Release/usr/local/include/wtf/Vector.h\n'
                'DA:2,7\nDA:3,0\nend_of_record\n')
        files = parse_lcov(lcov, PathCanonicalizer('/checkout'))
        self.assertEqual(list(files), ['/checkout/Source/WTF/wtf/Vector.h'])
        coverage = files['/checkout/Source/WTF/wtf/Vector.h']
        self.assertEqual(coverage.lines, {1: 5, 2: 7, 3: 0})
        # 3 lines, 2 of them executed -- not 4 lines.
        self.assertEqual(coverage.totals()['lines'], (3, 2))

    def test_uncovered_in_one_view_but_covered_in_another_counts_as_covered(self):
        from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov
        directory = tempfile.mkdtemp()
        lcov = os.path.join(directory, 'coverage.lcov')
        with open(lcov, 'w') as handle:
            handle.write(
                'SF:/checkout/Source/WTF/wtf/Vector.h\nDA:10,0\nend_of_record\n'
                'SF:/checkout/WebKitBuild/Release/usr/local/include/wtf/Vector.h\nDA:10,3\nend_of_record\n')
        files = parse_lcov(lcov, PathCanonicalizer('/checkout'))
        self.assertEqual(files['/checkout/Source/WTF/wtf/Vector.h'].totals()['lines'], (1, 1))


class DirectoryIndexTest(unittest.TestCase):
    def _lcov(self, records):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, 'coverage.lcov')
        with open(path, 'w') as handle:
            for filename, lines in records:
                handle.write('SF:{}\n'.format(filename))
                for number, count in lines:
                    handle.write('DA:{},{}\n'.format(number, count))
                handle.write('end_of_record\n')
        return path

    def test_directories_aggregate_their_descendants(self):
        from webkitpy.coverage_directory_index import build_tree
        totals = lambda count, covered: {'lines': (count, covered), 'functions': (0, 0),
                                         'branches': (0, 0)}
        root = build_tree([
            (('Source', 'WebCore', 'dom', 'Node.cpp'), totals(100, 50)),
            (('Source', 'WebCore', 'dom', 'Element.cpp'), totals(100, 10)),
            (('Source', 'WebCore', 'css', 'CSSParser.cpp'), totals(200, 200)),
        ])
        self.assertEqual(root.totals['lines'], [400, 260])
        webcore = root.children['Source'].children['WebCore']
        self.assertEqual(webcore.children['dom'].totals['lines'], [200, 60])
        self.assertEqual(webcore.children['css'].totals['lines'], [200, 200])

    def test_single_child_chains_collapse(self):
        from webkitpy.coverage_directory_index import build_tree, _collapse_single_child_chain
        totals = {'lines': (10, 5), 'functions': (0, 0), 'branches': (0, 0)}
        root = build_tree([(('Source', 'WebCore', 'dom', 'Node.cpp'), totals)])
        prefix, node = _collapse_single_child_chain(root.children['Source'])
        self.assertEqual(prefix, ['Source', 'WebCore', 'dom'])

    def test_writes_small_pages_with_working_links_sorted_by_biggest_gap(self):
        from webkitpy.coverage_directory_index import write_directory_index
        lcov = self._lcov([
            ('/checkout/Source/WebCore/dom/Node.cpp', [(1, 1), (2, 0), (3, 0)]),
            ('/checkout/Source/WebCore/css/CSSParser.cpp', [(1, 1)]),
        ])
        output = os.path.join(os.path.dirname(lcov), 'report')
        pages = write_directory_index(lcov, output, source_root='/checkout')
        self.assertGreaterEqual(pages, 3)

        index = os.path.join(output, 'index.html')
        self.assertLess(os.path.getsize(index), 32 * 1024)

        with open(os.path.join(output, 'Source', 'WebCore', 'index.html')) as handle:
            webcore = handle.read()
        # dom has 2 uncovered lines, css has 0, so dom must be listed first.
        self.assertLess(webcore.index('dom/index.html'), webcore.index('css/index.html'))

        with open(os.path.join(output, 'Source', 'WebCore', 'dom', 'index.html')) as handle:
            # The line view is a sibling of this page, so the link is just the file name.
            self.assertIn('<a href="Node.cpp.html">Node.cpp</a>', handle.read())

    def test_empty_lcov_is_an_error_rather_than_an_empty_report(self):
        from webkitpy.coverage_directory_index import write_directory_index
        lcov = self._lcov([])
        with self.assertRaises(RuntimeError):
            write_directory_index(lcov, os.path.join(os.path.dirname(lcov), 'report'),
                                  source_root='/checkout')


if __name__ == '__main__':
    unittest.main()

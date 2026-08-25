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
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

from webkitpy import llvm_profile_utils
from webkitpy.llvm_profile_utils import (
    COVERAGE_PROFILE_DIRECTORY, LLVMCov, collect_coverage_profiles, objects_with_no_profile_data,
    profile_name_prefix, read_instrumentation)


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

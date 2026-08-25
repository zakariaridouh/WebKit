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
import tempfile
import unittest
from unittest import mock

from webkitpy import llvm_profile_utils
from webkitpy.llvm_profile_utils import LLVMCov, collect_coverage_profiles


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
            self.assertIn('html/coverage/checkout/Source/WebCore/dom/Node.cpp.html', handle.read())

    def test_empty_lcov_is_an_error_rather_than_an_empty_report(self):
        from webkitpy.coverage_directory_index import write_directory_index
        lcov = self._lcov([])
        with self.assertRaises(RuntimeError):
            write_directory_index(lcov, os.path.join(os.path.dirname(lcov), 'report'),
                                  source_root='/checkout')


if __name__ == '__main__':
    unittest.main()

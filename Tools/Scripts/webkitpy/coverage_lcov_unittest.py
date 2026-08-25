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
import os
import shutil
import tempfile
import unittest

from webkitpy.coverage_lcov import (
    PathCanonicalizer, open_lcov, parse_lcov, parse_lcov_source_files,
    third_party_copied_header_ignore_regexes)


class _Checkout(unittest.TestCase):
    """A throwaway checkout, so canonicalization can be tested against a real filesystem."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relative, contents=''):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path

    def absolute(self, relative):
        return os.path.join(self.root, relative)


class InstalledHeaderCanonicalizationTest(_Checkout):
    BMALLOC_COPY = '/tmp/Build/Release/usr/local/include/bmalloc/'

    def test_libpas_header_resolves_to_libpas_and_not_to_bmalloc(self):
        # The copy phase flattens Source/bmalloc/libpas/src/libpas and Source/bmalloc/bmalloc
        # into one directory, so only the filesystem can say which one a header came from.
        self.write('Source/bmalloc/libpas/src/libpas/pas_alignment.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.BMALLOC_COPY + 'pas_alignment.h'),
                         self.absolute('Source/bmalloc/libpas/src/libpas/pas_alignment.h'))

    def test_bmalloc_header_still_resolves_to_bmalloc(self):
        self.write('Source/bmalloc/bmalloc/IsoHeap.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.BMALLOC_COPY + 'IsoHeap.h'),
                         self.absolute('Source/bmalloc/bmalloc/IsoHeap.h'))

    def test_first_candidate_wins_when_a_name_exists_in_both(self):
        self.write('Source/bmalloc/bmalloc/Ambiguous.h')
        self.write('Source/bmalloc/libpas/src/libpas/Ambiguous.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.BMALLOC_COPY + 'Ambiguous.h'),
                         self.absolute('Source/bmalloc/bmalloc/Ambiguous.h'))

    def test_unresolvable_header_falls_back_to_the_first_candidate(self):
        # Better a plausible source path than a path inside the build directory: the report
        # groups by directory, and the build directory is not one of them.
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.BMALLOC_COPY + 'Vanished.h'),
                         self.absolute('Source/bmalloc/bmalloc/Vanished.h'))

    def test_installed_wtf_header_resolves_without_needing_the_file(self):
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize('/tmp/Build/Release/usr/local/include/wtf/Vector.h'),
                         self.absolute('Source/WTF/wtf/Vector.h'))
        self.assertEqual(canonicalizer.installed_header_count, 1)

    def test_a_path_that_matches_no_rule_is_returned_unchanged(self):
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize('/elsewhere/Source/WebCore/dom/Node.cpp'),
                         '/elsewhere/Source/WebCore/dom/Node.cpp')
        self.assertEqual(canonicalizer.installed_header_count, 0)

    def test_a_libpas_header_keeps_its_subdirectory(self):
        self.write('Source/bmalloc/libpas/src/libpas/sub/pas_utils.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.BMALLOC_COPY + 'sub/pas_utils.h'),
                         self.absolute('Source/bmalloc/libpas/src/libpas/sub/pas_utils.h'))

    def test_an_installed_pal_header_resolves_into_webcore(self):
        # PAL builds as part of WebCore but installs its headers under its own name, so
        # every other framework's translation units see them here. 23 files and 533 physical
        # lines of a full-suite trace, at 82.93%, were reported under the build directory.
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(
            canonicalizer.canonicalize('/tmp/Build/Release/usr/local/include/pal/text/TextEncoding.h'),
            self.absolute('Source/WebCore/PAL/pal/text/TextEncoding.h'))


class FrameworkHeaderCanonicalizationTest(_Checkout):
    WEBCORE_COPY = '/tmp/Build/Release/WebCore.framework/PrivateHeaders/'

    def test_a_unique_basename_resolves(self):
        self.write('Source/WebCore/dom/Document.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.WEBCORE_COPY + 'Document.h'),
                         self.absolute('Source/WebCore/dom/Document.h'))
        self.assertEqual(canonicalizer.framework_header_count, 1)

    def test_another_ports_copy_does_not_make_the_name_ambiguous(self):
        # ResourceRequest.h, ResourceResponse.h, ResourceError.h, CertificateInfo.h and
        # AuthenticationChallenge.h each exist three times -- cf, curl and soup -- and a
        # macOS build compiles only the first, so before this all five resolved to nothing.
        self.write('Source/WebCore/platform/network/cf/ResourceRequest.h')
        self.write('Source/WebCore/platform/network/curl/ResourceRequest.h')
        self.write('Source/WebCore/platform/network/soup/ResourceRequest.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.WEBCORE_COPY + 'ResourceRequest.h'),
                         self.absolute('Source/WebCore/platform/network/cf/ResourceRequest.h'))

    def test_pal_is_not_a_candidate_for_a_webcore_framework_header(self):
        # PAL lives inside Source/WebCore but is a project of its own, and it installs to
        # usr/local/include/pal, so a WebCore.framework header is never PAL's copy.
        self.write('Source/WebCore/platform/ThreadGlobalData.h')
        self.write('Source/WebCore/PAL/pal/ThreadGlobalData.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.WEBCORE_COPY + 'ThreadGlobalData.h'),
                         self.absolute('Source/WebCore/platform/ThreadGlobalData.h'))

    def test_two_cocoa_candidates_stay_unresolved(self):
        # Narrowing to what a macOS build could have compiled is not a licence to guess.
        self.write('Source/WebCore/accessibility/AXIsolatedTree.h')
        self.write('Source/WebCore/accessibility/isolatedtree/AXIsolatedTree.h')
        canonicalizer = PathCanonicalizer(self.root)
        copied = self.WEBCORE_COPY + 'AXIsolatedTree.h'
        self.assertEqual(canonicalizer.canonicalize(copied), copied)
        self.assertEqual(canonicalizer.unresolved_framework_headers, {copied})

    def test_a_webgpu_framework_header_resolves(self):
        self.write('Source/WebGPU/WebGPU/WebGPU.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(
            canonicalizer.canonicalize(
                '/tmp/Build/Release/WebGPU.framework/Versions/A/Headers/WebGPU.h'),
            self.absolute('Source/WebGPU/WebGPU/WebGPU.h'))


class BuildDirectoryResidueTest(_Checkout):
    BUILD = '/tmp/Build/Release'

    def canonicalizer(self):
        return PathCanonicalizer(self.root, build_directory=self.BUILD)

    def test_an_unresolved_framework_header_is_counted_with_a_reason(self):
        canonicalizer = self.canonicalizer()
        copied = self.BUILD + '/WebCore.framework/PrivateHeaders/JSDocument.h'
        canonicalizer.canonicalize(copied)
        self.assertEqual(canonicalizer.build_directory_paths,
                         {PathCanonicalizer.COPIED_FRAMEWORK_HEADER: {copied}})

    def test_a_webkitadditions_source_is_counted_separately(self):
        # 47 files and 6,202 lines at 10.85% on the measured run. They are product code, so
        # they belong in the report; their only path is the copy, because the repository they
        # come from is not this checkout.
        canonicalizer = self.canonicalizer()
        copied = self.BUILD + '/usr/local/include/WebKitAdditions/QuirksAdditions.cpp'
        self.assertEqual(canonicalizer.canonicalize(copied), copied)
        self.assertEqual(canonicalizer.build_directory_paths,
                         {PathCanonicalizer.WEBKIT_ADDITIONS: {copied}})

    def test_a_checkout_path_is_not_counted(self):
        canonicalizer = self.canonicalizer()
        canonicalizer.canonicalize(self.absolute('Source/WebCore/dom/Node.cpp'))
        self.assertEqual(canonicalizer.build_directory_paths, {})

    def test_nothing_is_counted_without_a_build_directory(self):
        canonicalizer = PathCanonicalizer(self.root)
        canonicalizer.canonicalize(self.BUILD + '/WebCore.framework/PrivateHeaders/JSDocument.h')
        self.assertEqual(canonicalizer.build_directory_paths, {})


class ThirdPartyCopiedHeaderTest(_Checkout):
    def include_directory(self):
        return os.path.join(self.root, 'Release', 'usr', 'local', 'include')

    def test_only_first_party_copies_are_kept(self):
        for name in ('wtf', 'pal', 'bmalloc', 'WebKitAdditions', 'ANGLE', 'api', 'rtc_base'):
            os.makedirs(os.path.join(self.include_directory(), name))
        self.assertEqual(
            third_party_copied_header_ignore_regexes(os.path.join(self.root, 'Release')),
            ['/usr/local/include/ANGLE', '/usr/local/include/api', '/usr/local/include/rtc_base'])

    def test_a_build_directory_with_no_installed_headers_yields_nothing(self):
        self.assertEqual(third_party_copied_header_ignore_regexes(self.root), [])


class ParseLcovTest(_Checkout):
    TRACE = ('SF:/checkout/Source/WTF/wtf/Vector.h\n'
             'FN:12,_ZN3WTF6VectorIiE5clearEv\n'
             'FNDA:3,_ZN3WTF6VectorIiE5clearEv\n'
             'FN:20,_ZN3WTF6VectorIiE6shrinkEm\n'
             'DA:12,3\n'
             'DA:13,0\n'
             'BRDA:12,0,0,3\n'
             'BRDA:12,0,1,-\n'
             'end_of_record\n')

    def test_lines_functions_and_branches(self):
        path = self.write('trace.lcov', self.TRACE)
        files = parse_lcov(path)
        self.assertEqual(list(files), ['/checkout/Source/WTF/wtf/Vector.h'])
        coverage = files['/checkout/Source/WTF/wtf/Vector.h']
        self.assertEqual(coverage.lines, {12: 3, 13: 0})
        self.assertEqual(coverage.functions,
                         {'_ZN3WTF6VectorIiE5clearEv': 3, '_ZN3WTF6VectorIiE6shrinkEm': 0})
        self.assertEqual(coverage.branches, {('12', '0', '0'): 3, ('12', '0', '1'): 0})
        self.assertEqual(coverage.totals()['lines'], (2, 1))

    def test_duplicate_records_for_one_path_are_unioned_line_by_line(self):
        path = self.write('trace.lcov', self.TRACE + self.TRACE.replace('DA:13,0', 'DA:13,9'))
        files = parse_lcov(path)
        self.assertEqual(files['/checkout/Source/WTF/wtf/Vector.h'].lines, {12: 3, 13: 9})


class GzippedTraceTest(_Checkout):
    TRACE = ParseLcovTest.TRACE

    def compress(self, relative, contents):
        path = os.path.join(self.root, relative)
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            handle.write(contents)
        return path

    def test_open_lcov_reads_a_gzipped_trace_and_a_plain_one_identically(self):
        plain = self.write('plain.lcov', self.TRACE)
        compressed = self.compress('compressed.lcov.gz', self.TRACE)
        with open_lcov(plain) as handle:
            self.assertEqual(handle.read(), self.TRACE)
        with open_lcov(compressed) as handle:
            self.assertEqual(handle.read(), self.TRACE)

    def test_parse_lcov_returns_the_same_dictionary_either_way(self):
        plain = parse_lcov(self.write('plain.lcov', self.TRACE))
        compressed = parse_lcov(self.compress('compressed.lcov.gz', self.TRACE))
        self.assertEqual(list(plain), list(compressed))
        for path, coverage in plain.items():
            self.assertEqual(coverage.lines, compressed[path].lines)
            self.assertEqual(coverage.functions, compressed[path].functions)
            self.assertEqual(coverage.branches, compressed[path].branches)

    def test_detection_is_by_magic_so_a_misnamed_gzip_stream_still_parses(self):
        # Traces are archived as build artifacts and renamed on the way through CI. Trusting
        # the extension would make a renamed trace parse as line noise and report no records.
        misnamed = self.compress('current.lcov', self.TRACE)
        self.assertEqual(list(parse_lcov(misnamed)), ['/checkout/Source/WTF/wtf/Vector.h'])

    def test_parse_lcov_source_files_reads_a_gzipped_trace_too(self):
        # The second reader of a trace. It was added after open_lcov's caller, so this is
        # here to fail if a third one is added with a plain open().
        compressed = self.compress('compressed.lcov.gz', self.TRACE)
        self.assertEqual(parse_lcov_source_files(compressed),
                         {'/checkout/Source/WTF/wtf/Vector.h'})

    def test_parse_lcov_source_files_canonicalizes_a_gzipped_trace(self):
        self.write('Source/bmalloc/libpas/src/libpas/pas_alignment.h')
        compressed = self.compress('compressed.lcov.gz', self.TRACE.replace(
            '/checkout/Source/WTF/wtf/Vector.h',
            '/tmp/Build/Release/usr/local/include/bmalloc/pas_alignment.h'))
        self.assertEqual(parse_lcov_source_files(compressed, PathCanonicalizer(self.root)),
                         {self.absolute('Source/bmalloc/libpas/src/libpas/pas_alignment.h')})


if __name__ == '__main__':
    unittest.main()

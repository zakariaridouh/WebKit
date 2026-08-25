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
    PathCanonicalizer, _INSTALLED_HEADER_RULES, compiled_copy_candidates, open_lcov, parse_lcov,
    parse_lcov_source_files, project_totals, third_party_copied_header_ignore_regexes)


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


class GeneratorFixtureTest(_Checkout):
    """A copied framework header is never a generator's expected-output fixture.

    Three were, in the shipped report. The worst said "437 of 446 instrumented lines never
    executed" over 176 rows, on a page under css/scripts/test/TestCSSPropertiesResults, while
    the directory page above it classified that fixture's siblings as "Generator fixture or
    benchmark". One page presented a generator test-fixture directory as product code at 2%.
    """
    WEBCORE_COPY = '/tmp/Build/Release/WebCore.framework/PrivateHeaders/'

    def test_a_generated_header_does_not_land_on_the_css_generators_fixture(self):
        # The real CSSPropertyNames.h is generated into DerivedSources, which is excluded from
        # the report, so the fixture was the only candidate in the checkout and it won.
        self.write('Source/WebCore/css/scripts/test/TestCSSPropertiesResults/CSSPropertyNames.h')
        canonicalizer = PathCanonicalizer(self.root)
        copied = self.WEBCORE_COPY + 'CSSPropertyNames.h'
        self.assertEqual(canonicalizer.canonicalize(copied), copied)
        self.assertEqual(canonicalizer.unresolved_framework_headers, {copied})

    def test_a_generated_header_does_not_land_on_the_bindings_generators_fixture(self):
        # coverage_build_inventory.BuildDescriptionIndex guards against this exact collision
        # from the other side, and names this file in its docstring.
        self.write('Source/WebCore/bindings/scripts/test/JS/JSDOMWindow.h')
        canonicalizer = PathCanonicalizer(self.root)
        copied = self.WEBCORE_COPY + 'JSDOMWindow.h'
        self.assertEqual(canonicalizer.canonicalize(copied), copied)

    def test_the_ipc_generators_fixture_directory_is_pruned_too(self):
        self.write('Source/WebKit/Scripts/webkit/tests/TestWithStreamMessages.h')
        canonicalizer = PathCanonicalizer(self.root)
        copied = '/tmp/Build/Release/WebKit.framework/PrivateHeaders/TestWithStreamMessages.h'
        self.assertEqual(canonicalizer.canonicalize(copied), copied)

    def test_a_real_header_still_wins_over_a_fixture_of_the_same_name(self):
        self.write('Source/WebCore/css/CSSPropertyNames.h')
        self.write('Source/WebCore/css/scripts/test/TestCSSPropertiesResults/CSSPropertyNames.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.WEBCORE_COPY + 'CSSPropertyNames.h'),
                         self.absolute('Source/WebCore/css/CSSPropertyNames.h'))

    def test_a_directory_merely_called_testing_is_product_code_and_is_kept(self):
        # Source/WebCore/testing compiles into WebCoreTestSupport, exactly as
        # coverage_build_inventory's own fixture list notes.
        self.write('Source/WebCore/testing/Internals.h')
        canonicalizer = PathCanonicalizer(self.root)
        self.assertEqual(canonicalizer.canonicalize(self.WEBCORE_COPY + 'Internals.h'),
                         self.absolute('Source/WebCore/testing/Internals.h'))


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


class CompiledCopyCandidateTest(_Checkout):
    """Where the text that was actually compiled is, given the path it is reported under.

    canonicalize() names the checkout path, which is the right path to report a copied header
    under and the wrong text to render it from: the copy was made once, at build time. On the
    shipped report Source/WTF/wtf/Expected.h had been rewritten from 403 lines to 31, and the
    403-line text the profile describes was still in <build>/usr/local/include/wtf/Expected.h.
    """
    BUILD = '/tmp/Build/Release'

    def candidates(self, path):
        return list(compiled_copy_candidates(path, self.BUILD))

    def test_a_wtf_header_maps_to_the_installed_copy(self):
        self.assertIn(self.BUILD + '/usr/local/include/wtf/Expected.h',
                      self.candidates('/checkout/Source/WTF/wtf/Expected.h'))

    def test_a_wtf_subdirectory_is_kept(self):
        self.assertIn(self.BUILD + '/usr/local/include/wtf/text/AtomString.h',
                      self.candidates('/checkout/Source/WTF/wtf/text/AtomString.h'))

    def test_a_libpas_header_is_tried_under_both_names_the_copy_phase_uses(self):
        # The copy phase flattens libpas into bmalloc/, so that is where it is found in
        # practice; pas/ is in the table for the day that changes.
        candidates = self.candidates('/checkout/Source/bmalloc/libpas/src/libpas/pas_utils.h')
        self.assertIn(self.BUILD + '/usr/local/include/bmalloc/pas_utils.h', candidates)
        self.assertIn(self.BUILD + '/usr/local/include/pas/pas_utils.h', candidates)

    def test_a_framework_header_is_found_by_basename_in_both_header_directories(self):
        candidates = self.candidates('/checkout/Source/WebCore/css/CSSPropertyNames.h')
        self.assertEqual(candidates[:2],
                         [self.BUILD + '/WebCore.framework/PrivateHeaders/CSSPropertyNames.h',
                          self.BUILD + '/WebCore.framework/Headers/CSSPropertyNames.h'])

    def test_an_implementation_file_has_no_copy_because_nothing_copies_one(self):
        self.assertEqual(self.candidates('/checkout/Source/WebCore/dom/Document.cpp'), [])

    def test_the_installed_rule_is_tried_before_the_framework_rule(self):
        # A PAL header is under Source/WebCore, so both rules match it, and the installed one
        # is the location PAL's own copy phase writes to.
        candidates = self.candidates('/checkout/Source/WebCore/PAL/pal/text/TextEncoding.h')
        self.assertEqual(candidates[0], self.BUILD + '/usr/local/include/pal/text/TextEncoding.h')

    def test_nothing_is_offered_without_a_build_directory(self):
        self.assertEqual(list(compiled_copy_candidates('/checkout/Source/WTF/wtf/Vector.h', None)),
                         [])

    def test_the_two_directions_are_derived_from_one_table(self):
        # Every installed-header rule canonicalize() knows about has an inverse here, so the
        # two cannot drift apart the day a copy phase changes.
        for location, sources in _INSTALLED_HEADER_RULES:
            for source in sources:
                self.assertIn(self.BUILD + location + 'Probe.h',
                              self.candidates('/checkout/' + source + 'Probe.h'))


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
        self.assertEqual(coverage.function_lines, {12: 3, 20: 0})
        self.assertEqual(coverage.branches, {('12', '0', '0'): 3, ('12', '0', '1'): 0})
        self.assertEqual(coverage.totals()['lines'], (2, 1))

    def test_duplicate_records_for_one_path_are_unioned_line_by_line(self):
        path = self.write('trace.lcov', self.TRACE + self.TRACE.replace('DA:13,0', 'DA:13,9'))
        files = parse_lcov(path)
        self.assertEqual(files['/checkout/Source/WTF/wtf/Vector.h'].lines, {12: 3, 13: 9})


class FunctionMetricTest(_Checkout):
    """llvm-cov merges a template's instantiations and lcov does not.

    lcov emits one FN:/FNDA: pair per instantiation and then an FNF:/FNH: pair counting the
    distinct start lines, so keying by mangled name counted a Vector<T> method once per
    instantiation. Measured over the shipped trace: 1,978,649 functions against llvm-cov's
    255,297, a 7.75x denominator, and 55.06% where summary.txt in the same output directory
    said 72.09%. --fail-under-functions=70 failed a build llvm-cov called 72.09%.
    """
    # The first record of the shipped trace, verbatim, shortened. Four instantiations at two
    # start lines; llvm-cov's own FNF:/FNH: for it are 2 and 1.
    TRACE = ('SF:/checkout/Source/JavaScriptCore/API/APICallbackFunction.h\n'
             'FN:46,_ZN3JSC19APICallbackFunction8callImplINS_20ObjCCallbackFunctionEEEx\n'
             'FN:86,_ZN3JSC19APICallbackFunction13constructImplINS_20ObjCCallbackFunctionEEEx\n'
             'FN:86,_ZN3JSC19APICallbackFunction13constructImplINS_21JSCallbackConstructorEEEx\n'
             'FN:46,_ZN3JSC19APICallbackFunction8callImplINS_18JSCallbackFunctionEEEx\n'
             'FNDA:191,_ZN3JSC19APICallbackFunction8callImplINS_20ObjCCallbackFunctionEEEx\n'
             'FNDA:0,_ZN3JSC19APICallbackFunction13constructImplINS_20ObjCCallbackFunctionEEEx\n'
             'FNDA:0,_ZN3JSC19APICallbackFunction13constructImplINS_21JSCallbackConstructorEEEx\n'
             'FNDA:837570,_ZN3JSC19APICallbackFunction8callImplINS_18JSCallbackFunctionEEEx\n'
             'FNF:2\n'
             'FNH:1\n'
             'DA:46,837761\n'
             'end_of_record\n')

    def coverage(self, trace=None):
        files = parse_lcov(self.write('trace.lcov', trace or self.TRACE))
        return files[list(files)[0]]

    def test_the_count_reproduces_llvm_covs_own_fnf_and_fnh(self):
        # Which is the whole point: the trace states them two lines down.
        self.assertEqual(self.coverage().totals()['functions'], (2, 1))

    def test_the_mangled_names_are_all_still_there_for_the_delta_tool(self):
        # coverage_delta compares the two sides by name, because a mangled name survives a
        # source edit and a line number does not.
        self.assertEqual(len(self.coverage().functions), 4)

    def test_a_start_line_is_covered_if_any_instantiation_at_it_ran(self):
        coverage = self.coverage()
        self.assertEqual(coverage.function_lines[46], 837570)
        self.assertEqual(coverage.function_lines[86], 0)

    def test_an_fnda_before_its_fn_is_still_folded_in(self):
        # Nothing in the format says llvm-cov emits every FN: first, though it does today.
        reordered = ('SF:/checkout/a.cpp\n'
                     'FNDA:7,_Z1fv\n'
                     'FN:3,_Z1fv\n'
                     'end_of_record\n')
        self.assertEqual(self.coverage(reordered).function_lines, {3: 7})

    def test_an_fnda_with_no_fn_is_counted_by_name_but_not_by_line(self):
        # It has no start line to be counted at, and inventing one would invent a function.
        orphan = 'SF:/checkout/a.cpp\nFNDA:7,_Z1fv\nend_of_record\n'
        coverage = self.coverage(orphan)
        self.assertEqual(coverage.functions, {'_Z1fv': 7})
        self.assertEqual(coverage.function_lines, {})

    def test_duplicate_records_for_one_file_are_unioned_by_start_line(self):
        # A copied header's two records instantiate different templates, so they carry
        # different mangled names at the same start lines. Summing would double-count.
        second = self.TRACE.replace('FNDA:0,_ZN3JSC19APICallbackFunction13constructImplINS_'
                                    '20ObjCCallbackFunctionEEEx',
                                    'FNDA:5,_ZN3JSC19APICallbackFunction13constructImplINS_'
                                    '20ObjCCallbackFunctionEEEx')
        coverage = self.coverage(self.TRACE + second)
        self.assertEqual(coverage.totals()['functions'], (2, 2))

    def test_an_unparsable_start_line_does_not_lose_the_function_by_name(self):
        broken = 'SF:/checkout/a.cpp\nFN:not a number,_Z1fv\nFNDA:2,_Z1fv\nend_of_record\n'
        coverage = self.coverage(broken)
        self.assertEqual(coverage.functions, {'_Z1fv': 2})
        self.assertEqual(coverage.function_lines, {})

    def test_lines_only_carries_no_function_data_at_all(self):
        files = parse_lcov(self.write('trace.lcov', self.TRACE), lines_only=True)
        coverage = files[list(files)[0]]
        self.assertEqual((coverage.functions, coverage.function_lines), ({}, {}))

    def test_project_totals_counts_functions_the_same_way(self):
        totals = project_totals({'a': self.coverage()})
        self.assertEqual(totals['functions'], (2, 1))


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

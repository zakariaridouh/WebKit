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

import logging
import os
import shutil
import tempfile
import unittest

from webkitpy.coverage_build_inventory import (
    AbsenceReport, AbsentFile, BuildDescriptionIndex, BuildInventory, REASON_ORDER,
    REPORTED_TARGETS, enumerate_source_files, find_absent_files, physical_line_count,
    whole_file_conditional)


class _Tree(unittest.TestCase):
    """A throwaway checkout and build directory, written file by file."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # These modules log a summary line per run, which is useful in a report and noise
        # in a test.
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

    def write(self, relative, contents=''):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path

    def absolute(self, relative):
        return os.path.join(self.root, relative)


class WholeFileConditionalTest(_Tree):
    def test_body_wrapped_in_one_conditional_names_the_flag(self):
        # This is the shape that makes 515 files invisible on the measured build: the file
        # compiles, but to nothing, so llvm-cov has no record of it to report at 0%.
        path = self.write('Guarded.cpp', '\n'.join([
            '// Copyright.',
            '#include "config.h"',
            '#include "Guarded.h"',
            '',
            '#if ENABLE(WEBXR)',
            'namespace WebCore {',
            'void f() { }',
            '}',
            '#endif // ENABLE(WEBXR)',
            '']))
        self.assertEqual(whole_file_conditional(path), 'ENABLE(WEBXR)')

    def test_code_outside_the_conditional_is_not_a_whole_file_guard(self):
        path = self.write('Partly.cpp', '\n'.join([
            '#include "config.h"',
            '#if ENABLE(WEBXR)',
            'void f() { }',
            '#endif',
            'void g() { }',
            '']))
        self.assertIsNone(whole_file_conditional(path))

    def test_nested_conditionals_do_not_confuse_the_outer_one(self):
        path = self.write('Nested.cpp', '\n'.join([
            '#include "config.h"',
            '#if PLATFORM(IOS_FAMILY)',
            '#if HAVE(PEPPER_UI_CORE)',
            'void f() { }',
            '#endif',
            'void g() { }',
            '#endif',
            '']))
        self.assertEqual(whole_file_conditional(path), 'PLATFORM(IOS_FAMILY)')

    def test_an_else_branch_disqualifies_the_conditional(self):
        # #if USE(X) ... #else ... #endif always compiles to something, so it cannot be
        # why the file is missing, however much it looks like a guard.
        path = self.write('Either.cpp', '\n'.join([
            '#include "config.h"',
            '#if USE(SKIA)',
            'void f() { }',
            '#else',
            'void f() { }',
            '#endif',
            '']))
        self.assertIsNone(whole_file_conditional(path))

    def test_ifdef_and_ifndef_are_reported_as_defined(self):
        self.assertEqual(
            whole_file_conditional(self.write('A.cpp', '#ifdef LIBPAS_ENABLED\nx;\n#endif\n')),
            'defined(LIBPAS_ENABLED)')
        self.assertEqual(
            whole_file_conditional(self.write('B.cpp', '#ifndef NDEBUG\nx;\n#endif\n')),
            '!defined(NDEBUG)')

    def test_the_largest_top_level_region_is_the_one_holding_the_body(self):
        # WebKit files often guard their includes separately from their body.
        path = self.write('TwoRegions.cpp', '\n'.join([
            '#include "config.h"',
            '#if PLATFORM(COCOA)',
            '#include <Cocoa/Cocoa.h>',
            '#endif',
            '#if ENABLE(MODEL_PROCESS)',
            'void a() { }',
            'void b() { }',
            'void c() { }',
            '#endif',
            '']))
        self.assertEqual(whole_file_conditional(path), 'ENABLE(MODEL_PROCESS)')

    def test_a_trailing_comment_is_not_part_of_the_condition(self):
        path = self.write('Commented.cpp', '#if ENABLE(MHTML) // rdar://1\nx;\n#endif\n')
        self.assertEqual(whole_file_conditional(path), 'ENABLE(MHTML)')

    def test_a_file_with_no_conditional_has_none(self):
        self.assertIsNone(whole_file_conditional(self.write('Plain.cpp', 'void f() { }\n')))

    def test_a_missing_file_is_not_an_error(self):
        self.assertIsNone(whole_file_conditional(self.absolute('Nope.cpp')))


class PhysicalLineCountTest(_Tree):
    def test_counts_the_last_line_without_a_trailing_newline(self):
        self.assertEqual(physical_line_count(self.write('A.cpp', 'a\nb\nc')), 3)

    def test_counts_the_same_file_with_a_trailing_newline(self):
        self.assertEqual(physical_line_count(self.write('B.cpp', 'a\nb\nc\n')), 3)

    def test_an_empty_or_missing_file_is_zero(self):
        self.assertEqual(physical_line_count(self.write('C.cpp', '')), 0)
        self.assertEqual(physical_line_count(self.absolute('D.cpp')), 0)


class EnumerateSourceFilesTest(_Tree):
    def test_finds_implementation_files_and_skips_headers_and_derived_sources(self):
        self.write('Source/WebCore/dom/Node.cpp')
        self.write('Source/WebCore/dom/Node.h')
        self.write('Source/WebKit/Shared/Cocoa/Thing.mm')
        self.write('Source/WebKit/SwiftThing.swift')
        self.write('Source/WebCore/DerivedSources/JSNode.cpp')
        self.assertEqual(sorted(enumerate_source_files(self.root)), [
            'Source/WebCore/dom/Node.cpp',
            'Source/WebKit/Shared/Cocoa/Thing.mm',
            'Source/WebKit/SwiftThing.swift',
        ])

    def test_source_thirdparty_is_not_first_party_and_is_not_in_the_denominator(self):
        # 12,538 files of vendored code. Counting them would make the headline meaningless
        # in the other direction.
        self.write('Source/WTF/wtf/Vector.cpp')
        self.write('Source/ThirdParty/libwebrtc/Source/webrtc/thing.cc')
        self.assertEqual(enumerate_source_files(self.root), ['Source/WTF/wtf/Vector.cpp'])


class BuildInventoryTest(_Tree):
    def _depfile(self, target, name, prerequisites):
        self.write(
            'WebKitBuild/{0}.build/Release/{0}.build/Objects-normal/arm64e/{1}.d'.format(target, name),
            'dependencies: \\\n' + ''.join('  {} \\\n'.format(p) for p in prerequisites))

    def test_a_unified_bundles_members_are_all_recorded_as_compiled(self):
        self._depfile('WebCore', 'UnifiedSource100', [
            self.absolute('WebKitBuild/Release/DerivedSources/WebCore/unified-sources/UnifiedSource100.cpp'),
            self.absolute('Source/WebCore/dom/Node.cpp'),
            self.absolute('Source/WebCore/dom/Element.cpp'),
            self.absolute('Source/WebCore/dom/Node.h'),
        ])
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Release'))
        self.assertEqual(inventory.compiled,
                         frozenset(('Source/WebCore/dom/Node.cpp', 'Source/WebCore/dom/Element.cpp')))
        self.assertEqual(inventory.targets('Source/WebCore/dom/Node.cpp'), frozenset(('WebCore',)))

    def test_non_unified_translation_units_are_found_too(self):
        # WTF, bmalloc, PAL and WebGPU have no unified-sources directory at all on the Xcode
        # build. Reading only the bundles reported 587 compiled files as never built.
        self._depfile('WTF', 'ASCIICType', [self.absolute('Source/WTF/wtf/ASCIICType.cpp')])
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Release'))
        self.assertEqual(inventory.compiled, frozenset(('Source/WTF/wtf/ASCIICType.cpp',)))
        self.assertEqual(inventory.targets('Source/WTF/wtf/ASCIICType.cpp'), frozenset(('WTF',)))

    def test_unified_bundles_are_read_even_with_no_dependency_files(self):
        # The bundles live in the product directory rather than the intermediates, so they
        # are the half of the record that survives the intermediates being pruned.
        self.write('Source/WebCore/dom/Node.cpp')
        self.write('WebKitBuild/Release/DerivedSources/WebCore/unified-sources/UnifiedSource1.cpp',
                   '#include "dom/Node.cpp"\n')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Release'))
        self.assertEqual(inventory.bundle_count, 1)
        self.assertEqual(inventory.compiled, frozenset(('Source/WebCore/dom/Node.cpp',)))

    def test_a_build_directory_that_does_not_exist_yields_nothing_rather_than_raising(self):
        inventory = BuildInventory(self.root, self.absolute('NoSuchBuild/Release'))
        self.assertEqual(inventory.compiled, frozenset())
        self.assertEqual(inventory.dependency_file_count, 0)

    def test_a_sibling_configuration_is_not_credited_to_this_one(self):
        # The walk used to take os.path.dirname(build_directory) wholesale. On a CMake tree
        # that is WebKitBuild/cmake-mac, so Debug/ and Release/ sit beside Coverage/ and a
        # file only an older configuration ever compiled was reported as compiled here --
        # which is precisely the state the third state exists to make visible.
        self.write('Source/WebCore/dom/OnlyInDebug.cpp')
        self.write('WebKitBuild/cmake-mac/Debug/Source/WebCore/CMakeFiles/WebCore.dir/'
                   'OnlyInDebug.cpp.d',
                   'x: \\\n  {} \\\n'.format(self.absolute('Source/WebCore/dom/OnlyInDebug.cpp')))
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/cmake-mac/Coverage'))
        self.assertEqual(inventory.compiled, frozenset())

    def test_xcodes_intermediates_beside_the_configuration_are_still_read(self):
        # They have to be: on the reference instrumented tree every dependency file is under
        # <output>/<Target>.build/<Configuration>/ and none under <Configuration>/, so
        # restricting the walk to the configuration directory alone would find nothing at all.
        self._depfile('WTF', 'ASCIICType', [self.absolute('Source/WTF/wtf/ASCIICType.cpp')])
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Release'))
        self.assertEqual(inventory.compiled, frozenset(('Source/WTF/wtf/ASCIICType.cpp',)))


class CMakeBuildInventoryTest(_Tree):
    """CMake's layout: no surviving depfiles, transposed bundles, objects as the evidence."""

    def _object(self, binary_directory, target, mangled_source):
        return self.write('{}/CMakeFiles/{}.dir/{}.o'.format(
            binary_directory, target, mangled_source))

    def test_an_object_file_proves_its_source_was_compiled(self):
        # ninja pairs depfile with deps = gcc, so it folds every .o.d into .ninja_deps and
        # deletes it: 4,718 objects against five .d files on a complete tree. The objects are
        # what is left, and unlike compile_commands.json an object proves a translation unit
        # was compiled rather than merely configured.
        self.write('Source/WebCore/dom/Node.cpp')
        self._object('WebKitBuild/Coverage/Source/WebCore', 'WebCore', 'dom/Node.cpp')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Coverage'))
        self.assertEqual(inventory.object_file_count, 1)
        self.assertEqual(inventory.compiled, frozenset(('Source/WebCore/dom/Node.cpp',)))
        self.assertEqual(inventory.targets('Source/WebCore/dom/Node.cpp'),
                         frozenset(('WebCore',)))

    def test_the_cmake_target_directory_names_the_target(self):
        # _target_of only knew Xcode's <Target>.build, so every CMake object was attributed
        # to the empty target name -- which is in no REPORTED_TARGETS, so the file would have
        # been labelled as compiled only into a binary the report excludes.
        self.write('Source/WebCore/style/StyleResolver.cpp')
        self._object('WebKitBuild/Coverage/Source/WebCore', 'WebCoreStyle',
                     'style/StyleResolver.cpp')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Coverage'))
        self.assertEqual(inventory.targets('Source/WebCore/style/StyleResolver.cpp'),
                         frozenset(('WebCoreStyle',)))

    def test_a_generated_source_resolves_against_the_binary_directory(self):
        # CMake names an object after the source path relative to the target's source
        # directory, or to its binary directory when the source is generated, writing each
        # '..' as '__'. A generated source is not checked-in code, so it is not recorded --
        # but it must not be misresolved onto a same-named file in the checkout either.
        self.write('WebKitBuild/Coverage/WebCore/DerivedSources/unified-sources/'
                   'UnifiedSource-dom-1.cpp', '')
        self._object('WebKitBuild/Coverage/Source/WebCore', 'WebCore',
                     '__/__/WebCore/DerivedSources/unified-sources/UnifiedSource-dom-1.cpp')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Coverage'))
        self.assertEqual(inventory.object_file_count, 1)
        self.assertEqual(inventory.compiled, frozenset())

    def test_the_transposed_unified_sources_layout_is_read(self):
        # CMake puts the bundles at <build>/<Component>/DerivedSources/unified-sources, not
        # <build>/DerivedSources/<Component>/unified-sources. Only the second was recognised,
        # so all 216 bundles on a complete CMake tree were invisible; with the depfiles gone
        # too, both signals were zero and the report dropped its third state entirely.
        self.write('Source/WebCore/dom/Node.cpp')
        self.write('WebKitBuild/Coverage/WebCore/DerivedSources/unified-sources/'
                   'UnifiedSource-dom-1.cpp', '#include "dom/Node.cpp"\n')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Coverage'))
        self.assertEqual(inventory.bundle_count, 1)
        self.assertEqual(inventory.compiled, frozenset(('Source/WebCore/dom/Node.cpp',)))

    def test_both_unified_sources_layouts_coexist_on_one_tree(self):
        # They really do: CMake writes TestWebKit's bundles in the Xcode shape and every
        # framework's in its own.
        self.write('Source/WebCore/dom/Node.cpp')
        self.write('Tools/TestWebKitAPI/Helpers/cocoa/DaemonTestUtilities.mm')
        self.write('WebKitBuild/Coverage/WebCore/DerivedSources/unified-sources/'
                   'UnifiedSource-dom-1.cpp', '#include "dom/Node.cpp"\n')
        self.write('WebKitBuild/Coverage/DerivedSources/TestWebKit/unified-sources/'
                   'UnifiedSource-Helpers-1-nonARC.mm',
                   '#include "Helpers/cocoa/DaemonTestUtilities.mm"\n')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Coverage'))
        self.assertEqual(inventory.bundle_count, 2)
        self.assertEqual(inventory.compiled, frozenset((
            'Source/WebCore/dom/Node.cpp',
            'Tools/TestWebKitAPI/Helpers/cocoa/DaemonTestUtilities.mm')))
        self.assertEqual(
            inventory.targets('Tools/TestWebKitAPI/Helpers/cocoa/DaemonTestUtilities.mm'),
            frozenset(('TestWebKit',)))

    def test_pal_and_webgpu_members_resolve_to_their_real_source_directories(self):
        # The same framework name means a different directory on the two build systems, which
        # is why the candidates are tried rather than fixed: CMake's WebGPU bundle holds a
        # bare Adapter.mm under Source/WebGPU/WebGPU, and its PAL bundle holds
        # avfoundation/OutputContext.mm under Source/WebCore/PAL/pal.
        self.write('Source/WebGPU/WebGPU/Adapter.mm')
        self.write('Source/WebCore/PAL/pal/avfoundation/OutputContext.mm')
        self.write('WebKitBuild/Coverage/WebGPU/DerivedSources/unified-sources/'
                   'UnifiedSource-root-1-ARC.mm', '#include "Adapter.mm"\n')
        self.write('WebKitBuild/Coverage/PAL/DerivedSources/unified-sources/'
                   'UnifiedSource-avfoundation-1-nonARC.mm',
                   '#include "avfoundation/OutputContext.mm"\n')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Coverage'))
        self.assertEqual(inventory.compiled, frozenset((
            'Source/WebGPU/WebGPU/Adapter.mm',
            'Source/WebCore/PAL/pal/avfoundation/OutputContext.mm')))

    def test_a_member_that_matches_no_candidate_directory_is_not_invented(self):
        # Requiring the file to exist is what keeps a wrong candidate from being recorded as
        # a compiled file that the report would then not have to account for.
        self.write('WebKitBuild/Coverage/WebGPU/DerivedSources/unified-sources/'
                   'UnifiedSource-root-1-ARC.mm', '#include "NoSuchThing.mm"\n')
        inventory = BuildInventory(self.root, self.absolute('WebKitBuild/Coverage'))
        self.assertEqual(inventory.bundle_count, 1)
        self.assertEqual(inventory.compiled, frozenset())

    def test_cmake_targets_that_link_into_a_framework_count_as_reported(self):
        # CMake splits each framework across several targets -- WebCore alone has WebCore,
        # WebCoreDOMAndRendering, WebCoreStyle, WebCoreJSBindings, WebCoreInspector,
        # WebCoreAVFoundation and PAL -- and links them together. If those names are not
        # recognised, every file they compile is reported as being only in a binary the
        # report excludes. The list is the transitive closure of the CMakeFiles/<T>.dir
        # directories reachable from the five framework link edges in build.ninja.
        for target in ('WebCoreDOMAndRendering', 'WebCoreStyle', 'WebCoreJSBindings',
                       'WebCoreInspector', 'WebCoreAVFoundation', 'PAL_SwiftInterop',
                       'JavaScriptCoreJIT', 'LowLevelInterpreterLib', 'WGSLCore',
                       'WebGPU_SwiftInterop', 'WebKitARC', 'WebKitShared', 'WebKitUIProcess',
                       'WebKitWebProcess', 'WebKitGPUProcess', 'WebKitNetworkProcess',
                       'WebKit_SwiftInterop'):
            self.assertIn(target, REPORTED_TARGETS)

    def test_test_and_tool_targets_are_still_excluded(self):
        for target in ('TestWebKit', 'TestWTF', 'WebKitTestRunner', 'jsc', 'wgslc',
                       'MiniBrowser', 'WebCoreTestSupport'):
            self.assertNotIn(target, REPORTED_TARGETS)


class BuildDescriptionIndexTest(_Tree):
    def test_a_ports_source_list_is_recognized_from_its_name(self):
        self.assertEqual(BuildDescriptionIndex.named_port('Source/WebCore/SourcesGTK.txt'), 'GTK')
        self.assertEqual(BuildDescriptionIndex.named_port('Source/WebCore/PlatformWin.cmake'),
                         'Windows')
        self.assertEqual(BuildDescriptionIndex.named_port('Source/WebCore/SourcesCocoa.txt'), '')
        self.assertEqual(BuildDescriptionIndex.named_port('Source/WebCore/Sources.txt'), '')
        self.assertIsNone(BuildDescriptionIndex.named_port('Source/WebCore/CMakeLists.txt'))

    def test_paths_resolve_against_the_descriptions_ancestors_not_by_basename(self):
        # Source/WebCore/Sources.txt lists a bare JSDOMWindow.cpp, which is the *derived*
        # one. Matching on basename alone would have attached it to the unrelated
        # bindings/scripts/test fixture of the same name, and marked that fixture as being
        # in the Cocoa build.
        self.write('Source/WebCore/Sources.txt', 'JSDOMWindow.cpp\ndom/Node.cpp\n')
        self.write('Source/WebCore/bindings/scripts/test/JS/JSDOMWindow.cpp')
        self.write('Source/WebCore/dom/Node.cpp')
        index = BuildDescriptionIndex(self.root, enumerate_source_files(self.root))
        self.assertIsNone(index.ports('Source/WebCore/bindings/scripts/test/JS/JSDOMWindow.cpp'))
        self.assertEqual(index.ports('Source/WebCore/dom/Node.cpp'), {''})

    def test_cmake_variables_are_stripped_before_resolving(self):
        self.write('Source/WebCore/CMakeLists.txt', '    ${WEBCORE_DIR}/dom/Touch.cpp\n')
        self.write('Source/WebCore/dom/Touch.cpp')
        index = BuildDescriptionIndex(self.root, enumerate_source_files(self.root))
        self.assertEqual(index.ports('Source/WebCore/dom/Touch.cpp'), {''})

    def test_a_directory_with_only_other_ports_lists_makes_its_shared_list_theirs_too(self):
        # Source/WebDriver has PlatformGTK.cmake and PlatformWPE.cmake but no Cocoa list, so
        # its plain CMakeLists.txt is a GTK/WPE list, and its nine files belong to those
        # ports rather than to no configuration in particular.
        self.write('Source/WebDriver/CMakeLists.txt', '    Session.cpp\n')
        self.write('Source/WebDriver/PlatformGTK.cmake', '')
        self.write('Source/WebDriver/PlatformWPE.cmake', '')
        self.write('Source/WebDriver/Session.cpp')
        index = BuildDescriptionIndex(self.root, enumerate_source_files(self.root))
        self.assertEqual(index.ports('Source/WebDriver/Session.cpp'), {'GTK', 'WPE'})

    def test_a_shared_list_beside_a_cocoa_list_stays_shared(self):
        self.write('Source/WebCore/CMakeLists.txt', '    dom/Node.cpp\n')
        self.write('Source/WebCore/SourcesCocoa.txt', '')
        self.write('Source/WebCore/PlatformGTK.cmake', '')
        self.write('Source/WebCore/dom/Node.cpp')
        index = BuildDescriptionIndex(self.root, enumerate_source_files(self.root))
        self.assertEqual(index.ports('Source/WebCore/dom/Node.cpp'), {''})

    def test_a_cmake_file_named_after_neither_a_port_nor_a_list_is_still_read(self):
        # Source/WebCore/platform/ImageDecoders.cmake is the only thing in the checkout that
        # names the eight non-Cocoa image decoders. Reading only Platform*.cmake reported
        # them as being in no build description at all.
        self.write('Source/WebCore/platform/ImageDecoders.cmake',
                   '    platform/image-decoders/gif/GIFImageDecoder.cpp\n')
        self.write('Source/WebCore/platform/image-decoders/gif/GIFImageDecoder.cpp')
        index = BuildDescriptionIndex(self.root, enumerate_source_files(self.root))
        self.assertEqual(
            index.descriptions('Source/WebCore/platform/image-decoders/gif/GIFImageDecoder.cpp'),
            ['Source/WebCore/platform/ImageDecoders.cmake'])


class FindAbsentFilesTest(_Tree):
    """One synthetic checkout with one file per reason, classified end to end."""

    def setUp(self):
        super().setUp()
        self.write('Source/WebCore/Sources.txt',
                   'dom/Node.cpp\ndom/Touch.cpp\nplatform/Data.cpp\n')
        self.write('Source/WebCore/SourcesGTK.txt', 'platform/gtk/ThingGtk.cpp\n')

        self.write('Source/WebCore/dom/Node.cpp', 'void f() { }\n')
        self.write('Source/WebCore/dom/Touch.cpp',
                   '#include "config.h"\n#if ENABLE(TOUCH_EVENTS)\nvoid f() { }\n#endif\n')
        self.write('Source/WebCore/platform/Data.cpp',
                   '#include "config.h"\nconst int table[] = { 1, 2 };\n')
        self.write('Source/WebCore/platform/gtk/ThingGtk.cpp', 'void f() { }\n')
        self.write('Source/WebCore/testing/Internals.cpp', 'void f() { }\n')
        self.write('Source/WebCore/bindings/scripts/test/JS/JSTestObj.cpp', 'void f() { }\n')
        self.write('Source/WebCore/PAL/ThirdParty/dav1d/src/lib.c', 'void f() { }\n')
        self.write('Source/WebCore/Orphan.cpp', 'void f() { }\n')

        compiled = {
            'WebCore': ['Source/WebCore/dom/Node.cpp',
                        'Source/WebCore/dom/Touch.cpp',
                        'Source/WebCore/platform/Data.cpp'],
            'WebCoreTestSupport': ['Source/WebCore/testing/Internals.cpp'],
            'dav1d': ['Source/WebCore/PAL/ThirdParty/dav1d/src/lib.c'],
        }
        for target, files in compiled.items():
            self.write('WebKitBuild/{0}.build/Release/{0}.build/Objects-normal/arm64e/x.d'.format(target),
                       'dependencies: \\\n' + ''.join(
                           '  {} \\\n'.format(self.absolute(f)) for f in files))

        self.report = find_absent_files(
            self.root, self.absolute('WebKitBuild/Release'),
            {self.absolute('Source/WebCore/dom/Node.cpp')},
            third_party_regexes=('/ThirdParty/',))
        self.reasons = {absent.path: (absent.reason, absent.detail) for absent in self.report.files}

    def test_a_reported_file_is_not_absent(self):
        self.assertNotIn('Source/WebCore/dom/Node.cpp', self.reasons)
        self.assertEqual(self.report.reported_file_count, 1)
        self.assertEqual(self.report.total_file_count, 8)
        self.assertEqual(self.report.absent_file_count, 7)

    def test_a_file_compiled_to_nothing_names_the_flag_that_removed_it(self):
        self.assertEqual(self.reasons['Source/WebCore/dom/Touch.cpp'],
                         ('feature-flag-off', 'ENABLE(TOUCH_EVENTS)'))

    def test_a_compiled_file_with_no_mapping_and_no_flag_is_not_given_a_zero_percent_row(self):
        # 135 files on the measured build. Synthesizing 0% for a lookup table would invent a
        # denominator, so it gets a reason of its own instead.
        self.assertEqual(self.reasons['Source/WebCore/platform/Data.cpp'],
                         ('no-executable-code', 'WebCore'))

    def test_another_ports_file_is_attributed_to_that_port(self):
        self.assertEqual(self.reasons['Source/WebCore/platform/gtk/ThingGtk.cpp'],
                         ('other-port', 'GTK'))

    def test_a_file_only_in_an_excluded_binary_says_which_binary(self):
        self.assertEqual(self.reasons['Source/WebCore/testing/Internals.cpp'],
                         ('test-or-tool-target', 'WebCoreTestSupport'))

    def test_a_generator_fixture_is_labelled_as_one(self):
        self.assertEqual(self.reasons['Source/WebCore/bindings/scripts/test/JS/JSTestObj.cpp'][0],
                         'fixture')

    def test_vendored_third_party_is_labelled_as_filtered_rather_than_missing(self):
        self.assertEqual(self.reasons['Source/WebCore/PAL/ThirdParty/dav1d/src/lib.c'],
                         ('third-party', ''))

    def test_a_file_no_description_names_is_reported_as_such(self):
        self.assertEqual(self.reasons['Source/WebCore/Orphan.cpp'], ('no-build-description', ''))

    def test_every_reason_used_is_one_the_report_knows_how_to_label(self):
        for reason, _ in self.reasons.values():
            self.assertIn(reason, REASON_ORDER)

    def test_the_denominator_sentence_carries_both_numbers(self):
        sentence = self.report.denominator_sentence()
        self.assertIn('1 of 8', sentence)
        self.assertIn('7', sentence)

    def test_reasons_are_returned_in_a_fixed_order_with_counts(self):
        order = [row[0] for row in self.report.reasons()]
        self.assertEqual(order, [r for r in REASON_ORDER if r in order])
        self.assertEqual(sum(row[2] for row in self.report.reasons()),
                         self.report.absent_file_count)


class AbsenceReportTest(unittest.TestCase):
    def test_files_are_grouped_by_directory_for_the_per_directory_pages(self):
        report = AbsenceReport()
        report.add(AbsentFile('Source/WebCore/dom/Touch.cpp', 'feature-flag-off', 'X', 10))
        report.add(AbsentFile('Source/WebCore/dom/TouchList.cpp', 'feature-flag-off', 'X', 5))
        report.add(AbsentFile('Source/WTF/wtf/A.cpp', 'no-executable-code', 'WTF', 3))
        self.assertEqual(len(report.by_directory['Source/WebCore/dom']), 2)
        self.assertEqual(report.absent_physical_lines, 18)

    def test_an_empty_report_has_no_denominator_sentence_to_offer(self):
        self.assertEqual(AbsenceReport().denominator_sentence(), '')


class DirectoryIndexThirdStateTest(_Tree):
    def _write_index(self):
        from webkitpy.coverage_directory_index import write_directory_index
        lcov = self.write('coverage.lcov', '\n'.join([
            'SF:{}'.format(self.absolute('Source/WebCore/dom/Node.cpp')),
            'DA:1,1', 'DA:2,0', 'end_of_record', '']))
        absence = AbsenceReport()
        absence.total_file_count = 3
        absence.reported_file_count = 1
        absence.compiled_file_count = 2
        absence.add(AbsentFile('Source/WebCore/dom/Touch.cpp', 'feature-flag-off',
                               'ENABLE(TOUCH_EVENTS)', 97))
        absence.add(AbsentFile('Source/WebCore/platform/gtk/ThingGtk.cpp', 'other-port', 'GTK', 184))
        output = self.absolute('report')
        write_directory_index(lcov, output, source_root=self.root, absence=absence)
        return output

    def test_a_directory_holding_only_not_built_files_still_gets_a_page(self):
        output = self._write_index()
        page = os.path.join(output, 'Source', 'WebCore', 'platform', 'gtk', 'index.html')
        self.assertTrue(os.path.exists(page))
        with open(page) as handle:
            contents = handle.read()
        self.assertIn('ThingGtk.cpp', contents)
        self.assertIn('Another port only', contents)

    def test_not_built_files_do_not_change_any_percentage(self):
        # The whole point: a file with no coverage mapping has no denominator, so it must not
        # appear in the coverage table and must not move the 50% Node.cpp reports.
        output = self._write_index()
        with open(os.path.join(output, 'Source', 'WebCore', 'dom', 'index.html')) as handle:
            contents = handle.read()
        coverage_table, _, absent_table = contents.partition('<h2>')
        self.assertIn('>50.00%<', coverage_table)
        self.assertNotIn('>33.33%<', coverage_table)
        self.assertNotIn('>0.00%<', coverage_table)
        self.assertNotIn('Touch.cpp', coverage_table)
        self.assertIn('Touch.cpp', absent_table)

    def test_the_root_page_states_the_denominator_the_percentages_are_over(self):
        output = self._write_index()
        with open(os.path.join(output, 'index.html')) as handle:
            contents = handle.read()
        self.assertIn('1 of 3 first-party implementation files', contents)
        self.assertIn('Not built', contents)

    def test_no_absence_data_leaves_the_pages_as_they_were(self):
        from webkitpy.coverage_directory_index import write_directory_index
        lcov = self.write('coverage.lcov', '\n'.join([
            'SF:{}'.format(self.absolute('Source/WebCore/dom/Node.cpp')),
            'DA:1,1', 'end_of_record', '']))
        output = self.absolute('plain')
        write_directory_index(lcov, output, source_root=self.root)
        with open(os.path.join(output, 'index.html')) as handle:
            contents = handle.read()
        self.assertNotIn('not built here', contents)
        self.assertNotIn('first-party implementation files', contents)


if __name__ == '__main__':
    unittest.main()

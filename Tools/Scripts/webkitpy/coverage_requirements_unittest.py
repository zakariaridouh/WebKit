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
import struct
import tempfile
import unittest

from webkitpy.coverage_requirements import (
    FATAL, INSTRUMENTED_PRODUCTS, MANDATORY_BUILD_SETTINGS, NOTE, TEST_SUPPORT_PRODUCTS, WARNING,
    build_directory_from_environment, conflicting_build_settings, coverage_build_command,
    instrumentation_findings, missing_build_settings, normalize_build_root, product_name,
    survey_products)


class MandatoryBuildSettingsTest(unittest.TestCase):
    def test_the_command_carries_every_mandatory_setting(self):
        command = coverage_build_command('Release')
        for setting in MANDATORY_BUILD_SETTINGS:
            self.assertIn(setting.argument, command)

    def test_the_command_is_a_release_xcode_build_by_default(self):
        self.assertEqual(coverage_build_command('Release')[:3],
                         ['Tools/Scripts/build-webkit', '--xcode', '--release'])

    def test_the_configuration_is_lowercased_into_a_flag(self):
        self.assertIn('--debug', coverage_build_command('Debug'))

    def test_extra_arguments_come_last(self):
        # So that a developer's own setting overrides a mandatory one on the same flag rather
        # than being overridden by it. xcodebuild and clang are both last-wins.
        command = coverage_build_command('Release', extra_arguments=['ENABLE_WEBGPU=NO'])
        self.assertEqual(command[-1], 'ENABLE_WEBGPU=NO')

    def test_every_setting_says_why(self):
        # The whole point of the tuple: a tool that silently corrects an invocation teaches
        # nobody what the invocation should have been.
        for setting in MANDATORY_BUILD_SETTINGS:
            self.assertTrue(setting.why)
            self.assertGreater(len(setting.why), 40)

    def test_no_lto_mode_is_mandatory(self):
        # It was, until it was measured: Debug/Release/Profiling already default to no LTO, and a
        # real --coverage --lto-mode=thin build links with zero duplicate symbols. Duplicate
        # __llvm_profile_filename needs two *strong* definitions and fails with or without LTO.
        # Pinned so nobody reintroduces the requirement without new evidence.
        self.assertEqual([setting for setting in MANDATORY_BUILD_SETTINGS
                          if 'lto' in setting.argument.lower()], [])

    def test_nothing_is_reported_missing_from_a_complete_command(self):
        self.assertEqual(missing_build_settings(coverage_build_command('Release')), [])

    def test_an_ordinary_build_is_missing_all_of_them(self):
        self.assertEqual(len(missing_build_settings(['--xcode', '--release'])),
                         len(MANDATORY_BUILD_SETTINGS))

    def test_a_setting_given_with_a_different_value_is_not_reported_missing(self):
        # It is reported as a conflict instead, so the message can say "replacing" rather than
        # "adding" and the command line does not end up with both spellings.
        missing = missing_build_settings(['ENABLE_USER_SCRIPT_SANDBOXING=YES'])
        self.assertNotIn('ENABLE_USER_SCRIPT_SANDBOXING=NO',
                         [setting.argument for setting in missing])

    def test_a_different_value_is_reported_as_a_conflict(self):
        conflicts = conflicting_build_settings(['ENABLE_USER_SCRIPT_SANDBOXING=YES'])
        self.assertEqual([(given, setting.argument) for given, setting in conflicts],
                         [('ENABLE_USER_SCRIPT_SANDBOXING=YES',
                           'ENABLE_USER_SCRIPT_SANDBOXING=NO')])

    def test_the_required_value_is_not_a_conflict_with_itself(self):
        self.assertEqual(conflicting_build_settings(['ENABLE_USER_SCRIPT_SANDBOXING=NO']), [])


class BuildRootTest(unittest.TestCase):
    def test_a_tree_is_left_alone(self):
        root = normalize_build_root('/src/WebKitBuild-Coverage', 'Release')
        self.assertEqual(root.root, '/src/WebKitBuild-Coverage')
        self.assertIsNone(root.note)

    def test_a_configuration_directory_is_stripped(self):
        # Port._build_path() appends the configuration, so passing this through resolves to
        # .../Release/Release, which exists nowhere and makes every product "not found".
        root = normalize_build_root('/src/WebKitBuild-Coverage/Release', 'Release')
        self.assertEqual(root.root, '/src/WebKitBuild-Coverage')
        self.assertIn('Release/Release', root.note)

    def test_a_trailing_slash_does_not_defeat_the_check(self):
        self.assertEqual(normalize_build_root('/src/WebKitBuild-Coverage/Release/', 'Release').root,
                         '/src/WebKitBuild-Coverage')

    def test_an_embedded_configuration_directory_is_stripped(self):
        root = normalize_build_root('/src/WebKitBuild/Release-iphonesimulator', 'Release')
        self.assertEqual(root.root, '/src/WebKitBuild')

    def test_a_mismatched_configuration_is_named_in_the_note(self):
        root = normalize_build_root('/src/WebKitBuild-Coverage/Debug', 'Release')
        self.assertEqual(root.root, '/src/WebKitBuild-Coverage')
        self.assertIn('Debug', root.note)
        self.assertIn('Release', root.note)

    def test_an_unknown_configuration_is_not_a_configuration_directory(self):
        root = normalize_build_root('/src/WebKitBuild-Coverage/Coverage', 'Release')
        self.assertEqual(root.root, '/src/WebKitBuild-Coverage/Coverage')
        self.assertIsNone(root.note)

    def test_the_environment_variable_is_read_from_one_place(self):
        self.assertEqual(build_directory_from_environment({'WEBKIT_OUTPUTDIR': '/x'}), '/x')
        self.assertIsNone(build_directory_from_environment({}))
        self.assertIsNone(build_directory_from_environment({'WEBKIT_OUTPUTDIR': ''}))


class _FakeMachO:
    """The smallest 64-bit Mach-O with load commands this needs, written to a real file.

    read_instrumentation() reads the header, the segment load commands, the symbol table and one
    string, so a fixture only has to be well-formed that far. Built here rather than copied from
    a build directory so the test can run anywhere and so each of the three states -- not
    instrumented, instrumented with a baked profile path, instrumented with none -- can be
    produced on its own.
    """

    MAGIC = b'\xcf\xfa\xed\xfe'
    COUNTERS_ADDRESS = 0x1000
    STRING_ADDRESS = 0x2000
    SYMBOL_TABLE_OFFSET = 0x3000
    STRING_TABLE_OFFSET = 0x3100
    SIZE = 0x4000
    PROFILE_FILENAME_SYMBOL = b'___llvm_profile_filename'

    @classmethod
    def _section(cls, name, address, size):
        section = name.encode('utf-8').ljust(16, b'\0') + b'__DATA'.ljust(16, b'\0')
        section += struct.pack('<QQ', address, size)
        section += struct.pack('<II', address, 0)            # file offset == address here
        section += struct.pack('<IIIIII', 0, 0, 0, 0, 0, 0)  # reloff..reserved3
        assert len(section) == 80, len(section)
        return section

    @classmethod
    def _segment(cls, sections):
        body = b''.join(sections)
        segment = struct.pack('<II', 0x19, 72 + len(body))
        segment += b'__DATA'.ljust(16, b'\0')
        segment += struct.pack('<QQQQ', 0, 0, 0, 0)
        segment += struct.pack('<iiII', 0, 0, len(sections), 0)
        return segment + body

    @classmethod
    def write(cls, path, instrumented=False, profile_filename=None):
        sections = []
        if instrumented:
            sections.append(cls._section('__llvm_prf_cnts', cls.COUNTERS_ADDRESS, 0x10))
        commands = [cls._segment(sections)] if sections else []
        strings = b''
        if profile_filename is not None:
            commands = [cls._segment(
                sections + [cls._section('__const', cls.STRING_ADDRESS, 0x100)])]
            strings = b'\0' + cls.PROFILE_FILENAME_SYMBOL + b'\0'
            commands.append(struct.pack('<IIIIII', 0x02, 24, cls.SYMBOL_TABLE_OFFSET, 1,
                                        cls.STRING_TABLE_OFFSET, len(strings)))
        body = b''.join(commands)
        header = cls.MAGIC + struct.pack('<iiIIIII', 0x100000c, 0, 6, len(commands),
                                         len(body), 0, 0)
        image = bytearray(b'\0' * cls.SIZE)
        image[0:len(header) + len(body)] = header + body
        if profile_filename is not None:
            encoded = profile_filename.encode('utf-8') + b'\0'
            image[cls.STRING_ADDRESS:cls.STRING_ADDRESS + len(encoded)] = encoded
            image[cls.SYMBOL_TABLE_OFFSET:cls.SYMBOL_TABLE_OFFSET + 16] = struct.pack(
                '<IBBHQ', 1, 0x0f, 1, 0, cls.STRING_ADDRESS)
            image[cls.STRING_TABLE_OFFSET:cls.STRING_TABLE_OFFSET + len(strings)] = strings
        with open(path, 'wb') as handle:
            handle.write(bytes(image))


BAKED_PROFILE_FILENAME = '/private/tmp/WebKitCoverage/WebCore_%4m%c.profraw'


class FakeMachOTest(unittest.TestCase):
    """The fixture has to be believable, so check it against the reader it is a fixture for."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='coverage-requirements-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def read(self, **arguments):
        from webkitpy.llvm_profile_utils import read_instrumentation

        path = os.path.join(self.directory, 'binary')
        _FakeMachO.write(path, **arguments)
        return read_instrumentation(path)

    def test_a_bare_binary_is_uninstrumented(self):
        self.assertEqual(self.read(), (False, None))

    def test_a_counters_section_makes_it_instrumented(self):
        self.assertEqual(self.read(instrumented=True), (True, None))

    def test_a_baked_path_is_read_back(self):
        self.assertEqual(self.read(instrumented=True, profile_filename=BAKED_PROFILE_FILENAME),
                         (True, BAKED_PROFILE_FILENAME))

    def test_an_unbaked_path_reads_as_the_empty_string(self):
        # Which is what the profile runtime's weak definition looks like, and is a different
        # fact from "the symbol is not in the symbol table".
        self.assertEqual(self.read(instrumented=True, profile_filename=''), (True, ''))


class SurveyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='coverage-requirements-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def write(self, relative, **arguments):
        path = os.path.join(self.directory, *relative.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _FakeMachO.write(path, **arguments)

    def test_an_empty_tree_reports_everything_missing(self):
        survey = survey_products(self.directory)
        self.assertEqual(len(survey.missing), len(INSTRUMENTED_PRODUCTS + TEST_SUPPORT_PRODUCTS))
        self.assertEqual(survey.instrumented, [])

    def test_an_uninstrumented_product_is_not_reported_as_missing(self):
        for relative in INSTRUMENTED_PRODUCTS:
            self.write(relative)
        survey = survey_products(self.directory, INSTRUMENTED_PRODUCTS)
        self.assertEqual(len(survey.uninstrumented), len(INSTRUMENTED_PRODUCTS))
        self.assertEqual(survey.missing, [])

    def test_a_counters_section_is_what_makes_a_product_instrumented(self):
        self.write(INSTRUMENTED_PRODUCTS[0], instrumented=True,
                   profile_filename=BAKED_PROFILE_FILENAME)
        survey = survey_products(self.directory, [INSTRUMENTED_PRODUCTS[0]])
        self.assertEqual(len(survey.instrumented), 1)
        self.assertEqual(survey.uninstrumented, [])
        self.assertEqual(survey.unbaked, [])
        self.assertEqual(survey.unverifiable, [])

    def test_an_unbaked_product_is_instrumented_and_unbaked(self):
        self.write(INSTRUMENTED_PRODUCTS[0], instrumented=True, profile_filename='')
        survey = survey_products(self.directory, [INSTRUMENTED_PRODUCTS[0]])
        self.assertEqual(len(survey.instrumented), 1)
        self.assertEqual(len(survey.unbaked), 1)

    def test_a_product_with_no_symbol_table_is_unverifiable(self):
        self.write(INSTRUMENTED_PRODUCTS[0], instrumented=True)
        survey = survey_products(self.directory, [INSTRUMENTED_PRODUCTS[0]])
        self.assertEqual(len(survey.instrumented), 1)
        self.assertEqual(len(survey.unverifiable), 1)
        self.assertEqual(survey.unbaked, [])

    def test_the_survey_times_itself(self):
        # Because "it is instant" is the entire reason to do this before a run rather than
        # after one; the measured figure over a real tree of nine binaries is 0.34 s.
        self.assertGreaterEqual(survey_products(self.directory).seconds, 0.0)


class InstrumentationFindingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='coverage-requirements-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.command = coverage_build_command('Release')

    def write(self, relative, **arguments):
        path = os.path.join(self.directory, *relative.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _FakeMachO.write(path, **arguments)

    def write_good(self, relative):
        self.write(relative, instrumented=True, profile_filename=BAKED_PROFILE_FILENAME)

    def findings(self, relative_products=None):
        survey = survey_products(self.directory, relative_products)
        return instrumentation_findings(survey, self.directory, self.command)

    def test_an_uninstrumented_tree_is_one_fatal_finding_and_not_nine(self):
        # One message, because there is one cause: this tree was not built with --coverage.
        for relative in INSTRUMENTED_PRODUCTS:
            self.write(relative)
        findings = self.findings(INSTRUMENTED_PRODUCTS)
        self.assertEqual([finding.severity for finding in findings], [FATAL])
        self.assertIn('is instrumented for coverage', findings[0].summary)

    def test_the_fatal_finding_carries_the_build_command_as_its_remedy(self):
        for relative in INSTRUMENTED_PRODUCTS:
            self.write(relative)
        remedy = self.findings(INSTRUMENTED_PRODUCTS)[0].remedy
        self.assertIn('--coverage', remedy)
        self.assertIn('ENABLE_USER_SCRIPT_SANDBOXING=NO', remedy)
        self.assertNotIn('--lto-mode', remedy)

    def test_a_partial_build_is_fatal_and_names_the_framework(self):
        # The dangerous case: llvm-cov has no mapping for the uninstrumented one, so the files
        # only it contains read as absent rather than as untested.
        for relative in INSTRUMENTED_PRODUCTS:
            self.write_good(relative)
        self.write(INSTRUMENTED_PRODUCTS[1])
        findings = self.findings(INSTRUMENTED_PRODUCTS)
        self.assertEqual([finding.severity for finding in findings], [FATAL])
        self.assertIn(product_name(INSTRUMENTED_PRODUCTS[1]), findings[0].summary)

    def test_a_missing_required_framework_is_fatal(self):
        for relative in INSTRUMENTED_PRODUCTS[:-1]:
            self.write_good(relative)
        findings = self.findings(INSTRUMENTED_PRODUCTS)
        self.assertEqual([finding.severity for finding in findings], [FATAL])
        self.assertIn('has not been built', findings[0].summary)

    def test_a_fully_instrumented_tree_has_no_findings(self):
        for relative in INSTRUMENTED_PRODUCTS:
            self.write_good(relative)
        self.assertEqual(self.findings(INSTRUMENTED_PRODUCTS), [])

    def test_a_missing_test_support_binary_is_not_a_finding(self):
        # jsc and libWebCoreTestSupport are excluded from the report by default, so their
        # absence is not a problem to report before a run.
        for relative in INSTRUMENTED_PRODUCTS:
            self.write_good(relative)
        self.assertEqual(self.findings(), [])

    def test_an_unbaked_test_support_binary_is_one_note_and_not_four_warnings(self):
        # jsc, libWebCoreTestSupport, webpushd and adattributiond really are in this state in the
        # shipped build (PLAN 10.5), and they are excluded from the report by default, so nothing
        # they do is misreported. Four identical warnings would train a reader to skip them.
        for relative in INSTRUMENTED_PRODUCTS:
            self.write_good(relative)
        for relative in TEST_SUPPORT_PRODUCTS:
            self.write(relative, instrumented=True, profile_filename='')
        findings = self.findings()
        self.assertEqual([finding.severity for finding in findings], [NOTE])
        self.assertIn('jsc', findings[0].detail)
        self.assertIn('default.profraw', findings[0].detail)

    def test_an_unbaked_required_framework_is_a_warning(self):
        # WebGPU and WebKitLegacy were both in exactly this state: instrumented, in the report,
        # and reported at 0.00% and 0.13% over 84,332 lines while executing everywhere.
        for relative in INSTRUMENTED_PRODUCTS:
            self.write_good(relative)
        self.write(INSTRUMENTED_PRODUCTS[-1], instrumented=True, profile_filename='')
        findings = self.findings(INSTRUMENTED_PRODUCTS)
        self.assertEqual([finding.severity for finding in findings], [WARNING])
        self.assertIn(product_name(INSTRUMENTED_PRODUCTS[-1]), findings[0].summary)
        self.assertIn('84,332', findings[0].detail)

    def test_severities_are_the_three_this_module_defines(self):
        self.assertEqual(sorted({FATAL, WARNING, NOTE}), ['fatal', 'note', 'warning'])


if __name__ == '__main__':
    unittest.main()

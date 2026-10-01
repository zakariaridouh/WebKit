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

"""The parts of generate-coverage-report that do not need a build directory to exercise."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock


class _Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The script has no .py extension, so it cannot simply be imported by name.
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'generate-coverage-report')
        loader = importlib.machinery.SourceFileLoader('generate_coverage_report', path)
        specification = importlib.util.spec_from_loader(loader.name, loader)
        cls.script = importlib.util.module_from_spec(specification)
        loader.exec_module(cls.script)


class ProductSelectionTest(_Script):
    def select(self, products, available=None):
        return self.script.selected_products(
            products, available if available is not None else self.script.INSTRUMENTED_PRODUCTS)

    def test_none_selects_everything(self):
        self.assertEqual(self.select(None), list(self.script.INSTRUMENTED_PRODUCTS))

    def test_a_name_selects_that_product(self):
        self.assertEqual(self.select('JavaScriptCore'),
                         ['JavaScriptCore.framework/Versions/A/JavaScriptCore'])

    def test_several_names_keep_the_declared_order(self):
        # llvm-cov takes the first object positionally and the rest as -object=, so order matters.
        self.assertEqual(self.select('WebKit,JavaScriptCore'),
                         ['JavaScriptCore.framework/Versions/A/JavaScriptCore',
                          'WebKit.framework/Versions/A/WebKit'])

    def test_an_empty_list_selects_nothing(self):
        # Which is how run-javascriptcore-tests asks for only what --object gives it.
        self.assertEqual(self.select(''), [])

    def test_restricting_the_products_selects_none_of_the_test_support_list(self):
        # Not an error: --products is validated against every known product, not one list at a time.
        self.assertEqual(self.select('JavaScriptCore', self.script.TEST_SUPPORT_PRODUCTS), [])

    def test_a_test_support_product_can_be_named(self):
        self.assertEqual(self.select('jsc', self.script.TEST_SUPPORT_PRODUCTS), ['jsc'])

    def test_left_out_binaries_are_the_known_products_built_but_not_reported(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for relative in ('JavaScriptCore.framework/Versions/A/JavaScriptCore', 'jsc',
                         'libWebCoreTestSupport.dylib'):
            os.makedirs(os.path.dirname(os.path.join(root, relative)), exist_ok=True)
            open(os.path.join(root, relative), 'w').close()

        class Port:
            def _build_path(self, *components):
                return os.path.join(root, *components)

        objects = [os.path.join(root, 'JavaScriptCore.framework/Versions/A/JavaScriptCore')]
        self.assertEqual(self.script.left_out_binary_paths(Port(), objects),
                         [os.path.join(root, 'libWebCoreTestSupport.dylib'), os.path.join(root, 'jsc')])

    def test_every_known_product_has_a_unique_name(self):
        # Two products with the same basename would make --products ambiguous.
        names = [self.script.product_name(entry) for entry in self.script.KNOWN_PRODUCTS]
        self.assertEqual(len(names), len(set(names)))


class SourceScopeTest(_Script):
    """--sources scoping must be exact both ways: too wide is silently the unscoped report, too narrow is empty."""

    def setUp(self):
        self.checkout = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.checkout, ignore_errors=True)
        for relative in ('Source/WebCore/dom/Node.cpp', 'Source/WebCore/dom/Element.cpp',
                         'Source/WebCore/domsomething/Other.cpp',
                         'Source/WebCore/css/CSSParser.cpp'):
            path = os.path.join(self.checkout, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as handle:
                handle.write('int f() { return 0; }\n')

    def resolve(self, *scopes):
        return self.script.resolve_source_scopes(list(scopes), self.checkout)

    def test_a_path_relative_to_the_checkout_root_resolves_against_it(self):
        # llvm-cov resolves a relative --sources against its own cwd, not the checkout root.
        resolved, missing = self.resolve('Source/WebCore/dom')
        self.assertEqual(resolved, [os.path.join(self.checkout, 'Source/WebCore/dom')])
        self.assertEqual(missing, [])

    def test_an_absolute_path_is_taken_as_given(self):
        absolute = os.path.join(self.checkout, 'Source/WebCore/css')
        self.assertEqual(self.resolve(absolute), ([absolute], []))

    def test_a_single_file_is_a_legal_scope(self):
        resolved, missing = self.resolve('Source/WebCore/dom/Node.cpp')
        self.assertEqual(missing, [])
        self.assertTrue(resolved[0].endswith('Node.cpp'))

    def test_a_path_that_does_not_exist_is_reported_rather_than_passed_through(self):
        resolved, missing = self.resolve('Source/WebCore/dom', 'Source/WebCore/nope')
        self.assertEqual(len(resolved), 2)
        self.assertEqual(missing, [os.path.join(self.checkout, 'Source/WebCore/nope')])

    def test_a_scope_is_a_path_prefix_and_not_a_string_prefix(self):
        # 'Source/WebCore/domsomething' must not fall inside 'Source/WebCore/dom'.
        scope = [os.path.join(self.checkout, 'Source/WebCore/dom')]
        self.assertTrue(self.script.is_under_any(
            os.path.join(self.checkout, 'Source/WebCore/dom/Node.cpp'), scope))
        self.assertFalse(self.script.is_under_any(
            os.path.join(self.checkout, 'Source/WebCore/domsomething/Other.cpp'), scope))
        self.assertTrue(self.script.is_under_any(scope[0], scope))
        self.assertTrue(self.script.is_under_any(
            os.path.join(self.checkout, 'Source/WebCore/dom/Node.cpp'), [scope[0] + '/']))

    def test_the_never_built_denominator_is_recomputed_over_the_scope(self):
        # Unrestricted, files outside the scope would show as never built.
        from webkitpy.coverage_build_inventory import AbsenceReport, AbsentFile
        absence = AbsenceReport()
        absence.compiled_file_count = 4
        absence.total_file_count = 4
        absence.reported_file_count = 2
        for relative in ('Source/WebCore/dom/Element.cpp', 'Source/WebCore/css/CSSParser.cpp'):
            absence.add(AbsentFile(relative, 'other-port', 'gtk', 10))

        restricted = self.script.restrict_absence_to_scope(
            absence, self.checkout, [os.path.join(self.checkout, 'Source/WebCore/dom')])

        self.assertEqual([absent.path for absent in restricted.files],
                         ['Source/WebCore/dom/Element.cpp'])
        # dom holds two files, one of which is absent, so the scope's denominator is 2 and not 4.
        self.assertEqual(restricted.total_file_count, 2)
        self.assertEqual(restricted.reported_file_count, 1)
        self.assertIn('1 of 2 first-party implementation files',
                      restricted.denominator_sentence())


class ArgumentValidationTest(_Script):
    def parse(self, *arguments):
        return self.script.parse_args(list(arguments))

    def error(self, expected, *arguments):
        """Assert parse_args rejects these arguments for the expected reason, not because another check fired first."""
        # parser.error() prints usage and exits 2; keep that out of test output.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.parse(*arguments)
        self.assertIn(expected, stderr.getvalue())

    # Any existing file will do as an indexed profile: parse_args only checks that it is one.
    PROFILE = '--profdata=' + os.path.abspath(__file__)

    def test_a_misspelled_product_is_rejected_before_anything_is_read(self):
        self.error('--products names JavascriptCore', '--output-dir=/tmp/report', self.PROFILE,
                   '--products=JavascriptCore')

    def test_one_bad_name_among_good_ones_is_still_rejected(self):
        self.error('--products names Nope', '--output-dir=/tmp/report', self.PROFILE,
                   '--products=JavaScriptCore,Nope,WebCore')

    def test_a_real_product_is_accepted(self):
        options = self.parse('--output-dir=/tmp/report', self.PROFILE,
                             '--products=JavaScriptCore,WebCore')
        self.assertEqual(options.products, 'JavaScriptCore,WebCore')

    def test_an_empty_product_list_is_accepted(self):
        self.assertEqual(self.parse('--output-dir=/tmp/report', self.PROFILE,
                                    '--products=').products, '')

    def test_exactly_one_profile_source_is_required(self):
        self.error('exactly one', '--output-dir=/tmp/report')
        self.error('--coverage-dir and --profdata', '--output-dir=/tmp/report',
                   '--coverage-dir=/tmp', self.PROFILE)
        self.error('--profdata and --suite', '--output-dir=/tmp/report', self.PROFILE,
                   '--suite=layout:/tmp/cov')

    def test_a_malformed_suite_is_rejected(self):
        self.error('has no label', '--output-dir=/tmp/report', '--suite=/tmp/cov')

    def test_a_threshold_outside_0_to_100_is_rejected(self):
        self.error('--fail-under-lines is a percentage', '--output-dir=/tmp/report',
                   self.PROFILE, '--fail-under-lines=101')
        self.error('--fail-under-branches is a percentage', '--output-dir=/tmp/report',
                   self.PROFILE, '--fail-under-branches=-1')

    def test_a_threshold_at_either_end_is_accepted(self):
        for value in ('0', '100'):
            self.assertEqual(
                self.parse('--output-dir=/tmp/report', self.PROFILE,
                           '--fail-under-functions=' + value).fail_under_functions, float(value))

    def test_export_is_off_by_default(self):
        self.assertEqual(self.parse('--output-dir=/tmp/report', self.PROFILE).export_formats, ())

    def test_export_takes_commas_repeats_and_all(self):
        self.assertEqual(self.parse('--output-dir=/tmp/report', self.PROFILE,
                                    '--export=json,cobertura', '--export=csv').export_formats,
                         ('cobertura', 'csv', 'json'))
        self.assertEqual(self.parse('--output-dir=/tmp/report', self.PROFILE,
                                    '--export=all').export_formats,
                         ('cobertura', 'istanbul', 'sonarqube', 'csv', 'json'))

    def test_an_unknown_export_format_is_rejected_before_anything_is_read(self):
        self.error("--export names 'xml', which is not one of cobertura", '--output-dir=/tmp/report',
                   self.PROFILE, '--export=csv,xml')

    def test_an_empty_export_is_rejected(self):
        self.error('--export needs a format', '--output-dir=/tmp/report', self.PROFILE, '--export=')


class WriteExportsTest(_Script):
    TRACE = 'SF:/checkout/Source/a.cpp\nDA:1,1\nDA:2,0\nend_of_record\n'

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.trace = os.path.join(self.directory, 'coverage.lcov')

    def write_trace(self, text):
        with open(self.trace, 'w') as handle:
            handle.write(text)

    def test_the_formats_go_under_export_and_each_file_is_logged(self):
        self.write_trace(self.TRACE)
        with self.assertLogs('generate-coverage-report', 'INFO') as logs:
            self.assertTrue(self.script.write_exports(('csv', 'json'), self.trace, self.directory,
                                                      '/checkout', None))
        self.assertEqual(sorted(os.listdir(os.path.join(self.directory, 'export'))),
                         ['coverage.csv', 'coverage.json'])
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all('Wrote ' in line for line in logs.output))

    def test_a_failure_is_an_error_message_and_a_false_result(self):
        self.write_trace('')
        with self.assertLogs('generate-coverage-report', 'ERROR') as logs:
            self.assertFalse(self.script.write_exports(('csv',), self.trace, self.directory,
                                                       '/checkout', None))
        self.assertIn('--export failed: ', logs.output[0])
        self.assertIn('contained no coverage records', logs.output[0])


class _FakePort:
    def __init__(self, checkout, build):
        self.checkout = checkout
        self.build = build

    def webkit_base(self):
        return self.checkout

    def _build_path(self, *components):
        return os.path.join(self.build, *components)

    def name(self):
        return 'mac'

    def get_option(self, name):
        return 'Release'


class _FakeLLVMCov:
    trace = ''

    @classmethod
    def report(cls, *arguments, **keywords):
        return types.SimpleNamespace(returncode=0, stderr='', stdout='TOTAL   1   1   0.00%\n')

    @classmethod
    def export_lcov(cls, objects, profile, lcov_path, *arguments, **keywords):
        with open(lcov_path, 'w') as handle:
            handle.write(cls.trace)
        return types.SimpleNamespace(returncode=0, stderr='')


class MainExportTest(_Script):
    """What main() does around a failing --export: the report and provenance are written first, and the exit status is the gate's."""

    TRACE = 'SF:{0}/b.cpp\nDA:1,0\nend_of_record\n'

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.checkout = os.path.join(self.directory, 'checkout')
        self.output = os.path.join(self.directory, 'report')
        for name in ('checkout', 'build'):
            os.mkdir(os.path.join(self.directory, name))
        self.binary = os.path.join(self.directory, 'build', 'binary')
        self.profile = os.path.join(self.directory, 'coverage.profdata')
        for path in (self.binary, self.profile):
            open(path, 'w').close()
        self.calls = []

    def block_the_export_directory(self):
        os.makedirs(self.output)
        open(os.path.join(self.output, 'export'), 'w').close()

    def run_main(self, *arguments):
        _FakeLLVMCov.trace = self.TRACE.format(self.checkout)
        port = _FakePort(self.checkout, os.path.join(self.directory, 'build'))
        host = mock.Mock()
        host.port_factory.get.return_value = port

        def write_provenance(record, directory):
            self.calls.append('provenance')
            path = os.path.join(directory, 'provenance.json')
            open(path, 'w').close()
            return path

        def write_report(*positional, **keywords):
            self.calls.append('report')
            open(os.path.join(self.output, 'index.html'), 'w').close()
            return types.SimpleNamespace(directory_pages=1, source_pages=1, source_bytes=0)

        script = self.script
        patches = [mock.patch.object(script, name, value) for name, value in (
            ('Host', mock.Mock(return_value=host)), ('LLVMCov', _FakeLLVMCov),
            ('report_instrumentation_survey', lambda *a: True),
            ('warn_about_objects_with_no_profile_data', lambda *a: None),
            ('warn_about_collected_profiles_with_no_object', lambda *a: None),
            ('provenance_record', lambda *a, **k: {}), ('summary_lines', lambda record: []),
            ('trace_comment', lambda record: ''), ('add_trace_measurements', lambda record, path: record),
            ('write_provenance', write_provenance), ('write_report', write_report),
            ('write_exports', self.recording(script.write_exports)))]
        patches.append(mock.patch.object(script.logging, 'basicConfig'))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertLogs('generate-coverage-report', 'INFO') as logs, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = script.main(['--output-dir=' + self.output, '--profdata=' + self.profile,
                                  '--products=', '--object=' + self.binary, '--no-build-inventory',
                                  '--no-regions', '--no-compress-lcov'] + list(arguments))
        return status, logs.output

    def recording(self, function):
        def record(*arguments, **keywords):
            self.calls.append('export')
            return function(*arguments, **keywords)
        return record

    def test_a_good_export_changes_nothing_about_the_exit_status(self):
        status, _ = self.run_main('--export=csv')
        self.assertEqual(status, 0)
        self.assertEqual(os.listdir(os.path.join(self.output, 'export')), ['coverage.csv'])

    def test_a_failing_export_still_writes_the_provenance_and_the_report_and_then_fails(self):
        self.block_the_export_directory()
        status, logs = self.run_main('--export=csv')
        self.assertEqual(status, 1)
        self.assertEqual(self.calls, ['provenance', 'report', 'export'])
        for name in ('provenance.json', 'index.html', 'summary.txt'):
            self.assertTrue(os.path.exists(os.path.join(self.output, name)), name)
        self.assertTrue(any('--export failed: Could not export' in line for line in logs), logs)

    def test_a_failing_gate_keeps_its_own_exit_code_when_the_export_fails_too(self):
        self.block_the_export_directory()
        status, _ = self.run_main('--export=csv', '--fail-under-lines=50')
        self.assertEqual(status, self.script.COVERAGE_GATE_EXIT_CODE)
        self.assertNotEqual(status, 1)
        self.assertEqual(self.calls, ['provenance', 'report', 'export'])

    def test_a_passing_gate_does_not_hide_a_failed_export(self):
        self.block_the_export_directory()
        status, _ = self.run_main('--export=csv', '--fail-under-lines=0')
        self.assertEqual(status, 1)

    def test_a_failing_gate_without_an_export_is_unchanged(self):
        status, _ = self.run_main('--fail-under-lines=50')
        self.assertEqual(status, self.script.COVERAGE_GATE_EXIT_CODE)

    def test_the_trace_is_still_there_for_the_export_when_lcov_is_not_kept(self):
        status, _ = self.run_main('--export=csv', '--no-lcov')
        self.assertEqual(status, 0)
        self.assertEqual(os.listdir(os.path.join(self.output, 'export')), ['coverage.csv'])

    def test_the_exit_status_is_the_gates_unless_only_the_export_failed(self):
        status = self.script.exit_status
        self.assertEqual([status(0, False), status(0, True), status(3, False), status(3, True)],
                         [0, 1, 3, 3])


class SummaryTotalLinesTest(_Script):
    """What gets echoed to the terminal out of llvm-cov's table: the TOTAL row, never an arbitrary file's row."""

    TABLE = (
        'Filename                        Regions    Missed Regions    Cover\n'
        '-----------------------------------------------------------------\n'
        'Source/WTF/wtf/Assertions.cpp        10                 2   80.00%\n'
        'WebKitBuild/bmalloc/pas_stats.h       0                 0        -\n'
        '-----------------------------------------------------------------\n'
        'TOTAL                                10                 2   80.00%\n'
    )

    def test_only_the_rule_and_the_total_are_echoed(self):
        self.assertEqual(self.script.summary_total_lines(self.TABLE),
                         [self.TABLE.splitlines()[-2], self.TABLE.splitlines()[-1]])

    def test_no_file_row_is_echoed(self):
        echoed = '\n'.join(self.script.summary_total_lines(self.TABLE))
        self.assertNotIn('pas_stats.h', echoed)
        self.assertNotIn('Assertions.cpp', echoed)

    def test_a_total_with_no_rule_above_it_is_echoed_alone(self):
        table = 'Source/WTF/wtf/Assertions.cpp   10   2   80.00%\nTOTAL   10   2   80.00%\n'
        self.assertEqual(self.script.summary_total_lines(table), ['TOTAL   10   2   80.00%'])

    def test_output_with_no_total_falls_back_to_the_tail(self):
        # Show the tail so output this function does not model stays visible.
        table = 'one\ntwo\nthree\nfour\n'
        self.assertEqual(self.script.summary_total_lines(table), ['two', 'three', 'four'])


if __name__ == '__main__':
    unittest.main()

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

"""The parts of webkit-coverage that need neither a build directory nor a test run."""

import contextlib
import importlib.machinery
import importlib.util
import io
import optparse
import os
import shutil
import subprocess
import tempfile
import unittest

from webkitpy.coverage_requirements import BUILD_OUTPUT_ENVIRONMENT_VARIABLE, MANDATORY_BUILD_SETTINGS
from webkitpy.coverage_scope import CoverageScope
from webkitpy.coverage_test_scope import Association, Declined, ScopeSuggestion


class _Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The script has no .py extension, so it cannot simply be imported by name.
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'webkit-coverage')
        loader = importlib.machinery.SourceFileLoader('webkit_coverage', path)
        specification = importlib.util.spec_from_loader(loader.name, loader)
        cls.script = importlib.util.module_from_spec(specification)
        loader.exec_module(cls.script)

    @contextlib.contextmanager
    def captured(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            yield stream


class ArgumentTest(_Script):
    def parse(self, args):
        return self.script.parse_args(args)

    def error(self, args, expected):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.parse(args)
        self.assertIn(expected, stderr.getvalue())

    def test_tests_are_positional(self):
        _, tests = self.parse(['--release', 'svg', 'fast/css'])
        self.assertEqual(tests, ['svg', 'fast/css'])

    def test_no_tests_is_not_an_error(self):
        options, tests = self.parse([])
        self.assertEqual(tests, [])
        self.assertFalse(options.full_suite)

    def test_the_full_suite_cannot_also_be_a_subset(self):
        self.error(['--full-suite', 'svg'], 'cannot be combined with a test list')

    def test_a_patch_threshold_is_a_percentage(self):
        self.error(['--fail-under-patch=101'], 'between 0 and 100')

    def test_building_is_automatic_by_default(self):
        self.assertEqual(self.parse([])[0].build, 'auto')

    def test_an_unknown_build_mode_is_rejected(self):
        self.error(['--build=sometimes'], 'choice')

    def test_the_build_inventory_is_off_by_default(self):
        self.assertFalse(self.parse([])[0].build_inventory)


class DefaultSinceRefTest(_Script):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def git(self, *arguments):
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', '-c', 'commit.gpgsign=false', *arguments],
                       cwd=self.root, check=True, capture_output=True)

    def commit(self, name):
        self.git('commit', '--allow-empty', '-m', name)

    def test_the_nearest_of_main_and_origin_main_wins(self):
        self.git('init', '-q', '-b', 'main')
        self.commit('one')
        self.commit('two')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('checkout', '-q', '-b', 'feature')
        self.commit('three')
        self.git('branch', '-f', 'main', 'HEAD~2')
        self.assertEqual(self.script.default_since_ref(self.root), 'origin/main')

    def test_main_wins_a_tie_and_no_refs_means_none(self):
        self.git('init', '-q', '-b', 'trunk')
        self.commit('one')
        self.assertIsNone(self.script.default_since_ref(self.root))
        self.git('branch', 'main')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.assertEqual(self.script.default_since_ref(self.root), 'main')


class ExitCodeTest(_Script):
    def test_needing_a_scope_is_its_own_exit_code(self):
        from webkitpy.coverage_thresholds import COVERAGE_GATE_EXIT_CODE

        self.assertNotIn(self.script.NEEDS_A_SCOPE_EXIT_CODE, (0, 1, COVERAGE_GATE_EXIT_CODE))


class BuildCommandTest(_Script):
    def command(self, arguments=(), use_cmake=False, platform=None):
        options = optparse.Values({'configuration': 'Release',
                                   'build_arguments': list(arguments),
                                   'use_cmake': use_cmake,
                                   'platform': platform})
        commands, explanations = self.script.build_command(options)
        if not use_cmake:
            self.assertEqual(len(commands), 1)
            return commands[0], explanations
        return commands, explanations

    def test_the_command_carries_every_required_setting(self):
        command, _ = self.command()
        for argument in ('--coverage', 'ENABLE_USER_SCRIPT_SANDBOXING=NO'):
            self.assertIn(argument, command)
        # --lto-mode is deliberately not required.
        self.assertFalse([argument for argument in command if 'lto-mode' in argument])

    def test_every_addition_is_explained(self):
        _, explanations = self.command()
        self.assertEqual(len(explanations), len(MANDATORY_BUILD_SETTINGS))
        for explanation in explanations:
            self.assertTrue(explanation.startswith('added '))
            self.assertGreater(len(explanation), 60)

    def test_a_contradicting_argument_is_called_out(self):
        _, explanations = self.command(['ENABLE_USER_SCRIPT_SANDBOXING=YES'])
        self.assertTrue(any('ENABLE_USER_SCRIPT_SANDBOXING=YES' in explanation
                            and 'yours wins' in explanation
                            for explanation in explanations))

    def test_an_extra_argument_comes_last(self):
        command, _ = self.command(['ENABLE_WEBGPU=NO'])
        self.assertEqual(command[-1], 'ENABLE_WEBGPU=NO')

    def test_cmake_configures_before_it_builds(self):
        commands, _ = self.command(use_cmake=True)
        self.assertEqual(commands, [['cmake', '--preset', 'mac-coverage'],
                                    ['cmake', '--build', '--preset', 'mac-coverage']])

    def test_cmake_needs_none_of_the_xcode_settings(self):
        commands, explanations = self.command(use_cmake=True)
        flattened = [argument for command in commands for argument in command]
        for argument in ('--coverage', '--xcode', 'ENABLE_USER_SCRIPT_SANDBOXING=NO'):
            self.assertNotIn(argument, flattened)
        self.assertTrue(any('ENABLE_USER_SCRIPT_SANDBOXING' in explanation
                            for explanation in explanations))

    def test_the_cmake_preset_follows_the_platform(self):
        self.assertEqual(self.script.cmake_preset(optparse.Values({'platform': None})),
                         'mac-coverage')
        self.assertEqual(self.script.cmake_preset(optparse.Values({'platform': 'mac-sequoia'})),
                         'mac-coverage')
        self.assertEqual(self.script.cmake_preset(optparse.Values({'platform': 'gtk'})),
                         'gtk-coverage')
        self.assertIsNone(self.script.cmake_preset(optparse.Values({'platform': 'win'})))


class BuildRootTest(_Script):
    def setUp(self):
        self.checkout = tempfile.mkdtemp(prefix='webkit-coverage-')
        self.addCleanup(shutil.rmtree, self.checkout, ignore_errors=True)
        self.original = os.environ.pop(BUILD_OUTPUT_ENVIRONMENT_VARIABLE, None)
        if self.original is not None:
            self.addCleanup(os.environ.__setitem__, BUILD_OUTPUT_ENVIRONMENT_VARIABLE,
                            self.original)

    def resolve(self, build_directory=None, use_cmake=False, platform=None):
        options = optparse.Values({'build_directory': build_directory,
                                   'configuration': 'Release',
                                   'use_cmake': use_cmake,
                                   'platform': platform})
        with contextlib.redirect_stdout(io.StringIO()):
            return self.script.resolve_build_root(options, self.checkout)

    def test_the_flag_wins(self):
        root, provenance = self.resolve(os.path.join(self.checkout, 'Elsewhere'))
        self.assertEqual(root, os.path.join(self.checkout, 'Elsewhere'))
        self.assertEqual(provenance, '--build-directory')

    def test_a_configuration_directory_is_accepted(self):
        root, _ = self.resolve(os.path.join(self.checkout, 'Elsewhere', 'Release'))
        self.assertEqual(root, os.path.join(self.checkout, 'Elsewhere'))

    def test_a_relative_path_becomes_absolute(self):
        root, _ = self.resolve('WebKitBuild-Coverage')
        self.assertTrue(os.path.isabs(root))

    def test_the_environment_is_next(self):
        os.environ[BUILD_OUTPUT_ENVIRONMENT_VARIABLE] = os.path.join(self.checkout, 'FromEnv')
        self.addCleanup(os.environ.pop, BUILD_OUTPUT_ENVIRONMENT_VARIABLE, None)
        root, provenance = self.resolve()
        self.assertEqual(root, os.path.join(self.checkout, 'FromEnv'))
        self.assertEqual(provenance, '$' + BUILD_OUTPUT_ENVIRONMENT_VARIABLE)

    def test_the_default_is_not_the_ordinary_build_directory(self):
        root, provenance = self.resolve()
        self.assertEqual(os.path.basename(root), 'WebKitBuild-Coverage')
        self.assertIn('default', provenance)


class SourceScopeTest(_Script):
    def setUp(self):
        self.checkout = tempfile.mkdtemp(prefix='webkit-coverage-')
        self.addCleanup(shutil.rmtree, self.checkout, ignore_errors=True)

    def write(self, relative):
        path = os.path.join(self.checkout, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write('')
        return path

    def test_a_source_file_becomes_a_sources_argument(self):
        self.write('Source/WebCore/dom/Document.cpp')
        self.assertEqual(
            self.script.source_scope_arguments(['Source/WebCore/dom/Document.cpp'],
                                               self.checkout),
            ['--sources', os.path.join(self.checkout, 'Source/WebCore/dom/Document.cpp')])

    def test_a_header_is_a_source_file(self):
        self.write('Source/WebCore/dom/Document.h')
        self.assertEqual(len(self.script.source_scope_arguments(
            ['Source/WebCore/dom/Document.h'], self.checkout)), 2)

    def test_a_non_source_file_is_not_scoped_on(self):
        self.write('Source/WebCore/Sources.txt')
        self.assertEqual(self.script.source_scope_arguments(
            ['Source/WebCore/Sources.txt'], self.checkout), [])

    def test_a_deleted_file_is_not_scoped_on(self):
        # llvm-cov silently reports nothing for a --sources path that matches no recorded path.
        self.assertEqual(self.script.source_scope_arguments(
            ['Source/WebCore/dom/Gone.cpp'], self.checkout), [])


class SummaryTest(_Script):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='webkit-coverage-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def write_summary(self, text):
        with open(os.path.join(self.directory, 'patch-summary.txt'), 'w') as handle:
            handle.write(text)

    def test_the_uncovered_count_is_the_difference(self):
        self.write_summary('Patch coverage: 81.25% (26 of 32 added lines with coverage data '
                           'covered)\n')
        summary = self.script.summarize(self.directory, CoverageScope.full_suite())
        self.assertIn('6 uncovered added line(s)', summary)

    def test_thousands_separators_do_not_break_it(self):
        self.write_summary('Patch coverage: 50.00% (1,000 of 2,500 added lines with coverage '
                           'data covered)\n')
        self.assertIn('1,500 uncovered added line(s)',
                      self.script.summarize(self.directory, CoverageScope.full_suite()))

    def test_a_selective_run_says_at_most(self):
        self.write_summary('Patch coverage: 81.25% (26 of 32 added lines with coverage data '
                           'covered)\n')
        summary = self.script.summarize(self.directory,
                                        CoverageScope.selective(['svg'], tests_run=1))
        self.assertIn('at most', summary)

    def test_a_full_suite_run_does_not(self):
        self.write_summary('Patch coverage: 81.25% (26 of 32 added lines with coverage data '
                           'covered)\n')
        self.assertNotIn('at most',
                         self.script.summarize(self.directory, CoverageScope.full_suite()))

    def test_the_patch_page_is_named_before_the_whole_tree_index(self):
        from webkitpy.coverage_patch import PATCH_REPORT_NAME
        self.write_summary('Patch coverage: 100.00% (2 of 2 added lines with coverage data '
                           'covered)\n')
        open(os.path.join(self.directory, PATCH_REPORT_NAME), 'w').close()
        lines = self.script.summarize(self.directory,
                                      CoverageScope.full_suite()).splitlines()
        self.assertEqual(lines[-2], '{}  <- the patch, line by line'.format(
            os.path.join(self.directory, PATCH_REPORT_NAME)))
        self.assertEqual(lines[-1], '{}  <- the whole tree'.format(
            os.path.join(self.directory, 'index.html')))

    def test_the_patch_page_is_not_named_when_it_was_not_written(self):
        self.write_summary('Patch coverage: 100.00% (2 of 2 added lines with coverage data '
                           'covered)\n')
        lines = self.script.summarize(self.directory,
                                      CoverageScope.full_suite()).splitlines()
        self.assertEqual(lines[-1], '{}  <- the whole tree'.format(
            os.path.join(self.directory, 'index.html')))
        self.assertNotIn('the patch, line by line', '\n'.join(lines))

    def test_a_missing_summary_says_so_rather_than_printing_a_wrong_number(self):
        summary = self.script.summarize(self.directory, CoverageScope.full_suite())
        self.assertIn('no uncovered-line count', summary)
        self.assertEqual(summary.splitlines()[-1], '{}  <- the whole tree'.format(
            os.path.join(self.directory, 'index.html')))


class SuggestionPrintingTest(_Script):
    def output(self, suggestion):
        with self.captured() as stream:
            self.script.print_scope_suggestion(suggestion)
        return stream.getvalue()

    def test_an_empty_suggestion_says_scope_it_yourself(self):
        text = self.output(ScopeSuggestion(
            declined=[Declined('Source/WebCore/dom/Document.cpp', 'unbounded')]))
        self.assertIn('No suggestion', text)
        self.assertIn('scope it yourself, or run the suite', text)
        self.assertIn('Source/WebCore/dom/Document.cpp', text)

    def test_a_suggestion_says_it_has_not_been_applied(self):
        text = self.output(ScopeSuggestion(
            [Association('Source/WebCore/svg/SVGElement.cpp', ['svg'], 'alignment')]))
        self.assertIn('has not been applied', text)
        self.assertIn('svg', text)

    def test_an_incomplete_suggestion_says_so(self):
        text = self.output(ScopeSuggestion(
            [Association('Source/WebCore/svg/SVGElement.cpp', ['svg'], 'alignment')],
            [Declined('Source/WebCore/dom/Document.cpp', 'unbounded')]))
        self.assertIn('INCOMPLETE', text)
        # Wrapped, so match on a fragment that survives the wrap.
        self.assertIn('cover the whole change', text)

    def test_a_very_wide_suggestion_is_not_dumped_in_full(self):
        tests = ['dir{}/test.html'.format(index) for index in range(200)]
        text = self.output(ScopeSuggestion(
            [Association('Source/WebCore/svg/SVGElement.cpp', tests, 'alignment')]))
        self.assertIn('... and 180 more', text)
        self.assertIn('more paths than are worth retyping', text)
        self.assertLess(max(len(line) for line in text.splitlines()), 200)


if __name__ == '__main__':
    unittest.main()

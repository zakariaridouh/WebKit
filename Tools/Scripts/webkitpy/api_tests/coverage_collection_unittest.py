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

"""run-api-tests' coverage plumbing, and plain harness startup not importing the coverage stack."""

import importlib.machinery
import importlib.util
import io
import logging
import os
import subprocess
import sys
import unittest

from unittest import mock

from webkitcorepy import TaskPool

from webkitpy.api_tests import runner
from webkitpy.common.system.filesystem_mock import MockFileSystem
from webkitpy.layout_tests import run_webkit_tests
from webkitpy.layout_tests.controllers import layout_test_runner
from webkitpy.llvm_profile_utils import COVERAGE_PROFILE_DIRECTORY

COLLECTOR_FACTORY = 'webkitpy.coverage_attribution.collector_for_port'


def _scripts_directory():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_run_api_tests():
    loader = importlib.machinery.SourceFileLoader(
        'run_api_tests', os.path.join(_scripts_directory(), 'run-api-tests'))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(specification)
    # On Linux this can re-exec the process inside a container.
    with mock.patch('webkitpy.port.linux_container_sdk_utils.maybe_enter_webkit_container_sdk'):
        loader.exec_module(module)
    return module


class _RunAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _load_run_api_tests()


class MainCoverageDirectoryTest(_RunAPITests):
    COVERAGE_DIRECTORY = '/coverage'

    def setUp(self):
        self.filesystem = MockFileSystem()
        port = mock.MagicMock()
        port.operating_system.return_value = 'mac'
        port.host.filesystem = self.filesystem
        host = mock.MagicMock()
        host.port_factory.get.return_value = port
        self.directory_when_run_started = []

        def fake_run(*arguments):
            self.directory_when_run_started.append(
                self.filesystem.isdir(self.COVERAGE_DIRECTORY))
            return 0

        for name, replacement in (('Host', mock.Mock(return_value=host)),
                                  ('run', mock.Mock(side_effect=fake_run))):
            patcher = mock.patch.object(self.script, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.run_mock = self.script.run
        self.stderr = io.StringIO()

    def main(self):
        return self.script.main(['--coverage', '--coverage-dir=' + self.COVERAGE_DIRECTORY],
                                io.StringIO(), self.stderr)

    def test_an_unwritable_coverage_directory_stops_the_run_before_it_starts(self):
        self.filesystem.write_binary_file = mock.Mock(side_effect=OSError('Permission denied'))
        self.assertEqual(self.main(), self.script.EXCEPTIONAL_EXIT_STATUS)
        self.run_mock.assert_not_called()
        self.assertIn(self.COVERAGE_DIRECTORY, self.stderr.getvalue())
        self.assertIn('Permission denied', self.stderr.getvalue())

    def test_a_writable_coverage_directory_exists_by_the_time_the_run_starts(self):
        self.assertEqual(self.main(), 0)
        self.run_mock.assert_called_once()
        self.assertEqual(self.directory_when_run_started, [True])

    def test_the_probe_file_is_not_left_behind(self):
        self.main()
        self.assertEqual(self.filesystem.listdir(self.COVERAGE_DIRECTORY), [])


class RunCollectionTest(_RunAPITests):
    COVERAGE_DIRECTORY = '/coverage'

    def setUp(self):
        root_logger = logging.getLogger()
        self.addCleanup(root_logger.setLevel, root_logger.level)
        self.manager = mock.MagicMock()
        self.collect = mock.Mock(return_value=True)
        for name, replacement in (('Manager', mock.Mock(return_value=self.manager)),
                                  ('MeteredStream', mock.MagicMock()),
                                  ('prepare_coverage_profile_directory', mock.Mock()),
                                  ('collect_coverage_profiles', self.collect)):
            patcher = mock.patch.object(self.script, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.options = self.script.parse_args(
            ['--coverage', '--coverage-dir=' + self.COVERAGE_DIRECTORY])[0]

    def run_tests(self):
        return self.script.run(mock.MagicMock(), self.options, [], io.StringIO())

    def test_the_profiles_are_collected_into_the_coverage_directory(self):
        self.manager.run.return_value = 0
        self.run_tests()
        self.collect.assert_called_once_with(self.COVERAGE_DIRECTORY)

    def test_a_failed_collection_does_not_replace_the_runs_result(self):
        self.manager.run.return_value = 7
        self.collect.side_effect = OSError('No space left on device')
        with self.assertLogs(self.script._log, 'ERROR'):
            self.assertEqual(self.run_tests(), 7)

    def test_a_failed_collection_says_where_the_profiles_still_are(self):
        self.collect.side_effect = OSError('No space left on device')
        with self.assertLogs(self.script._log, 'ERROR') as logged:
            self.run_tests()
        output = '\n'.join(logged.output)
        self.assertIn(self.COVERAGE_DIRECTORY, output)
        self.assertIn('No space left on device', output)
        self.assertIn(COVERAGE_PROFILE_DIRECTORY, output)

    def test_an_interrupted_run_still_collects(self):
        self.manager.run.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_tests()
        self.collect.assert_called_once_with(self.COVERAGE_DIRECTORY)

    def test_a_failed_collection_does_not_replace_an_interrupt(self):
        self.manager.run.side_effect = KeyboardInterrupt
        self.collect.side_effect = OSError('No space left on device')
        with self.assertLogs(self.script._log, 'ERROR'), self.assertRaises(KeyboardInterrupt):
            self.run_tests()


class CoverageDirectoryCheckParityTest(_RunAPITests):
    def failure(self, verify):
        filesystem = MockFileSystem()
        filesystem.write_binary_file = mock.Mock(side_effect=OSError('Permission denied'))
        with self.assertRaises(RuntimeError) as raised:
            verify(filesystem, '/coverage')
        return filesystem.write_binary_file.call_args[0][0], str(raised.exception)

    def test_both_harnesses_probe_the_same_file_and_report_the_same_failure(self):
        self.assertEqual(self.failure(self.script._verify_coverage_directory_is_writable),
                         self.failure(run_webkit_tests._verify_coverage_directory_is_writable))


class _WorkerTest(unittest.TestCase):
    PROCESS_NAME = 'worker/2'

    def setUp(self):
        self.options = {}
        self.port = mock.MagicMock()
        self.port.get_option.side_effect = self.options.get
        self.port.DEVICE_MANAGER = None
        self.port.port_name = 'mac'
        self.port.host.platform.is_win.return_value = False
        for name, value in (('name', self.PROCESS_NAME), ('queue', mock.MagicMock())):
            patcher = mock.patch.object(TaskPool.Process, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class _CollectorConstruction:
    """Mixed into a _WorkerTest that defines SUITE and make_worker()."""

    def test_no_collector_is_built_unless_per_test_coverage_is_requested(self):
        with mock.patch(COLLECTOR_FACTORY) as factory:
            worker = self.make_worker()
        factory.assert_not_called()
        self.assertIsNone(worker._coverage_collector)

    def test_the_collector_is_built_for_this_worker(self):
        self.options['per_test_coverage'] = True
        with mock.patch(COLLECTOR_FACTORY) as factory:
            worker = self.make_worker()
        factory.assert_called_once_with(self.port, self.PROCESS_NAME, self.SUITE)
        self.assertIs(worker._coverage_collector, factory.return_value)


class LayoutWorkerCollectorTest(_CollectorConstruction, _WorkerTest):
    SUITE = 'layout'

    def make_worker(self):
        return layout_test_runner.Worker(self.port, '/results')


class APIWorkerCollectorTest(_CollectorConstruction, _WorkerTest):
    SUITE = 'api'

    def make_worker(self):
        return runner._Worker(self.port, 1000)


class APIWorkerRunsATestTest(_WorkerTest):
    TEST = 'Suite.Case'
    FULL_NAME = 'TestWTF.Suite.Case'
    PROFILE_ENVIRONMENT = {'LLVM_PROFILE_FILE': '/profiles/case.profraw'}

    def setUp(self):
        super().setUp()
        self.events = []
        self.port.environment_for_api_tests.return_value = {'PATH': '/bin'}
        self.port.path_to_api_test.return_value = '/build/TestWTF'

        self.server = mock.MagicMock()
        self.server.read_either_stdout_or_stderr_line.return_value = ('**PASS**\n', None)
        self.server.timed_out = False
        self.server.has_crashed.return_value = False
        self.server.pop_all_buffered_stdout.return_value = ''
        self.server.pop_all_buffered_stderr.return_value = ''
        self.server.stop.side_effect = lambda: self.events.append(('stop',))

        def start_process(*arguments, **keywords):
            self.events.append(('process', keywords['env']))
            return self.server

        patcher = mock.patch.object(runner, 'ServerProcess', side_effect=start_process)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_test(self, collector=None):
        if collector:
            self.options['per_test_coverage'] = True
        with mock.patch(COLLECTOR_FACTORY, return_value=collector):
            worker = runner._Worker(self.port, 1000)
        worker._run_single_test('TestWTF', self.TEST)

    def test_the_test_process_gets_the_collectors_environment_and_is_indexed_after_it_stops(self):
        collector = mock.MagicMock()
        collector.begin.side_effect = lambda name: (
            self.events.append(('begin', name)) or dict(self.PROFILE_ENVIRONMENT))
        collector.finish.side_effect = lambda name, extra_objects=(): self.events.append(
            ('finish', name, list(extra_objects)))
        self.run_test(collector)
        self.assertEqual(self.events, [
            ('begin', self.FULL_NAME),
            ('process', {'PATH': '/bin', **self.PROFILE_ENVIRONMENT}),
            ('stop',),
            ('finish', self.FULL_NAME, ['/build/TestWTF']),
        ])

    def test_a_run_without_per_test_coverage_adds_nothing_to_the_environment(self):
        self.run_test()
        self.assertEqual(self.events, [('process', {'PATH': '/bin'}), ('stop',)])


class LazyCoverageImportTest(unittest.TestCase):
    SCRIPT = ('import importlib, sys; importlib.import_module(sys.argv[1]); '
              'assert "webkitpy.coverage_attribution" not in sys.modules')

    def test_starting_a_harness_does_not_import_the_coverage_stack(self):
        for module in ('webkitpy.layout_tests.controllers.layout_test_runner',
                       'webkitpy.api_tests.runner',
                       'webkitpy.port.driver'):
            with self.subTest(module):
                result = subprocess.run(
                    [sys.executable, '-c', self.SCRIPT, module], cwd=_scripts_directory(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                    timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

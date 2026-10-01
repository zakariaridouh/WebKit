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
import unittest

from unittest import mock

from webkitpy.common.system.filesystem_mock import MockFileSystem
from webkitpy.layout_tests import run_webkit_tests
from webkitpy.llvm_profile_utils import COVERAGE_PROFILE_DIRECTORY


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

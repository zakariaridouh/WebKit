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
import tempfile
import unittest
from unittest import mock

from webkitpy import coverage_simulator
from webkitpy.coverage_simulator import (CORE_SIMULATOR_DEVICES, collect_container_profiles,
                                         container_profile_paths, report_stray_container_profiles,
                                         simulator_data_paths)


def _simctl_json(devices):
    """`simctl list devices -j` output for (runtime, udid, state, dataPath) tuples."""
    grouped = {}
    for runtime, udid, state, data_path in devices:
        grouped.setdefault(runtime, []).append({
            'udid': udid, 'name': udid, 'state': state, 'dataPath': data_path,
        })
    return json.dumps({'devices': grouped})


class _SimulatorTreeFixture(unittest.TestCase):
    """A directory laid out like a CoreSimulator device's data root.

    Real files rather than a MockFileSystem, because the module walks with os.walk and glob and
    moves with shutil, matching llvm_profile_utils's collection code. Testing the walk against a
    mock would test the mock.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.data_path = os.path.join(self.directory, 'device', 'data')
        self.destination = os.path.join(self.directory, 'collected')

    def _write(self, relative, contents='profile'):
        path = os.path.join(self.data_path, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path


class ContainerProfilePathsTest(_SimulatorTreeFixture):
    def test_finds_both_tmpdir_shapes(self):
        # The two places %t resolves to inside a simulator: the device's own tmp for a spawned
        # process, and an app container's tmp for an installed app.
        spawned = self._write(os.path.join('tmp', 'WebKitCoverage', 'WebCore_1_0.profraw'))
        app = self._write(os.path.join('Containers', 'Data', 'Application', 'ABC-123', 'tmp',
                                       'WebKitCoverage', 'WebKit_2_0.profraw'))
        self.assertEqual(container_profile_paths(self.data_path), sorted([spawned, app]))

    def test_finds_non_application_container_kinds(self):
        # An XPC service or app extension gets a container of a different kind, and the wildcard
        # has to cover it: attributing "no profiles" to a run whose WebContent counters are in a
        # PluginKitPlugin container would be exactly the wrong answer.
        extension = self._write(os.path.join('Containers', 'Data', 'PluginKitPlugin', 'D-4',
                                             'tmp', 'WebCore_3_0.profraw'))
        self.assertEqual(container_profile_paths(self.data_path), [extension])

    def test_ignores_everything_that_is_not_a_profile(self):
        self._write(os.path.join('tmp', 'notes.txt'))
        self._write(os.path.join('tmp', 'com.apple.dt.instruments', 'something.plist'))
        self.assertEqual(container_profile_paths(self.data_path), [])

    def test_a_correctly_built_tree_has_none(self):
        # The case that should hold on every real simulator run: /private/tmp inside the
        # simulator is the host's, so the profiles are never in here at all.
        os.makedirs(os.path.join(self.data_path, 'tmp'))
        self.assertEqual(container_profile_paths(self.data_path), [])

    def test_missing_data_root_is_not_an_error(self):
        self.assertEqual(container_profile_paths(os.path.join(self.directory, 'gone')), [])


class CollectContainerProfilesTest(_SimulatorTreeFixture):
    def test_moves_rather_than_copies(self):
        source = self._write(os.path.join('tmp', 'WebCore_1_0.profraw'))
        collected = collect_container_profiles(self.destination, [self.data_path])
        self.assertEqual([os.path.basename(path) for path in collected], ['WebCore_1_0.profraw'])
        self.assertFalse(os.path.exists(source))
        # Moved, so calling it twice cannot fold the same counters in again -- %Nm merges into an
        # existing profile rather than replacing it.
        self.assertEqual(collect_container_profiles(self.destination, [self.data_path]), [])

    def test_keeps_the_product_prefix(self):
        # The report groups profiles by the text before the first '_', so renaming a collected
        # profile would silently detach it from the product it measures.
        self._write(os.path.join('tmp', 'WebCore_4820_0.profraw'))
        collected = collect_container_profiles(self.destination, [self.data_path])
        self.assertTrue(os.path.basename(collected[0]).startswith('WebCore_'))

    def test_two_devices_with_the_same_pooled_name_do_not_clobber(self):
        # %8m pools by module, so every device in a parallel run writes the same filenames.
        second = os.path.join(self.directory, 'device2', 'data')
        for root, contents in ((self.data_path, 'first'), (second, 'second')):
            path = os.path.join(root, 'tmp', 'WebCore_1_0.profraw')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as handle:
                handle.write(contents)

        collect_container_profiles(self.destination, [self.data_path, second])

        self.assertEqual(sorted(os.listdir(self.destination)),
                         ['WebCore_1_0-1.profraw', 'WebCore_1_0.profraw'])

    def test_no_profiles_creates_no_destination(self):
        # Called on every simulator run that collected nothing, including runs that were simply
        # not built with instrumentation, so it must not leave an empty directory behind.
        os.makedirs(os.path.join(self.data_path, 'tmp'))
        self.assertEqual(collect_container_profiles(self.destination, [self.data_path]), [])
        self.assertFalse(os.path.exists(self.destination))


class ReportStrayContainerProfilesTest(_SimulatorTreeFixture):
    def test_warns_and_names_the_cause(self):
        self._write(os.path.join('tmp', 'WebCore_1_0.profraw'))
        with self.assertLogs(coverage_simulator.logger, level='WARNING') as logs:
            collected = report_stray_container_profiles(self.destination, [self.data_path])
        self.assertEqual(len(collected), 1)
        self.assertIn('%t', '\n'.join(logs.output))
        self.assertIn('iOSCoverage.md', '\n'.join(logs.output))

    def test_silent_when_there_is_nothing_to_report(self):
        os.makedirs(os.path.join(self.data_path, 'tmp'))
        with mock.patch.object(coverage_simulator.logger, 'warning') as warning:
            self.assertEqual(report_stray_container_profiles(self.destination, [self.data_path]),
                             [])
        warning.assert_not_called()


class SimulatorDataPathsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _executive(self, output=None, failure=None):
        executive = mock.Mock()
        if failure is not None:
            executive.run_command.side_effect = failure
        else:
            executive.run_command.return_value = output
        return executive

    def test_prefers_the_data_path_simctl_reports(self):
        # dataPath rather than a path assembled from the udid, because a CoreSimulator
        # environment override can move a device's filesystem.
        output = _simctl_json([
            ('iOS-27-5', 'UDID-A', 'Booted', '/elsewhere/A/data'),
            ('iOS-27-5', 'UDID-B', 'Shutdown', '/elsewhere/B/data'),
        ])
        self.assertEqual(simulator_data_paths(executive=self._executive(output)),
                         ['/elsewhere/A/data', '/elsewhere/B/data'])

    def test_booted_only_and_udid_filters(self):
        output = _simctl_json([
            ('iOS-27-5', 'UDID-A', 'Booted', '/d/A'),
            ('iOS-27-5', 'UDID-B', 'Shutdown', '/d/B'),
            ('iOS-26-0', 'UDID-C', 'Booted', '/d/C'),
        ])
        self.assertEqual(
            simulator_data_paths(executive=self._executive(output), booted_only=True),
            ['/d/A', '/d/C'])
        self.assertEqual(
            simulator_data_paths(executive=self._executive(output), udids=['UDID-B']), ['/d/B'])

    def test_falls_back_to_the_on_disk_layout(self):
        # The caller is already in the degraded case -- a run that collected nothing -- so an
        # unusable simctl must not turn that into no diagnostic at all.
        for udid in ('UDID-A', 'UDID-B'):
            os.makedirs(os.path.join(self.directory, udid, 'data'))
        os.makedirs(os.path.join(self.directory, 'not-a-device'))
        with mock.patch.object(coverage_simulator, 'CORE_SIMULATOR_DEVICES', self.directory):
            paths = simulator_data_paths(executive=self._executive(failure=OSError('no simctl')))
        self.assertEqual(paths, [os.path.join(self.directory, 'UDID-A', 'data'),
                                 os.path.join(self.directory, 'UDID-B', 'data')])

    def test_unparseable_simctl_output_falls_back_too(self):
        os.makedirs(os.path.join(self.directory, 'UDID-A', 'data'))
        with mock.patch.object(coverage_simulator, 'CORE_SIMULATOR_DEVICES', self.directory):
            paths = simulator_data_paths(executive=self._executive('not json at all'))
        self.assertEqual(paths, [os.path.join(self.directory, 'UDID-A', 'data')])

    def test_no_simulators_anywhere_is_not_an_error(self):
        with mock.patch.object(coverage_simulator, 'CORE_SIMULATOR_DEVICES',
                               os.path.join(self.directory, 'gone')):
            self.assertEqual(simulator_data_paths(executive=self._executive('{"devices": {}}')),
                             [])

    def test_the_default_devices_location_is_the_documented_one(self):
        self.assertTrue(CORE_SIMULATOR_DEVICES.endswith(
            os.path.join('Library', 'Developer', 'CoreSimulator', 'Devices')))


if __name__ == '__main__':
    unittest.main()

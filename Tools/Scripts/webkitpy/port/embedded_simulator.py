# Copyright (C) 2014-2025 Apple Inc. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1.  Redistributions of source code must retain the above copyright
#     notice, this list of conditions and the following disclaimer.
# 2.  Redistributions in binary form must reproduce the above copyright
#     notice, this list of conditions and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY APPLE INC. AND ITS CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL APPLE INC. OR ITS CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import logging

from webkitcorepy import Version

from webkitpy.common.memoized import memoized
from webkitpy.coverage_simulator import (report_stray_container_profiles,
                                         simulator_data_paths)
from webkitpy.port.embedded_port import EmbeddedPort
from webkitpy.xcode.simulated_device import SimulatedDeviceManager

_log = logging.getLogger(__name__)


class EmbeddedSimulatorPort(EmbeddedPort):
    """Base class for simulator ports (iOS, watchOS, visionOS simulators)."""

    DEVICE_MANAGER = SimulatedDeviceManager

    @staticmethod
    def _version_from_name(name):
        if len(name.split('-')) > 2 and name.split('-')[2].isdigit():
            return Version.from_string(name.split('-')[2])
        return None

    @memoized
    def device_version(self):
        if self.get_option('version'):
            return Version.from_string(self.get_option('version'))
        return self._version_from_name(self._name) if self._version_from_name(self._name) else self.host.platform.xcode_sdk_version(self.SDK)

    def environment_for_api_tests(self):
        inherited_env = super().environment_for_api_tests()
        new_environment = {}
        SIMCTL_ENV_PREFIX = 'SIMCTL_CHILD_'
        for value in inherited_env:
            if not value.startswith(SIMCTL_ENV_PREFIX):
                new_environment[SIMCTL_ENV_PREFIX + value] = inherited_env[value]
            else:
                new_environment[value] = inherited_env[value]
        return new_environment

    def setup_environ_for_server(self, server_name=None):
        _log.debug('Setting up environment for server on {}'.format(self.operating_system()))
        env = super().setup_environ_for_server(server_name)
        if server_name == self.driver_name() and self.get_option('leaks'):
            env['MallocStackLogging'] = '1'
            env['__XPC_MallocStackLogging'] = '1'
            env['MallocScribble'] = '1'
            env['__XPC_MallocScribble'] = '1'
        return env

    def reset_preferences(self):
        SimulatedDeviceManager.tear_down(self.host)

    def collect_stray_coverage_profiles(self, destination_directory):
        """Raw profiles that landed inside the simulators this run used, rather than on the host.

        A correctly built simulator coverage tree never produces any: /private/tmp inside a
        CoreSimulator runtime is the host's /private/tmp, so the baked
        /private/tmp/WebKitCoverage path resolves to the directory the harness already collects
        from, and this returns []. It is called only when that collection found nothing, and its
        job is to name the one other place the profiles can be -- a TMPDIR-relative (%t) path,
        which puts them inside the simulator where nothing looks. See
        webkitpy/coverage_simulator.py for the measurements behind that.

        The devices this run used are preferred over every simulator on the machine, so that a
        stale profile in some unrelated device cannot be folded into this run's report. Falling
        back to all of them when the run's devices are not known is safe in the other direction:
        collect_container_profiles() moves the files, so a second call finds nothing.
        """
        udids = None
        try:
            udids = [device.udid for device in self.devices() if getattr(device, 'udid', None)]
        except Exception as failure:  # noqa: BLE001 - device enumeration raises several types
            _log.debug('Could not list this run\'s simulators: %s', failure)
        data_paths = simulator_data_paths(executive=self._executive, udids=udids or None)
        return report_stray_container_profiles(destination_directory, data_paths)

    @property
    @memoized
    def developer_dir(self):
        return self._executive.run_command(['xcode-select', '--print-path']).rstrip()

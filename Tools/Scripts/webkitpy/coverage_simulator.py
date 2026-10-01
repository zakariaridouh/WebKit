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

"""Raw coverage profiles that landed inside a simulator's own filesystem.
A baked %t (TMPDIR) path lands there, not in the host's /private/tmp that collection reads; this finds and moves them.
"""

import glob
import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# simctl's per-device dataPath is preferred; this is the fallback when simctl cannot be run.
CORE_SIMULATOR_DEVICES = os.path.expanduser('~/Library/Developer/CoreSimulator/Devices')

# Where a %t-baked profile lands, relative to a device data root: TMPDIR of a `simctl spawn`ed
# process, and of an installed app or extension (Containers/Data/<kind>/<UUID>/tmp).
CONTAINER_PROFILE_SUBPATHS = ('tmp', os.path.join('Containers', 'Data', '*', '*', 'tmp'))

PROFILE_SUFFIX = '.profraw'


def simulator_data_paths(executive=None, udids=None, booted_only=False):
    """The host-visible data root of each simulator; falls back to the CoreSimulator layout if simctl fails."""
    command = ['/usr/bin/xcrun', 'simctl', 'list', 'devices', '-j']
    output = None
    try:
        if executive:
            output = executive.run_command(command, return_exit_code=False)
        else:
            output = subprocess.run(command, check=True, text=True, capture_output=True,
                                    timeout=120).stdout
    except Exception as failure:  # noqa: BLE001 - executive raises ScriptError, subprocess raises several
        logger.debug('Could not list simulators with %s: %s', ' '.join(command), failure)

    paths = []
    if output:
        try:
            devices = json.loads(output).get('devices', {})
        except ValueError as failure:
            logger.debug('Could not parse `simctl list devices -j`: %s', failure)
            devices = {}
        for runtime_devices in devices.values():
            for device in runtime_devices:
                if udids is not None and device.get('udid') not in udids:
                    continue
                if booted_only and device.get('state') != 'Booted':
                    continue
                data_path = device.get('dataPath')
                if data_path and data_path not in paths:
                    paths.append(data_path)
        if paths:
            return paths

    # The on-disk fallback cannot honour booted_only, so it returns more than asked for.
    if not os.path.isdir(CORE_SIMULATOR_DEVICES):
        return paths
    for name in sorted(os.listdir(CORE_SIMULATOR_DEVICES)):
        if udids is not None and name not in udids:
            continue
        data_path = os.path.join(CORE_SIMULATOR_DEVICES, name, 'data')
        if os.path.isdir(data_path) and data_path not in paths:
            paths.append(data_path)
    return paths


def container_profile_paths(data_path):
    """Every .profraw inside one simulator's own filesystem, sorted."""
    found = []
    for subpath in CONTAINER_PROFILE_SUBPATHS:
        for root_directory in sorted(glob.glob(os.path.join(data_path, subpath))):
            for directory, _, names in os.walk(root_directory):
                for name in names:
                    if name.endswith(PROFILE_SUFFIX):
                        found.append(os.path.join(directory, name))
    return sorted(found)


def collect_container_profiles(destination_directory, data_paths):
    """Move stray in-simulator profiles into destination_directory and return their new paths.
    Moved, not copied, so a later run cannot merge into them; names are kept because the report groups by prefix."""
    collected = []
    for data_path in data_paths:
        for source in container_profile_paths(data_path):
            os.makedirs(destination_directory, exist_ok=True)
            name = os.path.basename(source)
            target = os.path.join(destination_directory, name)
            suffix = 0
            while os.path.exists(target):
                suffix += 1
                base, extension = os.path.splitext(name)
                target = os.path.join(
                    destination_directory, '{}-{}{}'.format(base, suffix, extension))
            shutil.move(source, target)
            collected.append(target)
    return collected


def report_stray_container_profiles(destination_directory, data_paths):
    """collect_container_profiles(), plus a warning that names where the profiles were."""
    collected = collect_container_profiles(destination_directory, data_paths)
    if not collected:
        return collected
    logger.warning(
        'Found %d raw profile(s) inside the simulator rather than in the machine-global '
        'coverage directory, and moved them into %s. That means this build baked a profile path '
        'relative to the process\'s own TMPDIR (%%t) instead of /private/tmp/WebKitCoverage. '
        'A simulator shares /private/tmp with the host, so the baked path should be the same one '
        'macOS uses; see Tools/CodeCoverage/iOSCoverage.md.',
        len(collected), destination_directory)
    return collected

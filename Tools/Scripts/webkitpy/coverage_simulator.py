#!/usr/bin/env python3
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

The headline is a negative result, and it is the reason this module is a fallback rather than
the collection path: **a simulator coverage run needs no path translation.** /private/tmp inside
a CoreSimulator runtime is the host's /private/tmp, so an instrumented process in the simulator
writing the baked /private/tmp/WebKitCoverage/<Product>_%8m%c.profraw puts the file exactly where
llvm_profile_utils.collect_coverage_profiles() already looks.

That was measured, not assumed. Against iphonesimulator27.5 on an iPhone 17 Pro simulator
running iOS 27.5:

  * A hand-written C program compiled with -fprofile-instr-generate -fcoverage-mapping and run
    with `simctl spawn` wrote a 49,152-byte .profraw at a /private/tmp path created on the host,
    and llvm-profdata merged it and llvm-cov reported 100% of its lines.
  * The same program packaged as a signed app, installed with `simctl install` and launched with
    `simctl launch` -- so through LaunchServices, with an app container -- carrying a *baked*
    `__llvm_profile_filename` of `/private/tmp/<dir>/app_%8m%c.profraw`, produced the file at
    that same host path.
  * sandbox_check(getpid(), NULL, 0) returned 0 in that app: no seatbelt profile is applied to a
    simulator process at all.

So what is left for this module is the failure that becomes possible the moment anyone changes
the baked path to %t, as the iOS PGO branches in InitializeThreading.cpp and
WebKit2InitializeCocoa.mm do. %t is TMPDIR, and in the simulator TMPDIR is *inside* the
simulator: `<data>/tmp` for a `simctl spawn`ed process and
`<data>/Containers/Data/Application/<UUID>/tmp` for an installed app. Nothing collects from
either, so the run reports that the tests executed nothing -- hours later, with no error.

The functions here find such profiles and, on request, move them where the report can read them.
They are a diagnostic: a run that needs them has a build whose profile path is wrong, and the
right fix is the path. Both simulator directories are host-visible ordinary directories, which is
why no simctl call is needed to read them; simctl is used only to enumerate the devices.
"""

import glob
import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Where CoreSimulator keeps a device's filesystem on the host. `simctl list devices -j` reports
# the same path as each device's "dataPath", and that is what simulator_data_paths() prefers,
# because a device can be relocated with a CoreSimulator environment override. This is the
# fallback for a host where simctl cannot be run.
CORE_SIMULATOR_DEVICES = os.path.expanduser('~/Library/Developer/CoreSimulator/Devices')

# The two places a %t-baked profile path can land inside a simulator, relative to a device's
# data root. Deliberately not a walk of the whole data root: that was 2.66 GB on the device this
# was measured against, almost none of it profiles.
#
#   tmp                             TMPDIR for a process spawned with `simctl spawn`
#   Containers/Data/*/*/tmp         TMPDIR for an installed app or app extension; the two
#                                   wildcards are the container kind (Application,
#                                   PluginKitPlugin, InternalDaemon, ...) and its UUID
CONTAINER_PROFILE_SUBPATHS = ('tmp', os.path.join('Containers', 'Data', '*', '*', 'tmp'))

PROFILE_SUFFIX = '.profraw'


def simulator_data_paths(executive=None, udids=None, booted_only=False):
    """The host-visible data root of each simulator, newest simctl answer first.

    executive is a webkitpy Executive when one is at hand and None to use subprocess directly,
    so that this module can be called from a script that has no Host. udids filters to specific
    devices; booted_only filters to the ones currently running, which is what a test run used.

    Falls back to the on-disk CoreSimulator layout if simctl cannot be run or its output cannot
    be parsed, because the caller is already in a degraded case -- a run that collected nothing --
    and an unreadable simctl should not turn that into no diagnostic at all.
    """
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

    # simctl told us nothing usable. Read the layout instead. This cannot honour booted_only --
    # nothing on disk says which devices are running -- so it deliberately returns more than
    # asked for rather than fewer: an extra empty data root costs one directory listing.
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
    """Every .profraw inside one simulator's own filesystem, sorted.

    Empty for a correctly configured coverage build, which writes to the host's
    /private/tmp/WebKitCoverage instead. See the module docstring.
    """
    found = []
    for subpath in CONTAINER_PROFILE_SUBPATHS:
        for root_directory in sorted(glob.glob(os.path.join(data_path, subpath))):
            for directory, _, names in os.walk(root_directory):
                for name in names:
                    if name.endswith(PROFILE_SUFFIX):
                        found.append(os.path.join(directory, name))
    return sorted(found)


def collect_container_profiles(destination_directory, data_paths):
    """Move stray in-simulator profiles into destination_directory, and return their paths.

    Names are kept as they are and made unique on collision, exactly as
    llvm_profile_utils.collect_coverage_profiles() does. Renaming them would be worse than a
    collision: the report groups collected profiles by the product prefix before the first '_',
    so `WebCore_1234_0.profraw` has to stay `WebCore_...` to be attributed to WebCore at all.

    Moved rather than copied, so that a following run cannot fold this run's counters into its
    own -- %Nm merges into an existing profile rather than replacing it.
    """
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
                target = os.path.join(destination_directory, '{}-{}{}'.format(base, suffix,
                                                                             extension))
            shutil.move(source, target)
            collected.append(target)
    return collected


def report_stray_container_profiles(destination_directory, data_paths):
    """collect_container_profiles(), plus the explanation the caller would otherwise have to write.

    For the one caller that matters: a simulator run that collected nothing from the
    machine-global directory. Saying "no profiles" there is true and useless; naming the
    simulator directory the profiles are actually in is the whole value.
    """
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

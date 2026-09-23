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

"""The LLVM_PROFILE_FILE pair for the test currently being started, or nothing.

A module of its own, with no imports at all, so that webkitpy/port/driver.py can read this
without depending on the coverage stack. driver.py is on the import path of every layout-test
run whether it collects coverage or not, and reaching into coverage_attribution for it made
driver.py depend transitively on coverage_lcov, llvm_profile_utils and coverage_delta -- for a
function that returns an empty dict on every run that is not collecting per-test coverage.

That is a layering argument and not a speed one: measured as the minimum of five fresh
processes, `import webkitpy.port.driver` is 211 ms and adding coverage_lcov to it is 213 ms,
because driver.py already pays for most of the chain underneath. The cost of the old shape was
that a change to the coverage modules could break every port's driver.

The state is a module global, and deliberately. The layout-test worker decides which test is
next; the environment is assembled four frames below it, inside
Driver._setup_environ_for_driver(), from a port method that copies a fixed allow-list of
variables out of os.environ -- so LLVM_PROFILE_FILE cannot simply be exported. Threading a
per-test value from the worker through create_driver() and Driver.__init__() would touch every
port's driver subclass. The worker and the driver are the same process, and this is set and
cleared around one test at a time in it.
"""

_current_profile_environment = {}


def set_current_profile_environment(environment):
    global _current_profile_environment
    _current_profile_environment = dict(environment or {})


def current_profile_environment():
    """The LLVM_PROFILE_FILE pair for the test being started, or {} when not collecting."""
    return dict(_current_profile_environment)

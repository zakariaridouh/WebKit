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

"""run-api-tests' coverage plumbing: the two cliffs run-webkit-tests already guards against.

Asserted against the source rather than by running the harness, because run-api-tests is a
script and not a module -- importing it would execute maybe_enter_webkit_container_sdk() and
construct a Host. The two shapes checked here are the ones that cost a whole run and are easy
to reintroduce: validating --coverage-dir only by using it, in a finally block after the tests,
and letting a failure in that finally block replace the run's own result.
"""

import ast
import os
import unittest


def _scripts_directory():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(*components):
    with open(os.path.join(_scripts_directory(), *components)) as handle:
        return handle.read()


class RunAPITestsCoverageCollectionTest(unittest.TestCase):
    def setUp(self):
        self.source = _read('run-api-tests')
        self.tree = ast.parse(self.source)

    def _function(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail('run-api-tests has no {}()'.format(name))

    def test_the_coverage_directory_is_verified_before_the_run(self):
        # Not after it. --coverage-dir is used in a finally block once the tests have finished,
        # so a typo or an unwritable parent used to be discovered when there was already a
        # result to lose -- and then lost it.
        main = self._function('main')
        verification = None
        run_call = None
        for node in ast.walk(main):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == '_verify_coverage_directory_is_writable':
                    verification = node.lineno
                elif node.func.id == 'run':
                    run_call = node.lineno
        self.assertIsNotNone(verification,
                             'main() must verify --coverage-dir before it starts the run')
        self.assertIsNotNone(run_call)
        self.assertLess(verification, run_call)

    def test_the_collection_cannot_discard_the_runs_result(self):
        # A raise inside a finally block replaces whatever the try block produced, so an
        # unguarded collection turns a passing run into a traceback with no test results, and
        # turns Ctrl-C into an OSError about the profile directory.
        collection = self.source.index('collect_coverage_profiles(options.coverage_dir)')
        preceding = self.source[:collection]
        self.assertIn('try:', preceding[preceding.rindex('finally:'):])

    def test_a_failed_collection_says_where_the_profiles_still_are(self):
        # They are not lost: they are in the machine-global directory, which the *next* coverage
        # run clears. Saying so is the difference between a recoverable and an unrecoverable run.
        self.assertIn('COVERAGE_PROFILE_DIRECTORY', self.source)
        self.assertIn('still in', self.source)

    def test_it_says_the_same_thing_as_run_webkit_tests(self):
        # The two harnesses share the option and the failure but not a module, so the wording and
        # the probe filename are asserted to match rather than left to drift.
        layout = _read('webkitpy', 'layout_tests', 'run_webkit_tests.py')
        for shared in ('.webkit-coverage-writable',
                       'cannot be written to',
                       'moved there when it finishes',
                       'before the next coverage run, which clears that directory.'):
            self.assertIn(shared, layout, 'run_webkit_tests.py no longer says %r' % shared)
            self.assertIn(shared, self.source, 'run-api-tests does not say %r' % shared)


if __name__ == '__main__':
    unittest.main()

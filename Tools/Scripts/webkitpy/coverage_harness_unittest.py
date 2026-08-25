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

"""The coverage plumbing in run-webkit-tests, and the two first-run cliffs in it.

Kept out of run_webkit_tests_integrationtest.py because these are unit tests of two functions
and need no harness: one writes a file into a directory, and the other must not let a failure to
move some profiles discard a whole run's results.
"""

import os
import shutil
import stat
import tempfile
import unittest

from webkitpy.common.system.filesystem import FileSystem
from webkitpy.layout_tests.run_webkit_tests import _verify_coverage_directory_is_writable


class CoverageDirectoryValidationTest(unittest.TestCase):
    """--coverage-dir was validated only by being used, in a finally block after the run.

    So a typo or an unwritable parent cost the whole run and then reported 254 with a traceback
    instead of the run's own exit code. It is a filesystem check that takes microseconds; it
    belongs before the tests.
    """

    def setUp(self):
        self.filesystem = FileSystem()
        self.directory = tempfile.mkdtemp(prefix='coverage-harness-')
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_an_existing_writable_directory_passes(self):
        _verify_coverage_directory_is_writable(self.filesystem, self.directory)

    def test_a_directory_that_does_not_exist_yet_is_created(self):
        target = os.path.join(self.directory, 'a', 'b', 'coverage')
        _verify_coverage_directory_is_writable(self.filesystem, target)
        self.assertTrue(os.path.isdir(target))

    def test_nothing_is_left_behind(self):
        _verify_coverage_directory_is_writable(self.filesystem, self.directory)
        self.assertEqual(os.listdir(self.directory), [])

    def test_an_unwritable_parent_is_reported_before_the_run(self):
        parent = os.path.join(self.directory, 'readonly')
        os.makedirs(parent)
        os.chmod(parent, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, parent, stat.S_IRWXU)
        with self.assertRaises(RuntimeError) as raised:
            _verify_coverage_directory_is_writable(self.filesystem,
                                                   os.path.join(parent, 'coverage'))
        self.assertIn('cannot be written to', str(raised.exception))
        # The message has to say why it is being said now, or it reads as a refusal to run.
        self.assertIn('after the run', str(raised.exception))

    def test_a_path_that_is_a_file_is_reported(self):
        path = os.path.join(self.directory, 'not-a-directory')
        with open(path, 'w') as handle:
            handle.write('')
        with self.assertRaises(RuntimeError):
            _verify_coverage_directory_is_writable(self.filesystem, path)


class CollectionMustNotDiscardTheRunTest(unittest.TestCase):
    """A raise inside a finally block replaces whatever the try block produced.

    Which is how a failure to move the profiles turned a passing run into exit 254 with a
    traceback and no test results at all. This is the language rule the fix is about, asserted
    directly, because the shape it guards against is easy to reintroduce and the harness cannot
    be run here to observe it.
    """

    def unguarded(self):
        try:
            return 'the run passed'
        finally:
            raise OSError('could not move the profiles')

    def guarded(self):
        try:
            return 'the run passed'
        finally:
            try:
                raise OSError('could not move the profiles')
            except OSError:
                pass

    def test_an_unguarded_finally_discards_the_result(self):
        with self.assertRaises(OSError):
            self.unguarded()

    def test_a_guarded_finally_keeps_it(self):
        self.assertEqual(self.guarded(), 'the run passed')

    def test_an_unguarded_finally_also_replaces_a_keyboard_interrupt(self):
        # Ctrl-C during a coverage run reported an OSError about the profile directory instead
        # of the interrupt, so main() returned 254 rather than INTERRUPTED_EXIT_STATUS.
        def interrupted():
            try:
                raise KeyboardInterrupt()
            finally:
                raise OSError('could not move the profiles')

        with self.assertRaises(OSError):
            interrupted()

    def test_the_source_guards_the_collection(self):
        # The behavioural test above cannot see run_webkit_tests, and the harness cannot be run
        # from here, so check that the call really is inside a try in the file that matters.
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'webkitpy', 'layout_tests', 'run_webkit_tests.py')
        with open(path) as handle:
            source = handle.read()
        collection = source.index('collect_coverage_profiles(options.coverage_dir)')
        preceding = source[:collection]
        self.assertIn('try:', preceding[preceding.rindex('finally:'):])


if __name__ == '__main__':
    unittest.main()

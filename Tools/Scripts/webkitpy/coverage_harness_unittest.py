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

"""Unit tests for the coverage plumbing in run-webkit-tests."""

import os
import shutil
import stat
import tempfile
import unittest

from webkitpy.common.system.filesystem import FileSystem
from webkitpy.layout_tests.run_webkit_tests import _verify_coverage_directory_is_writable


class CoverageDirectoryValidationTest(unittest.TestCase):
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
        self.assertIn('after the run', str(raised.exception))

    def test_a_path_that_is_a_file_is_reported(self):
        path = os.path.join(self.directory, 'not-a-directory')
        with open(path, 'w') as handle:
            handle.write('')
        with self.assertRaises(RuntimeError):
            _verify_coverage_directory_is_writable(self.filesystem, path)


class CollectionMustNotDiscardTheRunTest(unittest.TestCase):
    """A raise inside a finally block replaces whatever the try block produced."""

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
        def interrupted():
            try:
                raise KeyboardInterrupt()
            finally:
                raise OSError('could not move the profiles')

        with self.assertRaises(OSError):
            interrupted()

    def test_the_source_guards_the_collection(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'webkitpy', 'layout_tests', 'run_webkit_tests.py')
        with open(path) as handle:
            source = handle.read()
        collection = source.index('collect_coverage_profiles(options.coverage_dir)')
        preceding = source[:collection]
        self.assertIn('try:', preceding[preceding.rindex('finally:'):])


if __name__ == '__main__':
    unittest.main()

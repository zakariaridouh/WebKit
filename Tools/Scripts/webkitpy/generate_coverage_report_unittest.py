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

"""The parts of generate-coverage-report that do not need a build directory to exercise."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import unittest


class _Script(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The script has no .py extension, so it cannot simply be imported by name.
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'generate-coverage-report')
        loader = importlib.machinery.SourceFileLoader('generate_coverage_report', path)
        specification = importlib.util.spec_from_loader(loader.name, loader)
        cls.script = importlib.util.module_from_spec(specification)
        loader.exec_module(cls.script)


class ProductSelectionTest(_Script):
    def select(self, products, available=None):
        return self.script.selected_products(
            products, available if available is not None else self.script.INSTRUMENTED_PRODUCTS)

    def test_none_selects_everything(self):
        self.assertEqual(self.select(None), list(self.script.INSTRUMENTED_PRODUCTS))

    def test_a_name_selects_that_product(self):
        self.assertEqual(self.select('JavaScriptCore'),
                         ['JavaScriptCore.framework/Versions/A/JavaScriptCore'])

    def test_several_names_keep_the_declared_order(self):
        # llvm-cov takes the first object positionally and the rest as -object=, so the order
        # is the one thing about the list that is not free to vary with the command line.
        self.assertEqual(self.select('WebKit,JavaScriptCore'),
                         ['JavaScriptCore.framework/Versions/A/JavaScriptCore',
                          'WebKit.framework/Versions/A/WebKit'])

    def test_an_empty_list_selects_nothing(self):
        # Which is how run-javascriptcore-tests asks for only what --object gives it.
        self.assertEqual(self.select(''), [])

    def test_restricting_the_products_selects_none_of_the_test_support_list(self):
        # And is not an error. Validating each list separately made --products=JavaScriptCore
        # fail with "not one of libWebCoreTestSupport.dylib, jsc, webpushd, adattributiond".
        self.assertEqual(self.select('JavaScriptCore', self.script.TEST_SUPPORT_PRODUCTS), [])

    def test_a_test_support_product_can_be_named(self):
        self.assertEqual(self.select('jsc', self.script.TEST_SUPPORT_PRODUCTS), ['jsc'])

    def test_every_known_product_has_a_unique_name(self):
        # Two products with the same basename would make --products ambiguous.
        names = [self.script.product_name(entry) for entry in self.script.KNOWN_PRODUCTS]
        self.assertEqual(len(names), len(set(names)))


class ArgumentValidationTest(_Script):
    def parse(self, *arguments):
        return self.script.parse_args(list(arguments))

    def error(self, expected, *arguments):
        """Assert parse_args rejects these arguments, and for the expected reason.

        The reason matters: a test that only asserts "rejected" passes when a different check
        fires first, which is how the --products check was first tested against a nonexistent
        --coverage-dir and appeared to work.
        """
        # optparse prints the usage and exits 2 from parser.error(); neither belongs in test
        # output.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                self.parse(*arguments)
        self.assertIn(expected, stderr.getvalue())

    # Any existing file will do as an indexed profile: parse_args only checks that it is one.
    PROFILE = '--profdata=' + os.path.abspath(__file__)

    def test_a_misspelled_product_is_rejected_before_anything_is_read(self):
        self.error('--products names JavascriptCore', '--output-dir=/tmp/report', self.PROFILE,
                   '--products=JavascriptCore')

    def test_one_bad_name_among_good_ones_is_still_rejected(self):
        self.error('--products names Nope', '--output-dir=/tmp/report', self.PROFILE,
                   '--products=JavaScriptCore,Nope,WebCore')

    def test_a_real_product_is_accepted(self):
        options = self.parse('--output-dir=/tmp/report', self.PROFILE,
                             '--products=JavaScriptCore,WebCore')
        self.assertEqual(options.products, 'JavaScriptCore,WebCore')

    def test_an_empty_product_list_is_accepted(self):
        self.assertEqual(self.parse('--output-dir=/tmp/report', self.PROFILE,
                                    '--products=').products, '')

    def test_exactly_one_profile_source_is_required(self):
        self.error('exactly one', '--output-dir=/tmp/report')
        self.error('--coverage-dir and --profdata', '--output-dir=/tmp/report',
                   '--coverage-dir=/tmp', self.PROFILE)
        self.error('--profdata and --suite', '--output-dir=/tmp/report', self.PROFILE,
                   '--suite=layout:/tmp/cov')

    def test_a_malformed_suite_is_rejected(self):
        self.error('has no label', '--output-dir=/tmp/report', '--suite=/tmp/cov')

    def test_a_threshold_outside_0_to_100_is_rejected(self):
        self.error('--fail-under-lines is a percentage', '--output-dir=/tmp/report',
                   self.PROFILE, '--fail-under-lines=101')
        self.error('--fail-under-branches is a percentage', '--output-dir=/tmp/report',
                   self.PROFILE, '--fail-under-branches=-1')

    def test_a_threshold_at_either_end_is_accepted(self):
        for value in ('0', '100'):
            self.assertEqual(
                self.parse('--output-dir=/tmp/report', self.PROFILE,
                           '--fail-under-functions=' + value).fail_under_functions, float(value))


if __name__ == '__main__':
    unittest.main()

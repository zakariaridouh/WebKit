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

import ast
import os
import re
import shutil
import tempfile
import unittest

from webkitpy.common.system.filesystem import FileSystem
from webkitpy.common.webkit_finder import WebKitFinder
from webkitpy.coverage_test_scope import (
    DECLINED_NOT_CODE, DECLINED_NO_RULE, EDITED_TEST_EXTENSIONS, MAX_MODIFIED_TESTS,
    TEST_DIRECTORIES_TO_IGNORE, TEST_SUFFIXES_TO_IGNORE, _aligned_candidates, _split_alternation,
    baseline_layout_tests, edited_layout_tests, newly_unskipped_tests,
    read_watchlist_definitions, suggest_scope)


class _Checkout(unittest.TestCase):
    """A throwaway checkout with a watchlist and a handful of layout tests in it."""

    WATCHLIST = '''
# a comment, as the real file has
{
    "DEFINITIONS": {
        "Widgets": {
            "filename": r"Source/WebCore/widgets/"
                        r"|LayoutTests/widgets/",
        },
        "Grouped": {
            "filename": r"Source/WebCore/thing/(Alpha|Beta)Thing\\."
                        r"|LayoutTests/things/",
        },
        "Rotted": {
            "filename": r"Source/WebCore/gone/"
                        r"|LayoutTests/gone-away/",
        },
        "RegexOnly": {
            "filename": r"Source/WebCore/regexy/"
                        r"|LayoutTests/.*regexy",
        },
        "SourceOnly": {
            "filename": r"Source/WebCore/lonely/",
        },
    },
    "CC_RULES": {},
    "MESSAGE_RULES": {},
}
'''

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='coverage-test-scope-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.write(os.path.join('Tools', 'Scripts', 'webkitpy', 'common', 'config', 'watchlist'),
                   self.WATCHLIST)
        # No LayoutTests/regexy on purpose: it would satisfy the alignment rule.
        for test in ('widgets/one.html', 'things/two.html',
                     'svg/four.html', 'fast/css/five.html'):
            self.write(os.path.join('LayoutTests', test), '<html>')

    def write(self, relative, contents=''):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path

    def suggest(self, paths, diff_text=None):
        return suggest_scope(self.root, paths, diff_text)


class AlternationTest(unittest.TestCase):
    def test_a_flat_alternation_splits(self):
        self.assertEqual(_split_alternation('a|b|c'), ['a', 'b', 'c'])

    def test_a_group_is_not_split(self):
        self.assertEqual(_split_alternation(r'Source/x/(Content|DOM)Policy\.|LayoutTests/y/'),
                         [r'Source/x/(Content|DOM)Policy\.', 'LayoutTests/y/'])

    def test_an_escaped_bar_is_not_a_separator(self):
        self.assertEqual(_split_alternation(r'a\|b|c'), [r'a\|b', 'c'])


class WatchlistTest(_Checkout):
    def test_only_blocks_that_co_map_both_are_kept(self):
        definitions = read_watchlist_definitions(self.root)
        self.assertEqual(sorted(definitions), ['Grouped', 'RegexOnly', 'Rotted', 'Widgets'])

    def test_a_grouped_source_pattern_survives_intact(self):
        sources, _ = read_watchlist_definitions(self.root)['Grouped']
        self.assertEqual(sources, [r'Source/WebCore/thing/(Alpha|Beta)Thing\.'])

    def test_a_missing_watchlist_is_not_an_error(self):
        self.assertEqual(read_watchlist_definitions(tempfile.gettempdir() + '/nope'), {})


class SuggestionTest(_Checkout):
    def test_a_watchlist_block_suggests_its_layout_tests(self):
        suggestion = self.suggest(['Source/WebCore/widgets/Widget.cpp'])
        self.assertEqual(suggestion.tests, ['widgets'])
        self.assertIn('Widgets', suggestion.associations[0].rule)

    def test_a_grouped_pattern_still_matches(self):
        self.assertEqual(self.suggest(['Source/WebCore/thing/AlphaThing.cpp']).tests, ['things'])

    def test_a_rotted_mapping_suggests_nothing(self):
        # LayoutTests/gone-away/ is in the watchlist and not on disk.
        suggestion = self.suggest(['Source/WebCore/gone/Gone.cpp'])
        self.assertEqual(suggestion.tests, [])
        self.assertEqual(suggestion.declined[0].reason, DECLINED_NO_RULE)

    def test_a_regex_test_path_is_not_offered(self):
        self.assertEqual(self.suggest(['Source/WebCore/regexy/Regexy.cpp']).tests, [])

    def test_a_source_only_block_is_not_a_mapping(self):
        self.assertEqual(self.suggest(['Source/WebCore/lonely/Lonely.cpp']).tests, [])

    def test_directory_name_alignment_suggests_the_matching_directory(self):
        self.assertEqual(self.suggest(['Source/WebCore/svg/SVGElement.cpp']).tests, ['svg'])

    def test_alignment_falls_back_to_the_fast_form(self):
        self.assertEqual(self.suggest(['Source/WebCore/css/CSSValue.cpp']).tests, ['fast/css'])

    def test_a_modules_feature_aligns_on_the_feature_name(self):
        self.write(os.path.join('LayoutTests', 'widgets', 'nested.html'), '<html>')
        self.assertEqual(self.suggest(['Source/WebCore/Modules/widgets/Widget.cpp']).tests,
                         ['widgets'])

    def test_a_plural_directory_matches_the_singular_name(self):
        self.write(os.path.join('LayoutTests', 'gadgets', 'one.html'), '<html>')
        self.assertEqual(self.suggest(['Source/WebCore/gadget/Gadget.cpp']).tests, ['gadgets'])

    def test_alignment_reaches_the_web_platform_tests(self):
        self.write(os.path.join('LayoutTests', 'imported', 'w3c', 'web-platform-tests', 'gadget',
                                'one.html'), '<html>')
        self.assertEqual(self.suggest(['Source/WebCore/gadget/Gadget.cpp']).tests,
                         ['imported/w3c/web-platform-tests/gadget'])

    def test_candidates_are_only_generated_for_webcore(self):
        self.assertEqual(_aligned_candidates('Source/WebKit/UIProcess/WebPageProxy.cpp'), [])
        self.assertNotEqual(_aligned_candidates('Source/WebCore/svg/SVGElement.cpp'), [])


class RefusalTest(_Checkout):
    def test_a_non_code_file_gets_no_suggestion(self):
        suggestion = self.suggest(['Source/WebCore/Sources.txt'])
        self.assertEqual(suggestion.tests, [])
        self.assertEqual(suggestion.declined[0].reason, DECLINED_NOT_CODE)

    def test_a_build_file_gets_no_suggestion(self):
        for path in ('Source/WebCore/WebCore.xcodeproj/project.pbxproj',
                     'Source/WTF/Scripts/Preferences/UnifiedWebPreferences.yaml'):
            self.assertEqual(self.suggest([path]).tests, [], path)

    def test_an_unscopable_directory_gets_no_suggestion_even_when_a_test_directory_matches(self):
        for component in ('platform', 'rendering', 'style', 'layout', 'page', 'bindings',
                          'loader', 'dom', 'testing'):
            self.write(os.path.join('LayoutTests', component, 'one.html'), '<html>')
            path = 'Source/WebCore/{}/Thing.cpp'.format(component)
            suggestion = self.suggest([path])
            self.assertEqual(suggestion.tests, [], path)
            self.assertIn(component, suggestion.declined[0].reason)

    def test_an_unscopable_directory_deeper_in_the_path_still_refuses(self):
        self.assertEqual(self.suggest(['Source/WebCore/svg/platform/Thing.cpp']).tests, [])

    def test_a_deeper_directory_that_is_not_unscopable_keeps_the_suggestion(self):
        self.assertEqual(self.suggest(['Source/WebCore/svg/graphics/filters/Thing.cpp']).tests,
                         ['svg'])

    def test_the_component_test_is_not_a_substring_test(self):
        self.assertEqual(self.suggest(['Source/WebCore/css/StyleRule.cpp']).tests, ['fast/css'])

    def test_a_path_with_no_rule_gets_no_suggestion(self):
        suggestion = self.suggest(['Source/JavaScriptCore/wasm/WasmTypeDefinition.cpp'])
        self.assertEqual(suggestion.tests, [])
        self.assertEqual(suggestion.declined[0].reason, DECLINED_NO_RULE)

    def test_a_partly_scopable_change_is_not_complete(self):
        suggestion = self.suggest(['Source/WebCore/svg/SVGElement.cpp',
                                   'Source/WebCore/dom/Document.cpp'])
        self.assertEqual(suggestion.tests, ['svg'])
        self.assertFalse(suggestion.complete)
        self.assertEqual(len(suggestion.declined), 1)

    def test_a_wholly_scopable_change_is_complete(self):
        suggestion = self.suggest(['Source/WebCore/svg/SVGElement.cpp'])
        self.assertTrue(suggestion.complete)


class EditedTestsTest(_Checkout):
    def test_an_edited_test_is_the_scope(self):
        self.assertEqual(edited_layout_tests(['LayoutTests/svg/four.html']), ['svg/four.html'])

    def test_html_svg_and_xml_files_are_tests(self):
        self.assertEqual(
            edited_layout_tests(['LayoutTests/fast/a.html', 'LayoutTests/svg/b.svg',
                                 'LayoutTests/dom/c.xml']),
            ['fast/a.html', 'svg/b.svg', 'dom/c.xml'])

    def test_other_files_are_not_tests(self):
        for name in ('script.js', 'notes.txt', 'image.png'):
            self.assertEqual(edited_layout_tests(['LayoutTests/fast/' + name]), [], name)

    def test_a_baseline_or_reference_file_is_not_a_test(self):
        for suffix in TEST_SUFFIXES_TO_IGNORE:
            self.assertEqual(edited_layout_tests(['LayoutTests/svg/four{}.html'.format(suffix)]),
                             [], suffix)

    def test_a_support_directory_holds_no_tests(self):
        for directory in TEST_DIRECTORIES_TO_IGNORE:
            self.assertEqual(edited_layout_tests(
                ['LayoutTests/svg/{}/helper.html'.format(directory)]), [], directory)

    def test_a_source_file_is_not_an_edited_test(self):
        self.assertEqual(edited_layout_tests(['Source/WebCore/svg/SVGElement.cpp']), [])

    def test_a_large_change_is_capped_at_the_ews_limit(self):
        paths = ['LayoutTests/svg/t{}.html'.format(index)
                 for index in range(MAX_MODIFIED_TESTS + 5)]
        self.assertEqual(len(self.suggest(paths).tests), MAX_MODIFIED_TESTS)


class BaselineTest(_Checkout):
    def test_a_baseline_names_its_test(self):
        self.write(os.path.join('LayoutTests', 'svg', 'six.html'), '<html>')
        self.assertEqual(
            baseline_layout_tests(['LayoutTests/svg/six-expected.txt'], self.root),
            ['svg/six.html'])

    def test_a_platform_baseline_resolves_to_the_generic_test(self):
        self.write(os.path.join('LayoutTests', 'svg', 'seven.html'), '<html>')
        self.assertEqual(
            baseline_layout_tests(['LayoutTests/platform/glib/svg/seven-expected.txt'],
                                  self.root),
            ['svg/seven.html'])

    def test_a_baseline_for_a_test_that_does_not_exist_is_not_suggested(self):
        self.assertEqual(baseline_layout_tests(['LayoutTests/svg/absent-expected.txt'],
                                               self.root), [])

    def test_a_mismatch_baseline_is_handled(self):
        self.write(os.path.join('LayoutTests', 'svg', 'eight.html'), '<html>')
        self.assertEqual(
            baseline_layout_tests(['LayoutTests/svg/eight-expected-mismatch.html'], self.root),
            ['svg/eight.html'])


class UnskippedTest(unittest.TestCase):
    DIFF = '''diff --git a/LayoutTests/TestExpectations b/LayoutTests/TestExpectations
--- a/LayoutTests/TestExpectations
+++ b/LayoutTests/TestExpectations
@@ -100,2 +100,2 @@
-svg/one.html [ Skip ]
+svg/one.html [ Pass Failure ]
+svg/two.html [ Failure ]
+# svg/commented.html [ Failure ]
+svg/three.html [ Skip ]
diff --git a/Source/WebCore/svg/SVGElement.cpp b/Source/WebCore/svg/SVGElement.cpp
--- a/Source/WebCore/svg/SVGElement.cpp
+++ b/Source/WebCore/svg/SVGElement.cpp
@@ -1,0 +2,1 @@
+    unrelated.html();
'''

    def test_an_unskipped_test_is_found(self):
        self.assertIn('svg/one.html', newly_unskipped_tests(self.DIFF))
        self.assertIn('svg/two.html', newly_unskipped_tests(self.DIFF))

    def test_a_line_that_is_still_skipped_is_not(self):
        self.assertNotIn('svg/three.html', newly_unskipped_tests(self.DIFF))

    def test_a_comment_is_not_an_expectation(self):
        self.assertNotIn('svg/commented.html', newly_unskipped_tests(self.DIFF))

    def test_only_expectations_files_are_read(self):
        # Otherwise every added line of C++ mentioning a .html string becomes a test.
        self.assertNotIn('unrelated.html', newly_unskipped_tests(self.DIFF))

    def test_an_empty_diff_finds_nothing(self):
        self.assertEqual(newly_unskipped_tests(''), [])


class _RealCheckout(unittest.TestCase):
    def setUp(self):
        self.checkout = WebKitFinder(FileSystem()).webkit_base()


class EWSDriftTest(_RealCheckout):
    """The constants copied from the EWS step still match it."""

    def setUp(self):
        super().setUp()
        path = os.path.join(self.checkout, 'Tools', 'CISupport', 'ews-build', 'steps.py')
        if not os.path.exists(path):
            self.skipTest('no EWS configuration in this checkout')
        try:
            with open(path) as handle:
                tree = ast.parse(handle.read())
        except SyntaxError:
            self.skipTest('steps.py does not parse with this Python')
        steps = [node for node in ast.walk(tree)
                 if isinstance(node, ast.ClassDef) and node.name == 'FindModifiedLayoutTests']
        self.assertTrue(steps, 'steps.py has no FindModifiedLayoutTests: the copies in '
                        'coverage_test_scope.py need a new source')
        self.constants = {}
        for statement in steps[0].body:
            if isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name):
                try:
                    self.constants[statement.targets[0].id] = ast.literal_eval(statement.value)
                except ValueError:
                    pass

    def constant(self, name):
        self.assertIn(name, self.constants,
                      'FindModifiedLayoutTests.{} is no longer a literal'.format(name))
        return self.constants[name]

    def test_the_ignored_directories_match(self):
        self.assertEqual(set(self.constant('DIRECTORIES_TO_IGNORE')),
                         set(TEST_DIRECTORIES_TO_IGNORE))

    def test_the_ignored_suffixes_match(self):
        self.assertEqual(set(self.constant('SUFFIXES_TO_IGNORE')), set(TEST_SUFFIXES_TO_IGNORE))

    def test_the_cap_matches(self):
        self.assertEqual(self.constant('MAX_MODIFIED_TESTS'), MAX_MODIFIED_TESTS)

    def test_the_extensions_match(self):
        extensions = re.findall(rb'\\\.(\w+)', self.constant('RE_LAYOUT_TEST'))
        self.assertEqual({'.' + extension.decode() for extension in extensions},
                         set(EDITED_TEST_EXTENSIONS))


class RealTreeTest(_RealCheckout):
    """What the suggester assumes about the real tree, as invariants that hold as it changes."""

    def setUp(self):
        super().setUp()
        for directory in ('LayoutTests', os.path.join('Source', 'WebCore')):
            if not os.path.isdir(os.path.join(self.checkout, directory)):
                self.skipTest('no {} in this checkout'.format(directory))

    def subdirectories(self, relative):
        directory = os.path.join(self.checkout, relative)
        return sorted(name for name in os.listdir(directory)
                      if os.path.isdir(os.path.join(directory, name)))

    def test_the_watchlist_still_yields_mappings(self):
        self.assertTrue(
            read_watchlist_definitions(self.checkout),
            'no watchlist block co-maps Source/ and LayoutTests/ paths, or the file no longer '
            'parses; the suggester would silently ignore the watchlist')

    def test_every_suggested_path_exists_on_disk(self):
        # A nonexistent suggestion makes the harness say "Found 0 tests" and exit 0.
        paths = ['Source/WebCore/{}/Thing.cpp'.format(name)
                 for name in self.subdirectories(os.path.join('Source', 'WebCore'))]
        paths += ['Source/WebCore/Modules/{}/Thing.cpp'.format(name)
                  for name in self.subdirectories(os.path.join('Source', 'WebCore', 'Modules'))]
        for sources, _ in read_watchlist_definitions(self.checkout).values():
            paths += [source.rstrip('/') + '/Thing.cpp' for source in sources
                      if re.fullmatch(r'[\w/.-]+', source)]

        suggested = 0
        for association in suggest_scope(self.checkout, paths).associations:
            for test in association.tests:
                suggested += 1
                self.assertTrue(
                    os.path.exists(os.path.join(self.checkout, 'LayoutTests', test)),
                    '{} suggests LayoutTests/{}, which does not exist'.format(
                        association.path, test))
        self.assertTrue(suggested, 'no source directory maps to any layout test directory')


if __name__ == '__main__':
    unittest.main()

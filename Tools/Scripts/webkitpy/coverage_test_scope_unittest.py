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

import os
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
    """A throwaway checkout with a watchlist and a handful of layout tests in it.

    A synthetic tree rather than the real one for everything that is about the rules, so that a
    test does not fail because somebody deleted a LayoutTests directory. The tests that are about
    the real tree say so and use it deliberately.
    """

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
        # Deliberately no LayoutTests/regexy: the RegexOnly block's only test alternative is a
        # regex, and if a directory of that name existed the alignment rule would suggest it and
        # the test below would be passing for the wrong reason.
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
        # ContentSecurityPolicyFiles is Source/WebCore/page/(Content|DOM)SecurityPolicy\., and a
        # naive split produces a fragment starting `DOM)`.
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

    def test_the_real_watchlist_still_co_maps_twelve_blocks(self):
        # The premise this whole rule rests on, checked against the real file rather than
        # asserted. 77 DEFINITIONS blocks, 12 of which name both Source/ and LayoutTests/.
        checkout = WebKitFinder(FileSystem()).webkit_base()
        definitions = read_watchlist_definitions(checkout)
        self.assertEqual(len(definitions), 12)
        for name in ('Track', 'MediaStream', 'Accessibility', 'IndexedDB', 'CSSGridLayout',
                     'WebRTC'):
            self.assertIn(name, definitions)


class SuggestionTest(_Checkout):
    def test_a_watchlist_block_suggests_its_layout_tests(self):
        suggestion = self.suggest(['Source/WebCore/widgets/Widget.cpp'])
        self.assertEqual(suggestion.tests, ['widgets'])
        self.assertIn('Widgets', suggestion.associations[0].rule)

    def test_a_grouped_pattern_still_matches(self):
        self.assertEqual(self.suggest(['Source/WebCore/thing/AlphaThing.cpp']).tests, ['things'])

    def test_a_rotted_mapping_suggests_nothing(self):
        # LayoutTests/gone-away/ is in the watchlist and not on disk. Suggesting it would make
        # the harness report "Found 0 tests" and exit 0, which is the worst possible outcome:
        # a coverage report over nothing at all.
        suggestion = self.suggest(['Source/WebCore/gone/Gone.cpp'])
        self.assertEqual(suggestion.tests, [])
        self.assertEqual(suggestion.declined[0].reason, DECLINED_NO_RULE)

    def test_a_regex_test_path_is_not_offered(self):
        # LayoutTests/.*regexy cannot be handed to run-webkit-tests, and guessing what it
        # expands to would be inventing a mapping.
        self.assertEqual(self.suggest(['Source/WebCore/regexy/Regexy.cpp']).tests, [])

    def test_a_source_only_block_is_not_a_mapping(self):
        self.assertEqual(self.suggest(['Source/WebCore/lonely/Lonely.cpp']).tests, [])

    def test_directory_name_alignment_suggests_the_matching_directory(self):
        self.assertEqual(self.suggest(['Source/WebCore/svg/SVGElement.cpp']).tests, ['svg'])

    def test_alignment_falls_back_to_the_fast_form(self):
        # There is no LayoutTests/css, which is why the candidates are generated and filtered
        # rather than written down.
        self.assertEqual(self.suggest(['Source/WebCore/css/CSSValue.cpp']).tests, ['fast/css'])

    def test_a_modules_feature_aligns_on_the_feature_name(self):
        self.write(os.path.join('LayoutTests', 'widgets', 'nested.html'), '<html>')
        self.assertEqual(self.suggest(['Source/WebCore/Modules/widgets/Widget.cpp']).tests,
                         ['widgets'])

    def test_candidates_are_only_generated_for_webcore(self):
        self.assertEqual(_aligned_candidates('Source/WebKit/UIProcess/WebPageProxy.cpp'), [])
        self.assertNotEqual(_aligned_candidates('Source/WebCore/svg/SVGElement.cpp'), [])


class RefusalTest(_Checkout):
    """The two refusals are the load-bearing part: the failure direction must be more tests."""

    def test_a_non_code_file_gets_no_suggestion(self):
        suggestion = self.suggest(['Source/WebCore/Sources.txt'])
        self.assertEqual(suggestion.tests, [])
        self.assertEqual(suggestion.declined[0].reason, DECLINED_NOT_CODE)

    def test_a_build_file_gets_no_suggestion(self):
        # PLAN 10.2: a build or config file forces a full suite for 20.2% of commits, and
        # UnifiedWebPreferences.yaml is the single most-touched file in the tree.
        for path in ('Source/WebCore/WebCore.xcodeproj/project.pbxproj',
                     'Source/WTF/Scripts/Preferences/UnifiedWebPreferences.yaml'):
            self.assertEqual(self.suggest([path]).tests, [], path)

    def test_an_unscopable_directory_gets_no_suggestion(self):
        for component in ('platform', 'rendering', 'style', 'layout', 'page', 'bindings',
                          'loader', 'dom', 'testing'):
            path = 'Source/WebCore/{}/Thing.cpp'.format(component)
            suggestion = self.suggest([path])
            self.assertEqual(suggestion.tests, [], path)
            self.assertIn(component, suggestion.declined[0].reason)

    def test_an_unscopable_directory_deeper_in_the_path_still_refuses(self):
        # The refusal is over every component, not just the one below the framework, so a
        # platform-specific file under an otherwise scopable directory is still refused.
        self.assertEqual(self.suggest(['Source/WebCore/svg/platform/Thing.cpp']).tests, [])

    def test_a_deeper_directory_that_is_not_unscopable_keeps_the_suggestion(self):
        self.assertEqual(self.suggest(['Source/WebCore/svg/graphics/filters/Thing.cpp']).tests,
                         ['svg'])

    def test_the_component_test_is_not_a_substring_test(self):
        # Source/WebCore/css/StyleRule.cpp is not the "style" case, and a substring test over the
        # whole path would swallow most of css/.
        self.assertEqual(self.suggest(['Source/WebCore/css/StyleRule.cpp']).tests, ['fast/css'])

    def test_a_path_with_no_rule_gets_no_suggestion(self):
        suggestion = self.suggest(['Source/JavaScriptCore/wasm/WasmTypeDefinition.cpp'])
        self.assertEqual(suggestion.tests, [])
        self.assertEqual(suggestion.declined[0].reason, DECLINED_NO_RULE)

    def test_a_partly_scopable_change_is_not_complete(self):
        # The important case. A change touching svg/ and dom/ must not read as "run svg/": the
        # dom edit is unbounded, so the suggestion covers part of the change and says so.
        suggestion = self.suggest(['Source/WebCore/svg/SVGElement.cpp',
                                   'Source/WebCore/dom/Document.cpp'])
        self.assertEqual(suggestion.tests, ['svg'])
        self.assertFalse(suggestion.complete)
        self.assertEqual(len(suggestion.declined), 1)

    def test_a_wholly_scopable_change_is_complete(self):
        suggestion = self.suggest(['Source/WebCore/svg/SVGElement.cpp'])
        self.assertTrue(suggestion.complete)


class EditedTestsTest(_Checkout):
    """A change that edits layout tests IS its own scope; no inference is involved."""

    def test_an_edited_test_is_the_scope(self):
        self.assertEqual(edited_layout_tests(['LayoutTests/svg/four.html']), ['svg/four.html'])

    def test_the_extensions_are_the_three_the_ews_step_uses(self):
        self.assertEqual(EDITED_TEST_EXTENSIONS, ('.html', '.svg', '.xml'))
        self.assertEqual(edited_layout_tests(['LayoutTests/svg/a.svg', 'LayoutTests/dom/b.xml']),
                         ['svg/a.svg', 'dom/b.xml'])

    def test_an_expected_file_is_not_a_test(self):
        self.assertEqual(edited_layout_tests(['LayoutTests/svg/four-expected.html']), [])

    def test_a_reference_file_is_not_a_test(self):
        self.assertEqual(edited_layout_tests(['LayoutTests/svg/four-ref.html']), [])
        self.assertEqual(edited_layout_tests(['LayoutTests/svg/four-notref.html']), [])

    def test_a_support_directory_holds_no_tests(self):
        for directory in TEST_DIRECTORIES_TO_IGNORE:
            self.assertEqual(edited_layout_tests(
                ['LayoutTests/svg/{}/helper.html'.format(directory)]), [], directory)

    def test_a_source_file_is_not_an_edited_test(self):
        self.assertEqual(edited_layout_tests(['Source/WebCore/svg/SVGElement.cpp']), [])

    def test_the_ews_rules_have_not_drifted(self):
        # The rules above are copied from FindModifiedLayoutTests, because that module imports
        # buildbot and twisted and a developer tool must not need a CI framework. This is what
        # makes the copy detectable if the original changes.
        checkout = WebKitFinder(FileSystem()).webkit_base()
        path = os.path.join(checkout, 'Tools', 'CISupport', 'ews-build', 'steps.py')
        with open(path) as handle:
            source = handle.read()
        self.assertIn('class FindModifiedLayoutTests', source)
        self.assertIn("DIRECTORIES_TO_IGNORE = {}".format(list(TEST_DIRECTORIES_TO_IGNORE)),
                      source)
        self.assertIn("SUFFIXES_TO_IGNORE = {}".format(list(TEST_SUFFIXES_TO_IGNORE)), source)
        self.assertIn('MAX_MODIFIED_TESTS = {}'.format(MAX_MODIFIED_TESTS), source)
        for extension in EDITED_TEST_EXTENSIONS:
            self.assertIn(extension, source)


class BaselineTest(_Checkout):
    """A rebaselined test is one whose expected output just changed, so it is worth running."""

    def test_a_baseline_names_its_test(self):
        self.write(os.path.join('LayoutTests', 'svg', 'six.html'), '<html>')
        self.assertEqual(
            baseline_layout_tests(['LayoutTests/svg/six-expected.txt'], self.root),
            ['svg/six.html'])

    def test_a_platform_baseline_resolves_to_the_generic_test(self):
        # There is no test at the platform path at all, which is why this needs a rule of its own.
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


class RealTreeTest(unittest.TestCase):
    """The premises PLAN 10.3 states, checked against the tree rather than believed."""

    def setUp(self):
        self.checkout = WebKitFinder(FileSystem()).webkit_base()

    def suggest(self, path):
        return suggest_scope(self.checkout, [path]).tests

    def test_the_aligned_directories_that_do_align(self):
        for directory, expected in (('svg', 'svg'), ('editing', 'editing'),
                                    ('accessibility', 'accessibility'), ('mathml', 'mathml'),
                                    ('workers', 'workers')):
            tests = self.suggest('Source/WebCore/{}/Thing.cpp'.format(directory))
            self.assertIn(expected, tests, directory)

    def test_the_aligned_directories_that_do_not_align_literally(self):
        # PLAN 10.3 lists css, html and animation among the directories where alignment "already
        # holds". There is no LayoutTests/css, LayoutTests/html or LayoutTests/animation, so the
        # literal rule produces nothing and the generated forms are what make these work.
        for directory in ('css', 'html', 'animation'):
            self.assertFalse(os.path.exists(os.path.join(self.checkout, 'LayoutTests', directory)),
                             directory)
            self.assertTrue(self.suggest('Source/WebCore/{}/Thing.cpp'.format(directory)),
                            directory)

    def test_dom_is_refused_even_though_the_directory_aligns(self):
        # LayoutTests/dom exists, and Document.cpp is still not scopable: PLAN 10.2 measured
        # every one of the 19 most-changed implementation files as indeterminate.
        self.assertTrue(os.path.exists(os.path.join(self.checkout, 'LayoutTests', 'dom')))
        self.assertEqual(self.suggest('Source/WebCore/dom/Document.cpp'), [])

    def test_a_real_watchlist_block_produces_real_directories(self):
        tests = self.suggest('Source/WebCore/Modules/mediastream/MediaStream.cpp')
        self.assertIn('fast/mediastream', tests)
        for test in tests:
            self.assertTrue(os.path.exists(os.path.join(self.checkout, 'LayoutTests', test)), test)

    def test_every_suggestion_for_every_rule_exists_on_disk(self):
        # The invariant: a suggestion that does not exist makes the harness say "Found 0 tests"
        # and exit 0, which produces a coverage report over nothing.
        definitions = read_watchlist_definitions(self.checkout)
        for name in definitions:
            for source in definitions[name][0]:
                if '(' in source or '*' in source:
                    continue
                for test in suggest_scope(self.checkout, [source.rstrip('/') + '/Thing.cpp']).tests:
                    self.assertTrue(
                        os.path.exists(os.path.join(self.checkout, 'LayoutTests', test)),
                        '{}: {}'.format(name, test))


if __name__ == '__main__':
    unittest.main()

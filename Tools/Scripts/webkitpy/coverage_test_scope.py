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

"""Which layout tests are worth running for a change: a suggestion by name, and often none.

Advisory only. Nothing here restricts a run; the failure direction is always more tests.
"""

import ast
import logging
import os
import re

from collections import namedtuple

from webkitpy.coverage_delta import SOURCE_EXTENSIONS

logger = logging.getLogger(__name__)

LAYOUT_TESTS_DIRECTORY = 'LayoutTests'

WATCHLIST_PATH = os.path.join('Tools', 'Scripts', 'webkitpy', 'common', 'config', 'watchlist')

# Directories whose blast radius is the whole suite. Matched against whole path components, not
# substrings. This list wins over the aligned directories, dom included.
UNSCOPABLE_COMPONENTS = re.compile(
    r'platform|rendering|style|layout|page|bindings|loader|dom|testing', re.IGNORECASE)

# Copied from FindModifiedLayoutTests in Tools/CISupport/ews-build/steps.py, which imports
# buildbot and cannot be imported here. coverage_test_scope_unittest checks they have not drifted.
EDITED_TEST_EXTENSIONS = ('.html', '.svg', '.xml')
TEST_DIRECTORIES_TO_IGNORE = ('reference', 'reftest', 'resources', 'support', 'script-tests',
                              'tools')
TEST_SUFFIXES_TO_IGNORE = ('-expected', '-expected-mismatch', '-ref', '-notref')
MAX_MODIFIED_TESTS = 100

# Same shape as the EWS step's `grep -v "\[.SKIP.\]"`.
_SKIP_EXPECTATION = re.compile(r'\[\s*[^]]*\bSkip\b[^]]*\]', re.IGNORECASE)

# Only literal paths are suggested; a regex alternative cannot be handed to run-webkit-tests.
_REGEX_SYNTAX = re.compile(r'[*?\[\]()\\|+$^]')

_BASELINE_SUFFIX = re.compile(r'-expected(-mismatch)?\.[A-Za-z0-9]+$')
_TEST_FILE_EXTENSIONS = ('.html', '.xhtml', '.svg', '.xml', '.htm', '.mht', '.pdf', '.php')

DECLINED_NOT_CODE = 'not a C, C++ or Objective-C source file'
DECLINED_UNSCOPABLE = ('under {}, whose blast radius is the whole suite -- measurement put '
                       'every one of the 19 most-changed implementation files in the tree as '
                       'indeterminate')
DECLINED_NO_RULE = 'no name-based rule maps it to any layout tests'

# tests: layout-test paths, relative to LayoutTests/.
Association = namedtuple('Association', ('path', 'tests', 'rule'))

Declined = namedtuple('Declined', ('path', 'reason'))


def _collapse_prefixes(tests):
    """Drop any test path an already-suggested directory contains, keeping the shortest."""
    directories = sorted(tests, key=len)
    kept = []
    for test in sorted(tests):
        if any(other != test and (test == other or test.startswith(other.rstrip('/') + '/'))
               for other in directories):
            continue
        kept.append(test)
    return kept


class ScopeSuggestion:
    """What to run, why, and -- as often as not -- why there is nothing to suggest.

    Tests for only some paths is not a suggestion to run only those tests: see complete.
    """

    def __init__(self, associations=(), declined=()):
        self.associations = list(associations)
        self.declined = list(declined)

    @property
    def tests(self):
        """Every suggested test path, deduplicated, prefix-collapsed, in a stable order."""
        seen = []
        for association in self.associations:
            for test in association.tests:
                if test not in seen:
                    seen.append(test)
        return _collapse_prefixes(seen)

    @property
    def complete(self):
        """True when every changed path got a suggestion, so the suggestion covers the change."""
        return bool(self.associations) and not self.declined

    @property
    def empty(self):
        return not self.associations

    def rules(self):
        """{rule: [test paths]}, prefix-collapsed against the whole suggestion."""
        covering = set(self.tests)
        by_rule = {}
        for association in self.associations:
            by_rule.setdefault(association.rule, [])
            for test in association.tests:
                if test in covering and test not in by_rule[association.rule]:
                    by_rule[association.rule].append(test)
        return by_rule


def _components(relative_path):
    return relative_path.replace(os.sep, '/').split('/')[:-1]


def _split_alternation(pattern):
    """Split a watchlist filename regex on the | between alternatives, at paren depth 0 only."""
    parts, depth, current = [], 0, ''
    escaped = False
    for character in pattern:
        if escaped:
            current += character
            escaped = False
            continue
        if character == '\\':
            current += character
            escaped = True
            continue
        if character == '(':
            depth += 1
        elif character == ')':
            depth = max(0, depth - 1)
        if character == '|' and not depth:
            parts.append(current)
            current = ''
            continue
        current += character
    parts.append(current)
    return [part for part in parts if part]


def read_watchlist_definitions(checkout_root):
    """{name: (source patterns, LayoutTests paths)} for the blocks that co-map both.

    Read with ast.literal_eval; a syntax error is check-webkit-style's to report.
    """
    path = os.path.join(checkout_root, WATCHLIST_PATH)
    try:
        with open(path) as handle:
            text = handle.read()
        definitions = ast.literal_eval(text[text.index('{'):])['DEFINITIONS']
    except (OSError, ValueError, SyntaxError, KeyError) as failure:
        logger.debug('Could not read %s: %s', path, failure)
        return {}

    mapping = {}
    for name, rules in definitions.items():
        pattern = rules.get('filename')
        if not pattern:
            continue
        alternatives = _split_alternation(pattern)
        sources = [alternative for alternative in alternatives
                   if alternative.startswith('Source/')]
        tests = [alternative for alternative in alternatives
                 if alternative.startswith(LAYOUT_TESTS_DIRECTORY + '/')]
        if sources and tests:
            mapping[name] = (sources, tests)
    return mapping


def _literal_test_paths(candidates, checkout_root):
    """The candidates that name a real path under LayoutTests/, relative to LayoutTests/.

    A stale literal path would make the harness say `Found 0 tests` and exit 0.
    """
    resolved = []
    for candidate in candidates:
        if _REGEX_SYNTAX.search(candidate):
            continue
        relative = candidate[len(LAYOUT_TESTS_DIRECTORY) + 1:].strip('/') \
            if candidate.startswith(LAYOUT_TESTS_DIRECTORY + '/') else candidate.strip('/')
        if not relative:
            continue
        if os.path.exists(os.path.join(checkout_root, LAYOUT_TESTS_DIRECTORY, relative)):
            if relative not in resolved:
                resolved.append(relative)
    return resolved


def _aligned_candidates(relative_path):
    """Layout-test paths that a Source/ path's own directory name suggests, before filtering.

    The caller filters against the filesystem: LayoutTests has no css/, html/ or animation/.
    """
    parts = relative_path.replace(os.sep, '/').split('/')
    if len(parts) < 4 or parts[0] != 'Source':
        return []
    name = None
    if parts[1] == 'WebCore' and parts[2] == 'Modules':
        name = parts[3]
    elif parts[1] == 'WebCore':
        name = parts[2]
    if not name:
        return []
    forms = [name, name + 's']
    candidates = []
    for form in forms:
        candidates.append(form)
        candidates.append('fast/' + form)
        candidates.append('imported/w3c/web-platform-tests/' + form)
    return candidates


def edited_layout_tests(relative_paths):
    """The layout tests a change edits (FindModifiedLayoutTests's rules): they are the scope."""
    tests = []
    for path in relative_paths:
        normalized = path.replace(os.sep, '/')
        if not normalized.startswith(LAYOUT_TESTS_DIRECTORY + '/'):
            continue
        if not normalized.endswith(EDITED_TEST_EXTENSIONS):
            continue
        relative = normalized[len(LAYOUT_TESTS_DIRECTORY) + 1:]
        stem = os.path.splitext(os.path.basename(relative))[0]
        if any(stem.endswith(suffix) for suffix in TEST_SUFFIXES_TO_IGNORE):
            continue
        if any(directory in relative.split('/') for directory in TEST_DIRECTORIES_TO_IGNORE):
            continue
        if relative not in tests:
            tests.append(relative)
    return tests


def newly_unskipped_tests(diff_text):
    """Tests an expectations diff has stopped skipping, from added lines in a TestExpectations diff.

    It sees an edited line, not a changed effective expectation, so it can miss a test.
    """
    tests = []
    in_expectations = False
    for line in diff_text.splitlines():
        if line.startswith('+++ ') or line.startswith('--- '):
            in_expectations = 'TestExpectations' in line
            continue
        if not in_expectations or not line.startswith('+') or line.startswith('+++'):
            continue
        body = line[1:].strip()
        if not body or body.startswith('#'):
            continue
        if _SKIP_EXPECTATION.search(body):
            continue
        for token in body.split():
            if token.endswith(EDITED_TEST_EXTENSIONS) and token not in tests:
                tests.append(token)
    return tests


def baseline_layout_tests(relative_paths, checkout_root):
    """The test a changed expected result belongs to: one test per baseline.

    A platform baseline also resolves to the generic test; no test lives at the platform path.
    """
    tests = []
    for path in relative_paths:
        normalized = path.replace(os.sep, '/')
        if not normalized.startswith(LAYOUT_TESTS_DIRECTORY + '/'):
            continue
        relative = normalized[len(LAYOUT_TESTS_DIRECTORY) + 1:]
        match = _BASELINE_SUFFIX.search(relative)
        if not match:
            continue
        stem = relative[:match.start()]
        candidates = [stem]
        parts = stem.split('/')
        if len(parts) > 2 and parts[0] == 'platform':
            candidates.append('/'.join(parts[2:]))
        for candidate in candidates:
            for extension in _TEST_FILE_EXTENSIONS:
                test = candidate + extension
                if os.path.exists(os.path.join(checkout_root, LAYOUT_TESTS_DIRECTORY, test)):
                    if test not in tests:
                        tests.append(test)
                    break
    return tests


def suggest_scope(checkout_root, relative_paths, diff_text=None):
    """A ScopeSuggestion for a change. Advisory, and often empty on purpose.

    relative_paths are checkout-relative; diff_text is the change's unified diff, if any.
    """
    watchlist = read_watchlist_definitions(checkout_root)
    compiled = {}
    for name, (sources, tests) in watchlist.items():
        resolved = _literal_test_paths(tests, checkout_root)
        if not resolved:
            continue
        for source in sources:
            try:
                compiled.setdefault(name, (resolved, []))[1].append(re.compile(source))
            except re.error as failure:
                logger.debug('Watchlist %s has an unusable pattern %r: %s', name, source, failure)

    associations, declined = [], []

    edited = edited_layout_tests(relative_paths)
    if edited:
        associations.append(Association(
            LAYOUT_TESTS_DIRECTORY + '/', edited[:MAX_MODIFIED_TESTS],
            'the change edits these layout tests, so they are the scope'))
    rebaselined = baseline_layout_tests(relative_paths, checkout_root)
    if rebaselined:
        associations.append(Association(
            LAYOUT_TESTS_DIRECTORY + '/', rebaselined[:MAX_MODIFIED_TESTS],
            'the change rebaselines these tests, so their expected output just changed'))
    unskipped = newly_unskipped_tests(diff_text or '')
    resolved_unskipped = [test for test in unskipped
                          if os.path.exists(os.path.join(checkout_root, LAYOUT_TESTS_DIRECTORY,
                                                         test))]
    if resolved_unskipped:
        associations.append(Association(
            'TestExpectations', resolved_unskipped[:MAX_MODIFIED_TESTS],
            'the change stops skipping these tests'))

    for path in relative_paths:
        normalized = path.replace(os.sep, '/')
        if normalized.startswith(LAYOUT_TESTS_DIRECTORY + '/'):
            # Handled above; a non-test file under LayoutTests/ gets no suggestion.
            continue
        if not normalized.endswith(SOURCE_EXTENSIONS):
            declined.append(Declined(normalized, DECLINED_NOT_CODE))
            continue
        blocked = [component for component in _components(normalized)
                   if UNSCOPABLE_COMPONENTS.fullmatch(component)]
        if blocked:
            declined.append(Declined(normalized, DECLINED_UNSCOPABLE.format(
                ', '.join(sorted(set(blocked))))))
            continue

        matched = False
        for name in sorted(compiled):
            tests, patterns = compiled[name]
            if any(pattern.search(normalized) for pattern in patterns):
                associations.append(Association(normalized, tests,
                                                'watchlist DEFINITIONS block ' + name))
                matched = True
        aligned = _literal_test_paths(_aligned_candidates(normalized), checkout_root)
        if aligned:
            associations.append(Association(
                normalized, aligned,
                'the directory name {} matches these LayoutTests directories'.format(
                    normalized.split('/')[2] if normalized.startswith('Source/WebCore/') else
                    normalized)))
            matched = True
        if not matched:
            declined.append(Declined(normalized, DECLINED_NO_RULE))

    return ScopeSuggestion(associations, declined)


def layout_test_names(port, paths):
    """The tests the harness's own finder finds for paths, so a count is the harness's count.

    This is the count before expectations are applied, so it exceeds the number a run executes.
    """
    from webkitpy.layout_tests.controllers.layout_test_finder_legacy import LayoutTestFinder

    return sorted(test.test_path for test in LayoutTestFinder(port, None).find_tests_by_path(
        list(paths)))

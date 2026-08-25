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

"""Which layout tests are worth running for a change -- as a suggestion, by name, and often none.

This is deliberately NOT a coverage-driven test map. That idea was measured and rejected (PLAN
10): over 1,656 real commits touching first-party Source/, the mean fraction of the suite that
must still run is 86.1%, p25/p50/p75/p90 are all 100%, 83.9% of commits get no useful saving at
all, and two independent methods bracket the best possible saving at 14-23% -- 107 minutes down
to 82-92. Meanwhile a developer who types `svg/` runs 2,893 tests, 2.7% of the suite, in about
three minutes. One word from a human beats the best possible map by an order of magnitude, so
the useful tool is one that helps a human choose that word, not one that chooses for them.

So three rules, in this order, and the first two are refusals:

1.  A path with no rule, a path under a directory whose blast radius is the whole suite, or a
    file that is not code: NO SUGGESTION. The message is "scope it yourself or run the suite".
2.  A change that edits layout tests IS its own scope, and needs no inference at all. 32.4% of
    commits touch LayoutTests/, and Tools/CISupport/ews-build/steps.py:1518's
    FindModifiedLayoutTests already works out exactly this -- the rules are reused below rather
    than reinvented, and a unit test checks they have not drifted from it.
3.  Otherwise, name-based association: the 12 watchlist DEFINITIONS blocks that already co-map
    Source/ to LayoutTests/, plus directory-name alignment.

Every suggestion is a suggestion. Nothing here ever restricts a run: webkit-coverage prints
what this returns and the developer accepts it, edits it, or ignores it. The failure direction is
always MORE tests -- a wrong suggestion that is too wide costs minutes, and one that is too
narrow produces a coverage number that is a lower bound nobody knows is a lower bound.

Every suggested path is checked against the filesystem before it is offered, which caught the
one place where that premise is looser than it reads. Directory-name alignment holds
literally for 6 of the 10 directories it names -- svg, dom, editing, accessibility, mathml and
workers all have a LayoutTests directory of that name -- and does not for css, html, animation
or xml: there is no LayoutTests/css, LayoutTests/html or LayoutTests/animation, and no
LayoutTests directory corresponds to Source/WebCore/xml at all. So the alignment is generated
and then filtered, with the plural and the fast/ and imported-wpt forms tried as well, rather
than being written down as a table that would be four-tenths wrong on the day it was written.
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

# Directories whose blast radius is the whole suite, so nothing under them gets a suggestion.
# Matched against whole path components rather than as a substring: a
# component test keeps Source/WebCore/css/StyleRule.cpp out of the "style" case, which a
# substring test would swallow, and it correctly does not treat Source/WebKit/.../WebPage.cpp as
# the "page" case -- that path gets no suggestion anyway, because no rule produces one for it.
#
# Note dom appears both here and among the aligned directories. This list wins:
# every one of the top 19 most-changed implementation files in the tree is indeterminate by
# the measurement, and Document.cpp is one of them, so declining is the honest answer and
# it is also the safe direction.
UNSCOPABLE_COMPONENTS = re.compile(
    r'platform|rendering|style|layout|page|bindings|loader|dom|testing', re.IGNORECASE)

# Reused from Tools/CISupport/ews-build/steps.py's FindModifiedLayoutTests, which already
# scrapes edited layout tests out of a patch. Copied rather than imported because that module
# imports buildbot and twisted, neither of which is installed with webkitpy, and a developer
# tool must not need a CI framework. coverage_test_scope_unittest asserts these still match the
# values in that file, so a change there fails a test here instead of drifting silently.
EDITED_TEST_EXTENSIONS = ('.html', '.svg', '.xml')
TEST_DIRECTORIES_TO_IGNORE = ('reference', 'reftest', 'resources', 'support', 'script-tests',
                              'tools')
TEST_SUFFIXES_TO_IGNORE = ('-expected', '-expected-mismatch', '-ref', '-notref')
MAX_MODIFIED_TESTS = 100

# A TestExpectations line that has stopped being skipped is a test somebody has just turned on,
# so it belongs in the scope. Same shape as the EWS step's `grep -v "\[.SKIP.\]"`.
_SKIP_EXPECTATION = re.compile(r'\[\s*[^]]*\bSkip\b[^]]*\]', re.IGNORECASE)

# A suggested test path is offered only if it is a literal path. Anything with regex syntax in it
# -- LayoutTests/.*accessibility, LayoutTests/platform/.*/fast/css-grid-layout/ -- cannot be
# handed to run-webkit-tests, and guessing what it expands to would be inventing a mapping.
_REGEX_SYNTAX = re.compile(r'[*?\[\]()\\|+$^]')

# An expected result, and the extensions a test itself can have. Deliberately short: every
# candidate is checked against the filesystem, so a missing extension costs a suggestion and a
# wrong one costs nothing.
_BASELINE_SUFFIX = re.compile(r'-expected(-mismatch)?\.[A-Za-z0-9]+$')
_TEST_FILE_EXTENSIONS = ('.html', '.xhtml', '.svg', '.xml', '.htm', '.mht', '.pdf', '.php')

DECLINED_NOT_CODE = 'not a C, C++ or Objective-C source file'
DECLINED_UNSCOPABLE = ('under {}, whose blast radius is the whole suite -- measurement put '
                       'every one of the 19 most-changed implementation files in the tree as '
                       'indeterminate')
DECLINED_NO_RULE = 'no name-based rule maps it to any layout tests'

# path: the changed file, relative to the checkout root.
# tests: layout-test paths, relative to LayoutTests/, that it suggests.
# rule: what produced them, named so the suggestion can be argued with.
Association = namedtuple('Association', ('path', 'tests', 'rule'))

# path: the changed file. reason: why there is no suggestion for it.
Declined = namedtuple('Declined', ('path', 'reason'))


def _collapse_prefixes(tests):
    """Drop any test path an already-suggested directory contains.

    A change that touches Source/WebCore/accessibility and edits 60 accessibility tests
    suggests both `accessibility` and all 60 of those tests, and passing all 61 to
    run-webkit-tests runs the same tests and prints 61 arguments. Keeping only the shortest
    covering path is what makes the suggestion something a human can read and retype.
    """
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

    A suggestion with tests for some paths and none for others is NOT a suggestion to run only
    those tests: a change that touches Source/WebCore/svg and Source/WebCore/dom needs the whole
    suite, because nothing here can bound what the dom edit affects. complete is what says which
    of those two it is, and webkit-coverage prints the difference.
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
        """{rule: [test paths]}, so the output can say what produced each suggestion.

        Prefix-collapsed against the whole suggestion, so a rule that contributed only paths
        another rule's directory already covers has an empty list rather than a redundant one.
        """
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
    """Split a watchlist filename regex on the | that separates its alternatives.

    At paren depth 0 only. ContentSecurityPolicyFiles is
    `Source/WebCore/page/(Content|DOM)SecurityPolicy\\.`, and a naive split on '|' turns that
    into two broken fragments, one of which starts with `DOM)`.
    """
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

    The watchlist is a Python literal, so it is read with ast.literal_eval rather than exec'd,
    and a syntax error in it is not this tool's problem to report -- it is check-webkit-style's,
    which already checks the file.
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

    Both halves matter. A regex alternative cannot be handed to run-webkit-tests, and expanding
    it would be inventing a mapping; and a literal path that does not exist means the mapping has
    rotted, which is a suggestion that would make the harness say `Found 0 tests` and exit 0.
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

    Source/WebCore/<name>/ and Source/WebCore/Modules/<name>/ are the two shapes that align.
    The plural and the fast/ and imported-wpt forms are generated as well, because the literal
    form is absent for four of the ten directories the plan lists -- there is no LayoutTests/css,
    LayoutTests/html or LayoutTests/animation -- and generating candidates that are then checked
    against the filesystem is what stops that being four wrong suggestions.
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
    """The layout tests a change edits, which need no inference: they ARE the scope.

    The rules are FindModifiedLayoutTests's: .html, .svg and .xml only, no -expected/-ref
    variants, and nothing under resources/, support/, script-tests/ and the rest, because those
    are not tests and naming one makes the harness find nothing.
    """
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

    The same rule as FindModifiedLayoutTests's `grep "^+[^+]" | grep -v "\\[.SKIP.\\]"`, applied
    to the diff of the expectations files themselves rather than to a diff of two
    `run-webkit-tests --print-expectations` runs. That is weaker -- it sees a line that was
    edited, not a test whose effective expectation changed, so it misses a test unskipped by an
    edit to a platform file that overrides a generic one -- and it costs nothing, where the EWS
    version costs two full expectation parses. Weaker in the safe direction: it suggests fewer
    tests than the strong version and the developer is looking at the list.
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
    """The test a changed expected result belongs to, which is exactly one test per baseline.

    FindModifiedLayoutTests deliberately ignores -expected files, because it is looking for
    modified tests. A rebaselined test is a different thing and it is worth running: the
    baseline is the test's expected output, so somebody has just changed what the test means to
    pass. The mapping is exact rather than inferred -- one baseline names one test -- so this
    extends the prior art in the safe direction.

    A platform baseline is resolved to the generic test as well, since
    LayoutTests/platform/glib/imported/.../foo-expected.txt is the expected result of
    LayoutTests/imported/.../foo.html and there is no test at the platform path at all.
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

    relative_paths are checkout-relative. diff_text, when given, is the change's unified diff,
    read only for TestExpectations lines that have stopped being skipped.
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
            # Already handled above, either as an edited test or as an expectations edit, and a
            # non-test file under LayoutTests/ -- a resource, a baseline -- is not something to
            # suggest tests for.
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

    Used for the test counts in the lower-bound banner: 'it ran 2,893 of the 106,172 layout
    tests'. Both numbers have to come from the same place as each other and from the same place
    the harness gets them, or the shortfall is a comparison of two different definitions of
    "a test". Measured at about 7 s of CPU for the whole suite over a warm filesystem, and 2,893
    for `svg`, which is exactly the measured figure.

    Note this is the finder's count BEFORE expectations are applied, so it is larger than the
    number a run reports having executed -- 106,172 against the 95,936 of the full-suite run,
    the difference being skipped tests. It is the right denominator for "how much of the suite
    did I ask for", which is the question the banner asks.
    """
    from webkitpy.layout_tests.controllers.layout_test_finder_legacy import LayoutTestFinder

    return sorted(test.test_path for test in LayoutTestFinder(port, None).find_tests_by_path(
        list(paths)))

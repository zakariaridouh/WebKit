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

"""Which tests a coverage number is over, and what that makes the number mean.

The mathematics is monotone: adding tests to a run can only turn a line from uncovered to
covered, never the other way. So for a run over a subset of the suite:

    a covered line is EXACT, and an uncovered line is UNKNOWN.

Every consequence in this module follows from that one sentence.

  * A selective line-coverage percentage is a LOWER BOUND. It is rendered as `>= 41.30%` and
    never as `41.30%`, in the page title, in the report's totals, in summary.txt and in
    anything printed. A number that is not qualified will be quoted as though it were exact,
    because there is nothing about "41.30%" that says otherwise.
  * The output directory and the trace get a `-selective` infix, because artifacts move. A
    coverage.lcov.gz found on a bot, in a bug or in somebody's home directory carries no
    context but its own name and the provenance record inside it.
  * The scope and a DIGEST of the test-name list go in the provenance record. Without the
    digest, two subset traces from different subsets compare happily and produce garbage:
    every line the second subset did not reach reads as a regression.
  * A gate on absolute coverage cannot be evaluated at all, so --fail-under-lines on a
    selective trace fails rather than passing on a number that does not mean what it says.
  * A gate on patch coverage CAN be evaluated, and is sound but not complete: it can raise a
    false alarm and it cannot grant a false pass, because a line it calls covered really was
    executed. That is the right direction for a gate, so --fail-under-patch is allowed.
  * Delta coverage is not sound under selection at all. A selective current against a full
    baseline fabricates a regression the size of the tests that did not run.
  * The shortfall is stated in TEST COUNTS and never as a percentage of the suite, because "3%
    of the suite ran" invites the reader to scale the coverage number by it, and the
    relationship between the two is not linear, not monotone in any useful direction, and not
    knowable from this side.

The scope is a value with two cases rather than a flag or a string, so that every consumer has
to handle both: there is no `str(scope)` that a caller can print, no truthiness that reads as
"is scoped", and the only way to format a percentage from it is a method that knows about `>=`.
"""

import hashlib
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# A percentage in somebody else's output. llvm-cov's summary table writes them as NN.NN%, and
# this is deliberately anchored on the decimal point so that an integer count is never mistaken
# for one.
_PERCENTAGE = re.compile(r'(\d+\.\d+%)')

FULL_SUITE = 'full-suite'
SELECTIVE = 'selective'

# The infix in an output directory name and a trace filename. Short, unmistakable, and the same
# string in both places so that one grep finds every selective artifact on a machine.
SELECTIVE_INFIX = '-selective'

# The prefix on every lower-bound number. A mathematical symbol rather than the word "at least",
# because it has to survive being pasted into a bug, a table and a terminal.
LOWER_BOUND_PREFIX = '≥ '

SCHEMA = 'webkit-coverage-scope-1'

_ONE_SENTENCE = ('Adding tests can only turn a line from uncovered to covered, so in a run over '
                 'part of the suite a covered line is exact and an uncovered line is unknown.')


def digest_test_names(names):
    """'sha256:<hex>' over a test-name list, or None for no list.

    Over the sorted distinct names, one per line, so that shard order, duplicate arguments and
    the order the harness happened to run them in do not change it: the question it answers is
    "were these two traces produced by the same set of tests", and nothing else.

    This is the field that makes two subset traces refuse to be compared. Without it, a trace
    from `svg/` and a trace from `fast/css/` are indistinguishable to compare-coverage-reports,
    which reports every line the second did not reach as a regression -- confidently, with line
    numbers, sorted worst first.
    """
    if names is None:
        return None
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        digest.update(name.encode('utf-8'))
        digest.update(b'\n')
    return 'sha256:' + digest.hexdigest()


class CoverageScope:
    """FULL_SUITE, or SELECTIVE over a named subset. Immutable, and never a string.

    Construct with CoverageScope.full_suite() or CoverageScope.selective(). The constructor is
    private-by-convention so that the two cases stay the only two: a third would have to be a
    third named constructor, and every consumer would fail to handle it loudly instead of
    silently taking the else branch.
    """

    __slots__ = ('_kind', '_argv', '_tests_run', '_tests_in_suite', '_test_names_digest',
                 '_suite_name')

    def __init__(self, kind, argv=(), tests_run=None, tests_in_suite=None,
                 test_names_digest=None, suite_name=None):
        self._kind = kind
        self._argv = tuple(argv)
        self._tests_run = tests_run
        self._tests_in_suite = tests_in_suite
        self._test_names_digest = test_names_digest
        self._suite_name = suite_name

    @classmethod
    def full_suite(cls):
        return cls(FULL_SUITE)

    @classmethod
    def selective(cls, argv, tests_run=None, tests_in_suite=None, test_names=None,
                  test_names_digest=None, suite_name=None):
        """argv is what the developer named, verbatim: ['svg', 'fast/css'].

        test_names, when given, is the full list of tests the run covered, and only its digest
        is kept -- 106,172 names is 4 MB of provenance record and the record has to stay
        readable. tests_run defaults to the length of that list, since counting the names is
        strictly better than being told a number that might not match them.
        """
        if test_names is not None:
            names = list(test_names)
            tests_run = len(names) if tests_run is None else tests_run
            test_names_digest = digest_test_names(names)
        return cls(SELECTIVE, argv=argv, tests_run=tests_run, tests_in_suite=tests_in_suite,
                   test_names_digest=test_names_digest, suite_name=suite_name)

    # -- the two cases ----------------------------------------------------------------------

    @property
    def kind(self):
        return self._kind

    @property
    def is_selective(self):
        return self._kind == SELECTIVE

    @property
    def is_full_suite(self):
        return self._kind == FULL_SUITE

    @property
    def argv(self):
        return self._argv

    @property
    def tests_run(self):
        return self._tests_run

    @property
    def tests_in_suite(self):
        return self._tests_in_suite

    @property
    def test_names_digest(self):
        return self._test_names_digest

    @property
    def suite_name(self):
        return self._suite_name or 'test'

    @property
    def tests_not_run(self):
        """How many tests were not run, or None when the suite size is unknown."""
        if self._tests_run is None or self._tests_in_suite is None:
            return None
        return max(0, self._tests_in_suite - self._tests_run)

    def __eq__(self, other):
        if not isinstance(other, CoverageScope):
            return NotImplemented
        return self.to_json() == other.to_json()

    def __hash__(self):
        return hash(json.dumps(self.to_json(), sort_keys=True))

    def __repr__(self):
        if self.is_full_suite:
            return 'CoverageScope.full_suite()'
        return 'CoverageScope.selective({!r}, tests_run={!r}, tests_in_suite={!r})'.format(
            list(self._argv), self._tests_run, self._tests_in_suite)

    # -- rendering, which is the whole point ------------------------------------------------

    def qualify(self, text):
        """'41.30%' -> '>= 41.30%' for a selective scope, and unchanged for a full-suite one."""
        if not self.is_selective:
            return text
        if text.startswith(LOWER_BOUND_PREFIX):
            return text
        return LOWER_BOUND_PREFIX + text

    def format_percent(self, value, places=2):
        """The only way to get a percentage out of a scope, so it cannot be got unqualified."""
        if value is None:
            return '-'
        return self.qualify('{:.{places}f}%'.format(value, places=places))

    def qualify_percentages(self, text):
        """Every NN.NN% in a line of somebody else's output, marked as a lower bound.

        For llvm-cov's own summary table, which this does not reformat: the table is llvm-cov's
        output and is written verbatim, and this produces the restatement that goes beside it.
        Column alignment is lost, which is the right trade -- a misaligned true number beats an
        aligned one that means something other than what it says.
        """
        if not self.is_selective:
            return text
        return _PERCENTAGE.sub(lambda match: LOWER_BOUND_PREFIX + match.group(1), text)

    def qualify_title(self, title):
        """A page or report title, marked so that a screenshot of it is not misread."""
        if not self.is_selective:
            return title
        return '{} (lower bound, selective run)'.format(title)

    @property
    def infix(self):
        return SELECTIVE_INFIX if self.is_selective else ''

    def filename(self, name):
        """'coverage.lcov.gz' -> 'coverage-selective.lcov.gz'.

        The infix goes after the first component of the name rather than before the extension,
        so that .lcov.gz stays intact and `coverage-selective.lcov.gz` is still recognised by
        everything that sniffs for gzip by extension.
        """
        if not self.is_selective:
            return name
        head, dot, extensions = name.partition('.')
        if head.endswith(SELECTIVE_INFIX):
            return name
        return head + SELECTIVE_INFIX + dot + extensions

    def directory(self, path):
        """.../report -> .../report-selective, so a moved directory still says what it is."""
        if not self.is_selective:
            return path
        parent, name = os.path.split(path.rstrip(os.sep))
        if not name:
            return path
        if name.endswith(SELECTIVE_INFIX):
            return path
        return os.path.join(parent, name + SELECTIVE_INFIX)

    def banner_lines(self):
        """The shortfall, in test counts. Empty for a full-suite scope.

        Never a percentage of the suite: "3% of the suite ran" reads as an invitation to scale
        the coverage number, and the relationship between the two is not linear, not monotone
        in any useful direction, and not knowable from here.
        """
        if not self.is_selective:
            return []
        lines = ['This is a SELECTIVE run: {}.'.format(self._counts_sentence())]
        lines.append(_ONE_SENTENCE)
        lines.append('So every percentage here is a lower bound, written >=, and the list of '
                     'uncovered lines is the part to act on.')
        return lines

    def _counts_sentence(self):
        named = ' '.join(self._argv) if self._argv else 'a subset of the suite'
        if self._tests_run is None:
            return 'the tests it ran were {}, and how many that is was not recorded'.format(named)
        if self._tests_in_suite is None:
            return 'it ran {:,} {} test(s) ({}), out of a suite whose size was not ' \
                   'measured'.format(self._tests_run, self.suite_name, named)
        if not self.tests_not_run:
            # The counted suite ran in full, so what makes this selective is something the counts
            # do not cover -- a named subset of another suite. Saying "0 tests were not asked"
            # would read as "this is a full run", which is the misreading the whole class exists
            # to prevent.
            return ('it ran all {:,} {} tests, so what makes it selective is the rest of what it '
                    'named ({}) -- which these counts do not cover'.format(
                        self._tests_in_suite, self.suite_name, named))
        return 'it ran {:,} of the {:,} {} tests ({}), so {:,} test(s) were not asked'.format(
            self._tests_run, self._tests_in_suite, self.suite_name, named, self.tests_not_run)

    def gate_refusal(self, option):
        """Why an absolute-coverage gate cannot be evaluated over this scope, or None."""
        if not self.is_selective:
            return None
        return ('{} gates on absolute coverage, and this trace is from a selective run, so its '
                'coverage is a lower bound over a whole-tree denominator -- there is no number '
                'here for a threshold to be compared against. {} Use --fail-under-patch, which '
                'is sound under selection: it can raise a false alarm and it cannot grant a '
                'false pass.'.format(option, _ONE_SENTENCE))

    def comparison_refusal(self, other, this_name='current', other_name='baseline'):
        """Why two scopes cannot be compared for a delta, or None if they can.

        Selective against full fabricates a regression the size of the tests that did not run.
        Selective against selective is refused too unless the two ran the same tests, which is
        what the test-name digest is for: without it there is nothing to check, so the answer
        is no.
        """
        if not self.is_selective and not other.is_selective:
            return None
        which = [name for name, scope in ((this_name, self), (other_name, other))
                 if scope.is_selective]
        if self.is_selective != other.is_selective:
            return ('the {} trace is from a selective run and the {} trace is not, so a delta '
                    'between them would report every line the selective run did not reach as a '
                    'regression -- a fabricated regression the size of the tests that did not '
                    'run. {}'.format(which[0],
                                     other_name if which[0] == this_name else this_name,
                                     _ONE_SENTENCE))
        if self._test_names_digest is None or other._test_names_digest is None:
            return ('both traces are from selective runs and at least one of them does not '
                    'record which tests it ran, so there is no way to check that they ran the '
                    'same ones. Two subsets that differ compare as a regression the size of '
                    'the difference. {}'.format(_ONE_SENTENCE))
        if self._test_names_digest != other._test_names_digest:
            return ('both traces are from selective runs and they ran different tests ({} '
                    'against {}), so the difference between them is mostly the difference '
                    'between the two test lists. {}'.format(
                        self._test_names_digest[:19], other._test_names_digest[:19],
                        _ONE_SENTENCE))
        return None

    # -- serialization ----------------------------------------------------------------------

    def to_json(self):
        """The record that goes in provenance, in the trace comment and in the scope file."""
        return {'schema': SCHEMA,
                'kind': self._kind,
                'argv': list(self._argv),
                'tests_run': self._tests_run,
                'tests_in_suite': self._tests_in_suite,
                'test_names_digest': self._test_names_digest,
                'suite_name': self._suite_name}

    @classmethod
    def from_json(cls, record):
        """The inverse. An unreadable or absent record is FULL_SUITE, which is the safe reading.

        Safe because a full-suite reading makes the tooling stricter, not looser: a gate is
        evaluated, a comparison is allowed, and nothing is silently marked as a lower bound. The
        dangerous direction is the other one -- reading a selective trace as full-suite -- and
        that is what an absent record means, so an unknown kind is refused rather than guessed.
        """
        if not record:
            return cls.full_suite()
        kind = record.get('kind')
        if kind == FULL_SUITE:
            return cls.full_suite()
        if kind != SELECTIVE:
            raise ValueError('unknown coverage scope {!r}; this artifact was produced by a '
                             'newer tool than this one'.format(kind))
        return cls(SELECTIVE, argv=record.get('argv') or (),
                   tests_run=record.get('tests_run'),
                   tests_in_suite=record.get('tests_in_suite'),
                   test_names_digest=record.get('test_names_digest'),
                   suite_name=record.get('suite_name'))

    def write(self, path):
        with open(path, 'w') as handle:
            json.dump(self.to_json(), handle, indent=2, sort_keys=True)
            handle.write('\n')
        return path

    @classmethod
    def read(cls, path):
        with open(path) as handle:
            return cls.from_json(json.load(handle))


def scope_from_provenance(record):
    """The CoverageScope a provenance record or trace comment describes.

    A record from before this existed has no test_scope key at all, and that reads as
    FULL_SUITE -- which is what such a trace was in practice, since there was no way to make a
    selective one that said so.
    """
    if not record:
        return CoverageScope.full_suite()
    return CoverageScope.from_json(record.get('test_scope'))

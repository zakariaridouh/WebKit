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

A run over a subset can only under-report: a covered line is exact, an uncovered line is unknown.
"""

import hashlib
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# NN.NN% as llvm-cov's summary table writes it; the decimal point keeps integer counts out.
_PERCENTAGE = re.compile(r'(\d+\.\d+%)')

FULL_SUITE = 'full-suite'
SELECTIVE = 'selective'

SELECTIVE_INFIX = '-selective'

LOWER_BOUND_PREFIX = '≥ '

SCHEMA = 'webkit-coverage-scope-1'

_ONE_SENTENCE = ('Adding tests can only turn a line from uncovered to covered, so in a run over '
                 'part of the suite a covered line is exact and an uncovered line is unknown.')


def digest_test_names(names):
    """'sha256:<hex>' over the sorted distinct names of a test-name list, or None for no list."""
    if names is None:
        return None
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        digest.update(name.encode('utf-8'))
        digest.update(b'\n')
    return 'sha256:' + digest.hexdigest()


class CoverageScope:
    """FULL_SUITE, or SELECTIVE over a named subset. Immutable; use full_suite() or selective()."""

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

        Only the digest of test_names is kept; tests_run defaults to the length of that list.
        """
        if test_names is not None:
            names = list(test_names)
            tests_run = len(names) if tests_run is None else tests_run
            test_names_digest = digest_test_names(names)
        return cls(SELECTIVE, argv=argv, tests_run=tests_run, tests_in_suite=tests_in_suite,
                   test_names_digest=test_names_digest, suite_name=suite_name)

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
        """Every NN.NN% in a line of somebody else's output, marked as a lower bound."""
        if not self.is_selective:
            return text
        return _PERCENTAGE.sub(lambda match: LOWER_BOUND_PREFIX + match.group(1), text)

    def qualify_title(self, title):
        if not self.is_selective:
            return title
        return '{} (lower bound, selective run)'.format(title)

    @property
    def infix(self):
        return SELECTIVE_INFIX if self.is_selective else ''

    def filename(self, name):
        """'coverage.lcov.gz' -> 'coverage-selective.lcov.gz'.

        The infix goes after the first component so the .lcov.gz extension stays intact.
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

        Never a percentage of the suite: that invites scaling the coverage number by it.
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
            # The counted suite ran in full; "0 tests were not asked" would read as a full run
            # although a named subset of another suite makes this selective.
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
        """Why two scopes cannot be compared for a delta, or None if they can."""
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
        """The inverse. An absent record is FULL_SUITE; an unknown kind is refused."""
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
    """The CoverageScope a provenance record or trace comment describes (none is FULL_SUITE)."""
    if not record:
        return CoverageScope.full_suite()
    return CoverageScope.from_json(record.get('test_scope'))

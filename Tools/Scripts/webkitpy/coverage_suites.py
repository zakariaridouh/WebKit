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

"""Attribute coverage to the test suite that produced it.

One number for the whole tree says a gap exists; it cannot say which suite should have closed
it. A line covered only by the API tests and a line covered only by the layout tests look
identical in a merged report, and so does a line that no suite covers at all in a component
whose only suite was not run.

  Tools/Scripts/run-webkit-tests --release --coverage --coverage-dir=/tmp/cov-layout
  Tools/Scripts/run-api-tests    --release --coverage --coverage-dir=/tmp/cov-api
  Tools/Scripts/generate-coverage-report --release --output-dir=/tmp/report \\
      --suite=layout:/tmp/cov-layout --suite=api:/tmp/cov-api

A suite is named on the command line and its profiles come from a path, because that is the
only thing the collected profiles can support. Collection separates runs by DIRECTORY and by
nothing else: the raw profiles are named after the framework that wrote them
(WebCore_0.profraw), not after the run, and collect_coverage_profiles() deliberately
de-collides names so that two runs CAN accumulate into one directory for a single report. So
two runs that shared a --coverage-dir cannot be told apart afterwards, by this or by anything
else, and the separation has to have been asked for when the tests ran. That is what
shared_coverage_directory_warning() checks for, since the failure is otherwise silent: a
mislabelled column.

Given that, --suite=NAME:PATH is the spelling that composes with what already exists. Each
harness run is already given its own --coverage-dir, so the only new information is which
directory was which suite. PATH takes an already-indexed .profdata as readily as a directory
of raw profiles, which is what a long sharded run produces. The alternatives were worse:
repeated --profdata with a parallel list of labels pairs by position, which is easy to get
wrong and impossible to diagnose; and a subdirectory convention under one --coverage-dir would
need the harnesses to know about suites, and would silently mislabel every profile that landed
in the wrong place.

The combined column is the union of the executed lines, which is not their sum -- on two
measured jsc runs, 57,797 lines and 60,614 lines came to 66,408 combined, not 118,411. It is
computed by merging the suites' profiles and reporting from the merge, rather than by unioning
the per-suite traces, so that it is by construction the number a merged profile produces. The
two agree: see check_union_equals_combined(), which asserts it on every run rather than
trusting the one measurement.
"""

import logging
import os
import re

from collections import namedtuple

from webkitpy.llvm_profile_utils import LLVMProfileData, merge_all_raw_profiles_in_directory

logger = logging.getLogger(__name__)

# A suite label. Kept to characters that are safe in a filename and unambiguous in a column
# heading, because it becomes both.
SUITE_NAME_PATTERN = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_.+-]*\Z')

# collect_coverage_profiles() appends -1, -2 ... to a name that already exists in the
# destination, which happens only when a second run collects into the same directory.
_COLLISION_SUFFIX_PATTERN = re.compile(r'-\d+\.profraw\Z')

# name: the label, as it appears as a column heading.
# source: what was on the command line, for diagnostics.
# profdata: the indexed profile to report this suite from.
# raw_profiles: the .profraw paths that went into it, empty when an indexed profile was given.
Suite = namedtuple('Suite', ('name', 'source', 'profdata', 'raw_profiles'))


class SuiteSpecError(ValueError):
    pass


def parse_suite_spec(spec):
    """'layout:/tmp/cov' -> ('layout', '/tmp/cov').

    Split on the first colon, so a path may contain one. A label is required rather than
    derived from the basename: the label ends up as a column heading in a report somebody else
    reads, and /tmp/cov2 is not a description of a test suite.
    """
    name, separator, path = spec.partition(':')
    if not separator:
        raise SuiteSpecError(
            '--suite={} has no label. Pass --suite=NAME:PATH, for example '
            '--suite=layout:/tmp/cov-layout'.format(spec))
    if not SUITE_NAME_PATTERN.match(name):
        raise SuiteSpecError(
            '--suite={} has an unusable label: a label must start with a letter or digit and '
            'hold only letters, digits and _ . + -, because it is used both as a filename and '
            'as a column heading'.format(spec))
    if not path:
        raise SuiteSpecError('--suite={} has no path'.format(spec))
    return name, path


def parse_suite_specs(specs):
    """[(name, path)] for a list of NAME:PATH, rejecting a repeated label."""
    parsed = []
    for spec in specs:
        name, path = parse_suite_spec(spec)
        for existing_name, existing_path in parsed:
            if existing_name == name:
                raise SuiteSpecError(
                    'Two suites are both labelled {}, {} and {}. Two columns with the same '
                    'heading cannot be read.'.format(name, existing_path, path))
        parsed.append((name, path))
    return parsed


def shared_coverage_directory_warning(directory):
    """A warning string when a directory holds profiles from more than one run, else None.

    Worth checking because the consequence is a silently mislabelled column rather than an
    error. Exact rather than heuristic: %4m gives one profile per framework per pool slot with
    no collisions inside a run, so a name that had to be de-collided means a second run
    collected into this directory.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    collided = sorted(name for name in names if _COLLISION_SUFFIX_PATTERN.search(name))
    if not collided:
        return None
    return ('{} holds {} profile(s) whose names had to be de-collided ({}...), which happens '
            'only when a second run collected into it. Raw profiles are named after the '
            'framework that wrote them, not the run, so the runs in there cannot be told '
            'apart and this suite is the two of them together.'.format(
                directory, len(collided), ', '.join(collided[:3])))


def resolve_suite(name, path, output_directory):
    """A Suite for one NAME:PATH, indexing the raw profiles in a directory if that is what it is."""
    if os.path.isdir(path):
        warning = shared_coverage_directory_warning(path)
        if warning:
            logger.warning('%s', warning)
        profdata = os.path.join(output_directory, 'coverage-{}.profdata'.format(name))
        raw_profiles = merge_all_raw_profiles_in_directory(path, profdata)
        logger.info('Suite %s: indexed %d raw profiles from %s into %s',
                    name, len(raw_profiles), path, profdata)
        return Suite(name, path, profdata, raw_profiles)
    if os.path.isfile(path):
        logger.info('Suite %s: reporting from the already-indexed profile %s', name, path)
        return Suite(name, path, path, [])
    raise SuiteSpecError('--suite={}:{} is neither a directory of raw profiles nor an '
                         'indexed profile'.format(name, path))


def resolve_suites(specs, output_directory):
    """[Suite] for a list of NAME:PATH."""
    return [resolve_suite(name, path, output_directory)
            for name, path in parse_suite_specs(specs)]


def merge_suite_profiles(suites, output_path):
    """Index every suite's profile into one, and return its path.

    This is what the combined column is reported from. llvm-profdata merges counters by
    function name and sums them, so a line executed by two suites has their counts added; the
    report only asks whether a count is nonzero, which is the union.
    """
    if len(suites) == 1:
        return suites[0].profdata
    merge = LLVMProfileData.merge(
        output_path, unweighted_profiles=[suite.profdata for suite in suites],
        failure_mode='all', num_threads=0)
    if merge.stderr:
        logger.info('llvm-profdata stderr: %s', merge.stderr)
    merge.check_returncode()
    logger.info('Merged %d suite profiles into %s (%.1f MB)', len(suites), output_path,
                os.path.getsize(output_path) / 1e6)
    return output_path


def line_totals(coverage_by_path):
    """{path: (instrumented lines, executed lines)}, dropping the per-line detail.

    Called once a suite's trace has been checked against the combined one, so that the
    per-line maps -- 1.9 million entries per suite on a full-suite run -- can be released
    before the line views are rendered.
    """
    return {path: (len(coverage.lines), sum(1 for count in coverage.lines.values() if count))
            for path, coverage in coverage_by_path.items()}


# lines: instrumented lines compared.
# disagreeing_lines: lines the merge calls covered and no suite does, or the reverse.
# denominator_files: files whose instrumented-line set differs between merge and suites.
# examples: [(path, line, combined count, {suite: count})] for the first few disagreements.
UnionCheck = namedtuple('UnionCheck',
                        ('lines', 'disagreeing_lines', 'denominator_files', 'examples'))


def check_union_equals_combined(combined_by_path, suites_by_path, max_examples=5):
    """Assert the combined column really is the union of the suites. Returns a UnionCheck.

    The combined column is computed from a merged profile, and the per-suite columns from one
    profile each, so the report is making a claim that these two things agree. It is not
    obviously true -- llvm-cov's output is profile-dependent in at least one respect, since the
    set of function records it emits varies with which functions the profile has data for -- so
    it is checked on every run rather than assumed. A disagreement means a suite's profile does
    not belong to the same build as the rest, or that a suite was passed twice.

    suites_by_path is {suite name: {path: FileCoverage}}.
    """
    compared = 0
    disagreeing = 0
    denominator_files = 0
    examples = []
    for path, combined in combined_by_path.items():
        suite_lines = [(name, by_path[path].lines)
                       for name, by_path in suites_by_path.items() if path in by_path]
        instrumented = set()
        for _, lines in suite_lines:
            instrumented |= set(lines)
        if instrumented != set(combined.lines):
            denominator_files += 1
        for number, count in combined.lines.items():
            compared += 1
            covered_by_a_suite = any(lines.get(number) for _, lines in suite_lines)
            if bool(count) != covered_by_a_suite:
                disagreeing += 1
                if len(examples) < max_examples:
                    examples.append((path, number, count,
                                     {name: lines.get(number) for name, lines in suite_lines}))
    return UnionCheck(compared, disagreeing, denominator_files, examples)


def log_union_check(check, suite_names):
    """Say what the check found, loudly when it found something."""
    if not check.disagreeing_lines and not check.denominator_files:
        logger.info('The combined column is the union of %s: checked %d instrumented lines, '
                    'no disagreement between the merged profile and the suites.',
                    ' + '.join(suite_names), check.lines)
        return
    logger.error('The combined column and the union of the suites disagree, so at least one '
                 'suite does not describe the same build as the others:')
    if check.denominator_files:
        logger.error('    %d file(s) have a different set of instrumented lines in the merged '
                     'profile than in the suites put together', check.denominator_files)
    if check.disagreeing_lines:
        logger.error('    %d of %d lines are covered in the merge but in no suite, or the '
                     'reverse', check.disagreeing_lines, check.lines)
    for path, number, count, per_suite in check.examples:
        logger.error('    %s:%d merged=%s %s', path, number, count,
                     ' '.join('{}={}'.format(name, value) for name, value in sorted(per_suite.items())))

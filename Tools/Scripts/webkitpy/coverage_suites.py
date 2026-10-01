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

"""Attribute coverage to the test suite that produced it, via --suite=NAME:PATH.

Suites are told apart only by --coverage-dir, so each suite's run needs a directory of its own.
"""

import logging
import os
import re

from collections import namedtuple

from webkitpy.llvm_profile_utils import (
    CoverageLLVMProfDataExecutable, LLVMProfileData, merge_all_coverage_profiles)

logger = logging.getLogger(__name__)

# A suite label: it becomes both a filename and a column heading.
SUITE_NAME_PATTERN = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_.+-]*\Z')

# Added by collect_coverage_profiles() when a second run collects into the same directory.
_COLLISION_SUFFIX_PATTERN = re.compile(r'-\d+\.profraw\Z')

Suite = namedtuple('Suite', ('name', 'source', 'profdata', 'raw_profiles'))


class SuiteSpecError(ValueError):
    pass


def parse_suite_spec(spec):
    """'layout:/tmp/cov' -> ('layout', '/tmp/cov'). Split on the first colon."""
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

    A name that had to be de-collided means a second run collected into this directory.
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
        raw_profiles = merge_all_coverage_profiles(path, profdata)
        logger.info('Suite %s: indexed %d raw profiles from %s into %s',
                    name, len(raw_profiles), path, profdata)
        return Suite(name, path, profdata, raw_profiles)
    if os.path.isfile(path):
        logger.info('Suite %s: reporting from the already-indexed profile %s', name, path)
        return Suite(name, path, path, [])
    raise SuiteSpecError('--suite={}:{} is neither a directory of raw profiles nor an '
                         'indexed profile'.format(name, path))


def resolve_suites(specs, output_directory):
    return [resolve_suite(name, path, output_directory)
            for name, path in parse_suite_specs(specs)]


def merge_suite_profiles(suites, output_path):
    """Merge every suite's profile into one and return its path, for the combined column.

    llvm-profdata sums counters, and the report only asks whether a count is nonzero: the union.
    """
    if len(suites) == 1:
        return suites[0].profdata
    merge = LLVMProfileData.merge(
        output_path, unweighted_profiles=[suite.profdata for suite in suites],
        failure_mode='all', num_threads=0, executable=CoverageLLVMProfDataExecutable)
    if merge.stderr:
        logger.info('llvm-profdata stderr: %s', merge.stderr)
    merge.check_returncode()
    logger.info('Merged %d suite profiles into %s (%.1f MB)', len(suites), output_path,
                os.path.getsize(output_path) / 1e6)
    return output_path


def line_totals(coverage_by_path):
    """{path: (instrumented lines, executed lines)}, dropping the per-line detail."""
    return {path: (len(coverage.lines), sum(1 for count in coverage.lines.values() if count))
            for path, coverage in coverage_by_path.items()}


# denominator_files: files whose instrumented lines differ between the merge and the suites.
UnionCheck = namedtuple('UnionCheck',
                        ('lines', 'disagreeing_lines', 'denominator_files', 'examples'))


def check_union_equals_combined(combined_by_path, suites_by_path, max_examples=5):
    """Check that the combined column really is the union of the suites. -> UnionCheck

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

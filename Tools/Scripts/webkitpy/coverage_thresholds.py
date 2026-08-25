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

"""Gate a build on coverage. Shared by generate-coverage-report and compare-coverage-reports.

Both tools can fail a build, one on absolute coverage and one on a drop against a baseline,
and both have to fail it in the same way: a bot that has to know which of the two it is
running before it can interpret the exit code is a bot that will get it wrong.
"""

import logging

logger = logging.getLogger(__name__)

# A failed gate, as distinct from 1, which means there is no report at all. The two are
# different people's problem -- "coverage is too low" is the author's, "the tooling broke" is
# whoever owns the bot -- and telling them apart must not require parsing output.
COVERAGE_GATE_EXIT_CODE = 2

# The metrics an absolute threshold can be set on, and regions are deliberately not among
# them. lcov carries line, function and branch records but no region records, so the only
# region total available is llvm-cov's own, in summary.txt, over a denominator that counts a
# copied header once per framework that includes it and sums per-function line counts rather
# than counting distinct lines: 2,098,175 lines against the report's 1,888,952 on a full-suite
# run. A --fail-under-regions would gate on a number that appears nowhere else in the report.
#
# --fail-under-functions is gated on a count that merges a template's instantiations, as
# llvm-cov's does. Before that was fixed it was keyed by mangled name and counted every
# instantiation separately -- 1,978,649 functions against llvm-cov's 255,297, a 7.75x
# denominator -- so --fail-under-functions=70 failed a build that summary.txt, in the same
# output directory, called 72.09%. It still does not equal summary.txt exactly (73.84% against
# 72.09%), because the report unions the duplicate records for one canonical file and llvm-cov
# sums them; that difference is the point of gating on the report's own numbers.
GATED_METRICS = ('lines', 'functions', 'branches')


def option_name(metric):
    return '--fail-under-{}'.format(metric)


def destination_name(metric):
    return 'fail_under_{}'.format(metric)


def thresholds_from_options(options):
    """{metric: percent} for the --fail-under-METRIC flags that were given."""
    thresholds = {}
    for metric in GATED_METRICS:
        threshold = getattr(options, destination_name(metric), None)
        if threshold is not None:
            thresholds[metric] = threshold
    return thresholds


def check_absolute_thresholds(thresholds, totals):
    """0, or COVERAGE_GATE_EXIT_CODE if any threshold in thresholds was not met.

    totals is {metric: (count, covered)}, as coverage_lcov.project_totals() returns it -- the
    report's own numbers, not llvm-cov's, so that the gate and the report agree.

    Every gated metric is logged whether it passed or not, so that raising a threshold does
    not need a second run to find out what the headroom was. A metric with nothing to measure
    fails rather than passes: a gate that silently does nothing is worse than one that fires.
    """
    failed = False
    for metric in GATED_METRICS:
        if metric not in thresholds:
            continue
        threshold = thresholds[metric]
        count, covered = totals.get(metric, (0, 0))
        if not count:
            logger.error('The report has no %s to measure, so %s cannot be applied.',
                         metric, option_name(metric))
            failed = True
            continue
        percent = 100.0 * covered / count
        message = ('%s coverage is %.2f%% (%d of %d), %s the %.2f%% required by %s.')
        if percent < threshold:
            logger.error(message, metric.capitalize(), percent, covered, count, 'below',
                         threshold, option_name(metric))
            failed = True
        else:
            logger.info(message, metric.capitalize(), percent, covered, count,
                        'at or above', threshold, option_name(metric))
    return COVERAGE_GATE_EXIT_CODE if failed else 0

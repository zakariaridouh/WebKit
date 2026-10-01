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

Both tools fail a build in the same way, so a bot need not know which one it ran.
"""

import logging

logger = logging.getLogger(__name__)

# A failed gate, as distinct from 1, which means there is no report at all. Telling the two
# apart must not require parsing output.
COVERAGE_GATE_EXIT_CODE = 2

# Regions are deliberately not gated: lcov carries no region records, and llvm-cov's own total
# has a different denominator from the report's. Function counts merge a template's instantiations.
GATED_METRICS = ('lines', 'functions', 'branches')


def option_name(metric):
    return '--fail-under-{}'.format(metric)


def destination_name(metric):
    return 'fail_under_{}'.format(metric)


def thresholds_from_options(options):
    thresholds = {}
    for metric in GATED_METRICS:
        threshold = getattr(options, destination_name(metric), None)
        if threshold is not None:
            thresholds[metric] = threshold
    return thresholds


def check_absolute_thresholds(thresholds, totals, scope=None):
    """0, or COVERAGE_GATE_EXIT_CODE if any threshold in thresholds was not met.

    totals is {metric: (count, covered)}, as coverage_lcov.project_totals() returns it.
    A selective scope fails every gate, and so does a metric with nothing to measure.
    """
    if scope is not None and scope.is_selective and thresholds:
        for metric in GATED_METRICS:
            if metric in thresholds:
                logger.error('%s', scope.gate_refusal(option_name(metric)))
        return COVERAGE_GATE_EXIT_CODE
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

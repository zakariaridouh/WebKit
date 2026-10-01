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

import optparse
import unittest

from webkitpy.coverage_lcov import FileCoverage, project_totals
from webkitpy.coverage_thresholds import (
    COVERAGE_GATE_EXIT_CODE, GATED_METRICS, check_absolute_thresholds, destination_name,
    option_name, thresholds_from_options)


class GatedMetricsTest(unittest.TestCase):
    def test_regions_are_not_gatable(self):
        self.assertNotIn('regions', GATED_METRICS)

    def test_every_gated_metric_is_one_the_trace_can_produce(self):
        self.assertEqual(set(GATED_METRICS), set(FileCoverage().totals()))


class ThresholdsFromOptionsTest(unittest.TestCase):
    def options(self, **values):
        return optparse.Values({destination_name(metric): values.get(metric)
                                for metric in GATED_METRICS})

    def test_nothing_given_is_no_gate(self):
        self.assertEqual(thresholds_from_options(self.options()), {})

    def test_zero_is_a_gate_and_not_an_absence(self):
        self.assertEqual(thresholds_from_options(self.options(lines=0.0)), {'lines': 0.0})

    def test_each_metric_is_read_from_its_own_destination(self):
        self.assertEqual(thresholds_from_options(self.options(lines=60.0, branches=40.0)),
                         {'lines': 60.0, 'branches': 40.0})


class CheckAbsoluteThresholdsTest(unittest.TestCase):
    TOTALS = {'lines': (100, 67), 'functions': (10, 8), 'branches': (50, 20)}

    def check(self, thresholds, totals):
        """check_absolute_thresholds, asserting that every gated metric is logged."""
        with self.assertLogs('webkitpy.coverage_thresholds', level='INFO') as captured:
            result = check_absolute_thresholds(thresholds, totals)
        self.assertEqual(len(captured.output), len(thresholds))
        return result

    def test_no_thresholds_passes(self):
        self.assertEqual(check_absolute_thresholds({}, self.TOTALS), 0)

    def test_a_met_threshold_passes(self):
        self.assertEqual(self.check({'lines': 60.0}, self.TOTALS), 0)

    def test_exactly_on_the_threshold_passes(self):
        self.assertEqual(self.check({'lines': 67.0}, self.TOTALS), 0)

    def test_a_missed_threshold_fails_with_the_gate_exit_code(self):
        self.assertEqual(self.check({'lines': 67.01}, self.TOTALS), COVERAGE_GATE_EXIT_CODE)

    def test_one_missed_threshold_fails_the_whole_check(self):
        self.assertEqual(self.check({'lines': 60.0, 'branches': 90.0}, self.TOTALS),
                         COVERAGE_GATE_EXIT_CODE)

    def test_a_metric_with_nothing_to_measure_fails(self):
        self.assertEqual(self.check({'lines': 60.0}, {'lines': (0, 0)}),
                         COVERAGE_GATE_EXIT_CODE)

    def test_a_metric_missing_from_the_totals_fails(self):
        self.assertEqual(self.check({'branches': 1.0}, {'lines': (10, 10)}),
                         COVERAGE_GATE_EXIT_CODE)

    def test_it_gates_on_the_reports_own_totals(self):
        # Totals come from the parsed trace, not llvm-cov's summary, so the gate and report agree.
        coverage = FileCoverage()
        coverage.lines = {1: 1, 2: 0, 3: 1, 4: 0}
        totals = project_totals({'/checkout/Source/WTF/wtf/Vector.h': coverage})
        self.assertEqual(totals['lines'], (4, 2))
        self.assertEqual(self.check({'lines': 50.0}, totals), 0)
        self.assertEqual(self.check({'lines': 50.01}, totals), COVERAGE_GATE_EXIT_CODE)


class OptionNamingTest(unittest.TestCase):
    def test_the_option_and_its_destination_agree(self):
        for metric in GATED_METRICS:
            self.assertEqual(option_name(metric).lstrip('-').replace('-', '_'),
                             destination_name(metric))


if __name__ == '__main__':
    unittest.main()

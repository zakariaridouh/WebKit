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

from webkitpy.coverage_javascript import (
    ANCHOR_PHASE, CLEAN_PHASE, AttributionError, BasicBlock, BuiltinsCoverage, BuiltinsIndex,
    JavaScriptCoverageReport, JSCDriver, attribute_builtins_source, expand_test_paths,
    format_line_numbers, format_percent, format_report, locate_builtin_sources,
    mangled_code_name, parse_basic_block_dump, parse_combined_builtins, percent,
    report_for_builtin)

# A builtins file shaped like the real ones: a licence block whose collapse is what used to
# misplace every line number, a single-line comment inside a function body, and an @constructor.
# Line numbers are asserted against this text throughout, so it is written with them in view:
#
#   1  /*
#   2   * Copyright (C) 2026 Apple Inc. All rights reserved.
#   3   */
#   4
#   5  function alpha(a)
#   6  {
#   7      "use strict";
#   8      // count it
#   9      return a;
#  10  }
#  11
#  12  @constructor
#  13  function Beta(b)
#  14  {
#  15      return b;
#  16  }
_FIXTURE_JS = '''/*
 * Copyright (C) 2026 Apple Inc. All rights reserved.
 */

function alpha(a)
{
    "use strict";
    // count it
    return a;
}

@constructor
function Beta(b)
{
    return b;
}
'''

# What the generator embeds for each of the two functions above: wrapped in `(function ...)`,
# the declared name dropped, the comment body replaced by a bare `//`, and a newline appended.
# Written out rather than derived, so that the module reproducing the generator's rewrite is
# checked against something independent of itself.
_ALPHA_EMBEDDED = '(function (a)\n{\n    "use strict";\n    //\n    return a;\n})\n'
_BETA_EMBEDDED = '(function (b)\n{\n    return b;\n})\n'

# Offsets within _ALPHA_EMBEDDED, used by the line-attribution tests:
#    0..12  `(function (a)`      line 5
#      13   newline              ends line 5
#      14   `{`                  line 6
#      15   newline
#   16..32  `    "use strict";`  line 7
#      33   newline
#   34..39  `    //`             line 8
#      40   newline
#   41..53  `    return a;`      line 9, of which 41..44 are the indent and 45..53 the statement
#      54   newline
#   55..56  `})`                 line 10
_ALPHA_STATEMENT_START = 45
_ALPHA_STATEMENT_END = 53


def _generated_source(namespace='JSC', entries=()):
    """A minimal <NS>Builtins.cpp in the shape the combined generator writes.

    Only the three declarations this tool reads, in the same syntax -- including the newline
    between `=` and `s_JSCCombinedCode + N`, which the real generator emits and a single-line
    regex would miss.
    """
    combined = ''.join(source for _, source in entries)
    characters = ', '.join(str(ord(character)) for character in combined)
    lines = ['constinit const char s_%sCombinedCode[] = { %s };' % (namespace, characters),
             '',
             'constinit const unsigned s_%sCombinedCodeLength = %d;' % (namespace,
                                                                        len(combined))]
    offset = 0
    for name, source in entries:
        lines += ['',
                  'constinit const int s_%sCodeLength = %d;' % (name, len(source)),
                  'constinit const char* const s_%sCode =' % name,
                  's_%sCombinedCode + %d' % (namespace, offset),
                  ';']
        offset += len(source)
    return '\n'.join(lines) + '\n', combined


class _FixtureCheckout:
    """A checkout root holding one builtins file and one generated table over it."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='coverage-javascript-test-')
        self.builtins_directory = os.path.join('Source', 'JavaScriptCore', 'builtins')
        absolute = os.path.join(self.root, self.builtins_directory)
        os.makedirs(absolute)
        with open(os.path.join(absolute, 'Fixture.js'), 'w') as handle:
            handle.write(_FIXTURE_JS)
        self.generated, self.combined = _generated_source(entries=(
            ('fixtureAlpha', _ALPHA_EMBEDDED), ('fixtureBetaConstructor', _BETA_EMBEDDED)))
        self.generated_path = os.path.join(self.root, 'JSCBuiltins.cpp')
        with open(self.generated_path, 'w') as handle:
            handle.write(self.generated)

    def index(self):
        return BuiltinsIndex.load(self.root, self.generated_path,
                                  directories=(self.builtins_directory,))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TheGeneratedNameIsTheWholeMappingTest(unittest.TestCase):
    """A codeName is a file base name plus a function name, so it places a window in a file."""

    def test_a_prototype_method_gets_its_object_prefix(self):
        self.assertEqual(mangled_code_name('ArrayPrototype', 'forEach'), 'arrayPrototypeForEach')

    def test_a_leading_acronym_is_lowercased_as_a_unit(self):
        # WK_lcfirst turns 'jS' into 'js', so it is jsIteratorPrototype and not jSIterator...
        self.assertEqual(mangled_code_name('JSIteratorPrototype', 'flatMap'),
                         'jsIteratorPrototypeFlatMap')

    def test_a_constructor_gets_a_suffix_it_does_not_get_from_its_name(self):
        # Without this, PromiseConstructor.js's `function Promise` is the one builtin of 102
        # that cannot be placed.
        self.assertEqual(mangled_code_name('PromiseConstructor', 'Promise', is_constructor=True),
                         'promiseConstructorPromiseConstructor')

    def test_the_inspectors_injected_script_is_named_the_same_way(self):
        self.assertEqual(mangled_code_name('InjectedScriptSource', 'createArrayWithoutPrototype'),
                         'injectedScriptSourceCreateArrayWithoutPrototype')


class TheCombinedTableIsReadFromTheBuildTest(unittest.TestCase):
    """The offsets come out of the build's own generated file, byte for byte."""

    def setUp(self):
        self.text, self.combined = _generated_source(entries=(
            ('fixtureAlpha', _ALPHA_EMBEDDED), ('fixtureBetaConstructor', _BETA_EMBEDDED)))

    def test_the_char_array_decodes_back_to_the_source_it_was_made_from(self):
        decoded, _ = parse_combined_builtins(self.text)
        self.assertEqual(decoded, self.combined)

    def test_each_builtin_gets_an_offset_and_a_length(self):
        _, windows = parse_combined_builtins(self.text)
        self.assertEqual(windows['fixtureAlpha'], (0, len(_ALPHA_EMBEDDED)))
        self.assertEqual(windows['fixtureBetaConstructor'],
                         (len(_ALPHA_EMBEDDED), len(_BETA_EMBEDDED)))

    def test_the_windows_partition_the_combined_source_exactly(self):
        combined, windows = parse_combined_builtins(self.text)
        self.assertEqual(sum(length for _, length in windows.values()), len(combined))

    def test_a_separate_mode_builtins_file_is_refused_rather_than_half_read(self):
        # WebCore's builtins are generated one file at a time and have no combined offset space,
        # so there is nothing here to read and saying so is better than returning an empty table.
        with self.assertRaises(ValueError):
            parse_combined_builtins('constinit const char* const s_readableStreamPullCode = '
                                    '"(function () { })";\n')


class LineNumbersAreTheFilesOwnTest(unittest.TestCase):
    """The generator rewrites the text it reads; the report has to name the file's lines."""

    def setUp(self):
        self.checkout = _FixtureCheckout()
        self.locations = locate_builtin_sources(
            self.checkout.root, directories=(self.checkout.builtins_directory,))

    def tearDown(self):
        self.checkout.cleanup()

    def test_a_function_below_a_licence_block_is_not_pulled_up_to_where_the_block_collapsed(self):
        # `/*...*/` becomes `/**/`, losing two lines. Reading the line number out of the
        # rewritten text is how 93 of 102 real builtins ended up misplaced.
        self.assertEqual(self.locations['fixtureAlpha'].line, 5)

    def test_the_line_is_the_function_keyword_and_not_its_annotation(self):
        # `@constructor` is on line 12 and the keyword on 13. The embedded source begins at the
        # keyword, so 13 is the line the offsets are relative to.
        self.assertEqual(self.locations['fixtureBetaConstructor'].line, 13)

    def test_the_function_name_is_recorded_as_written_not_as_mangled(self):
        self.assertEqual(self.locations['fixtureBetaConstructor'].function_name, 'Beta')

    def test_every_declared_function_is_found(self):
        self.assertEqual(sorted(self.locations), ['fixtureAlpha', 'fixtureBetaConstructor'])


class ThePlacementIsVerifiedNotAssumedTest(unittest.TestCase):
    """A builtin whose file no longer agrees with the table reports no line detail."""

    def setUp(self):
        self.checkout = _FixtureCheckout()

    def tearDown(self):
        self.checkout.cleanup()

    def test_a_matching_file_and_table_verify(self):
        index = self.checkout.index()
        self.assertEqual(index.verification_summary(), (2, 2, 2))

    def test_the_span_runs_from_the_keyword_to_the_closing_brace(self):
        alpha = self.checkout.index().builtins['fixtureAlpha']
        self.assertEqual((alpha.first_line, alpha.last_line), (5, 10))

    def test_an_edited_file_fails_verification_instead_of_reporting_shifted_lines(self):
        # A statement added inside alpha's body. The generated table still says the function is
        # six lines long, so the closing brace is no longer where the mapping predicts and the
        # line numbers this would report are the right numbers of the wrong text.
        path = os.path.join(self.checkout.root, self.checkout.builtins_directory, 'Fixture.js')
        with open(path, 'w') as handle:
            handle.write(_FIXTURE_JS.replace('    return a;\n', '    var b = a;\n    return a;\n'))
        index = self.checkout.index()
        self.assertFalse(index.builtins['fixtureAlpha'].line_mapping_verified)
        # The builtin below the edit is re-derived from the file, so a whole-file shift on its
        # own is not a staleness that matters. Only the edited function loses its detail.
        self.assertTrue(index.builtins['fixtureBetaConstructor'].line_mapping_verified)

    def test_an_unverified_builtin_offers_no_lines_at_all(self):
        path = os.path.join(self.checkout.root, self.checkout.builtins_directory, 'Fixture.js')
        with open(path, 'w') as handle:
            handle.write('function alpha(a)\n{\n')
        index = self.checkout.index()
        self.assertEqual(index.builtins['fixtureAlpha'].code_lines(), [])
        self.assertIsNone(index.builtins['fixtureAlpha'].file_line_for_offset(0))


class TheDenominatorIsStatementsTest(unittest.TestCase):
    """Blank, comment and punctuation-only lines are not lines a test can execute."""

    def setUp(self):
        self.checkout = _FixtureCheckout()
        self.index = self.checkout.index()

    def tearDown(self):
        self.checkout.cleanup()

    def test_braces_and_comments_are_out_and_statements_are_in(self):
        # Of lines 5..10: 6 is `{`, 8 is a comment, 10 is `}`.
        self.assertEqual(self.index.builtins['fixtureAlpha'].code_lines(), [5, 7, 9])

    def test_a_second_builtin_gets_its_own_span(self):
        self.assertEqual(self.index.builtins['fixtureBetaConstructor'].code_lines(), [13, 15])


class ABlocksIndentationDoesNotClaimALineTest(unittest.TestCase):
    """JSC's blocks are contiguous, so one reaches into the next line's whitespace."""

    def setUp(self):
        self.checkout = _FixtureCheckout()
        self.alpha = self.checkout.index().builtins['fixtureAlpha']

    def tearDown(self):
        self.checkout.cleanup()

    def test_a_block_ending_in_the_next_lines_indent_stops_at_the_previous_line(self):
        # 33..44 is the newline that ends `"use strict";`, the whole `//` line, and the four
        # spaces that indent `return a;`. The only line it holds text on is 8: the newline is
        # not text on line 7 and the indent is not text on line 9.
        self.assertEqual(self.alpha.lines_substantially_covered_by(33, 44), (8,))

    def test_a_block_that_reaches_a_statement_claims_its_line(self):
        self.assertEqual(self.alpha.lines_substantially_covered_by(16, 44), (7, 8))

    def test_the_statement_itself_claims_its_line(self):
        self.assertEqual(
            self.alpha.lines_substantially_covered_by(_ALPHA_STATEMENT_START,
                                                      _ALPHA_STATEMENT_END), (9,))

    def test_a_newline_alone_claims_nothing(self):
        self.assertEqual(self.alpha.lines_substantially_covered_by(33, 33), ())

    def test_an_executed_block_next_to_an_unexecuted_one_does_not_cover_its_line(self):
        # The whole point: without the whitespace rule the executed block below would report
        # line 9 covered, and the throw-style statement that never ran would be invisible.
        report = report_for_builtin(self.alpha, [
            BasicBlock(14, 32, True, 3),
            BasicBlock(33, 44, True, 3),
            BasicBlock(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, False, 0),
        ])
        self.assertTrue(report.executed)
        self.assertEqual(report.uncovered_lines, (9,))
        self.assertEqual((report.lines_covered, report.lines_total), (2, 3))


class NoBlocksMeansNeverCalledTest(unittest.TestCase):
    """Zero blocks is not missing data: JSC generates a builtin's bytecode on first call."""

    def setUp(self):
        self.checkout = _FixtureCheckout()
        self.index = self.checkout.index()

    def tearDown(self):
        self.checkout.cleanup()

    def test_a_builtin_with_no_blocks_reports_every_line_uncovered(self):
        report = report_for_builtin(self.index.builtins['fixtureBetaConstructor'], [])
        self.assertFalse(report.executed)
        self.assertEqual(report.blocks_known, 0)
        self.assertEqual(report.uncovered_lines, (13, 15))
        self.assertEqual((report.lines_covered, report.lines_total), (0, 2))

    def test_one_executed_block_makes_the_function_executed(self):
        report = report_for_builtin(self.index.builtins['fixtureAlpha'],
                                    [BasicBlock(0, 12, True, 1)])
        self.assertTrue(report.executed)
        self.assertEqual(report.blocks_executed, 1)


class TheDumpIsParsedLenientlyTest(unittest.TestCase):
    """jsc's stderr carries whatever the tests printed as well as the dump."""

    DUMP = '\n'.join([
        'Some warning the VM emitted',
        '--> ' + CLEAN_PHASE,
        'SourceID: 2',
        '\tBasicBlock: [0, -1] hasExecuted: false, executionCount:0',
        '\tBasicBlock: [15, 65] hasExecuted: true, executionCount:4',
        '--> ' + ANCHOR_PHASE,
        'SourceID: 9',
        '\tBasicBlock: [45, 53] hasExecuted: true, executionCount:7',
        '--> WEBKIT-JS-COVERAGE-END',
        '']),

    def test_the_two_phases_are_kept_apart(self):
        phases = parse_basic_block_dump(self.DUMP[0])
        self.assertEqual(sorted(phases), sorted([CLEAN_PHASE, ANCHOR_PHASE,
                                                 'WEBKIT-JS-COVERAGE-END']))

    def test_the_blocks_land_under_their_source_id(self):
        phases = parse_basic_block_dump(self.DUMP[0])
        self.assertEqual(len(phases[CLEAN_PHASE][2]), 2)
        self.assertEqual(phases[ANCHOR_PHASE][9][0].execution_count, 7)

    def test_a_line_that_is_neither_a_marker_a_header_nor_a_block_is_ignored(self):
        self.assertNotIn(None, parse_basic_block_dump(self.DUMP[0]))

    def test_blocks_printed_with_no_marker_at_all_still_parse(self):
        phases = parse_basic_block_dump(
            'SourceID: 3\n\tBasicBlock: [1, 2] hasExecuted: true, executionCount:1\n')
        self.assertEqual(phases[None][3][0].end, 2)


class AttributionIsADifferenceBetweenTwoDumpsTest(unittest.TestCase):
    """The builtins SourceID is the one that GAINED the planted count, not the one holding it."""

    def setUp(self):
        self.checkout = _FixtureCheckout()
        self.index = self.checkout.index()

    def tearDown(self):
        self.checkout.cleanup()

    def _phases(self, clean_sources=(), anchor_sources=()):
        lines = []
        for marker, sources in ((CLEAN_PHASE, clean_sources), (ANCHOR_PHASE, anchor_sources)):
            lines.append('--> ' + marker)
            for source_id, blocks in sources:
                lines.append('SourceID: {}'.format(source_id))
                for start, end, count in blocks:
                    lines.append('\tBasicBlock: [{}, {}] hasExecuted: true, '
                                 'executionCount:{}'.format(start, end, count))
        return parse_basic_block_dump('\n'.join(lines) + '\n')

    def _attribute(self, phases):
        return attribute_builtins_source(
            phases, self.index, anchor_code_name='fixtureAlpha', nonce=7)

    def test_a_window_block_that_appears_only_in_the_anchor_dump_carries_the_nonce(self):
        phases = self._phases(
            anchor_sources=[(9, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 7)])])
        self.assertEqual(self._attribute(phases), 9)

    def test_a_builtin_the_tests_already_ran_is_still_recognised(self):
        # The bug this replaced: with the test's own six executions already on the block, an
        # absolute count of 7 matches nothing and the driver's source wins instead. Measured on
        # JSTests/stress/array-species-functions.js, which reported 0 of 102 builtins executed
        # while its dump showed Array.prototype.forEach at count 6.
        phases = self._phases(
            clean_sources=[(9, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 6)])],
            anchor_sources=[(9, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 13)])])
        self.assertEqual(self._attribute(phases), 9)

    def test_a_source_that_did_not_change_between_the_dumps_is_never_the_builtins(self):
        # Nothing but the anchor runs between the two dumps, so a test file's every delta is 0 --
        # which is what makes a test file's offsets landing inside a builtin window harmless.
        phases = self._phases(
            clean_sources=[(2, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 7)])],
            anchor_sources=[(2, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 7)])])
        with self.assertRaises(AttributionError):
            self._attribute(phases)

    def test_a_delta_outside_the_anchors_window_does_not_count(self):
        beta = self.index.builtins['fixtureBetaConstructor']
        phases = self._phases(anchor_sources=[(9, [(beta.offset, beta.offset + 3, 7)])])
        with self.assertRaises(AttributionError):
            self._attribute(phases)

    def test_a_delta_of_the_wrong_size_does_not_count(self):
        phases = self._phases(
            anchor_sources=[(9, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 8)])])
        with self.assertRaises(AttributionError):
            self._attribute(phases)

    def test_two_carriers_are_refused_rather_than_one_of_them_chosen(self):
        phases = self._phases(
            anchor_sources=[(9, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 7)]),
                            (11, [(_ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 7)])])
        with self.assertRaises(AttributionError):
            self._attribute(phases)

    def test_a_run_that_ended_before_the_driver_is_refused(self):
        # A test that calls quit() ends the process, so the anchor phase never happens and the
        # clean dump alone cannot be attributed to anything.
        phases = parse_basic_block_dump('--> ' + CLEAN_PHASE + '\nSourceID: 2\n')
        with self.assertRaises(AttributionError):
            self._attribute(phases)


class RunsAccumulateWithoutBeingSummedTest(unittest.TestCase):
    """A block executed by any run is executed, and batching must not change the number."""

    def setUp(self):
        self.checkout = _FixtureCheckout()
        self.coverage = BuiltinsCoverage(self.checkout.index())

    def tearDown(self):
        self.checkout.cleanup()

    def _dump(self, source_id, blocks, anchor=True):
        lines = ['--> ' + CLEAN_PHASE, 'SourceID: {}'.format(source_id)]
        for start, end, executed, count in blocks:
            lines.append('\tBasicBlock: [{}, {}] hasExecuted: {}, executionCount:{}'.format(
                start, end, 'true' if executed else 'false', count))
        if anchor:
            lines += ['--> ' + ANCHOR_PHASE, 'SourceID: {}'.format(source_id),
                      '\tBasicBlock: [{}, {}] hasExecuted: true, executionCount:{}'.format(
                          _ALPHA_STATEMENT_START, _ALPHA_STATEMENT_END, 7)]
        return '\n'.join(lines) + '\n'

    def _add(self, text, label=None):
        # The fixture's anchor is fixtureAlpha with nonce 7, so the coverage object has to be
        # told; the default is the real arrayPrototypeForEach.
        phases = parse_basic_block_dump(text)
        source_id = attribute_builtins_source(
            phases, self.coverage.index, anchor_code_name='fixtureAlpha', nonce=7)
        for block in phases.get(CLEAN_PHASE, {}).get(source_id, ()):
            self.coverage.add_block(block)
        self.coverage.runs_attributed += 1

    def test_the_highest_count_for_a_block_survives(self):
        self._add(self._dump(9, [(14, 32, True, 2)]))
        self._add(self._dump(9, [(14, 32, True, 900)]))
        blocks = self.coverage.blocks_by_builtin()['fixtureAlpha']
        self.assertEqual([block.execution_count for block in blocks], [900])

    def test_a_block_one_run_executed_stays_executed(self):
        self._add(self._dump(9, [(14, 32, False, 0)]))
        self._add(self._dump(9, [(14, 32, True, 1)]))
        self.assertTrue(self.coverage.blocks_by_builtin()['fixtureAlpha'][0].has_executed)

    def test_the_empty_range_a_real_dump_contains_is_dropped(self):
        self.coverage.add_block(BasicBlock(0, -1, False, 0))
        self.assertEqual(self.coverage.blocks_by_builtin(), {})
        self.assertEqual(self.coverage.blocks_outside_any_builtin, 0)

    def test_a_block_no_builtin_owns_is_counted_and_not_attributed(self):
        self.coverage.add_block(BasicBlock(100000, 100010, True, 1))
        self.assertEqual(self.coverage.blocks_by_builtin(), {})
        self.assertEqual(self.coverage.blocks_outside_any_builtin, 1)

    def test_a_block_straddling_two_builtins_belongs_to_neither(self):
        alpha = self.coverage.index.builtins['fixtureAlpha']
        self.coverage.add_block(BasicBlock(alpha.end - 2, alpha.end + 2, True, 1))
        self.assertEqual(self.coverage.blocks_by_builtin(), {})
        self.assertEqual(self.coverage.blocks_outside_any_builtin, 1)

    def test_an_unattributable_run_is_named_rather_than_dropped(self):
        self.assertFalse(self.coverage.add_dump('nothing here at all', label='quitting.js'))
        self.assertEqual(self.coverage.runs_attributed, 0)
        self.assertEqual(self.coverage.runs_unattributed[0][0], 'quitting.js')


class TheReportLeadsWithWhatNeverRanTest(unittest.TestCase):
    """The answer this exists to produce is the list of builtins nothing called."""

    def setUp(self):
        self.checkout = _FixtureCheckout()
        self.coverage = BuiltinsCoverage(self.checkout.index())
        self.coverage.add_block(BasicBlock(14, 32, True, 5))
        self.coverage.runs_attributed = 1
        self.report = JavaScriptCoverageReport(self.coverage, jsc_path='/build/jsc', test_count=1)

    def tearDown(self):
        self.checkout.cleanup()

    def test_the_denominator_is_every_builtin_the_build_embedded(self):
        # Not every builtin the dump mentioned: one of these two produced no blocks at all and
        # is still counted.
        self.assertEqual(self.report.functions_total, 2)
        self.assertEqual(self.report.functions_executed, 1)

    def test_the_never_executed_list_names_the_file_the_function_and_the_line(self):
        never = self.report.never_executed()
        self.assertEqual(len(never), 1)
        self.assertEqual(never[0].builtin.function_name, 'Beta')
        self.assertEqual(never[0].builtin.first_line, 13)
        self.assertTrue(never[0].builtin.path.endswith('Fixture.js'))

    def test_the_files_are_grouped_and_totalled(self):
        self.assertEqual(len(self.report.files), 1)
        self.assertEqual((self.report.files[0].functions_executed,
                          self.report.files[0].functions_total), (1, 2))

    def test_the_text_report_says_the_headline_out_loud(self):
        text = '\n'.join(format_report(self.report))
        self.assertIn('1 of 2 builtins executed', text)
        self.assertIn('1 builtins were never executed', text)
        self.assertIn('Beta', text)

    def test_the_json_carries_the_mechanism_and_the_provenance(self):
        record = self.report.to_json()
        self.assertIn('useControlFlowProfiler', record['mechanism'])
        self.assertEqual(record['builtins_total'], 2)
        self.assertEqual(record['builtins_with_verified_line_mapping'], 2)
        self.assertEqual(record['functions'], {'total': 2, 'executed': 1})


class TheDriverNeverWritesToTheSharedProfileDirectoryTest(unittest.TestCase):
    """An instrumented jsc has a machine-global .profraw path baked in; running it 5,000 times
    into the middle of somebody else's coverage run is not an acceptable side effect."""

    def test_a_profile_directory_is_used_when_one_is_given(self):
        driver = JSCDriver('/build/jsc', profile_directory='/tmp/mine')
        environment = driver.environment(base={})
        self.assertTrue(environment['LLVM_PROFILE_FILE'].startswith('/tmp/mine/'))

    def test_the_variable_is_set_even_with_no_directory_to_write_to(self):
        # Unset is not an option: unset means the baked-in path.
        self.assertEqual(JSCDriver('/build/jsc').environment(base={})['LLVM_PROFILE_FILE'],
                         os.devnull)

    def test_an_inherited_value_is_overridden_rather_than_respected(self):
        environment = JSCDriver('/build/jsc', profile_directory='/tmp/mine').environment(
            base={'LLVM_PROFILE_FILE': '/private/tmp/WebKitCoverage/JSC_%8m%c.profraw'})
        self.assertNotIn('WebKitCoverage', environment['LLVM_PROFILE_FILE'])

    def test_the_build_directory_goes_in_front_of_the_system_frameworks(self):
        # Without this the CMake build's jsc loads /System/Library/Frameworks/JavaScriptCore
        # and dies on the first symbol the build added.
        environment = JSCDriver('/build/jsc', framework_path='/build').environment(base={})
        self.assertEqual(environment['DYLD_FRAMEWORK_PATH'], '/build')
        environment = JSCDriver('/build/jsc', framework_path='/build').environment(
            base={'DYLD_FRAMEWORK_PATH': '/elsewhere'})
        self.assertEqual(environment['DYLD_FRAMEWORK_PATH'], '/build:/elsewhere')


class TheDriverPlantsItsNonceAfterMeasuringTest(unittest.TestCase):
    """The anchor must not appear in the numbers it makes attributable."""

    def setUp(self):
        self.source = JSCDriver('/build/jsc', anchor_nonce=4242).driver_source()

    def test_the_measurement_dump_comes_before_the_anchor(self):
        self.assertLess(self.source.index(CLEAN_PHASE), self.source.index(ANCHOR_PHASE))
        self.assertLess(self.source.index(CLEAN_PHASE), self.source.index('4242'))

    def test_there_are_two_dumps(self):
        self.assertEqual(self.source.count('$vm.dumpBasicBlockExecutionRanges()'), 2)

    def test_the_anchor_uses_a_pristine_realm_so_a_test_cannot_move_it(self):
        # jsc evaluates every file into one global object, so a test that assigns to
        # Array.prototype.forEach would otherwise be the function the anchor calls.
        self.assertIn('$vm.createGlobalObject()', self.source)

    def test_the_command_puts_the_driver_last(self):
        command = JSCDriver('/build/jsc').command(['a.js', 'b.js'], '/tmp/driver.js')
        self.assertEqual(command[-1], '/tmp/driver.js')
        self.assertIn('--useControlFlowProfiler=true', command)
        self.assertIn('--useDollarVM=true', command)

    def test_the_drivers_own_code_starts_past_the_end_of_the_combined_source(self):
        # Otherwise the driver's anchor loop -- which really does run 104,729 times -- has a
        # block inside the first builtin's window and is indistinguishable from what is being
        # looked for.
        padded = JSCDriver('/build/jsc', combined_source_length=140940).driver_source()
        self.assertGreater(padded.index('debug('), 140940)

    def test_the_padding_is_a_comment_so_it_produces_no_basic_blocks(self):
        padded = JSCDriver('/build/jsc', combined_source_length=1000).driver_source()
        self.assertTrue(padded.startswith('/*'))
        self.assertLess(padded.index('*/'), padded.index('debug('))


class NumbersWithNoDenominatorAreNotZeroTest(unittest.TestCase):
    """0 of 0 is not 0%."""

    def test_no_denominator_gives_no_percentage(self):
        self.assertIsNone(percent(0, 0))
        self.assertEqual(format_percent(None), '-')

    def test_a_denominator_gives_two_places(self):
        self.assertEqual(format_percent(percent(1, 3)), '33.33%')


class UncoveredLinesReadAsRangesTest(unittest.TestCase):
    """A list of forty line numbers is not something anybody reads."""

    def test_consecutive_lines_collapse(self):
        self.assertEqual(format_line_numbers([16576, 16585, 16605, 16621, 16622]),
                         '16576, 16585, 16605, 16621-16622')

    def test_a_single_line_is_not_written_as_a_range(self):
        self.assertEqual(format_line_numbers([9]), '9')

    def test_an_unbounded_list_is_truncated_and_says_so(self):
        rendered = format_line_numbers(range(0, 100, 2), limit=3)
        self.assertTrue(rendered.endswith('(50 runs)'))


class DirectoriesExpandInSortedOrderTest(unittest.TestCase):
    """A run has to be reproducible, and a resumed one has to line up with the first."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='coverage-javascript-test-')
        os.makedirs(os.path.join(self.root, 'sub'))
        for name in ('b.js', 'a.js', 'notes.txt'):
            open(os.path.join(self.root, name), 'w').close()
        open(os.path.join(self.root, 'sub', 'c.js'), 'w').close()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_only_js_files_and_in_order(self):
        found = [os.path.relpath(path, self.root) for path in expand_test_paths([self.root])]
        self.assertEqual(found, ['a.js', 'b.js', os.path.join('sub', 'c.js')])

    def test_a_named_file_is_taken_as_given(self):
        self.assertEqual(expand_test_paths(['/nowhere/x.js']), ['/nowhere/x.js'])


if __name__ == '__main__':
    unittest.main()

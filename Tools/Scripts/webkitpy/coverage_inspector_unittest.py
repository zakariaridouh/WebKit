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

"""Tests for coverage_inspector, over a payload captured from a real run.

No build is needed. _CAPTURED_PAYLOAD below is a verbatim Runtime.getBasicBlocks result and its
Debugger.scriptParsed event, taken from one WebKitTestRunner run of
generate-inspector-js-coverage over the page in _PAGE. That page was written to be checkable by
eye: `ran` is called with 1 so its early return is taken and its second return is not,
`neverCalled` is declared and never called, and `arrow` is called once. Every expected line
number in these tests was read off that page, and the offsets are the ones the profiler really
produced -- including the two shapes that caused bugs, an inverted range `[1, 0]` and an
enclosing range `[1, 89]`.
"""

import json
import os
import shutil
import tempfile
import unittest

from webkitpy.coverage_inspector import (
    CHUNK_PREFIX, COVERAGE_BEGIN, COVERAGE_END, RESOURCE_ROOTS, TARGET_FRONTEND, TARGET_PAGE,
    BasicBlock, InspectorCoverageReport, PayloadError, ScriptCoverage, WebKitTestRunnerDriver,
    drop_enclosing_ranges, driver_source, extract_payload, format_report, function_records,
    resolve_path, scripts_from_payload, verify_path_against_text)

# The page the payload was captured from. Line 1 is <!DOCTYPE html>, line 2 opens the <script>,
# and the script's own text therefore starts on line 2 and is reported with startLine 1.
#
#   1  <!DOCTYPE html>
#   2  <html><body><script>
#   3  function ran(value)
#   4  {
#   5      if (value > 0)
#   6          return "positive";
#   7      return "other";
#   8  }
#   9
#  10  function neverCalled(value)
#  11  {
#  12      return value * 2;
#  13  }
#  14
#  15  var arrow = () => ran(1);
#  16  window.result = arrow();
_PAGE = ('\nfunction ran(value)\n{\n    if (value > 0)\n        return "positive";\n'
         '    return "other";\n}\n\nfunction neverCalled(value)\n{\n    return value * 2;\n}\n\n'
         'var arrow = () => ran(1);\nwindow.result = arrow();\n')

_CAPTURED_PAYLOAD = {
    'target': 'page',
    'page': 'file:///tmp/tiny.html',
    'scripts': [{
        'scriptId': '27',
        'url': 'file:///tmp/tiny.html',
        'startLine': 1,
        'startColumn': 20,
        'endLine': 16,
        'endColumn': 0,
        'executionContextId': 3,
        'isContentScript': False,
        'scriptType': 'program',
    }],
    'sources': [{
        'sourceID': '27',
        'source': _PAGE,
        'basicBlocks': [
            {'startOffset': 88, 'endOffset': 89, 'hasExecuted': False, 'executionCount': 0},
            {'startOffset': 170, 'endOffset': 170, 'hasExecuted': False, 'executionCount': 0},
            {'startOffset': 50, 'endOffset': 67, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 1, 'endOffset': 0, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 90, 'endOffset': 91, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 145, 'endOffset': 158, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 171, 'endOffset': 197, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 159, 'endOffset': 170, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 68, 'endOffset': 87, 'hasExecuted': False, 'executionCount': 0},
            {'startOffset': 13, 'endOffset': 49, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 0, 'endOffset': 197, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 1, 'endOffset': 89, 'hasExecuted': True, 'executionCount': 1},
            {'startOffset': 92, 'endOffset': 144, 'hasExecuted': False, 'executionCount': 0},
            {'startOffset': 159, 'endOffset': 170, 'hasExecuted': True, 'executionCount': 1},
        ],
    }],
}


def _blocks():
    return [BasicBlock(entry['startOffset'], entry['endOffset'], entry['hasExecuted'],
                       entry['executionCount'])
            for entry in _CAPTURED_PAYLOAD['sources'][0]['basicBlocks']]


def _captured_coverage():
    return scripts_from_payload(json.loads(json.dumps(_CAPTURED_PAYLOAD)), '/nowhere')[0]


def _log_for(payload):
    """A WebKitTestRunner log carrying `payload`, chunked the way the driver chunks it."""
    text = json.dumps(payload)
    lines = ['com.apple.WebKit.GPU.Development(1) MallocStackLogging: noise', COVERAGE_BEGIN]
    for at in range(0, len(text), 64):
        lines.append(CHUNK_PREFIX + text[at:at + 64])
    lines.extend([COVERAGE_END, '#EOF', 'CONSOLE MESSAGE: something the front end complained about'])
    return '\n'.join(lines) + '\n'


class ThePayloadSurvivesEverythingElseOnTheStreamTest(unittest.TestCase):
    def test_a_chunked_payload_is_reassembled(self):
        self.assertEqual(extract_payload(_log_for(_CAPTURED_PAYLOAD))['target'], 'page')

    def test_lines_that_are_not_chunks_are_ignored(self):
        log = _log_for(_CAPTURED_PAYLOAD).replace(
            COVERAGE_BEGIN + '\n', COVERAGE_BEGIN + '\nWebContent: unrelated logging\n')
        self.assertEqual(len(extract_payload(log)['scripts']), 1)

    def test_a_run_that_never_reported_is_an_error_and_not_zero_coverage(self):
        with self.assertRaises(PayloadError):
            extract_payload('WebKitTestRunner crashed before anything ran\n')

    def test_a_truncated_payload_is_an_error_rather_than_a_partial_report(self):
        log = _log_for(_CAPTURED_PAYLOAD).replace(COVERAGE_END, 'gone')
        with self.assertRaises(PayloadError):
            extract_payload(log)

    def test_markers_with_no_chunks_between_them_are_an_error(self):
        with self.assertRaises(PayloadError):
            extract_payload(COVERAGE_BEGIN + '\n' + COVERAGE_END + '\n')

    def test_a_payload_that_is_not_json_names_how_much_it_read(self):
        log = COVERAGE_BEGIN + '\n' + CHUNK_PREFIX + '{"scripts": [' + '\n' + COVERAGE_END + '\n'
        with self.assertRaisesRegex(PayloadError, '1 chunks'):
            extract_payload(log)


class AnEnclosingRangeIsNotEvidenceAboutItsContentsTest(unittest.TestCase):
    """getBasicBlocks returns function extents alongside basic blocks; see the module docstring."""

    def test_a_range_that_strictly_contains_another_is_discarded(self):
        kept = {(block.start, block.end) for block in drop_enclosing_ranges(_blocks())}
        self.assertNotIn((0, 197), kept)      # the whole program
        self.assertNotIn((1, 89), kept)       # the extent of `ran`

    def test_the_extent_of_a_function_nothing_compiled_is_kept(self):
        kept = {(block.start, block.end) for block in drop_enclosing_ranges(_blocks())}
        self.assertIn((92, 144), kept)

    def test_an_empty_range_owns_no_text_and_is_dropped(self):
        kept = {(block.start, block.end) for block in drop_enclosing_ranges(_blocks())}
        self.assertNotIn((1, 0), kept)

    def test_a_range_the_backend_reported_twice_appears_at_most_once(self):
        # The capture really does contain [159, 170] twice: once as a basic block and once as the
        # arrow function's extent, which have the same offsets when the body is one expression.
        kept = drop_enclosing_ranges(_blocks())
        self.assertEqual(len(kept), len({(block.start, block.end) for block in kept}))

    def test_folding_takes_executed_from_either_record(self):
        blocks = [BasicBlock(10, 20, False, 0), BasicBlock(10, 20, True, 7)]
        folded = drop_enclosing_ranges(blocks)
        self.assertEqual(len(folded), 1)
        self.assertTrue(folded[0].has_executed)
        self.assertEqual(folded[0].execution_count, 7)


class TheFunctionListComesFromTheRangesTest(unittest.TestCase):
    def test_every_function_on_the_page_is_found_once(self):
        self.assertEqual([record.name for record in function_records(_PAGE, _blocks())],
                         ['ran', 'neverCalled', 'arrow'])

    def test_the_function_nothing_called_is_the_only_one_not_executed(self):
        executed = {record.name: record.executed
                    for record in function_records(_PAGE, _blocks())}
        self.assertEqual(executed, {'ran': True, 'neverCalled': False, 'arrow': True})

    def test_an_empty_marker_range_inside_an_extent_does_not_make_it_executed(self):
        # The bug this guards: `[1356, 1355]` is a real zero-width program-level marker at the
        # offset a top-level function declaration begins, and it is always hasExecuted. Counted as
        # a range inside the extent, it reported createCodeMirrorTextMarkers as executed in a file
        # the same run correctly reported as 0 of 117 lines covered.
        source = 'function only()\n{\n    return 1;\n}\n'
        blocks = [BasicBlock(0, 32, False, 0), BasicBlock(0, -1, True, 1)]
        self.assertEqual([record.executed for record in function_records(source, blocks)], [False])

    def test_a_control_flow_keyword_is_not_a_function(self):
        source = 'if (value) {\n    return 1;\n}\n'
        self.assertEqual(function_records(source, [BasicBlock(0, 27, True, 1)]), [])

    def test_a_functions_own_first_block_starts_at_the_parameters_and_is_not_a_function(self):
        # `(controller) {` is where a method's first basic block begins. Matching it would report
        # every method twice.
        source = '(controller) {\n    controller.close();\n}'
        self.assertEqual(function_records(source, [BasicBlock(0, 38, True, 1)]), [])

    def test_an_anonymous_function_takes_the_name_it_was_assigned_to(self):
        source = 'WI.roleSelectorForNode = function(node)\n{\n    return "";\n}\n'
        records = function_records(source, [BasicBlock(25, 56, False, 0)])
        self.assertEqual([record.name for record in records], ['WI.roleSelectorForNode'])

    def test_a_function_with_no_name_anywhere_is_reported_as_anonymous(self):
        source = '(function ()\n{\n    return 1;\n})'
        records = function_records(source, [BasicBlock(1, 28, False, 0)])
        self.assertEqual([record.name for record in records], ['(anonymous)'])


class LinesAreTheFilesOwnAndTheDenominatorIsStatementsTest(unittest.TestCase):
    def test_an_inline_script_reports_the_documents_line_numbers(self):
        # startLine 1 is zero-based, so the script's first line is file line 2 and `function ran`
        # is on file line 3.
        records = _captured_coverage().functions()
        self.assertEqual([(record.name, record.line) for record in records],
                         [('ran', 3), ('neverCalled', 10), ('arrow', 15)])

    def test_braces_and_blank_lines_are_not_in_the_denominator(self):
        code_lines, _, _ = _captured_coverage().coverage()
        self.assertEqual(code_lines, (3, 5, 6, 7, 10, 12, 15, 16))

    def test_the_branch_that_was_not_taken_is_the_uncovered_line(self):
        # `ran(1)` returns at line 6, so line 7 never runs; neverCalled is lines 10 and 12.
        _, _, uncovered = _captured_coverage().coverage()
        self.assertEqual(uncovered, (7, 10, 12))

    def test_a_line_reached_only_by_an_enclosing_range_is_not_covered(self):
        # [1, 89] is the extent of `ran` and it executed. Without dropping it, line 7 -- the
        # return that was never reached -- would read as covered.
        _, covered, _ = _captured_coverage().coverage()
        self.assertEqual(covered, (3, 5, 6, 15, 16))

    def test_a_source_whose_text_was_not_fetched_reports_nothing_rather_than_zero(self):
        coverage = ScriptCoverage('9', url='user-script:1', blocks=_blocks())
        self.assertEqual(coverage.coverage(), ((), (), ()))
        self.assertEqual(coverage.functions(), [])


class APathIsAClaimThatGetsCheckedTest(unittest.TestCase):
    def setUp(self):
        self.checkout = tempfile.mkdtemp(prefix='coverage-inspector-test-')
        self.relative = os.path.join('Source', 'WebInspectorUI', 'UserInterface', 'Base',
                                     'Main.js')
        os.makedirs(os.path.dirname(os.path.join(self.checkout, self.relative)))
        with open(os.path.join(self.checkout, self.relative), 'w') as handle:
            handle.write('function boot()\n{\n    return 1;\n}\n')

    def tearDown(self):
        shutil.rmtree(self.checkout, ignore_errors=True)

    def test_a_build_resource_is_mapped_back_to_the_source_it_was_copied_from(self):
        url = ('file:///build/WebInspectorUI.framework/Resources/Base/Main.js')
        self.assertEqual(resolve_path(url, self.checkout), self.relative)

    def test_the_versioned_bundle_layout_maps_the_same_way(self):
        url = 'file:///build/WebInspectorUI.framework/Versions/A/Resources/Base/Main.js'
        self.assertEqual(resolve_path(url, self.checkout), self.relative)

    def test_a_file_inside_the_checkout_maps_to_its_relative_path(self):
        url = 'file://' + os.path.join(self.checkout, 'LayoutTests', 'resources', 'js-test.js')
        self.assertEqual(resolve_path(url, self.checkout),
                         os.path.join('LayoutTests', 'resources', 'js-test.js'))

    def test_a_url_that_is_not_a_file_does_not_map(self):
        self.assertIsNone(resolve_path('user-script:1', self.checkout))
        self.assertIsNone(resolve_path('', self.checkout))
        self.assertIsNone(resolve_path('file:///elsewhere/thing.js', self.checkout))

    def test_the_fetched_text_is_compared_exactly_when_it_is_available(self):
        verified, text = verify_path_against_text(
            self.checkout, self.relative, source='function boot()\n{\n    return 1;\n}\n')
        self.assertTrue(verified)
        self.assertEqual(text, 'function boot()\n{\n    return 1;\n}\n')
        self.assertFalse(verify_path_against_text(self.checkout, self.relative,
                                                  source='something else')[0])

    def test_without_the_text_the_shape_scriptParsed_reported_is_checked(self):
        # Debugger.cpp derives endLine from the line count and endColumn from the last line's
        # length, so those two numbers pin the file's size where it matters.
        self.assertTrue(verify_path_against_text(self.checkout, self.relative, end_line=4,
                                                 end_column=0)[0])
        self.assertFalse(verify_path_against_text(self.checkout, self.relative, end_line=9,
                                                  end_column=0)[0])

    def test_a_missing_file_does_not_verify_and_does_not_raise(self):
        self.assertEqual(verify_path_against_text(self.checkout, 'Source/Gone.js'), (False, None))

    def test_an_inline_script_is_never_placed_by_shape_alone(self):
        # An inline <script>'s URL is the enclosing document, so endLine and endColumn describe the
        # script while the file on disk is HTML. Comparing them would be comparing a fragment's
        # shape against a document's.
        self.assertFalse(verify_path_against_text(self.checkout, self.relative, end_line=4,
                                                  end_column=0, whole_file=False)[0])
        payload = json.loads(json.dumps(_CAPTURED_PAYLOAD))
        payload['scripts'][0]['url'] = 'file://' + os.path.join(self.checkout, self.relative)
        del payload['sources'][0]['source']
        self.assertIsNone(scripts_from_payload(payload, self.checkout)[0].path)

    def test_a_path_that_does_not_verify_is_reported_as_a_url_and_not_as_a_file(self):
        payload = json.loads(json.dumps(_CAPTURED_PAYLOAD))
        url = 'file://' + os.path.join(self.checkout, self.relative)
        payload['scripts'][0]['url'] = url
        payload['sources'][0]['source'] = 'not what is on disk\n'
        coverage = scripts_from_payload(payload, self.checkout)[0]
        self.assertIsNone(coverage.path)
        self.assertFalse(coverage.path_verified)
        self.assertEqual(coverage.label, url)


class TheReportLeadsWithWhatNeverRanTest(unittest.TestCase):
    def setUp(self):
        self.report = InspectorCoverageReport([_captured_coverage()], target=TARGET_PAGE,
                                              page='file:///tmp/tiny.html')

    def test_the_never_executed_function_is_named_with_its_line(self):
        named = [(record.name, record.line) for _, record in self.report.never_executed()]
        self.assertEqual(named, [('neverCalled', 10)])

    def test_the_totals_are_the_sum_over_the_sources(self):
        self.assertEqual((self.report.functions_executed, self.report.functions_total), (2, 3))
        self.assertEqual((self.report.lines_covered, self.report.lines_total), (5, 8))

    def test_a_source_with_no_url_is_counted_as_unresolved_rather_than_dropped(self):
        report = InspectorCoverageReport(
            [_captured_coverage(),
             ScriptCoverage('44', url='', source='var x = 1;\n',
                            blocks=[BasicBlock(0, 10, True, 1)])],
            target=TARGET_PAGE)
        self.assertEqual(report.files_resolved, 0)
        self.assertEqual(len(report.to_json()['unresolved']), 2)

    def test_the_text_report_names_the_uncovered_function_and_the_file(self):
        lines = '\n'.join(format_report(self.report))
        self.assertIn('neverCalled', lines)
        self.assertIn('1 functions were never executed', lines)
        self.assertIn('file:///tmp/tiny.html', lines)

    def test_the_json_carries_the_uncovered_lines_a_line_view_would_render(self):
        document = self.report.to_json()
        self.assertEqual(document['schema'], 'webkit-inspector-javascript-coverage-1')
        self.assertEqual(document['files'][0]['uncovered_lines'], [7, 10, 12])
        self.assertEqual(document['files'][0]['never_executed_functions'],
                         [{'name': 'neverCalled', 'line': 10}])


class TheDriverPageCarriesEverythingItNeedsTest(unittest.TestCase):
    """frontendMain is sent to the stub page as a string and evaluated there.

    None of the driver page's constants exist in that scope, which is not a theory: the first
    version referred to COVERAGE_BEGIN directly and every run failed with
    `ReferenceError: Can't find variable: COVERAGE_BEGIN`.
    """

    def test_the_markers_travel_inside_the_options_object(self):
        source = driver_source(target=TARGET_PAGE, page='about:blank', stub_url='file:///stub')
        self.assertIn('begin: COVERAGE_BEGIN', source)
        self.assertIn('log(options.begin)', source)
        self.assertNotIn('log(COVERAGE_BEGIN)', source)

    def test_the_page_target_reloads_the_workload_frame_and_not_the_driver(self):
        # Reloading the driver page would destroy the Internals object that owns the stub frontend.
        source = driver_source(target=TARGET_PAGE, page='file:///workload.html',
                               stub_url='file:///stub')
        self.assertIn("getElementById('workload').contentWindow.location.reload()", source)
        self.assertIn('src="file:///workload.html"', source)

    def test_the_frontend_target_opens_the_front_end_and_then_inspects_it(self):
        source = driver_source(target=TARGET_FRONTEND, page='about:blank',
                               frontend_url='file:///Main.html', stub_url='file:///stub')
        self.assertIn('internals.openDummyInspectorFrontend(FRONTEND_URL)', source)
        self.assertIn('attachStub(frontend, options)', source)
        self.assertIn('window.opener.__coverageDone(', source)

    def test_an_unknown_target_is_refused_rather_than_generating_a_page_that_does_nothing(self):
        with self.assertRaises(ValueError):
            driver_source(target='both', stub_url='file:///stub')


class TheDriverNeverWritesToTheSharedProfileDirectoryTest(unittest.TestCase):
    """/private/tmp/WebKitCoverage is baked into every binary and is machine-global.

    Setting only the unprefixed names is not enough, and the failure is silent: the Network, GPU
    and WebContent processes are XPC services and read the __XPC_ copies. One run with
    LLVM_PROFILE_FILE set and __XPC_LLVM_PROFILE_FILE unset left 20 .profraw files, 400 MB, in the
    shared directory.
    """

    def setUp(self):
        self.driver = WebKitTestRunnerDriver('/build/WebKitTestRunner', '/build')

    def test_both_copies_of_the_profile_path_are_set(self):
        environment = self.driver.environment(base={})
        self.assertEqual(environment['LLVM_PROFILE_FILE'], os.devnull)
        self.assertEqual(environment['__XPC_LLVM_PROFILE_FILE'], os.devnull)

    def test_a_caller_that_wants_profiles_still_gets_both_copies(self):
        driver = WebKitTestRunnerDriver('/build/WebKitTestRunner', '/build',
                                        profile_destination='/tmp/mine/%p.profraw')
        environment = driver.environment(base={})
        self.assertEqual(environment['__XPC_LLVM_PROFILE_FILE'], '/tmp/mine/%p.profraw')

    def test_both_copies_of_the_framework_path_point_at_the_build(self):
        environment = self.driver.environment(base={})
        for name in ('DYLD_FRAMEWORK_PATH', 'DYLD_LIBRARY_PATH'):
            self.assertEqual(environment[name], '/build')
            self.assertEqual(environment['__XPC_' + name], '/build')

    def test_an_existing_framework_path_is_kept_behind_the_build(self):
        environment = self.driver.environment(base={'DYLD_FRAMEWORK_PATH': '/elsewhere'})
        self.assertEqual(environment['DYLD_FRAMEWORK_PATH'], '/build:/elsewhere')

    def test_the_command_asks_for_no_timeout_because_the_front_end_boot_is_slow(self):
        self.assertEqual(self.driver.command('/tmp/driver.html'),
                         ['/build/WebKitTestRunner', '--no-timeout', '/tmp/driver.html'])


class ResourceRootsAreDeclaredNotGuessedTest(unittest.TestCase):
    def test_every_resource_root_maps_a_build_path_to_a_source_path(self):
        for build_relative, source_relative in RESOURCE_ROOTS:
            self.assertIn('framework', build_relative.lower())
            self.assertTrue(source_relative.startswith('Source' + os.sep))


if __name__ == '__main__':
    unittest.main()

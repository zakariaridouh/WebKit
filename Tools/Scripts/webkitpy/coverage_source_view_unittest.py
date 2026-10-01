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

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from webkitpy.coverage_lcov import FileCoverage
from webkitpy.coverage_provenance import PROVENANCE_FILENAME
from webkitpy.coverage_report_assets import relative_link, search_box_html, source_page_path
from webkitpy.coverage_report_theme import (
    THEME_CSS, THEME_SCRIPT_NAME, THEME_TOGGLE_HTML, THEME_TOKENS)
from webkitpy.coverage_source_view import (
    CHUNK_LINES, COVERED, FOLD_MIN_LINES, NOT_INSTRUMENTED, OUTSIDE_SOURCE_ROOT_DIRECTORY,
    PARTIALLY_TAKEN, RECORDS_PAST_END_OF_FILE, ROW_CLASS, ROW_HEIGHT_PX, SCRIPT_NAME, SOURCE_VIEW_CORE_SCRIPT, SOURCE_VIEW_SCRIPT, SOURCE_VIEW_STYLE,
    STYLESHEET_NAME, UNCOVERED, UNREADABLE_SOURCE, _format_count, branch_counts,
    built_at_from_provenance, drop_unconfined, fitting_source, function_summaries, is_confined,
    line_state, partial_branch_lines, relative_source_path, render_source_view, write_source_views)

_ROW = re.compile(r'\n<li id=L(\d+)( class=(?:"[^"]*"|[^ >"]+))?(?: style="[^"]*")?>'
                  r'(?:<i[^>]*>[^<]*</i>)?(<b>[^<]*</b>)?')


def _rows(page):
    """[(line number, row class or '', count element's text or None)] in document order."""
    return [(int(number), (kind or '').strip(), count[3:-4] if count else None)
            for number, kind, count in _ROW.findall(page)]


def _row(page, number):
    """One row's markup, from its start tag to the next row or the end of its chunk."""
    start = page.index('\n<li id=L%d' % number)
    ends = [end for end in (page.find('\n<li', start + 1), page.find('</ol>', start)) if end != -1]
    return page[start:min(ends)]


class FormatCountTest(unittest.TestCase):
    def test_matches_llvm_covs_table(self):
        # llvm-cov's formatCount, reproduced so pages compare cell by cell.
        for count, expected in ((0, '0'), (1, '1'), (99, '99'), (999, '999'),
                                (1000, '1.00k'), (2534, '2.53k'), (9999, '9.99k'),
                                (10000, '10.0k'), (100000, '100k'), (999999, '999k'),
                                (1000000, '1.00M'), (40912345, '40.9M'),
                                (10 ** 9, '1.00G'), (10 ** 12, '1.00T')):
            self.assertEqual(_format_count(count), expected, 'formatCount({})'.format(count))

    def test_three_significant_digits_and_no_rounding(self):
        # 40.91M would be four significant digits, and llvm-cov truncates rather than rounds.
        self.assertEqual(_format_count(40912345), '40.9M')
        self.assertEqual(_format_count(40999999), '40.9M')


class _Tree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.output, ignore_errors=True)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        # WARNING stays on because one test collects those records; only keep it off the console. A NullHandler as well
        # as propagate=False: with no handler at all, logging falls back to lastResort and prints to stderr.
        view_logger = logging.getLogger('webkitpy.coverage_source_view')
        self.addCleanup(setattr, view_logger, 'propagate', view_logger.propagate)
        view_logger.propagate = False
        silence = logging.NullHandler()
        view_logger.addHandler(silence)
        self.addCleanup(view_logger.removeHandler, silence)

    def write(self, relative, contents):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path

    def coverage(self, lines=None, branches=None, function_lines=None):
        entry = FileCoverage()
        entry.lines = dict(lines or {})
        entry.branches = dict(branches or {})
        entry.function_lines = dict(function_lines or {})
        return entry

    def render(self, source_relative, contents, lines=None, branches=None, workers=1,
               build_directory=None, built_at=None, function_lines=None, regions=None):
        path = self.write(source_relative, contents)
        result = write_source_views({path: self.coverage(lines, branches, function_lines)},
                                    self.output, self.root, workers=workers,
                                    build_directory=build_directory, built_at=built_at,
                                    regions={path: regions} if regions is not None else None)
        return result, path

    def page(self, relative):
        with open(os.path.join(self.output, relative + '.html')) as handle:
            return handle.read()


class LineCountTest(_Tree):
    THREE_LINES = 'int one();\nint two();\nint three();'

    def test_a_file_with_a_trailing_newline_renders_one_row_per_line(self):
        self.render('a.cpp', self.THREE_LINES + '\n')
        self.assertEqual([number for number, _, _ in _rows(self.page('a.cpp'))], [1, 2, 3])

    def test_a_file_without_a_trailing_newline_renders_the_same_rows(self):
        self.render('a.cpp', self.THREE_LINES)
        self.assertEqual([number for number, _, _ in _rows(self.page('a.cpp'))], [1, 2, 3])

    def test_an_empty_file_renders_no_rows(self):
        self.render('a.cpp', '')
        self.assertEqual(_rows(self.page('a.cpp')), [])

    def test_blank_lines_in_the_middle_are_kept(self):
        self.render('a.cpp', 'one\n\n\nfour\n')
        self.assertEqual([number for number, _, _ in _rows(self.page('a.cpp'))], [1, 2, 3, 4])

    def test_a_form_feed_is_not_a_line_break(self):
        # XPathGrammar.cpp has form feeds: str.splitlines() splits on them and llvm-cov does not.
        self.render('a.cpp', 'one\ntwo\x0cstill two\nthree\n')
        rows = _rows(self.page('a.cpp'))
        self.assertEqual([number for number, _, _ in rows], [1, 2, 3])
        self.assertIn('two\x0cstill two', self.page('a.cpp'))

    def test_a_crlf_file_has_no_extra_lines(self):
        self.render('a.cpp', 'one\r\ntwo\r\n')
        self.assertEqual([number for number, _, _ in _rows(self.page('a.cpp'))], [1, 2])


class LineStateTest(_Tree):
    def setUp(self):
        super(LineStateTest, self).setUp()
        self.render('a.cpp', 'not instrumented\nnever executed\nexecuted twice\n',
                    lines={2: 0, 3: 2534})
        self.rows = dict((number, (kind, count)) for number, kind, count in _rows(self.page('a.cpp')))

    def test_a_line_with_no_coverage_data_has_no_count_element_and_no_class(self):
        self.assertEqual(self.rows[1], ('', None))
        self.assertEqual(_row(self.page('a.cpp'), 1), '\n<li id=L1>not instrumented')

    def test_a_line_that_never_executed_is_marked_uncovered(self):
        # Its count is always 0, so the stylesheet draws it, with the marker, from the class.
        self.assertEqual(self.rows[2], ('class=u', None))
        self.assertIn('li.u::after {\n  content: "0 !";', SOURCE_VIEW_STYLE)

    def test_an_executed_line_is_neither_marked_nor_empty(self):
        self.assertEqual(self.rows[3], ('', '2.53k'))


class LineStateNamingTest(unittest.TestCase):
    """The states are named so another can be added without rewriting the row loop."""

    def test_every_state_has_markup(self):
        # So that adding a state without deciding how it renders fails here.
        self.assertEqual(set(ROW_CLASS),
                         {NOT_INSTRUMENTED, UNCOVERED, COVERED, PARTIALLY_TAKEN})

    def test_no_record_at_all_is_not_the_same_as_a_record_of_zero(self):
        self.assertEqual(line_state(None, None), NOT_INSTRUMENTED)
        self.assertEqual(line_state(0, None), UNCOVERED)

    def test_a_covered_line_with_an_untaken_branch_is_its_own_state(self):
        self.assertEqual(line_state(7, None), COVERED)
        self.assertEqual(line_state(7, (1, 2)), PARTIALLY_TAKEN)

    def test_only_the_two_marked_states_carry_a_class(self):
        self.assertEqual([state for state, css in ROW_CLASS.items() if css],
                         [UNCOVERED, PARTIALLY_TAKEN])


class EscapingTest(_Tree):
    def test_markup_characters_in_the_source_are_escaped(self):
        self.render('a.cpp', 'if (a < b && c > d) f<int>();\n', lines={1: 1})
        page = self.page('a.cpp')
        self.assertIn('if (a &lt; b &amp;&amp; c &gt; d) f&lt;int&gt;();', page)
        self.assertNotIn('f<int>', page)

    def test_quotes_are_left_alone_because_the_source_is_text_not_an_attribute(self):
        self.render('a.cpp', 'const char* s = "quoted";\n', lines={1: 1})
        page = self.page('a.cpp')
        self.assertIn('const char* s = "quoted";', page)
        self.assertNotIn('&quot;', page)


class BranchAnnotationTest(_Tree):
    def test_an_executed_line_with_an_untaken_branch_is_marked_partial(self):
        # Branch data llvm-cov show does not display; the nearest lcov has to sub-line regions.
        self.render('a.cpp', 'if (a && b) f();\n', lines={1: 7},
                    branches={('1', '0', '0'): 7, ('1', '0', '1'): 0,
                              ('1', '0', '2'): 4, ('1', '0', '3'): 0})
        page = self.page('a.cpp')
        self.assertEqual(_rows(page), [(1, 'class=p', '7')])
        self.assertIn('>2/4</i>', page)

    def test_a_line_whose_branches_were_all_taken_is_not_marked(self):
        self.render('a.cpp', 'if (a) f();\n', lines={1: 7},
                    branches={('1', '0', '0'): 7, ('1', '0', '1'): 2})
        self.assertEqual(_rows(self.page('a.cpp')), [(1, '', '7')])
        self.assertNotIn('<i', _row(self.page('a.cpp'), 1))

    def test_an_uncovered_line_is_still_marked_uncovered_not_partial(self):
        self.render('a.cpp', 'if (a) f();\n', lines={1: 0},
                    branches={('1', '0', '0'): 0, ('1', '0', '1'): 0})
        self.assertEqual(_rows(self.page('a.cpp')), [(1, 'class=u', None)])

    def test_partial_branch_lines_counts_taken_over_total(self):
        self.assertEqual(partial_branch_lines({('12', '0', '0'): 3, ('12', '0', '1'): 0,
                                               ('20', '0', '0'): 1, ('20', '0', '1'): 1}),
                         {12: (1, 2)})

    def test_partial_branch_lines_ignores_an_unparsable_line_number(self):
        self.assertEqual(partial_branch_lines({('not a number', '0', '0'): 0}), {})
        self.assertEqual(partial_branch_lines({('not a number', '0', '0'): 0}, conditions=True),
                         {})


class BranchConditionTest(_Tree):
    """Each condition's true and false counts. llvm-cov's lcov export writes one BRDA per direction, branch ids in pairs (even true, odd false)."""

    # Line 2: fully taken. Line 3: condition 0 never false, condition 1 never true.
    BRANCHES = {('2', '0', '0'): 3, ('2', '0', '1'): 1,
                ('3', '0', '0'): 3, ('3', '0', '1'): 0, ('3', '1', '2'): 0, ('3', '1', '3'): 5}

    def test_the_conditions_come_in_true_false_pairs(self):
        self.assertEqual(partial_branch_lines(self.BRANCHES, conditions=True),
                         {3: (2, 4, ((3, 0), (0, 5)))})

    def test_without_conditions_the_shape_is_unchanged(self):
        self.assertEqual(partial_branch_lines(self.BRANCHES), {3: (2, 4)})

    def test_conditions_are_in_llvm_covs_order_not_the_dictionarys(self):
        branches = {('3', '1', '3'): 5, ('3', '1', '2'): 0, ('3', '0', '1'): 0, ('3', '0', '0'): 3,
                    ('3', '10', '20'): 1, ('3', '10', '21'): 0}
        self.assertEqual(partial_branch_lines(branches, conditions=True)[3][2],
                         ((3, 0), (0, 5), (1, 0)))

    def test_a_direction_with_no_record_is_none(self):
        self.assertEqual(partial_branch_lines({('4', '0', '0'): 2, ('4', '1', '3'): 0},
                                              conditions=True),
                         {4: (1, 2, ((2, None), (None, 0)))})

    def test_a_line_with_no_branch_taken_carries_no_pairs(self):
        # It did not run, or every pair on it would read 0/0.
        self.assertEqual(partial_branch_lines({('5', '0', '0'): 0, ('5', '0', '1'): 0},
                                              conditions=True), {5: (0, 2, ())})

    def test_the_figure_sits_in_the_gutter_before_the_count_and_the_code(self):
        # In the gutter, since after the code it is off-screen for a long line.
        self.render('a.cpp', 'one\ntwo\nif (a && b)\n', lines={3: 2534},
                    branches=self.BRANCHES)
        self.assertEqual(_row(self.page('a.cpp'), 3),
                         '\n<li id=L3 class=p><i data-b="3 0,0 5">2/4</i><b>2.53k</b>'
                         'if (a &amp;&amp; b)')

    def test_counts_in_the_detail_are_formatted_like_the_line_counts(self):
        self.render('a.cpp', 'if (a)\n', lines={1: 40912345},
                    branches={('1', '0', '0'): 40912345, ('1', '0', '1'): 0})
        self.assertIn('<i data-b="40.9M 0">1/2</i>', self.page('a.cpp'))

    def test_a_partial_line_without_detail_still_shows_its_figure(self):
        # A caller that passes partial_branch_lines() without conditions.
        page = render_source_view('a.cpp', {1: 7}, {1: (1, 2)}, ['if (a)'], '')
        self.assertIn('<li id=L1 class=p><i>1/2</i><b>7</b>if (a)', page)

    def test_the_gutter_is_as_wide_as_the_widest_figure(self):
        page = render_source_view('a.cpp', {1: 7, 2: 1000}, {1: (1, 2), 2: (10, 12)},
                                  ['one', 'two'], '')
        self.assertIn('--bw:6;--cw:5;', page)
        self.assertIn('--bw:0;--cw:0;', render_source_view('a.cpp', {}, {}, ['one'], ''))


class PageLocationTest(_Tree):
    def test_a_page_is_a_sibling_of_its_directory_index_with_relative_stylesheet(self):
        (pages, _, skipped), _ = self.render('Source/WTF/wtf/Vector.h', 'int f();\n', lines={1: 1})
        self.assertEqual((pages, skipped), (1, {}))
        self.assertTrue(os.path.isfile(os.path.join(self.output, 'Source/WTF/wtf/Vector.h.html')))
        page = self.page('Source/WTF/wtf/Vector.h')
        self.assertIn('<link rel=stylesheet href="../../../{}">'.format(STYLESHEET_NAME), page)
        self.assertIn('<a href="../../../index.html">All source</a>', page)
        self.assertIn('<a href="index.html">Source/WTF/wtf</a>', page)

    def test_a_page_at_the_top_of_the_tree_needs_no_hops(self):
        self.render('README.md', 'text\n', lines={1: 1})
        self.assertIn('<link rel=stylesheet href="{}">'.format(STYLESHEET_NAME),
                      self.page('README.md'))

    def test_the_stylesheet_is_written_once_at_the_report_root(self):
        self.render('Source/WTF/wtf/Vector.h', 'int f();\n', lines={1: 1})
        self.assertTrue(os.path.isfile(os.path.join(self.output, STYLESHEET_NAME)))

    def test_the_script_is_written_once_at_the_report_root_and_linked_relatively(self):
        self.render('Source/WTF/wtf/Vector.h', 'int f();\n', lines={1: 1})
        with open(os.path.join(self.output, SCRIPT_NAME)) as handle:
            self.assertEqual(handle.read(), SOURCE_VIEW_SCRIPT.lstrip('\n'))
        self.assertIn('<script defer src="../../../{}"></script>'.format(SCRIPT_NAME),
                      self.page('Source/WTF/wtf/Vector.h'))
        self.assertEqual(sorted(name for name in os.listdir(self.output) if '.' in name),
                         sorted([STYLESHEET_NAME, SCRIPT_NAME, THEME_SCRIPT_NAME]))

    def test_the_stats_bar_is_edged_so_that_its_shape_does_not_depend_on_its_fill(self):
        self.assertRegex(SOURCE_VIEW_STYLE, r'\.bar \{[^}]*outline: 1px solid var\(--cov-outline\)')

    def test_the_theme_script_blocks_in_the_head_so_a_saved_choice_applies_before_paint(self):
        self.render('Source/WTF/wtf/Vector.h', 'int f();\n', lines={1: 1})
        page = self.page('Source/WTF/wtf/Vector.h')
        head = page[:page.index('</head>')]
        self.assertIn('<script src="../../../{}"></script>'.format(THEME_SCRIPT_NAME), head)
        self.assertLess(head.index(THEME_SCRIPT_NAME), head.index(STYLESHEET_NAME))

    def test_relative_source_path_of_a_path_under_the_root(self):
        self.assertEqual(relative_source_path('/a/b/Source/WTF/wtf/Vector.h', '/a/b'),
                         'Source/WTF/wtf/Vector.h')

    def test_relative_source_path_of_a_path_outside_the_root_goes_under_one_node(self):
        # Not spliced in at its absolute depth, and not allowed to move the root (see effective_source_prefix).
        self.assertEqual(relative_source_path('/elsewhere/generated/Foo.cpp', '/a/b'),
                         OUTSIDE_SOURCE_ROOT_DIRECTORY + '/elsewhere/generated/Foo.cpp')

    def test_relative_source_path_does_not_match_a_partial_directory_name(self):
        self.assertEqual(relative_source_path('/a/bc/Foo.cpp', '/a/b'),
                         OUTSIDE_SOURCE_ROOT_DIRECTORY + '/a/bc/Foo.cpp')

    def test_relative_source_path_has_nowhere_to_root_without_a_source_root(self):
        self.assertEqual(relative_source_path('/elsewhere/generated/Foo.cpp', None),
                         'elsewhere/generated/Foo.cpp')

    def test_relative_source_path_is_normalised(self):
        self.assertEqual(relative_source_path('/a/b//Source/./WTF/../Vector.h', '/a/b'), 'Source/Vector.h')
        self.assertEqual(relative_source_path('/a/b/../c/Foo.cpp', '/a/b'),
                         OUTSIDE_SOURCE_ROOT_DIRECTORY + '/a/c/Foo.cpp')

    def test_a_page_is_confined_to_the_report_directory_unless_a_segment_walks_out_of_it(self):
        for path, root in (('/a/b/Source/Vector.h', '/a/b'), ('/elsewhere/Foo.cpp', '/a/b'),
                           ('/elsewhere/Foo.cpp', None), ('/a/b/../../../etc/x.cpp', '/a/b')):
            self.assertTrue(is_confined(relative_source_path(path, root)), (path, root))
        for path, root in (('../x.cpp', '/a/b'), ('Source/../../x.cpp', '/a/b'), ('../x.cpp', None),
                           ('/', None), ('/a/b/x\0.cpp', '/a/b'), ('', '/a/b')):
            self.assertFalse(is_confined(relative_source_path(path, root)), (path, root))

    def test_unconfined_paths_are_dropped_with_one_warning_that_counts_them(self):
        kept = self.coverage({1: 1})
        coverage = {'/a/b/ok.h': kept, '../x.cpp': self.coverage(), 'Source/../../y.cpp': self.coverage()}
        with self.assertLogs('webkitpy.coverage_source_view', 'WARNING') as logs:
            self.assertEqual(drop_unconfined(coverage, '/a/b'), {'/a/b/ok.h': kept})
        self.assertEqual(len(logs.output), 1)
        self.assertIn('2 files have a path that would put their page outside', logs.output[0])

    def test_write_source_views_writes_nothing_outside_the_output_directory(self):
        parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        output = os.path.join(parent, 'report')
        good = self.write('Source/ok.h', 'int f();\n')
        self.write('escape.cpp', 'int f();\n')
        coverage = {good: self.coverage({1: 1}), '../escape.cpp': self.coverage({1: 1}),
                    'Source/../../escape2.cpp': self.coverage({1: 1})}
        with self.assertLogs('webkitpy.coverage_source_view', 'WARNING') as logs:
            pages, _, skipped = write_source_views(coverage, output, self.root, workers=1)
        self.assertEqual(pages, 1)
        self.assertEqual(skipped, {})
        self.assertEqual(len([line for line in logs.output if 'outside the report directory' in line]), 1)
        self.assertEqual(os.listdir(parent), ['report'])

    def test_a_file_called_index_gets_a_page_that_is_not_its_directorys(self):
        self.render('Source/index', 'int f();\n', lines={1: 1})
        self.assertTrue(os.path.isfile(os.path.join(self.output, 'Source/index.html.html')))
        self.assertFalse(os.path.exists(os.path.join(self.output, 'Source/index.html')))
        self.assertIn('<title>Source/index</title>', self.page('Source/index.html'))

    def test_the_page_name_is_one_to_one_and_never_a_directory_page(self):
        names = ['index', 'index.html', 'index.html.html', 'a/index', 'a/index.html', 'a', 'a.html',
                 'indexx', 'x.index', 'index.h', 'a/b/index.html.html']
        pages = [source_page_path(name) for name in names]
        self.assertEqual(len(set(pages)), len(pages))
        for page in pages:
            self.assertNotEqual(page.rpartition('/')[2], 'index.html')
        self.assertEqual(source_page_path('Source/a.cpp'), 'Source/a.cpp.html')
        self.assertEqual(source_page_path('Source/index'), 'Source/index.html.html')

    def test_a_link_is_encoded_a_segment_at_a_time_and_cannot_parse_as_a_scheme(self):
        self.assertEqual(relative_link('Source/a b/c#d?e%f.cpp.html'), './Source/a%20b/c%23d%3Fe%25f.cpp.html')
        self.assertEqual(relative_link('javascript:alert(1).html'), './javascript%3Aalert%281%29.html')
        self.assertEqual(relative_link('na\u00efve.html'), './na%C3%AFve.html')

    def test_a_path_outside_the_source_root_still_gets_a_page(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        path = os.path.join(outside, 'generated', 'Foo.cpp')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as handle:
            handle.write('int f();\n')
        pages, written, skipped = write_source_views(
            {path: self.coverage({1: 1})}, self.output, self.root, workers=1)
        self.assertEqual((pages, skipped), (1, {}))
        self.assertTrue(written > 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.output, OUTSIDE_SOURCE_ROOT_DIRECTORY, path.lstrip('/') + '.html')))


class UnreadableSourceTest(_Tree):
    def test_a_missing_source_is_returned_as_skipped_rather_than_raising(self):
        missing = os.path.join(self.root, 'Source/WTF/wtf/Gone.h')
        pages, written, skipped = write_source_views(
            {missing: self.coverage({1: 1})}, self.output, self.root, workers=1)
        self.assertEqual((pages, written, skipped), (0, 0, {missing: UNREADABLE_SOURCE}))
        self.assertFalse(os.path.exists(os.path.join(self.output, 'Source/WTF/wtf/Gone.h.html')))

    def test_a_directory_where_a_file_is_expected_is_skipped_too(self):
        directory = os.path.join(self.root, 'Source/WTF/wtf')
        os.makedirs(directory)
        pages, _, skipped = write_source_views(
            {directory: self.coverage({1: 1})}, self.output, self.root, workers=1)
        self.assertEqual((pages, skipped), (0, {directory: UNREADABLE_SOURCE}))

    def test_the_readable_files_are_still_written(self):
        good = self.write('Source/WTF/wtf/Vector.h', 'int f();\n')
        missing = os.path.join(self.root, 'Source/WTF/wtf/Gone.h')
        pages, _, skipped = write_source_views(
            {good: self.coverage({1: 1}), missing: self.coverage({1: 0})},
            self.output, self.root, workers=1)
        self.assertEqual((pages, skipped), (1, {missing: UNREADABLE_SOURCE}))


class SubtitleTest(_Tree):
    def test_the_page_states_how_many_instrumented_lines_never_executed(self):
        self.render('a.cpp', 'one\ntwo\nthree\n', lines={1: 1, 2: 0, 3: 0})
        self.assertIn('2 of 3 instrumented lines never executed', self.page('a.cpp'))


class FitTest(_Tree):
    """A page must not describe text the coverage records are not about: a record past the end of the file proves the file has changed."""

    def build_copy(self, relative, contents):
        """The build directory's copy of a header, as the copy-headers phase leaves it."""
        build = os.path.join(self.root, 'WebKitBuild', 'Release')
        path = os.path.join(build, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return build

    def test_records_past_the_end_of_the_file_withhold_the_page(self):
        (pages, _, skipped), path = self.render('a.cpp', 'one\ntwo\n', lines={1: 1, 9: 0})
        self.assertEqual((pages, skipped), (0, {path: RECORDS_PAST_END_OF_FILE}))
        self.assertFalse(os.path.exists(os.path.join(self.output, 'a.cpp.html')))

    def test_the_coverage_of_a_withheld_file_is_not_lost_only_its_page(self):
        # The index still counts it; write_directory_index renders it without a link.
        (pages, written, skipped), path = self.render('a.cpp', 'one\n', lines={1: 1, 5: 0})
        self.assertEqual((pages, written), (0, 0))
        self.assertEqual(list(skipped), [path])

    def test_one_line_past_the_end_is_llvm_covs_own_segment_and_still_renders(self):
        # llvm-cov's mapping for a file #included into another translation unit ends one past the last line, so one line over is not drift.
        (pages, _, skipped), _ = self.render('a.def', 'one\ntwo\n', lines={1: 1, 3: 0})
        self.assertEqual((pages, skipped), (1, {}))
        self.assertEqual([number for number, _, _ in _rows(self.page('a.def'))], [1, 2])

    def test_a_record_that_cannot_be_placed_is_named_in_the_subtitle(self):
        # Said on the page, so the subtitle does not quote a count the rows do not match.
        self.render('a.def', 'one\ntwo\n', lines={1: 1, 3: 0})
        page = self.page('a.def')
        self.assertIn('1 of 2 instrumented lines never executed', page)
        self.assertIn('1 record sits past line 2, the end of this text, and is not shown', page)

    def test_two_lines_past_the_end_is_beyond_llvm_covs_segment(self):
        (pages, _, skipped), path = self.render('a.def', 'one\ntwo\n', lines={1: 1, 4: 0})
        self.assertEqual((pages, skipped), (0, {path: RECORDS_PAST_END_OF_FILE}))

    def test_the_build_directorys_copy_is_used_when_the_checkouts_no_longer_fits(self):
        # wtf/Expected.h: rewritten in the checkout, while the text that was compiled remains in the build directory.
        build = self.build_copy('usr/local/include/wtf/Expected.h', 'a\nb\nc\nd\ne\n')
        (pages, _, skipped), _ = self.render('Source/WTF/wtf/Expected.h', 'shrunk\n',
                                             lines={1: 1, 5: 0}, build_directory=build)
        self.assertEqual((pages, skipped), (1, {}))
        page = self.page('Source/WTF/wtf/Expected.h')
        self.assertEqual([number for number, _, _ in _rows(page)], [1, 2, 3, 4, 5])
        self.assertIn('Rendered from', page)
        self.assertIn('usr/local/include/wtf/Expected.h', page)

    def test_a_framework_header_copy_is_found_by_basename(self):
        # Generated headers must not resolve onto generator fixtures.
        build = self.build_copy('WebCore.framework/PrivateHeaders/CSSPropertyNames.h',
                                'a\nb\nc\nd\n')
        (pages, _, skipped), _ = self.render('Source/WebCore/css/CSSPropertyNames.h', 'one\n',
                                             lines={1: 1, 4: 0}, build_directory=build)
        self.assertEqual((pages, skipped), (1, {}))

    def test_a_copy_that_does_not_fit_either_withholds_the_page(self):
        build = self.build_copy('usr/local/include/wtf/Expected.h', 'a\nb\n')
        (pages, _, skipped), path = self.render('Source/WTF/wtf/Expected.h', 'shrunk\n',
                                                lines={1: 1, 9: 0}, build_directory=build)
        self.assertEqual((pages, skipped), (0, {path: RECORDS_PAST_END_OF_FILE}))

    def test_the_checkouts_copy_is_preferred_when_it_fits(self):
        # The copy is found by basename, which is no proof, so it is used only when the checkout's text provably does not fit.
        build = self.build_copy('usr/local/include/wtf/Vector.h', 'copy\ncopy\n')
        self.render('Source/WTF/wtf/Vector.h', 'checkout\n', lines={1: 1},
                    build_directory=build)
        page = self.page('Source/WTF/wtf/Vector.h')
        self.assertIn('checkout', page)
        self.assertNotIn('Rendered from', page)

    def test_a_deleted_file_is_rendered_from_the_build_directorys_copy(self):
        build = self.build_copy('usr/local/include/wtf/Gone.h', 'a\nb\n')
        gone = os.path.join(self.root, 'Source/WTF/wtf/Gone.h')
        pages, _, skipped = write_source_views({gone: self.coverage({1: 1, 2: 0})},
                                               self.output, self.root, workers=1,
                                               build_directory=build)
        self.assertEqual((pages, skipped), (1, {}))

    def test_fitting_source_reports_which_of_the_two_reasons_applies(self):
        short = self.write('short.cpp', 'one\n')
        self.assertEqual(fitting_source(short, 9), (None, None, RECORDS_PAST_END_OF_FILE))
        self.assertEqual(fitting_source(os.path.join(self.root, 'absent.cpp'), 1),
                         (None, None, UNREADABLE_SOURCE))
        self.assertEqual(fitting_source(short, 1), (['one'], None, None))


class SourceNewerThanTheBuildTest(_Tree):
    """The direction --check-binary-ids cannot see: a file that grew or moved lines renders every row against the wrong line with no symptom."""

    def warnings(self, built_at):
        """The WARNING messages one render logs. assertNoLogs is Python 3.10 and this is 3.9."""
        messages = []

        class _Collect(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        logger = logging.getLogger('webkitpy.coverage_source_view')
        handler = _Collect(level=logging.WARNING)
        logger.addHandler(handler)
        try:
            result, _ = self.render('a.cpp', 'one\n', lines={1: 1}, built_at=built_at)
        finally:
            logger.removeHandler(handler)
        return result, messages

    def test_a_file_newer_than_the_build_is_counted_and_named(self):
        (pages, _, skipped), messages = self.warnings(0)
        self.assertTrue(any('newer than' in message for message in messages), messages)
        self.assertTrue(any('a.cpp' in message for message in messages), messages)
        # The page is still written: a file can be touched without changing.
        self.assertEqual((pages, skipped), (1, {}))

    def test_a_file_older_than_the_build_says_nothing(self):
        # 2 ** 40 seconds is the year 36812, so nothing in the tree is newer than the build.
        _, messages = self.warnings(2 ** 40)
        self.assertEqual(messages, [])

    def test_no_build_time_means_no_check(self):
        _, messages = self.warnings(None)
        self.assertEqual(messages, [])


class ProvenanceTest(_Tree):
    """built_at comes from the record generate-coverage-report already writes."""

    def write_provenance(self, record):
        with open(os.path.join(self.output, PROVENANCE_FILENAME), 'w') as handle:
            json.dump(record, handle)

    def test_the_newest_object_wins(self):
        self.write_provenance({'objects': [{'modified_at': '2026-08-22T09:22:06Z'},
                                           {'modified_at': '2026-08-22T09:30:40Z'}]})
        expected = datetime.datetime.fromisoformat('2026-08-22T09:30:40+00:00').timestamp()
        self.assertEqual(built_at_from_provenance(self.output), expected)

    def test_no_record_at_all_means_no_check_rather_than_a_failure(self):
        self.assertIsNone(built_at_from_provenance(self.output))

    def test_a_record_with_no_object_times_means_no_check(self):
        self.write_provenance({'objects': [{'path': '/x', 'modified_at': None}]})
        self.assertIsNone(built_at_from_provenance(self.output))

    def test_an_unparsable_record_means_no_check(self):
        with open(os.path.join(self.output, PROVENANCE_FILENAME), 'w') as handle:
            handle.write('not json')
        self.assertIsNone(built_at_from_provenance(self.output))

    def test_a_record_from_a_schema_that_moved_on_means_no_check(self):
        self.write_provenance({'objects': ['a string, not an object state']})
        self.assertIsNone(built_at_from_provenance(self.output))


class MarkupTest(_Tree):
    def test_a_row_is_a_list_item_and_nothing_else(self):
        # No end tags and no line-number text (a CSS counter draws it); no count element when there is no record or it is 0.
        page = render_source_view('a.cpp', {1: 1, 2: 0}, {}, ['int f();', 'int g();', '}'], '')
        body = page[page.index('<main'):]
        self.assertEqual(body[body.index('>') + 1:],
                         '<ol style=--n:3>\n<li id=L1><b>1</b>int f();'
                         '\n<li id=L2 class=u>int g();\n<li id=L3>}</ol></main></body></html>\n')

    def test_the_page_is_a_complete_document(self):
        page = render_source_view('a.cpp', {1: 1}, {}, ['int f();'], '')
        self.assertTrue(page.startswith('<!DOCTYPE html>'))
        self.assertTrue(page.endswith('</ol></main></body></html>\n'))

    def test_an_empty_file_has_no_chunks(self):
        page = render_source_view('a.cpp', {}, {}, [], '')
        self.assertIn('<main style="--lw:1;--bw:0;--cw:0;--w:0"></main>', page)

    def test_the_markup_is_smaller_than_the_table_it_replaced(self):
        # The old table markup per row: `\n<tr id=L36 class=u><td>36<td>0<td>`.
        source = ['    code();'] * 3000
        lines = {number: (number % 7) * 1000 for number in range(1, 3001, 2)}
        page = render_source_view('Source/WebCore/dom/Document.cpp', lines, {}, source, '../../../')
        table = sum(len('\n<tr id=L%d%s><td>%d<td>%s<td>' % (
            number, '' if lines.get(number, 1) else ' class=u', number,
            '' if number not in lines else _format_count(lines[number])))
            for number in range(1, 3001)) + len('    code();') * 3000 + 538
        self.assertLess(len(page), table * 0.8)


class ChunkTest(_Tree):
    """Rows come in <ol> chunks the browser can skip laying out until they are scrolled near."""

    def setUp(self):
        super(ChunkTest, self).setUp()
        self.lines = 2 * CHUNK_LINES + 201
        self.page_text = render_source_view(
            'a.cpp', {1: 1}, {}, ['line %d' % number for number in range(1, self.lines + 1)], '')

    def test_every_chunk_but_a_short_last_one_is_the_size_the_stylesheet_assumes(self):
        main = self.page_text[self.page_text.index('<main'):]
        self.assertEqual(re.findall(r'<ol[^>]*>', main), ['<ol>', '<ol>', '<ol style=--n:201>'])
        self.assertIn('content-visibility: auto; contain-intrinsic-size: none '
                      'calc(var(--n, {}) * {}px);'.format(CHUNK_LINES, ROW_HEIGHT_PX),
                      SOURCE_VIEW_STYLE)

    def test_every_row_is_one_fixed_height(self):
        # Which is what makes a skipped chunk's placeholder exact, so #L123 lands exactly.
        self.assertIn('position: relative; height: {}px;'.format(ROW_HEIGHT_PX), SOURCE_VIEW_STYLE)
        self.assertIn('var ROW = {};'.format(ROW_HEIGHT_PX), SOURCE_VIEW_SCRIPT)
        self.assertIn('var CHUNK = {};'.format(CHUNK_LINES), SOURCE_VIEW_SCRIPT)

    def test_each_chunk_after_the_first_restates_its_first_line_number(self):
        # content-visibility contains style, and style containment scopes counters to the chunk.
        self.assertEqual(re.findall(r'<li id=L(\d+) style="counter-set:ln (\d+)">', self.page_text),
                         [(str(CHUNK_LINES + 1), str(CHUNK_LINES + 1)),
                          (str(2 * CHUNK_LINES + 1), str(2 * CHUNK_LINES + 1))])
        self.assertEqual(self.page_text.count('counter-set'), 2)

    def test_every_line_can_be_linked_to(self):
        self.assertEqual([number for number, _, _ in _rows(self.page_text)],
                         list(range(1, self.lines + 1)))

    def test_a_linked_line_clears_the_sticky_header(self):
        self.assertIn('html { scroll-padding-top:', SOURCE_VIEW_STYLE)
        self.assertIn('scroll-margin-top:', SOURCE_VIEW_STYLE)
        self.assertIn("root.style.scrollPaddingTop = barHeight + 'px';", SOURCE_VIEW_SCRIPT)


class FunctionOutlineTest(_Tree):
    """The outline's data: each function with the lines it owns, up to the next function's start."""

    def test_a_function_owns_the_lines_up_to_the_next_start(self):
        lines = {10: 5, 11: 0, 12: 5, 20: 0, 21: 0, 30: 3}
        self.assertEqual(function_summaries(lines, {10: 5, 20: 0}, 40),
                         [(10, 5, 2, 3), (20, 0, 1, 3)])

    def test_lines_before_the_first_start_belong_to_no_function(self):
        self.assertEqual(function_summaries({1: 9, 5: 0}, {5: 0}, 10), [(5, 0, 0, 1)])

    def test_a_start_past_the_end_of_the_text_is_not_listed(self):
        self.assertEqual(function_summaries({1: 1}, {1: 1, 99: 0}, 5), [(1, 1, 1, 1)])

    def test_a_function_with_no_instrumented_lines_is_still_listed(self):
        self.assertEqual(function_summaries({}, {3: 0}, 5), [(3, 0, 0, 0)])

    def test_no_functions_no_summaries(self):
        self.assertEqual(function_summaries({1: 1}, {}, 5), [])

    def test_the_page_carries_them_compactly(self):
        self.render('a.cpp', 'f()\n{\n  a;\n  b;\n}\ng()\n',
                    lines={1: 0, 3: 0, 4: 0, 6: 2534, 7: 0}, function_lines={1: 0, 6: 2534})
        self.assertIn('data-fns="1:0:0:3;6:2534:1:1"', self.page('a.cpp'))

    def test_a_page_without_function_data_carries_no_attribute(self):
        self.render('a.cpp', 'one\n', lines={1: 1})
        self.assertNotIn('data-fns', self.page('a.cpp'))

    def test_the_old_gap_attribute_is_gone(self):
        self.render('a.cpp', 'f()\n', lines={1: 0}, function_lines={1: 0})
        self.assertNotIn('data-gaps', self.page('a.cpp'))


class BranchCountsTest(unittest.TestCase):
    def test_counts_taken_over_all_branches(self):
        branches = {('1', '0', '0'): 3, ('1', '0', '1'): 0, ('2', '0', '0'): 1}
        self.assertEqual(branch_counts(branches), (2, 3))

    def test_no_branches(self):
        self.assertEqual(branch_counts({}), (0, 0))


class SummaryTest(_Tree):
    def bar(self, **options):
        self.render('a.cpp', 'f()\n{\n  a;\n  b;\n}\n', **options)
        page = self.page('a.cpp')
        return page[page.index('<ul class=stats>'):page.index('</ul>')]

    def test_it_gives_line_function_and_branch_coverage(self):
        stats = self.bar(lines={1: 5, 3: 0, 4: 2, 5: 1}, function_lines={1: 5},
                         branches={('3', '0', '0'): 1, ('3', '0', '1'): 0})
        self.assertIn('<li><b>Lines</b> <span class=pct>75.00%</span> <span>3/4</span>', stats)
        self.assertIn('<li><b>Functions</b> <span class=pct>100.00%</span> <span>1/1</span>', stats)
        self.assertIn('<li><b>Branches</b> <span class=pct>50.00%</span> <span>1/2</span>', stats)
        self.assertIn('<span class=bar style=--v:75.00%></span>', stats)

    def test_a_figure_with_nothing_to_count_is_left_out(self):
        stats = self.bar(lines={1: 5})
        self.assertNotIn('Functions', stats)
        self.assertNotIn('Branches', stats)

    def test_a_file_with_no_instrumented_lines_says_so(self):
        self.assertIn('<li><b>Lines</b> <span>no data</span>', self.bar())

    def test_the_figures_are_the_one_the_index_would_show(self):
        stats = self.bar(lines={number: 1 if number < 3 else 0 for number in range(1, 6)})
        self.assertIn('40.00%', stats)

    def test_thousands_are_grouped(self):
        stats = render_source_view('a.cpp', {n: 1 for n in range(1, 1235)}, {}, ['x'] * 1234, '')
        self.assertIn('<span>1,234/1,234</span>', stats)

    def test_a_copy_rendered_from_the_build_directory_is_flagged_where_it_cannot_scroll_away(self):
        page = render_source_view('a.cpp', {1: 1}, {}, ['one'], '', rendered_from='/b/a.cpp')
        header = page[page.index('<header>'):page.index('</header>')]
        self.assertIn('<li class=warn title="0 of 1 instrumented lines never executed. '
                      'Rendered from /b/a.cpp', header)
        self.assertIn('>rendered from a copy', header)

    def test_records_that_cannot_be_placed_are_flagged_too(self):
        page = render_source_view('a.cpp', {1: 1, 3: 0}, {}, ['one', 'two'], '')
        header = page[page.index('<header>'):page.index('</header>')]
        self.assertIn('>1 not shown', header)

    def test_a_page_with_nothing_to_flag_has_no_warning(self):
        page = render_source_view('a.cpp', {1: 1}, {}, ['one'], '')
        self.assertNotIn('class=warn', page)


class BreadcrumbTest(unittest.TestCase):
    """Only the report and the file's own directory are linked: the index collapses a chain of
    single-child directories onto one page, so an ancestor's page may not exist."""

    def crumbs(self, path):
        page = render_source_view(path, {1: 1}, {}, ['x'], '../' * path.count('/'))
        return page[page.index('<nav class=crumbs'):page.index('</nav>')]

    def test_the_report_and_the_directory_are_linked_and_the_file_is_the_current_page(self):
        self.assertEqual(self.crumbs('Source/WTF/wtf/Vector.h'),
                         '<nav class=crumbs aria-label=Breadcrumb><ol>'
                         '<li><a href="../../../index.html">All source</a>'
                         '<li><a href="index.html">Source/WTF/wtf</a>'
                         '<li aria-current=page>Vector.h</ol>')

    def test_a_file_at_the_top_has_only_the_report_to_go_back_to(self):
        self.assertEqual(self.crumbs('README.md'),
                         '<nav class=crumbs aria-label=Breadcrumb><ol>'
                         '<li><a href="index.html">All source</a><li aria-current=page>README.md</ol>')

    def test_names_are_escaped(self):
        crumbs = self.crumbs('d&amp/a<b>.h')
        self.assertIn('<li aria-current=page>a&lt;b&gt;.h', crumbs)
        self.assertIn('>d&amp;amp</a>', crumbs)


class HeaderTest(_Tree):
    def setUp(self):
        super(HeaderTest, self).setUp()
        self.render('Source/WTF/wtf/Vector.h', 'int f();\n', lines={1: 0})
        page = self.page('Source/WTF/wtf/Vector.h')
        self.header = page[page.index('<header>'):page.index('</header>')]
        self.info = page[page.index('<section class=info>'):page.index('</section>')]

    def test_the_header_is_sticky(self):
        self.assertIn('header {\n  position: sticky; top: 0;', SOURCE_VIEW_STYLE)

    def test_it_carries_the_breadcrumbs_the_search_box_and_the_theme_toggle(self):
        self.assertIn('<a href="index.html">Source/WTF/wtf</a>', self.header)
        self.assertIn(search_box_html('../../../'), self.header)
        self.assertIn(THEME_TOGGLE_HTML, self.header)

    def test_it_carries_the_summary_and_room_for_the_buttons_the_script_adds(self):
        self.assertIn('<b>Lines</b>', self.header)
        self.assertTrue(self.header.endswith('<div class=tools></div></div>'))
        self.assertNotIn('<button', self.header.replace(THEME_TOGGLE_HTML, ''))

    def test_the_path_and_the_subtitle_scroll_away_with_the_legend(self):
        self.assertIn('<h1>Source/WTF/wtf/Vector.h</h1>', self.info)
        self.assertIn('1 of 1 instrumented lines never executed', self.info)
        self.assertIn('<span class=u>uncovered</span><span class=p>partially taken</span>'
                      '<span class=x>not instrumented</span>', self.info)

    def test_the_legend_shows_every_heat_level(self):
        self.assertEqual(re.findall(r'<i[^>]*>([^<]*)</i>', self.info),
                         ['1+', '10+', '100+', '1k+', '10k+'])
        self.assertEqual(re.findall(r'<i class=(h\d)>', self.info), ['h2', 'h3', 'h4', 'h5'])

    def test_the_markers_do_not_depend_on_color(self):
        self.assertIn('content: "0 !";', SOURCE_VIEW_STYLE)
        self.assertIn('main li.p::after {\n  content: "~";', SOURCE_VIEW_STYLE)
        self.assertIn('background-image: var(--hatch);', SOURCE_VIEW_STYLE)

    def test_the_buttons_are_added_by_the_script_that_runs_them(self):
        for text in ("addButton('pu', '\\u25B2 Uncovered', 'Previous uncovered block', 'p');",
                     "addButton('nu', '\\u25BC Uncovered', 'Next uncovered block', 'n');",
                     "addButton('np', '\\u25BC Partial', 'Next partially taken line', 'b');",
                     "addButton('fold', 'Fold covered',"):
            self.assertIn(text, SOURCE_VIEW_SCRIPT)

    def test_the_keys_step_between_blocks_but_not_while_typing(self):
        self.assertIn("var KEYS = {n: 'nu', j: 'nu', p: 'pu', k: 'pu', b: 'np', B: 'pp', f: 'fold', "
                      "o: 'outline', '?': 'help'};", SOURCE_VIEW_SCRIPT)
        self.assertIn('/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)', SOURCE_VIEW_SCRIPT)

    def test_the_script_says_why_each_block_is_one(self):
        for text in ("'the enclosing function was never entered: look for a caller'",
                     "' but this block was skipped: look at the condition'",
                     "'no function in the trace encloses it'"):
            self.assertIn(text, SOURCE_VIEW_SCRIPT)


class LineLinkTest(unittest.TestCase):
    def test_a_range_is_highlighted_with_a_class_because_target_cannot(self):
        self.assertIn("/^#L(\\d+)(?:-L?(\\d+))?$/", SOURCE_VIEW_SCRIPT)
        self.assertIn("row.classList.add('s')", SOURCE_VIEW_SCRIPT)
        self.assertIn(':root:not(.js) main li:target, main li.s {', SOURCE_VIEW_STYLE)

    def test_the_range_format_is_the_one_coverage_delta_links_with(self):
        self.assertIn("return '#L' + first + (last > first ? '-L' + last : '');", SOURCE_VIEW_SCRIPT)

    def test_it_follows_the_hash_on_load_and_on_change(self):
        self.assertIn("window.addEventListener('hashchange', showHash);", SOURCE_VIEW_SCRIPT)
        self.assertIn('if (location.hash) { showHash(); }', SOURCE_VIEW_SCRIPT)

    def test_a_selected_line_number_is_inverted_so_the_link_is_not_color_alone(self):
        self.assertIn('main li.s::before {\n  color: var(--cov-bg); background: var(--cov-accent);',
                      SOURCE_VIEW_STYLE)


class ThemeTest(unittest.TestCase):
    LOCAL = {'--lw', '--bw', '--cw', '--w', '--n', '--v', '--h', '--hatch', '--sel', '--map', '--gutter'}

    def test_the_stylesheet_starts_with_the_shared_theme(self):
        self.assertTrue(SOURCE_VIEW_STYLE.startswith(THEME_CSS))

    def test_every_color_comes_from_the_theme(self):
        view = SOURCE_VIEW_STYLE[len(THEME_CSS):]
        tokens = {token[0] for token in THEME_TOKENS}
        used = set(re.findall(r'var\((--[\w-]+)', view))
        self.assertEqual(used - tokens - self.LOCAL, set())
        self.assertIn('--cov-heat-4', used)
        self.assertIn('--cov-heat-5', used)
        self.assertEqual(re.findall(r'#[0-9a-fA-F]{3,8}\b', view), [])

    def test_the_heat_levels_ramp_between_two_theme_tokens(self):
        self.assertIn('.h5 { --h: var(--cov-heat-5); }', SOURCE_VIEW_STYLE)
        self.assertIn('--h: var(--cov-heat-4);', SOURCE_VIEW_STYLE)
        self.assertEqual(len(re.findall(r'^\.h[2-4] \{ --h: color-mix', SOURCE_VIEW_STYLE, re.M)), 3)

    def test_there_is_a_print_stylesheet_that_shows_everything(self):
        printed = SOURCE_VIEW_STYLE[SOURCE_VIEW_STYLE.rindex('@media print {'):]
        self.assertIn('main ol { content-visibility: visible; }', printed)
        self.assertIn('main li.f { height: auto; overflow: visible; visibility: visible; }', printed)
        self.assertIn('main li.fold', printed)

    def test_motion_is_opt_in(self):
        motion = SOURCE_VIEW_STYLE[SOURCE_VIEW_STYLE.index('@media (prefers-reduced-motion: no-preference)'):]
        self.assertIn('animation: flash', motion[:motion.index('@media (max-width')])
        self.assertNotIn('scroll-behavior', SOURCE_VIEW_STYLE)
        self.assertEqual(SOURCE_VIEW_STYLE.count('animation:'), 1)

    def test_focus_is_visible_and_forced_colors_keep_the_markers(self):
        self.assertIn(':focus-visible { outline: 2px solid var(--cov-accent); outline-offset: 2px; }',
                      SOURCE_VIEW_STYLE)
        self.assertIn('@media (forced-colors: active)', SOURCE_VIEW_STYLE)

    def test_the_page_is_responsive(self):
        self.assertIn('@media (max-width: 720px)', SOURCE_VIEW_STYLE)

    def test_nothing_refers_to_a_network(self):
        for text in (SOURCE_VIEW_STYLE, SOURCE_VIEW_SCRIPT):
            self.assertNotRegex(text, r'https?://')


class PerformanceContractTest(unittest.TestCase):
    """A 20,000-line page has to stay smooth: listeners are delegated and scrolling does no work."""

    def test_listeners_sit_on_a_few_containers_never_on_rows(self):
        receivers = set(re.findall(r'(\w+)\.addEventListener', SOURCE_VIEW_SCRIPT))
        self.assertEqual(receivers - {'main', 'tools', 'document', 'window', 'minimap', 'help', 'close',
                                      'head', 'outlineList'}, set())

    def test_a_scroll_only_schedules_one_frame(self):
        self.assertIn("window.addEventListener('scroll', schedule, {passive: true});", SOURCE_VIEW_SCRIPT)
        self.assertIn('requestAnimationFrame(updateView)', SOURCE_VIEW_SCRIPT)

    def test_a_chunk_not_near_the_screen_is_not_laid_out(self):
        self.assertIn('content-visibility: auto;', SOURCE_VIEW_STYLE)

    def test_the_script_never_asks_a_row_where_it_is(self):
        self.assertNotRegex(SOURCE_VIEW_SCRIPT, r'rows\[[^\]]*\]\.(getBoundingClientRect|offsetTop|clientHeight)')
        self.assertNotRegex(SOURCE_VIEW_SCRIPT, r'getElementById\([^)]*\)\.(getBoundingClientRect|offsetTop)')


class StaticPageTest(_Tree):
    """Everything essential is in the HTML; the script only adds to it."""

    def test_the_markup_that_matters_without_the_script(self):
        self.render('Source/a.cpp', 'f()\n{\n  a;\n}\n', lines={1: 3, 3: 0}, function_lines={1: 3})
        page = self.page('Source/a.cpp')
        before_rows = page[:page.index('<main')]
        for text in ('<nav class=crumbs', '<ul class=stats>', '<h1>', '<div class=key>', '<main '):
            self.assertIn(text, page)
        self.assertNotIn('<script>', before_rows)
        self.assertIn(':root:not(.js) .tools { display: none; }', SOURCE_VIEW_STYLE)
        self.assertIn(':root:not(.js) main li:target', SOURCE_VIEW_STYLE)
        self.assertEqual(_rows(page), [(1, '', '3'), (2, '', None), (3, 'class=u', None), (4, '', None)])

    def test_rows_carry_no_per_row_script_hooks(self):
        self.render('a.cpp', 'f()\n', lines={1: 3})
        self.assertNotIn('onclick', self.page('a.cpp'))
        self.assertNotIn('<script>', self.page('a.cpp'))


class _Node(unittest.TestCase):
    NODE = shutil.which('node')

    def run_node(self, *arguments):
        return subprocess.run([self.NODE] + list(arguments), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class JavaScriptSyntaxTest(_Node):
    def check(self, text):
        handle, path = tempfile.mkstemp(suffix='.js')
        self.addCleanup(os.remove, path)
        with os.fdopen(handle, 'w') as out:
            out.write(text)
        result = self.run_node('--check', path)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_the_page_script_parses(self):
        self.check(SOURCE_VIEW_SCRIPT)

    def test_the_pure_core_parses_on_its_own(self):
        self.check(SOURCE_VIEW_CORE_SCRIPT)

    def test_the_core_touches_no_global(self):
        self.assertNotRegex(SOURCE_VIEW_CORE_SCRIPT, r'\b(document|window|location|history|sessionStorage)\b')


_CORE_TESTS = r"""
'use strict';
const assert = require('assert');
const fs = require('fs');
const names = ['NONE', 'COVERED', 'PARTIAL', 'UNCOVERED', 'formatCount', 'heatLevel', 'parseHash',
  'formatHash', 'foldRuns', 'foldIndex', 'runAt', 'rowsThrough', 'visualRow', 'lineAtRow',
  'bucketRanges', 'minimapSegments', 'findBlocks', 'pickBlock', 'describeBlock', 'parseFunctions',
  'sortFunctions', 'labelFromLine'];
const core = new Function(fs.readFileSync(process.argv[2], 'utf8') + '\nreturn {' + names.join(',') + '};')();
const CODE = {'.': core.NONE, c: core.COVERED, p: core.PARTIAL, u: core.UNCOVERED};
const states = (text) => Uint8Array.from([0].concat(Array.from(text, (ch) => CODE[ch])));
const tests = [];
const test = (name, body) => tests.push([name, body]);

test('formatCount is llvm-cov\'s', () => {
  [[0, '0'], [999, '999'], [1000, '1.00k'], [2534, '2.53k'], [9999, '9.99k'], [10000, '10.0k'],
   [100000, '100k'], [999999, '999k'], [1000000, '1.00M'], [40912345, '40.9M'], [1e9, '1.00G'],
   [1e12, '1.00T']].forEach(([count, text]) => assert.strictEqual(core.formatCount(count), text));
});

test('heatLevel is the number of digits, at most five', () => {
  [1, 9, 10, 99, 100, 999, 1000, 2534, 9999, 10000, 12345, 99999, 100000, 999999, 1e6, 40912345, 1e9, 1e12]
    .forEach((count) => assert.strictEqual(core.heatLevel(core.formatCount(count)),
                                           Math.min(String(count).length, 5), String(count)));
});

test('parseHash takes a line or a range, in either order, clamped to the file', () => {
  assert.deepStrictEqual(core.parseHash('#L12', 100), [12, 12]);
  assert.deepStrictEqual(core.parseHash('#L10-L20', 100), [10, 20]);
  assert.deepStrictEqual(core.parseHash('#L10-20', 100), [10, 20]);
  assert.deepStrictEqual(core.parseHash('#L20-L10', 100), [10, 20]);
  assert.deepStrictEqual(core.parseHash('#L90-L200', 100), [90, 100]);
  ['#L0', '#L101', '#top', '', '#L1-L2-L3'].forEach((hash) => assert.strictEqual(core.parseHash(hash, 100), null));
  assert.strictEqual(core.formatHash(5, 5), '#L5');
  assert.strictEqual(core.formatHash(5, 9), '#L5-L9');
});

test('foldRuns finds runs of covered or not-instrumented lines of at least the minimum', () => {
  assert.deepStrictEqual(core.foldRuns(states('cccccccc'), 8), [[1, 8]]);
  assert.deepStrictEqual(core.foldRuns(states('ccccccc'), 8), []);
  assert.deepStrictEqual(core.foldRuns(states('c.c.c.c.cu'), 8), [[1, 9]]);
  assert.deepStrictEqual(core.foldRuns(states('cccccccpcccccccc'), 8), [[9, 16]]);
  assert.deepStrictEqual(core.foldRuns(states('uccccccccup........u'), 8), [[2, 9], [12, 19]]);
  assert.deepStrictEqual(core.foldRuns(states(''), 8), []);
  assert.deepStrictEqual(core.foldRuns(states('........'), 8), [[1, 8]]);
});

// Draw the page the way a browser would: a row per visible line and one per collapsed run.
function draw(runs, n) {
  const rows = [], placed = {};
  for (let line = 1; line <= n; line++) {
    const run = runs.find((r) => line >= r[0] && line <= r[1]);
    if (!run) { placed[line] = rows.length; rows.push(line); }
    else if (line === run[0]) { placed[line] = rows.length; rows.push(-line); }
    else { placed[line] = placed[run[0]]; }
  }
  return {rows, placed};
}

test('the fold arithmetic agrees with a drawn page', () => {
  let seed = 7;
  const random = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  for (let trial = 0; trial < 200; trial++) {
    const n = 1 + Math.floor(random() * 120);
    const cuts = new Set();
    for (let i = 0; i < Math.floor(random() * 8); i++) { cuts.add(1 + Math.floor(random() * n)); }
    const runs = [];
    let at = 1;
    Array.from(cuts).sort((a, b) => a - b).forEach((cut) => {
      const length = 2 + Math.floor(random() * 10);
      if (cut >= at && cut + length - 1 <= n) { runs.push([cut, cut + length - 1]); at = cut + length + 1; }
    });
    const fold = core.foldIndex(runs);
    const {rows, placed} = draw(runs, n);
    for (let line = 1; line <= n; line++) {
      assert.strictEqual(core.visualRow(fold, line), placed[line], `row of ${line} in ${JSON.stringify(runs)}`);
    }
    assert.strictEqual(core.rowsThrough(fold, n), rows.length);
    assert.strictEqual(core.rowsThrough(fold, 0), 0);
    for (let row = 0; row <= rows.length; row++) {
      let expected = n;
      for (let line = n; line >= 1; line--) { if (placed[line] >= row) { expected = line; } }
      assert.strictEqual(core.lineAtRow(fold, row, n), expected, `line at row ${row}`);
    }
    const chunk = 1 + Math.floor(random() * 30);
    for (let first = 1; first <= n; first += chunk) {
      const last = Math.min(n, first + chunk - 1);
      const count = rows.filter((row) => Math.abs(row) >= first && Math.abs(row) <= last).length;
      assert.strictEqual(core.rowsThrough(fold, last) - core.rowsThrough(fold, first - 1), count);
    }
  }
});

test('the minimap marks buckets, at least two wide, merged', () => {
  const ranges = (marks) => core.bucketRanges(Uint8Array.from(marks), 2);
  assert.deepStrictEqual(ranges([0, 1, 1, 0, 0, 0, 1, 0]), [[1, 3], [6, 8]]);
  assert.deepStrictEqual(ranges([1, 0, 0, 0]), [[0, 2]]);
  assert.deepStrictEqual(ranges([0, 0, 0, 1]), [[2, 4]]);
  assert.deepStrictEqual(ranges([1, 0, 1, 0, 0, 0]), [[0, 4]]);
  assert.deepStrictEqual(ranges([0, 0]), []);
  assert.deepStrictEqual(ranges([1]), [[0, 1]]);
  const small = core.minimapSegments(states('cuu.pcc'), 512);
  assert.deepStrictEqual([small.count, small.uncovered, small.partial], [7, [[1, 3]], [[4, 6]]]);
  const long = states('c'.repeat(4999) + 'u' + 'c'.repeat(5000));
  const big = core.minimapSegments(long, 100);
  assert.deepStrictEqual([big.count, big.uncovered, big.partial], [100, [[49, 51]], []]);
  const none = core.minimapSegments(states(''), 100);
  assert.deepStrictEqual([none.count, none.uncovered, none.partial], [0, [], []]);
});

test('findBlocks', () => {
  const U = core.UNCOVERED, P = core.PARTIAL;
  const blocks = (text, state, starts) => core.findBlocks(states(text), state, starts || []).map((b) => [b.first, b.last, b.fn]);
  assert.deepStrictEqual(blocks('u..u', U), [[1, 4, 0]], 'a line with no record does not end a run');
  assert.deepStrictEqual(blocks('ucu', U), [[1, 1, 0], [3, 3, 0]], 'a covered line does');
  assert.deepStrictEqual(blocks('pupu', U), [[2, 2, 0], [4, 4, 0]], 'so does a partial one');
  assert.deepStrictEqual(blocks('pupu', P), [[1, 1, 0], [3, 3, 0]]);
  assert.deepStrictEqual(blocks('u.uu', U, [2]), [[1, 1, 0], [3, 4, 1]], 'a function start splits a run');
  assert.deepStrictEqual(blocks('ccccuccuccuu', U, [1, 5, 8]), [[5, 5, 2], [8, 8, 3], [11, 12, 3]],
                         'the enclosing function is the nearest start at or before');
  assert.deepStrictEqual(blocks('', U), []);
});

test('pickBlock steps from a line and wraps', () => {
  const blocks = [{first: 10}, {first: 20}, {first: 30}];
  [[0, 1, 0], [10, 1, 1], [29, 1, 2], [30, 1, 0], [31, -1, 2], [30, -1, 1], [10, -1, 2]]
    .forEach(([from, delta, expected]) => assert.strictEqual(core.pickBlock(blocks, from, delta), expected));
  assert.strictEqual(core.pickBlock([], 10, 1), -1);
});

test('describeBlock says why', () => {
  const U = core.UNCOVERED, P = core.PARTIAL;
  assert.strictEqual(core.describeBlock(U, {first: 40, last: 44}, 2, 12, 0),
    'Uncovered 3 of 12 at lines 40-44: the enclosing function was never entered: look for a caller');
  assert.strictEqual(core.describeBlock(U, {first: 6, last: 6}, 0, 1, 2534),
    'Uncovered 1 of 1 at line 6: the function ran 2.53k times but this block was skipped: look at the condition');
  assert.strictEqual(core.describeBlock(U, {first: 6, last: 6}, 0, 1, 1),
    'Uncovered 1 of 1 at line 6: the function ran 1 time but this block was skipped: look at the condition');
  assert.strictEqual(core.describeBlock(U, {first: 6, last: 6}, 0, 1, undefined),
    'Uncovered 1 of 1 at line 6: no function in the trace encloses it');
  assert.strictEqual(core.describeBlock(P, {first: 9, last: 9}, 1, 4, 5, '1/2'),
    'Partially taken 2 of 4 at line 9: 1/2 branches taken; the line ran but a branch never did');
});

test('the outline parses, sorts and names functions', () => {
  const parsed = core.parseFunctions('12:2534:10:12;40:0:0:8;junk;7:1:1:1');
  assert.deepStrictEqual(parsed, [{line: 12, hits: 2534, covered: 10, total: 12},
    {line: 40, hits: 0, covered: 0, total: 8}, {line: 7, hits: 1, covered: 1, total: 1}]);
  assert.deepStrictEqual(core.parseFunctions(undefined), []);
  const sorted = (key, descending) => core.sortFunctions(parsed, key, descending).map((f) => f.line);
  assert.deepStrictEqual(sorted('line', false), [7, 12, 40]);
  assert.deepStrictEqual(sorted('line', true), [40, 12, 7]);
  assert.deepStrictEqual(sorted('hits', true), [12, 7, 40]);
  assert.deepStrictEqual(sorted('hits', false), [40, 7, 12]);
  assert.deepStrictEqual(sorted('uncovered', true), [40, 12, 7]);
  const ties = [{line: 9, hits: 1, covered: 0, total: 1}, {line: 3, hits: 1, covered: 0, total: 1}];
  assert.deepStrictEqual(core.sortFunctions(ties, 'uncovered', true).map((f) => f.line), [3, 9]);
  const label = core.labelFromLine;
  assert.strictEqual(label('void Element::setAttribute(const QualifiedName& name, const AtomString& value)'), 'Element::setAttribute');
  assert.strictEqual(label('static Ref<Foo> create(Document& document)'), 'create');
  assert.strictEqual(label('Element::~Element()'), 'Element::~Element');
  assert.strictEqual(label('bool operator==(const A& a, const B& b)'), 'operator==');
  assert.strictEqual(label('    if (x) {'), 'if (x) {');
  assert.strictEqual(label('constructor() {}'), 'constructor');
  assert.strictEqual(label('template<typename T>'), 'template<typename T>');
  assert.strictEqual(label('x'.repeat(100)), 'x'.repeat(59) + '…');
});

let failed = 0;
tests.forEach(([name, body]) => {
  try { body(); } catch (error) { failed++; console.log('FAIL', name + '\n' + error.message); }
});
process.exit(failed ? 1 : 0);
"""


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class JavaScriptCoreTest(_Node):
    """The script's logic that needs no page, run under node: fold geometry, navigation, minimap."""

    def test_the_core_logic(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        paths = []
        for name, text in (('core.js', SOURCE_VIEW_CORE_SCRIPT), ('test.js', _CORE_TESTS)):
            paths.append(os.path.join(directory, name))
            with open(paths[-1], 'w', encoding='utf-8') as handle:
                handle.write(text)
        result = self.run_node(paths[1], paths[0])
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_run_is_at_least_the_documented_minimum(self):
        self.assertIn('var MIN_FOLD = {};'.format(FOLD_MIN_LINES), SOURCE_VIEW_SCRIPT)


class RegionTest(_Tree):
    """Uncovered regions inside a line that ran, from llvm-cov's JSON export."""

    def row(self, text, spans, count=5):
        page = render_source_view('a.cpp', {1: count}, {}, [text], '', regions={1: spans})
        return _row(page, 1)[len('\n<li id=L1><b>5</b>'):]

    def test_a_region_to_the_end_of_the_line(self):
        self.assertEqual(self.row('if (a) f();', [(8, None)]), 'if (a) <mark>f();</mark>')

    def test_columns_are_utf8_bytes_not_characters(self):
        # "é" is two bytes, so f(); starts at byte column 11 and character 10.
        self.assertEqual(self.row('x = "é"; f();', [(11, 15)]), 'x = "é"; <mark>f();</mark>')

    def test_the_text_inside_a_region_is_escaped(self):
        self.assertEqual(self.row('a < b && c', [(1, 6)]), '<mark>a &lt; b</mark> &amp;&amp; c')

    def test_overlapping_and_adjacent_spans_are_one_mark(self):
        self.assertEqual(self.row('abcdefgh', [(3, 5), (1, 3), (2, 4)]), '<mark>abcd</mark>efgh')

    def test_spans_outside_the_line_are_dropped(self):
        self.assertEqual(self.row('abc', [(9, None), (2, 2)]), 'abc')
        self.assertEqual(self.row('abc', [(2, 99)]), 'a<mark>bc</mark>')

    def test_a_line_that_did_not_run_or_has_no_record_is_not_marked(self):
        page = render_source_view('a.cpp', {1: 0}, {}, ['abc', 'def'], '',
                                  regions={1: [(1, None)], 2: [(1, None)]})
        self.assertNotIn('<mark>', page)

    def test_write_source_views_threads_them_by_path(self):
        self.render('a.cpp', 'if (a) f();\n', lines={1: 5}, regions={1: [(8, None)]})
        self.assertIn('if (a) <mark>f();</mark>', self.page('a.cpp'))

    def test_they_are_optional(self):
        self.render('a.cpp', 'if (a) f();\n', lines={1: 5})
        self.assertNotIn('<mark>', self.page('a.cpp'))


if __name__ == '__main__':
    unittest.main()

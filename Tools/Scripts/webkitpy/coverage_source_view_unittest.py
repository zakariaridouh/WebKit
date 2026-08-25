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

import logging
import os
import re
import shutil
import tempfile
import unittest

from webkitpy.coverage_lcov import FileCoverage
from webkitpy.coverage_source_view import (
    STYLESHEET_NAME, _format_count, partial_branch_lines, relative_source_path,
    render_source_view, write_source_views)

_ROW = re.compile(r'<tr id=L(\d+)( class=[up])?><td>\d+<td>([^<]*)<td>')


def _rows(page):
    """[(line number, row class or '', count cell)] in document order."""
    return [(int(number), (kind or '').strip(), count)
            for number, kind, count in _ROW.findall(page)]


class FormatCountTest(unittest.TestCase):
    def test_matches_llvm_covs_table(self):
        # llvm-cov's formatCount, reproduced so that a page rendered here and a page rendered
        # by llvm-cov show are comparable cell by cell.
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

    def write(self, relative, contents):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path

    def coverage(self, lines=None, branches=None):
        entry = FileCoverage()
        entry.lines = dict(lines or {})
        entry.branches = dict(branches or {})
        return entry

    def render(self, source_relative, contents, lines=None, branches=None, workers=1):
        path = self.write(source_relative, contents)
        result = write_source_views({path: self.coverage(lines, branches)},
                                    self.output, self.root, workers=workers)
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
        # Source/WebCore/xml/XPathGrammar.cpp is bison output with three form feeds in it.
        # str.splitlines() splits on those and llvm-cov does not, which numbered every line
        # after them differently from llvm-cov's page for the same file.
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

    def test_a_line_with_no_coverage_data_has_an_empty_count_and_no_class(self):
        self.assertEqual(self.rows[1], ('', ''))

    def test_a_line_that_never_executed_is_marked_uncovered(self):
        self.assertEqual(self.rows[2], ('class=u', '0'))

    def test_an_executed_line_is_neither_marked_nor_empty(self):
        self.assertEqual(self.rows[3], ('', '2.53k'))


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
        # Data llvm-cov show does not display at all, and the nearest thing lcov has to the
        # sub-line regions the line view cannot express.
        self.render('a.cpp', 'if (a && b) f();\n', lines={1: 7},
                    branches={('1', '0', '0'): 7, ('1', '0', '1'): 0,
                              ('1', '0', '2'): 4, ('1', '0', '3'): 0})
        page = self.page('a.cpp')
        self.assertEqual(_rows(page), [(1, 'class=p', '7')])
        self.assertIn('<td>2/4', page)

    def test_a_line_whose_branches_were_all_taken_is_not_marked(self):
        self.render('a.cpp', 'if (a) f();\n', lines={1: 7},
                    branches={('1', '0', '0'): 7, ('1', '0', '1'): 2})
        self.assertEqual(_rows(self.page('a.cpp')), [(1, '', '7')])
        self.assertNotIn('<td>2/2', self.page('a.cpp'))

    def test_an_uncovered_line_is_still_marked_uncovered_not_partial(self):
        self.render('a.cpp', 'if (a) f();\n', lines={1: 0},
                    branches={('1', '0', '0'): 0, ('1', '0', '1'): 0})
        self.assertEqual(_rows(self.page('a.cpp')), [(1, 'class=u', '0')])

    def test_partial_branch_lines_counts_taken_over_total(self):
        self.assertEqual(partial_branch_lines({('12', '0', '0'): 3, ('12', '0', '1'): 0,
                                               ('20', '0', '0'): 1, ('20', '0', '1'): 1}),
                         {12: (1, 2)})

    def test_partial_branch_lines_ignores_an_unparsable_line_number(self):
        self.assertEqual(partial_branch_lines({('not a number', '0', '0'): 0}), {})


class PageLocationTest(_Tree):
    def test_a_page_is_a_sibling_of_its_directory_index_with_relative_stylesheet(self):
        (pages, _, skipped), _ = self.render('Source/WTF/wtf/Vector.h', 'int f();\n', lines={1: 1})
        self.assertEqual((pages, skipped), (1, set()))
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

    def test_relative_source_path_of_a_path_under_the_root(self):
        self.assertEqual(relative_source_path('/a/b/Source/WTF/wtf/Vector.h', '/a/b'),
                         'Source/WTF/wtf/Vector.h')

    def test_relative_source_path_of_a_path_outside_the_root_keeps_its_shape(self):
        # Matching what write_directory_index does with a path it cannot root: strip the
        # leading slash so it lands inside the report rather than at /.
        self.assertEqual(relative_source_path('/elsewhere/generated/Foo.cpp', '/a/b'),
                         'elsewhere/generated/Foo.cpp')

    def test_relative_source_path_does_not_match_a_partial_directory_name(self):
        self.assertEqual(relative_source_path('/a/bc/Foo.cpp', '/a/b'), 'a/bc/Foo.cpp')

    def test_a_path_outside_the_source_root_still_gets_a_page(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        path = os.path.join(outside, 'generated', 'Foo.cpp')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as handle:
            handle.write('int f();\n')
        pages, written, skipped = write_source_views(
            {path: self.coverage({1: 1})}, self.output, self.root, workers=1)
        self.assertEqual((pages, skipped), (1, set()))
        self.assertTrue(written > 0)
        self.assertTrue(os.path.isfile(
            os.path.join(self.output, path.lstrip('/') + '.html')))


class UnreadableSourceTest(_Tree):
    def test_a_missing_source_is_returned_as_skipped_rather_than_raising(self):
        missing = os.path.join(self.root, 'Source/WTF/wtf/Gone.h')
        pages, written, skipped = write_source_views(
            {missing: self.coverage({1: 1})}, self.output, self.root, workers=1)
        self.assertEqual((pages, written, skipped), (0, 0, {missing}))
        self.assertFalse(os.path.exists(os.path.join(self.output, 'Source/WTF/wtf/Gone.h.html')))

    def test_a_directory_where_a_file_is_expected_is_skipped_too(self):
        directory = os.path.join(self.root, 'Source/WTF/wtf')
        os.makedirs(directory)
        pages, _, skipped = write_source_views(
            {directory: self.coverage({1: 1})}, self.output, self.root, workers=1)
        self.assertEqual((pages, skipped), (0, {directory}))

    def test_the_readable_files_are_still_written(self):
        good = self.write('Source/WTF/wtf/Vector.h', 'int f();\n')
        missing = os.path.join(self.root, 'Source/WTF/wtf/Gone.h')
        pages, _, skipped = write_source_views(
            {good: self.coverage({1: 1}), missing: self.coverage({1: 0})},
            self.output, self.root, workers=1)
        self.assertEqual((pages, skipped), (1, {missing}))


class SubtitleTest(_Tree):
    def test_the_page_states_how_many_instrumented_lines_never_executed(self):
        self.render('a.cpp', 'one\ntwo\nthree\n', lines={1: 1, 2: 0, 3: 0})
        self.assertIn('2 of 3 instrumented lines never executed', self.page('a.cpp'))


class MarkupTest(_Tree):
    def test_the_newline_leads_a_row_rather_than_trailing_it(self):
        # The code cell is white-space:pre, so a newline at the end of a row would land inside
        # that cell and render every row two lines tall.
        page = render_source_view('a.cpp', {1: 1, 2: 0}, {}, ['int f();', 'int g();'], '')
        body = page[page.index('<table>') + len('<table>'):page.index('</table>')]
        self.assertEqual(body, '\n<tr id=L1><td>1<td>1<td>int f();'
                               '\n<tr id=L2 class=u><td>2<td>0<td>int g();\n')

    def test_the_page_is_a_complete_document(self):
        page = render_source_view('a.cpp', {1: 1}, {}, ['int f();'], '')
        self.assertTrue(page.startswith('<!DOCTYPE html>'))
        self.assertTrue(page.endswith('</table></body></html>\n'))


if __name__ == '__main__':
    unittest.main()

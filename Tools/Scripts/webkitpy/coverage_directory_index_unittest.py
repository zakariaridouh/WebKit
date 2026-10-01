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

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from unittest import mock
from urllib.parse import unquote, urlparse

from webkitpy.coverage_directory_index import (
    effective_source_prefix, generated_source_totals, write_directory_index, write_report)
from webkitpy.coverage_report_theme_unittest import node_binary


# Names that are not safe in a URL, in a path, or in HTML. The last two are directories.
ODD_FILES = ('javascript:alert(1)%2F%2F.cpp', 'hash#tag.cpp', 'what?.cpp', '100%.cpp', 'with space.cpp',
             'na\u00efve.cpp', 'a&b<c>.cpp', 'q\'uote".cpp', 'index', 'index.html')
ODD_DIRECTORIES = ('javascript:dir', 'odd dir #1?%')


class _Report(unittest.TestCase):
    """A throwaway checkout, an lcov trace over it, and somewhere to write the report."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.output, ignore_errors=True)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        # Silence the expected warning without disabling logging: with no handler at all, lastResort prints to stderr.
        view_logger = logging.getLogger('webkitpy.coverage_source_view')
        self.addCleanup(setattr, view_logger, 'propagate', view_logger.propagate)
        view_logger.propagate = False
        silence = logging.NullHandler()
        view_logger.addHandler(silence)
        self.addCleanup(view_logger.removeHandler, silence)

    def write_source(self, relative, contents='int f();\nint g();\n'):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as handle:
            handle.write(contents)
        return path

    def write_trace(self, *relatives):
        records = []
        for relative in relatives:
            records.append('SF:{}\nFN:1,_Z1fv\nFNDA:1,_Z1fv\nDA:1,1\nDA:2,0\nend_of_record\n'.format(
                os.path.join(self.root, relative)))
        path = os.path.join(self.root, 'coverage.lcov')
        with open(path, 'w') as handle:
            handle.write(''.join(records))
        return path

    def page(self, relative):
        with open(os.path.join(self.output, relative)) as handle:
            return handle.read()

    def markup(self, relative):
        """The page without its inlined script or JSON payload, which mention every path and attribute name the markup does."""
        return self.page(relative).split('<script>')[0]

    def card(self, relative, heading):
        """One card's table, from its heading to the end of that table."""
        markup = self.markup(relative)
        start = markup.index(heading)
        return markup[start:markup.index('</table>', start)]

    def links(self):
        """[(page path, href)] for every href in every page of the report."""
        found = []
        for dirpath, _, filenames in os.walk(self.output):
            for name in filenames:
                if name.endswith('.html'):
                    with open(os.path.join(dirpath, name)) as handle:
                        text = handle.read()
                    found += [(os.path.join(dirpath, name), link)
                              for link in re.findall(r'href="([^"]*)"', text)]
        return found

    def broken_links(self):
        """The (page, href) pairs whose target is not a file or directory in the report."""
        return [(page, link) for page, link in self.links()
                if link.partition('#')[0]
                and not os.path.exists(os.path.normpath(os.path.join(
                    os.path.dirname(page), unquote(link.partition('#')[0]))))]


class FileLinkTest(_Report):
    def test_a_file_links_to_its_sibling_line_view(self):
        # Not up and back down into html/coverage/<absolute path>.html, where llvm-cov's pages were.
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('<a href="./Vector.h.html">Vector.h</a>', page)
        self.assertNotIn('html/coverage/', page)
        self.assertTrue(os.path.isfile(os.path.join(self.output, 'Source/WTF/wtf/Vector.h.html')))

    def test_a_file_with_no_line_view_is_rendered_as_text(self):
        # No source on disk, so no page was written and a link would be a link to a 404.
        from webkitpy.coverage_source_view import UNREADABLE_SOURCE
        trace = self.write_trace('Source/WTF/wtf/Gone.h')
        report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(report.source_pages, 0)
        self.assertEqual(report.skipped_paths,
                         {os.path.join(self.root, 'Source/WTF/wtf/Gone.h'): UNREADABLE_SOURCE})
        page = self.page('Source/WTF/wtf/index.html')
        self.assertNotIn('Gone.h.html', page)
        self.assertIn('nosource', page)
        self.assertIn('Gone.h', page)

    def test_the_index_says_why_a_file_has_no_line_view(self):
        # The two reasons want different actions: a cleaned build directory, or a tree that moved on since the build.
        from webkitpy.coverage_source_view import RECORDS_PAST_END_OF_FILE
        self.write_source('Source/WTF/wtf/Shrunk.h', 'one line only\n')
        trace = self.write_trace('Source/WTF/wtf/Shrunk.h')
        with open(trace, 'a') as handle:
            handle.write('SF:{}\nDA:900,0\nend_of_record\n'.format(
                os.path.join(self.root, 'Source/WTF/wtf/Shrunk.h')))
        report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(report.skipped_paths,
                         {os.path.join(self.root, 'Source/WTF/wtf/Shrunk.h'):
                          RECORDS_PAST_END_OF_FILE})
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('title="No line view: the coverage records run past the end of the file',
                      page)

    def test_a_bare_set_of_paths_gets_the_reason_the_caller_names(self):
        # --no-source-views writes no line views and passes the whole set of paths, so "could not be read" would be untrue.
        from webkitpy.coverage_source_view import LINE_VIEWS_NOT_WRITTEN
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_directory_index(trace, self.output, source_root=self.root,
                              unlinkable={os.path.join(self.root, 'Source/WTF/wtf/Vector.h')},
                              unlinkable_reason=LINE_VIEWS_NOT_WRITTEN)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('title="No line view: {}"'.format(LINE_VIEWS_NOT_WRITTEN), page)
        self.assertNotIn('Vector.h.html', page)

    def test_a_bare_set_of_paths_defaults_to_the_unreadable_reason(self):
        from webkitpy.coverage_source_view import UNREADABLE_SOURCE
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_directory_index(trace, self.output, source_root=self.root,
                              unlinkable=[os.path.join(self.root, 'Source/WTF/wtf/Vector.h')])
        self.assertIn('title="No line view: {}"'.format(UNREADABLE_SOURCE),
                      self.page('Source/WTF/wtf/index.html'))

    def test_a_directory_and_a_file_of_the_same_name_do_not_collide(self):
        self.write_source('Source/WTF/wtf/Vector.h')
        self.write_source('Source/WTF/wtf/Vector/Extra.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h', 'Source/WTF/wtf/Vector/Extra.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('<a href="./Vector/index.html">Vector</a>', page)
        self.assertIn('<a href="./Vector.h.html">Vector.h</a>', page)


class BreadcrumbTest(_Report):
    def test_a_collapsed_chain_is_not_linked_to_a_page_that_was_never_written(self):
        # A directory with one subdirectory gets no page of its own, so a breadcrumb linking to it points at nothing.
        self.write_source('Source/WebKit/WPEPlatform/wpe/View.cpp')
        self.write_source('Source/WebKit/Shared/Cocoa/Thing.mm')
        trace = self.write_trace('Source/WebKit/WPEPlatform/wpe/View.cpp',
                                 'Source/WebKit/Shared/Cocoa/Thing.mm')
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertFalse(os.path.exists(os.path.join(self.output, 'Source/WebKit/WPEPlatform/index.html')))
        page = self.page('Source/WebKit/WPEPlatform/wpe/index.html')
        crumbs = page.split('<p class="crumbs">')[1].split('</p>')[0]
        # Source collapses into Source/WebKit too, so only the root and Source/WebKit are linkable.
        self.assertIn('<a href="../../../../index.html">All source</a>', crumbs)
        self.assertIn('<a href="../../index.html">WebKit</a>', crumbs)
        self.assertIn('Source', crumbs)
        self.assertNotIn('>Source</a>', crumbs)
        self.assertIn('WPEPlatform', crumbs)
        self.assertNotIn('>WPEPlatform</a>', crumbs)

    def test_every_link_in_every_page_resolves(self):
        relatives = ['Source/WebKit/WPEPlatform/wpe/View.cpp',
                     'Source/WebKit/Shared/Cocoa/Thing.mm',
                     'Source/WTF/wtf/Vector.h',
                     'Source/WTF/wtf/text/AtomString.h']
        relatives += ['Source/WTF/wtf/' + name for name in ODD_FILES]
        relatives += ['Source/' + directory + '/' + name
                      for directory in ODD_DIRECTORIES for name in ODD_FILES[:3]]
        for relative in relatives:
            self.write_source(relative)
        trace = self.write_trace(*relatives)
        report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(report.source_pages, len(relatives))
        self.assertTrue(self.links())
        self.assertEqual(self.broken_links(), [])

    def test_no_link_can_be_read_as_a_url_with_a_scheme(self):
        relatives = ['Source/WTF/wtf/' + name for name in ODD_FILES]
        relatives += ['Source/' + directory + '/Thing.cpp' for directory in ODD_DIRECTORIES]
        for relative in relatives:
            self.write_source(relative)
        write_report(self.write_trace(*relatives), self.output, source_root=self.root, workers=1)
        for page, link in self.links():
            self.assertEqual(urlparse(link).scheme, '', (page, link))
            self.assertFalse(link.lower().startswith('javascript'), (page, link))
        listing = self.page('Source/WTF/wtf/index.html')
        self.assertIn('<a href="./javascript%3Aalert%281%29%252F%252F.cpp.html">', listing)
        self.assertIn('<a href="./hash%23tag.cpp.html">', listing)
        self.assertIn('<a href="./what%3F.cpp.html">', listing)
        self.assertIn('<a href="./100%25.cpp.html">', listing)
        self.assertIn('<a href="./with%20space.cpp.html">', listing)
        self.assertIn('<a href="./na%C3%AFve.cpp.html">', listing)
        root = self.page('Source/index.html')
        self.assertIn('<a href="./javascript%3Adir/index.html">', root)
        self.assertIn('<a href="./odd%20dir%20%231%3F%25/index.html">', root)

    def test_a_file_called_index_does_not_overwrite_its_directorys_page(self):
        for relative in ('Source/WTF/wtf/index', 'Source/WTF/wtf/index.html', 'Source/WTF/wtf/a.h'):
            self.write_source(relative)
        trace = self.write_trace('Source/WTF/wtf/index', 'Source/WTF/wtf/index.html',
                                 'Source/WTF/wtf/a.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        listing = self.page('Source/WTF/wtf/index.html')
        self.assertIn('<title>Coverage: Source/WTF/wtf</title>', listing)
        self.assertIn('<a href="./index.html.html">index</a>', listing)
        self.assertIn('<a href="./index.html.html.html">index.html</a>', listing)
        self.assertIn('<title>Source/WTF/wtf/index</title>', self.page('Source/WTF/wtf/index.html.html'))
        self.assertIn('<title>Source/WTF/wtf/index.html</title>',
                      self.page('Source/WTF/wtf/index.html.html.html'))
        self.assertEqual(self.broken_links(), [])


class ConfinementTest(_Report):
    """A path whose page would fall outside the output directory gets no page and no row."""

    def setUp(self):
        super().setUp()
        self.parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.parent, ignore_errors=True)
        self.output = os.path.join(self.parent, 'report')

    def raw_trace(self, *paths):
        path = os.path.join(self.root, 'coverage.lcov')
        with open(path, 'w') as handle:
            for raw in paths:
                handle.write('SF:{}\nDA:1,1\nDA:2,0\nend_of_record\n'.format(raw))
        return path

    def escapes(self):
        return ['../../escape.cpp', 'Source/../../escape2.cpp', 'Source/nul\0.cpp']

    def written_outside_the_report(self):
        return [os.path.join(current, name) for current, _, names in os.walk(self.parent)
                for name in names if not current.startswith(self.output)]

    def test_write_report_skips_them_with_one_warning_that_counts_them(self):
        self.write_source('Source/ok.h')
        trace = self.raw_trace(os.path.join(self.root, 'Source/ok.h'), *self.escapes())
        logging.disable(logging.NOTSET)
        with self.assertLogs('webkitpy.coverage_source_view', 'WARNING') as logs:
            report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn('3 files have a path that would put their page outside the report directory',
                      logs.output[0])
        self.assertEqual(report.source_pages, 1)
        self.assertEqual(self.written_outside_the_report(), [])
        self.assertEqual(os.listdir(self.parent), ['report'])
        self.assertNotIn('escape', self.page('index.html'))
        self.assertEqual(report.totals['lines'], (8, 4))

    def test_write_directory_index_skips_them_too(self):
        self.write_source('Source/ok.h')
        trace = self.raw_trace(os.path.join(self.root, 'Source/ok.h'), *self.escapes())
        logging.disable(logging.NOTSET)
        with self.assertLogs('webkitpy.coverage_source_view', 'WARNING') as logs:
            write_directory_index(trace, self.output, source_root=self.root)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.written_outside_the_report(), [])
        self.assertNotIn('escape', self.page('index.html'))

    def test_a_path_with_dot_dot_segments_that_stays_absolute_is_placed_where_it_resolves(self):
        # The same file the OS would open, so it is not an escape: it goes under the outside-the-checkout node.
        from webkitpy.coverage_source_view import OUTSIDE_SOURCE_ROOT_DIRECTORY
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        os.mkdir(os.path.join(elsewhere, 'sub'))
        with open(os.path.join(elsewhere, 'Real.h'), 'w') as handle:
            handle.write('int f();\nint g();\n')
        self.write_source('Source/ok.h')
        trace = self.raw_trace(os.path.join(self.root, 'Source/ok.h'),
                               os.path.join(elsewhere, 'sub', '..', '..', os.path.basename(elsewhere),
                                            'Real.h'))
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(self.written_outside_the_report(), [])
        self.assertTrue(os.path.isfile(os.path.join(
            self.output, OUTSIDE_SOURCE_ROOT_DIRECTORY, elsewhere.lstrip('/'), 'Real.h.html')))
        self.assertEqual(self.broken_links(), [])


class FlatIndexHintTest(_Report):
    def test_the_llvm_cov_index_is_not_advertised_when_it_was_not_written(self):
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_directory_index(trace, self.output, source_root=self.root)
        self.assertNotIn('Flat llvm-cov index', self.page('index.html'))

    def test_it_is_advertised_when_a_link_is_given(self):
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_directory_index(trace, self.output, source_root=self.root,
                              index_link='html/index.html')
        self.assertIn('Flat llvm-cov index', self.page('index.html'))
        self.assertIn('href="html/index.html"', self.page('index.html'))


class ParseHoistingTest(_Report):
    def test_write_directory_index_accepts_an_already_parsed_trace(self):
        from webkitpy.coverage_lcov import parse_lcov
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        coverage_by_path = parse_lcov(trace)
        os.unlink(trace)
        # Passing the parse in means the trace itself is never opened again.
        pages = write_directory_index(trace, self.output, source_root=self.root,
                                      coverage_by_path=coverage_by_path)
        self.assertTrue(pages >= 1)
        self.assertIn('Vector.h', self.page('Source/WTF/wtf/index.html'))

    def test_write_report_returns_the_page_and_byte_counts(self):
        self.write_source('Source/WTF/wtf/Vector.h')
        self.write_source('Source/WTF/wtf/HashMap.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h', 'Source/WTF/wtf/HashMap.h')
        report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(report.source_pages, 2)
        self.assertEqual(report.skipped_paths, {})
        self.assertTrue(report.source_bytes > 0)
        self.assertTrue(report.directory_pages >= 2)


class SuiteColumnTest(_Report):
    """One line-coverage column per suite, beside the combined one."""

    def suites(self):
        # Vector.h: layout only. HashMap.h: neither, and absent from api's trace, which differs from uncovered there.
        vector = os.path.join(self.root, 'Source/WTF/wtf/Vector.h')
        hash_map = os.path.join(self.root, 'Source/WTF/wtf/HashMap.h')
        return [('layout', {vector: (2, 1), hash_map: (2, 0)}),
                ('api', {vector: (2, 0)})]

    def build(self):
        self.write_source('Source/WTF/wtf/Vector.h')
        self.write_source('Source/WTF/wtf/HashMap.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h', 'Source/WTF/wtf/HashMap.h')
        write_report(trace, self.output, source_root=self.root, workers=1,
                     suite_line_totals=self.suites())
        return self.page('Source/WTF/wtf/index.html')

    def test_each_suite_gets_a_heading_in_the_order_it_was_given(self):
        page = self.build()
        self.assertLess(page.index('>layout %<'), page.index('>api %<'))
        self.assertIn('>All suites %<', page)
        self.assertNotIn('>Lines %<', page)

    def test_the_columns_are_where_the_sort_script_says_they_are(self):
        # The sort script addresses cells by index, so a mismatched data-col sorts the wrong column.
        page = self.build()
        headings = re.findall(r'<th class="[^"]*" data-col="(\d+)" data-numeric="\d" scope="col">'
                              r'<button type="button">([^<]*)</button></th>', page)
        self.assertEqual([label for _, label in headings],
                         ['Name', 'Line coverage', 'All suites %', 'Lines', 'Uncovered',
                          'layout %', 'api %', 'Functions %', 'Branches %', 'Not built'])
        self.assertEqual([int(column) for column, _ in headings], list(range(len(headings))))
        row = re.search(r'<tr><td class="file" data-v="Vector\.h".*?</tr>', page).group(0)
        self.assertEqual(row.count('<td'), len(headings))

    def test_a_suite_with_no_record_for_a_file_shows_no_number(self):
        # Not 0.00%: a file that suite's profile says nothing about has no denominator there.
        page = self.build()
        row = re.search(r'<tr><td class="file" data-v="HashMap\.h".*?</tr>', page).group(0)
        cells = re.findall(r'<td[^>]*>(?:<div[^>]*>(?:<i[^>]*></i>)?</div>)?([^<]*)', row)
        self.assertEqual(cells[5], '0.00%')   # layout, which has a record and covered nothing
        self.assertEqual(cells[6], '-')       # api, which has no record at all

    def test_a_directory_aggregates_each_suite_separately(self):
        page = self.build()
        totals = re.search(r'<tr class="totals">.*?</tr>', page).group(0)
        cells = re.findall(r'<td[^>]*>(?:<div[^>]*>(?:<i[^>]*></i>)?</div>)?([^<]*)', totals)
        # 4 instrumented lines, 2 executed by the merged profile; layout executed 1 of 4, api 0 of the 2 it has.
        self.assertEqual(cells[2], '50.00%')
        self.assertEqual(cells[5], '25.00%')
        self.assertEqual(cells[6], '0.00%')

    def test_without_suites_the_columns_are_unchanged(self):
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('>Lines %<', page)
        self.assertNotIn('All suites', page)

    def test_the_note_says_the_combined_column_is_a_union(self):
        # Because the first thing anybody does with two columns is add them up.
        self.assertIn('not the sum', self.build())

    def test_the_not_built_column_survives_the_suite_columns(self):
        # It is last, so inserting columns moves it, and it is reported once, not per suite: every suite ran the same binaries.
        from webkitpy.coverage_build_inventory import AbsenceReport, AbsentFile
        absence = AbsenceReport()
        absence.total_file_count = 3
        absence.reported_file_count = 1
        absence.compiled_file_count = 2
        absence.add(AbsentFile('Source/WTF/wtf/Touchy.cpp', 'feature-flag-off',
                               'ENABLE_TOUCH_EVENTS', 120))
        absence.add(AbsentFile('Source/WTF/wtf/ThingGtk.cpp', 'other-port', 'GTK', 184))
        self.write_source('Source/WTF/wtf/Vector.h')
        self.write_source('Source/WTF/wtf/HashMap.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h', 'Source/WTF/wtf/HashMap.h')
        write_report(trace, self.output, source_root=self.root, workers=1,
                     absence=absence, suite_line_totals=self.suites())
        page = self.page('index.html')
        totals = re.search(r'<tr class="totals">.*?</tr>', page).group(0)
        cells = re.findall(r'<td[^>]*>(?:<div[^>]*>(?:<i[^>]*></i>)?</div>)?([^<]*)', totals)
        self.assertEqual(cells[5], '25.00%')   # layout, still where the heading says it is
        self.assertEqual(cells[6], '0.00%')    # api
        self.assertEqual(cells[9], '2')        # not built, last and not per suite
        self.assertIn('first-party implementation', page)
        self.assertIn('Not built in this configuration', self.page('Source/WTF/wtf/index.html'))


class GeneratedSourcesTest(_Report):
    """Generated implementation files are in none of the other states: not reported on, not in the not-built denominator, not listed."""

    def absence(self):
        from webkitpy.coverage_build_inventory import AbsenceReport, AbsentFile
        absence = AbsenceReport()
        absence.total_file_count = 3
        absence.reported_file_count = 1
        absence.compiled_file_count = 2
        absence.add(AbsentFile('Source/WTF/wtf/ThingGtk.cpp', 'other-port', 'GTK', 184))
        return absence

    def build_directory(self, generated=('WebCore/JSDocument.cpp', 'WebKit/FooMessages.cpp')):
        build = os.path.join(self.root, 'WebKitBuild', 'Release')
        for relative in generated:
            path = os.path.join(build, 'DerivedSources', relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as handle:
                handle.write('int f();\nint g();\nint h();\n')
        return build

    def report(self, build=None):
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_report(trace, self.output, source_root=self.root, workers=1,
                     absence=self.absence(), build_directory=build)
        return self.page('index.html')

    def test_the_generated_files_get_a_row_of_their_own(self):
        page = self.report(self.build_directory())
        self.assertIn('Generated by the build, excluded from the report', page)
        self.assertIn('data-v="2">2</td>', page)
        self.assertIn('data-v="6">6</td>', page)

    def test_the_caveat_says_they_are_in_neither_count(self):
        page = self.report(self.build_directory())
        self.assertIn('A further 2 files of generated implementation files', page)
        self.assertIn('in neither count', page)

    def test_they_are_not_summed_into_the_not_built_total(self):
        # A different question from "not built": these were built and instrumented, then filtered out.
        page = self.report(self.build_directory())
        self.assertIn('Why 1 file of the tree is not in this report, plus 2 files the build '
                      'generated', page)

    def test_unified_source_bundles_are_not_counted_twice(self):
        # Each bundle only #includes files already counted.
        build = self.build_directory()
        bundles = os.path.join(build, 'DerivedSources', 'WebCore', 'unified-sources')
        os.makedirs(bundles)
        with open(os.path.join(bundles, 'UnifiedSource1.cpp'), 'w') as handle:
            handle.write('#include "JSDocument.cpp"\n')
        self.assertEqual(generated_source_totals(build), (2, 6))

    def test_a_pruned_build_directory_shows_no_row_rather_than_a_zero(self):
        # A pruned build says nothing about generated files; a zero would claim there are none.
        self.assertIsNone(generated_source_totals(os.path.join(self.root, 'WebKitBuild')))
        page = self.report(os.path.join(self.root, 'WebKitBuild'))
        self.assertNotIn('Generated by the build', page)
        self.assertIn('first-party implementation', page)

    def test_no_build_directory_at_all_still_reports_the_third_state(self):
        page = self.report(None)
        self.assertNotIn('Generated by the build', page)
        self.assertIn('Another port only', page)


class EffectiveSourcePrefixTest(unittest.TestCase):
    def test_the_source_root_is_used_when_every_path_is_under_it(self):
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/wtf/Vector.h', '/a/b/Source/WebCore/dom/Node.cpp'], '/a/b'), '/a/b')

    def test_a_trailing_slash_on_the_source_root_is_ignored(self):
        self.assertEqual(effective_source_prefix(['/a/b/Source/x.cpp'], '/a/b/'), '/a/b')

    def test_the_source_root_is_used_even_when_a_path_is_outside_it(self):
        # The common prefix collapses to '/' when the build directory is on another volume, silently dropping the third state.
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/wtf/Vector.h', '/Volumes/Scratch/Build/generated/Foo.h'], '/a/b'),
            '/a/b')

    def test_the_common_prefix_is_used_when_there_is_no_source_root(self):
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/x.h', '/a/b/Source/WTF/y.h']), '/a/b/Source/WTF')


class OutsideTheCheckoutTest(_Report):
    """A covered path that is not under the source root (e.g. copied framework headers) must not move the root."""

    def absence(self):
        from webkitpy.coverage_build_inventory import AbsenceReport, AbsentFile
        absence = AbsenceReport()
        absence.total_file_count = 3
        absence.reported_file_count = 1
        absence.compiled_file_count = 2
        absence.add(AbsentFile('Source/WTF/wtf/ThingGtk.cpp', 'other-port', 'GTK', 184))
        return absence

    def build(self, outside_relative='generated/Copied.h'):
        """A trace with one path under the root and one on another volume."""
        self.write_source('Source/WTF/wtf/Vector.h')
        elsewhere = os.path.join(self.elsewhere, outside_relative)
        os.makedirs(os.path.dirname(elsewhere), exist_ok=True)
        with open(elsewhere, 'w') as handle:
            handle.write('int f();\nint g();\n')
        trace = os.path.join(self.root, 'coverage.lcov')
        with open(trace, 'w') as handle:
            for path in (os.path.join(self.root, 'Source/WTF/wtf/Vector.h'), elsewhere):
                handle.write('SF:{}\nFN:1,_Z1fv\nFNDA:1,_Z1fv\nDA:1,1\nDA:2,0\n'
                             'end_of_record\n'.format(path))
        return trace, elsewhere

    def setUp(self):
        super(OutsideTheCheckoutTest, self).setUp()
        self.elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.elsewhere, ignore_errors=True)

    def test_the_not_built_caveat_survives_a_path_outside_the_checkout(self):
        trace, _ = self.build()
        write_report(trace, self.output, source_root=self.root, workers=1,
                     absence=self.absence())
        page = self.page('index.html')
        self.assertIn('first-party implementation', page)
        self.assertIn('Another port only', page)
        self.assertIn('Not built', page)

    def test_the_residue_hangs_off_one_synthetic_node(self):
        from webkitpy.coverage_source_view import OUTSIDE_SOURCE_ROOT_DIRECTORY
        trace, _ = self.build()
        write_report(trace, self.output, source_root=self.root, workers=1)
        # One row, whose label begins at the synthetic node; its single-child chain collapses onto one page.
        self.assertIn('data-v="{}/'.format(OUTSIDE_SOURCE_ROOT_DIRECTORY), self.page('index.html'))
        self.assertTrue(os.path.isdir(os.path.join(self.output, OUTSIDE_SOURCE_ROOT_DIRECTORY)))
        self.assertTrue(os.path.isfile(os.path.join(self.output, 'Source/WTF/wtf/index.html')))

    def test_the_line_view_of_an_outside_path_is_beside_the_row_that_links_to_it(self):
        trace, _ = self.build()
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(self.broken_links(), [])

    def test_the_totals_still_include_the_outside_path(self):
        # Those headers are product code whose only path is the copy, so they stay in the denominator.
        trace, _ = self.build()
        report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(report.totals['lines'], (4, 2))


class _RankedReport(_Report):
    """A trace whose files have deliberately different amounts of uncovered code."""

    def write_ranked_trace(self, *pairs):
        """pairs are (relative path, uncovered line count). Every file gets one covered line."""
        records = []
        for relative, uncovered in pairs:
            self.write_source(relative, 'int f();\n' * (uncovered + 1))
            lines = ['DA:1,1']
            lines += ['DA:{},0'.format(number) for number in range(2, uncovered + 2)]
            records.append('SF:{}\nFN:1,_Z1fv\nFNDA:1,_Z1fv\n{}\nend_of_record\n'.format(
                os.path.join(self.root, relative), '\n'.join(lines)))
        path = os.path.join(self.root, 'coverage.lcov')
        with open(path, 'w') as handle:
            handle.write(''.join(records))
        return path


class LeastCoveredCardTest(_RankedReport):
    def test_the_root_page_ranks_files_from_every_directory_together(self):
        # The worst file in the tree would otherwise be several pages away, and each page sorts only its own directory.
        trace = self.write_ranked_trace(('Source/WTF/wtf/Small.h', 2),
                                        ('Source/WebCore/dom/Huge.cpp', 40),
                                        ('Source/JavaScriptCore/runtime/Middle.cpp', 9))
        write_report(trace, self.output, source_root=self.root, workers=1)
        card = self.card('index.html', 'Least-covered files')
        order = [card.index('Huge.cpp'), card.index('Middle.cpp'), card.index('Small.h')]
        self.assertEqual(order, sorted(order))

    def test_it_ranks_by_uncovered_lines_and_not_by_percentage(self):
        # A 40-line file at 2% has more untested code than a 2-line file at 33%.
        trace = self.write_ranked_trace(('Source/WTF/wtf/Tiny.h', 2),
                                        ('Source/WebCore/dom/Big.cpp', 40))
        write_report(trace, self.output, source_root=self.root, workers=1)
        card = self.card('index.html', 'Least-covered files')
        self.assertLess(card.index('Big.cpp'), card.index('Tiny.h'))

    def test_a_fully_covered_file_is_not_listed(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Gap.h', 3),
                                        ('Source/WTF/wtf/Done.h', 0))
        write_report(trace, self.output, source_root=self.root, workers=1)
        card = self.card('index.html', 'Least-covered files')
        self.assertIn('Gap.h', card)
        self.assertNotIn('Done.h', card)

    def test_the_card_is_not_on_a_directory_page(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Gap.h', 3))
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertIn('Least-covered files', self.markup('index.html'))
        self.assertNotIn('Least-covered files', self.markup('Source/WTF/wtf/index.html'))

    def test_the_cap_is_stated_rather_than_applied_silently(self):
        from webkitpy.coverage_directory_index import WORST_FILES_LIMIT
        pairs = [('Source/WTF/wtf/F{}.h'.format(index), index + 1)
                 for index in range(WORST_FILES_LIMIT + 5)]
        trace = self.write_ranked_trace(*pairs)
        write_report(trace, self.output, source_root=self.root, workers=1)
        markup = self.markup('index.html')
        self.assertIn('the {:,} with the most uncovered lines'.format(WORST_FILES_LIMIT), markup)
        self.assertIn('of {:,} with any'.format(len(pairs)), markup)

    def test_a_file_with_no_line_view_is_listed_without_a_link(self):
        # A link to a page that was never written is a 404; write_trace does not create the file.
        trace = self.write_trace('Source/WTF/wtf/Gone.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        card = self.card('index.html', 'Least-covered files')
        self.assertIn('Gone.h', card)
        self.assertIn('nosource', card)
        self.assertNotIn('Gone.h.html', card)


class SearchTest(_RankedReport):
    def search_index(self):
        import json
        from webkitpy.coverage_report_assets import SEARCH_DATA_NAME
        with open(os.path.join(self.output, SEARCH_DATA_NAME)) as handle:
            text = handle.read()
        prefix = 'window.COVERAGE_FILES='
        self.assertTrue(text.startswith(prefix))
        return json.loads(text[len(prefix):].rstrip().rstrip(';'))

    def test_the_index_lists_every_file_and_directory_page(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4),
                                        ('Source/WebCore/dom/Document.cpp', 7))
        write_report(trace, self.output, source_root=self.root, workers=1)
        index = self.search_index()
        self.assertIn(['Source/WebCore/dom/Document.cpp', 8, 1, 1], index)
        self.assertIn(['Source/WTF/wtf/Vector.h', 5, 1, 1], index)
        directories = [entry[0] for entry in index if entry[3] == 2]
        self.assertIn('Source/WTF/wtf', directories)
        for directory in directories:
            self.assertTrue(os.path.exists(os.path.join(self.output, directory, 'index.html')), directory)

    def test_the_most_uncovered_file_comes_first(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4),
                                        ('Source/WebCore/dom/Document.cpp', 7))
        write_report(trace, self.output, source_root=self.root, workers=1)
        files = [entry[0] for entry in self.search_index() if entry[3] != 2]
        self.assertEqual(files[0], 'Source/WebCore/dom/Document.cpp')

    def test_a_file_with_no_line_view_is_listed_but_not_linked(self):
        trace = self.write_trace('Source/WTF/wtf/Gone.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertIn(['Source/WTF/wtf/Gone.h', 2, 1, 0], self.search_index())

    def test_a_file_whose_page_is_not_its_name_plus_html_says_where_it_is(self):
        for relative in ('Source/WTF/wtf/index', 'Source/WTF/wtf/a.h'):
            self.write_source(relative)
        trace = self.write_trace('Source/WTF/wtf/index', 'Source/WTF/wtf/a.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        index = self.search_index()
        self.assertIn(['Source/WTF/wtf/index', 2, 1, 1, 'Source/WTF/wtf/index.html.html'], index)
        self.assertIn(['Source/WTF/wtf/a.h', 2, 1, 1], index)

    @unittest.skipUnless(node_binary(), 'node is not installed')
    def test_every_search_result_resolves_to_a_page_in_the_report(self):
        from webkitpy.coverage_report_assets import SEARCH_SCRIPT_NAME
        relatives = ['Source/WTF/wtf/' + name for name in ODD_FILES]
        relatives += ['Source/' + directory + '/Thing.cpp' for directory in ODD_DIRECTORIES]
        for relative in relatives:
            self.write_source(relative)
        write_report(self.write_trace(*relatives), self.output, source_root=self.root, workers=1)
        entries = [entry for entry in self.search_index() if entry[3]]
        self.assertGreater(len(entries), len(relatives))
        with open(os.path.join(self.output, SEARCH_SCRIPT_NAME)) as handle:
            script = handle.read()
        pages = os.path.join(tempfile.mkdtemp(), 'pages.json')
        self.addCleanup(shutil.rmtree, os.path.dirname(pages), ignore_errors=True)
        run_in_node(self, script, "const entries = {};\n"
                    "require('fs').writeFileSync({}, JSON.stringify(entries.map(e => api.href('', e))));".format(
                        json.dumps(entries), json.dumps(pages)))
        with open(pages) as handle:
            links = json.load(handle)
        self.assertEqual(len(links), len(entries))
        for link in links:
            self.assertEqual(urlparse(link).scheme, '', link)
            self.assertTrue(os.path.exists(os.path.join(self.output, unquote(link))), link)

    def test_every_page_has_the_search_box_and_the_filter(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        for relative, up in (('index.html', ''), ('Source/WTF/wtf/index.html', '../../../')):
            page = self.page(relative)
            self.assertIn('id=report-search', page)
            self.assertIn('src="{}coverage-search.js" data-root="{}"'.format(up, up), page)
            self.assertIn('id="filter"', page)
            self.assertIn('id="filter-count"', page)

    def test_the_index_is_not_inlined_into_any_page(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertNotIn('COVERAGE_FILES', self.page('index.html'))

    def test_a_closing_script_tag_in_a_path_cannot_end_the_index_early(self):
        from webkitpy.coverage_report_assets import SEARCH_DATA_NAME, write_search_assets
        write_search_assets(self.output, [('Source/a</script>b.h', 2, 1, 1)])
        with open(os.path.join(self.output, SEARCH_DATA_NAME)) as handle:
            text = handle.read()
        self.assertNotIn('</script>', text)
        self.assertIn('<\\/script>', text)


class SharedAssetsTest(_RankedReport):
    def test_pages_link_one_stylesheet_and_script_instead_of_inlining_them(self):
        from webkitpy.coverage_directory_index import REPORT_SCRIPT_NAME, REPORT_STYLESHEET_NAME
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        for name in (REPORT_STYLESHEET_NAME, REPORT_SCRIPT_NAME):
            self.assertTrue(os.path.exists(os.path.join(self.output, name)), name)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertNotIn('<style>', page)
        self.assertIn('href="../../../coverage-report.css"', page)
        self.assertIn('src="../../../coverage-report.js"', page)

    def test_secondary_columns_can_be_dropped_on_a_narrow_screen(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        page = self.page('Source/WTF/wtf/index.html')
        for css in ('c-fn', 'c-br', 'c-nb'):
            self.assertIn(css, page)


class _Balanced(HTMLParser):
    VOID = {'meta', 'link', 'br', 'hr', 'img', 'input', 'col', 'area', 'base', 'source', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append('</{}> closes {}'.format(tag, self.stack[-1:] or 'nothing'))
        else:
            self.stack.pop()


class DashboardTest(_RankedReport):
    def index_page(self, scope=None, thresholds=None, page='Source/WTF/wtf/index.html'):
        trace = self.write_ranked_trace(('Source/WTF/wtf/A.h', 3), ('Source/WTF/wtf/B.h', 1))
        write_directory_index(trace, self.output, source_root=self.root, scope=scope,
                              thresholds=thresholds)
        return self.markup(page)

    def test_the_page_leads_with_line_function_and_branch_coverage(self):
        page = self.index_page()
        self.assertLess(page.index('aria-label="Coverage summary"'), page.index('id="entries"'))
        self.assertIn('<p class="k">Lines</p><p class="v">33.3%</p>', page)
        self.assertIn('<p class="k">Functions</p><p class="v">100.0%</p>', page)
        self.assertIn('<p class="k">Branches</p><p class="v">-</p>', page)
        self.assertNotIn('<p class="k">Regions</p>', page)

    def test_each_figure_has_a_meter_and_its_counts(self):
        page = self.index_page()
        self.assertIn('<div class="gauge" role="meter" aria-label="Lines coverage" aria-valuemin="0" '
                      'aria-valuemax="100" aria-valuenow="33.3" aria-valuetext="33.3%">'
                      '<i style="width:33.33%"></i></div>', page)
        self.assertIn('<p class="k">Branches</p><p class="v">-</p>'
                      '<div class="gauge" aria-hidden="true">', page)
        self.assertIn('2 of 6 lines covered', page)
        self.assertIn('None instrumented', page)

    def test_it_counts_files_and_executable_lines(self):
        self.assertIn('<p class="k">Files</p><p class="v">2</p><p class="s">6 executable lines</p>',
                      self.index_page())

    def test_the_gate_is_drawn_and_judged_on_the_root_page(self):
        page = self.index_page(thresholds={'lines': 70.0, 'functions': 90.0}, page='index.html')
        self.assertIn('<span class="gate" style="left:70.00%"></span>', page)
        self.assertIn('Gate 70%: not met', page)
        self.assertIn('Gate 90%: met', page)
        self.assertIn('<li class="stat low"><p class="k">Lines</p>', page)
        self.assertIn('<li class="stat ok"><p class="k">Functions</p>', page)

    def test_a_subdirectory_page_shows_its_numbers_with_no_gate_verdict(self):
        # The gate is on the project total, so a directory is not "met" or "not met" against it.
        page = self.index_page(thresholds={'lines': 10.0, 'functions': 90.0})
        self.assertNotIn('Gate', page)
        self.assertNotIn('class="gate"', page)
        self.assertIn('<p class="k">Lines</p><p class="v">33.3%</p>', page)
        self.assertIn('<li class="stat ok"><p class="k">Functions</p>', page)
        self.assertIn('<li class="stat low"><p class="k">Lines</p>', page)

    def test_without_a_gate_the_bands_are_below_50_and_80_and_over(self):
        from webkitpy.coverage_directory_index import _band
        values = (None, 0.0, 49.9, 50.0, 79.9, 80.0, 100.0)
        self.assertEqual([_band(value, None) for value in values],
                         ['', 'low', 'low', 'mid', 'mid', 'ok', 'ok'])
        self.assertEqual(_band(69.9, 70.0), 'low')
        self.assertEqual(_band(70.0, 70.0), 'ok')

    def test_a_selective_run_gets_a_lower_bound_and_no_verdict(self):
        from webkitpy.coverage_scope import CoverageScope
        page = self.index_page(scope=CoverageScope.selective(['wtf']), thresholds={'lines': 70.0})
        self.assertIn('<p class="v">&ge; 33.3%</p>'.replace('&ge;', '≥'), page)
        self.assertIn('2 of 6 lines executed', page)
        self.assertNotIn('Gate', page)
        self.assertNotIn('class="stat low"', page)
        self.assertNotIn('1 uncovered', page)

    def test_regions_are_shown_when_the_data_has_them(self):
        from webkitpy.coverage_directory_index import _Node, _dashboard_html
        node = _Node('')
        node.add('lines', 10, 5)
        node.add('regions', 8, 6)
        html = _dashboard_html(node)
        self.assertIn('<p class="k">Regions</p><p class="v">75.0%</p>', html)
        self.assertLess(html.index('Branches'), html.index('Regions'))

    def test_not_built_files_are_counted_beside_the_size(self):
        from webkitpy.coverage_directory_index import _Node, _dashboard_html
        node = _Node('')
        node.add('lines', 10, 5)
        node.file_count = 3
        node.absent_totals[:] = [4, 90]
        html = _dashboard_html(node)
        self.assertIn('<p class="v">3</p>', html)
        self.assertIn('4 files not built', html)


class TreemapPageTest(_RankedReport):
    def write(self, *pairs, **options):
        trace = self.write_ranked_trace(*pairs)
        write_directory_index(trace, self.output, source_root=self.root, **options)

    def test_a_directory_with_several_children_gets_a_chart_and_a_legend(self):
        self.write(('Source/WTF/wtf/A.h', 3), ('Source/WTF/wtf/B.h', 1))
        page = self.markup('Source/WTF/wtf/index.html')
        self.assertIn('id="treemap"', page)
        self.assertEqual(page.count('class="sw h'), 5)
        self.assertIn('0&ndash;20%', page)
        self.assertIn('80&ndash;100%', page)
        self.assertIn('data-prefix=""', page)
        self.assertNotIn('lower bound', page)

    def test_a_directory_with_fewer_than_two_children_has_none(self):
        self.write(('Source/WTF/wtf/A.h', 3))
        self.assertNotIn('id="treemap"', self.markup('Source/WTF/wtf/index.html'))
        self.assertNotIn('id="treemap"', self.markup('index.html'))

    def test_a_child_with_no_executable_lines_is_not_a_tile(self):
        from webkitpy.coverage_report_assets import treemap_html
        self.assertEqual(treemap_html(1), '')
        self.assertEqual(treemap_html(0), '')
        self.assertIn('id="treemap"', treemap_html(2))

    def test_the_chart_is_hidden_until_the_script_has_drawn_it(self):
        self.write(('Source/WTF/wtf/A.h', 3), ('Source/WTF/wtf/B.h', 1))
        self.assertRegex(self.markup('Source/WTF/wtf/index.html'),
                         r'<section class="chart" id="treemap"[^>]* hidden>')

    def test_a_selective_chart_says_every_percentage_is_a_lower_bound(self):
        from webkitpy.coverage_scope import LOWER_BOUND_PREFIX, CoverageScope
        self.write(('Source/WTF/wtf/A.h', 3), ('Source/WTF/wtf/B.h', 1),
                   scope=CoverageScope.selective(['wtf']))
        page = self.markup('Source/WTF/wtf/index.html')
        self.assertIn('data-prefix="{}"'.format(LOWER_BOUND_PREFIX), page)
        self.assertIn('Every percentage is a lower bound.', page)

    def test_the_chart_reads_its_numbers_from_the_table_so_the_page_carries_none_twice(self):
        self.write(('Source/WTF/wtf/A.h', 3), ('Source/WTF/wtf/B.h', 1))
        page = self.page('Source/WTF/wtf/index.html')
        self.assertNotIn('application/json', page)
        self.assertNotIn('<script>', page)
        self.assertEqual(page.count('class="c-lines"') + page.count('c-lines" data-v='), 3)


class TableMarkupTest(_RankedReport):
    def setUp(self):
        super().setUp()
        trace = self.write_ranked_trace(('Source/WTF/wtf/A.h', 3), ('Source/WTF/wtf/B.h', 1))
        write_directory_index(trace, self.output, source_root=self.root)
        self.html = self.markup('Source/WTF/wtf/index.html')

    def test_headings_are_column_headers_with_a_button_to_sort_by(self):
        headings = re.findall(r'<th [^>]*>.*?</th>', self.html)
        self.assertTrue(headings)
        for heading in headings:
            self.assertIn('scope="col"', heading)
            self.assertRegex(heading, r'<button type="button">[^<]+</button></th>$')
            self.assertNotIn('aria-sort', heading)

    def test_the_table_has_a_name_and_an_id_the_scripts_can_find(self):
        self.assertIn('<table id="entries" data-filterable="1">', self.html)
        self.assertIn('<caption class="vh">Coverage of the contents of Source/WTF/wtf</caption>',
                      self.html)

    def test_the_cells_the_filters_read_are_marked(self):
        row = re.search(r'<tr><td class="file" data-v="A\.h".*?</tr>', self.html).group(0)
        self.assertIn('<td class="n c-lines" data-v="4">', row)
        self.assertIn('<td class="n c-unc" data-v="3">', row)

    def test_there_are_controls_for_the_minimum_size_and_for_hiding_what_is_covered(self):
        self.assertRegex(self.html, r'<label>Min lines\s+<input id="min-lines" type="number" min="0"')
        self.assertIn('<input id="hide-covered" type="checkbox"> Hide fully covered', self.html)
        self.assertIn('id="filter"', self.html)

    def test_the_header_row_sticks_inside_a_card_that_does_not_scroll(self):
        self.assertIn('<div class="card sticky">', self.html)

    def test_a_table_wider_than_the_page_is_left_to_scroll_instead_of_sticking(self):
        # overflow: clip is what lets the header stick, and it would cut off the extra columns.
        totals = {os.path.join(self.root, 'Source/WTF/wtf/A.h'): (4, 1)}
        suites = [(name, totals) for name in ('layout', 'api', 'ipc')]
        write_directory_index(os.path.join(self.root, 'coverage.lcov'), self.output,
                              source_root=self.root, suite_line_totals=suites)
        self.assertNotIn('class="card sticky"', self.markup('Source/WTF/wtf/index.html'))
        self.assertIn('<div class="card">', self.markup('Source/WTF/wtf/index.html'))

    def test_the_markup_is_balanced(self):
        for relative in ('index.html', 'Source/WTF/wtf/index.html'):
            parser = _Balanced()
            parser.feed(self.page(relative))
            self.assertEqual(parser.problems, [], relative)
            self.assertEqual(parser.stack, [], relative)


class ThemeAssetsTest(_RankedReport):
    def setUp(self):
        super().setUp()
        self.trace = self.write_ranked_trace(('Source/WTF/wtf/A.h', 3), ('Source/WTF/wtf/B.h', 1))
        write_directory_index(self.trace, self.output, source_root=self.root)

    def read(self, name):
        with open(os.path.join(self.output, name)) as handle:
            return handle.read()

    def test_the_stylesheet_carries_the_theme(self):
        from webkitpy.coverage_report_theme import THEME_CSS
        self.assertIn(THEME_CSS, self.read('coverage-report.css'))

    def test_the_stylesheet_has_print_reduced_motion_focus_and_narrow_rules(self):
        css = self.read('coverage-report.css')
        for needle in ('@media print', '@media (prefers-reduced-motion: reduce)', ':focus-visible',
                       '@media (max-width: 700px)', '@media (forced-colors: active)'):
            self.assertIn(needle, css)

    def test_every_page_loads_the_theme_script_before_it_paints(self):
        from webkitpy.coverage_report_theme import THEME_SCRIPT_NAME, THEME_TOGGLE_HTML
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('<script src="../../../{}"></script>'.format(THEME_SCRIPT_NAME), page)
        self.assertIn(THEME_TOGGLE_HTML, page)
        self.assertLess(page.index(THEME_SCRIPT_NAME), page.index('</head>'))
        self.assertTrue(os.path.exists(os.path.join(self.output, THEME_SCRIPT_NAME)))

    @unittest.skipUnless(node_binary(), 'node is not installed')
    def test_every_script_the_report_writes_parses(self):
        for name in ('coverage-report.js', 'coverage-theme.js', 'coverage-search.js'):
            result = subprocess.run([node_binary(), '--check', os.path.join(self.output, name)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, name + result.stderr)

    def test_the_report_makes_no_network_reference(self):
        for current, _, names in os.walk(self.output):
            for name in names:
                with open(os.path.join(current, name), errors='replace') as handle:
                    text = handle.read()
                self.assertNotRegex(text, r'https?://', os.path.join(current, name))

    def test_the_output_does_not_change_from_run_to_run(self):
        second = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, second, ignore_errors=True)
        write_directory_index(self.trace, second, source_root=self.root)
        for name in ('index.html', 'Source/WTF/wtf/index.html', 'coverage-report.css',
                     'coverage-report.js', 'coverage-theme.js'):
            with open(os.path.join(second, name)) as handle:
                self.assertEqual(handle.read(), self.read(name), name)

    def test_the_old_property_names_still_resolve_for_the_delta_and_patch_reports(self):
        from webkitpy.coverage_directory_index import REPORT_STYLE
        for old in ('--surface-1', '--page', '--text-primary', '--text-secondary', '--muted',
                    '--gridline', '--border', '--meter-track', '--meter-fill'):
            self.assertRegex(REPORT_STYLE, old + r': var\(--cov-')

    def test_the_gate_reaches_the_root_page_through_write_report_and_no_other(self):
        second = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, second, ignore_errors=True)
        no_views = (0, 0, {})
        with mock.patch('webkitpy.coverage_source_view.write_source_views', return_value=no_views):
            write_report(self.trace, second, source_root=self.root, thresholds={'lines': 60.0})
        with open(os.path.join(second, 'index.html')) as handle:
            self.assertIn('Gate 60%: not met', handle.read())
        with open(os.path.join(second, 'Source/WTF/wtf/index.html')) as handle:
            self.assertNotIn('Gate', handle.read())

    def test_the_toolbar_is_hidden_until_the_script_that_drives_it_has_run(self):
        from webkitpy.coverage_directory_index import REPORT_STYLE
        self.assertIn(':root:not(.cov-js) .toolbar { display: none; }', self.read('coverage-report.css'))
        # The delta and patch reports share REPORT_STYLE and do not load the theme script that sets cov-js.
        self.assertNotIn(':root:not(.cov-js)', REPORT_STYLE)

    def test_shapes_are_edged_so_that_they_do_not_depend_on_their_fill(self):
        css = self.read('coverage-report.css')
        self.assertIn('.tm .t rect { stroke: var(--cov-outline); stroke-width: 1px;', css)
        self.assertRegex(css, r'\.sw \{[^}]*border: 1px solid var\(--cov-outline\)')

    def test_both_writers_write_the_theme_script_through_the_one_function(self):
        from webkitpy.coverage_report_theme import THEME_SCRIPT_NAME, THEME_TOGGLE_JS
        from webkitpy.coverage_source_view import write_source_views
        second = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, second, ignore_errors=True)
        write_source_views({}, second, self.root)
        with open(os.path.join(second, THEME_SCRIPT_NAME)) as handle:
            self.assertEqual(handle.read(), THEME_TOGGLE_JS)
        self.assertEqual(self.read(THEME_SCRIPT_NAME), THEME_TOGGLE_JS)
        with mock.patch('webkitpy.coverage_directory_index.write_theme_script') as index_writer, \
                mock.patch('webkitpy.coverage_source_view.write_theme_script') as view_writer:
            write_directory_index(self.trace, second, source_root=self.root)
            write_source_views({}, second, self.root)
        index_writer.assert_called_once_with(second)
        view_writer.assert_called_once_with(second)


def run_in_node(test, script, harness):
    """Run harness against script's module.exports, under node. harness sees api, assert and same()."""
    directory = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    source = os.path.join(directory, 'script.js')
    with open(source, 'w') as handle:
        handle.write(script)
    harness_path = os.path.join(directory, 'test.js')
    with open(harness_path, 'w') as handle:
        handle.write("const vm = require('vm'), assert = require('assert');\n"
                     "const shim = {exports: {}};\n"
                     "vm.runInNewContext(require('fs').readFileSync(process.argv[2], 'utf8'), {module: shim});\n"
                     "const api = shim.exports;\n"
                     "const same = (a, b) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b));\n" + harness + "\nconsole.log('ok');\n")
    result = subprocess.run([node_binary(), harness_path, source], capture_output=True, text=True)
    test.assertEqual(result.returncode, 0, result.stderr)
    test.assertEqual(result.stdout.strip(), 'ok')


@unittest.skipUnless(node_binary(), 'node is not installed')
class FrontEndScriptsTest(unittest.TestCase):
    """The pure functions in the report's scripts, run under node."""

    def run_script(self, script, harness):
        run_in_node(self, script, harness)

    def test_a_search_result_links_to_a_page_with_every_segment_encoded(self):
        from webkitpy.coverage_report_assets import SEARCH_SCRIPT_NAME, write_search_assets
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        write_search_assets(directory, [])
        with open(os.path.join(directory, SEARCH_SCRIPT_NAME)) as handle:
            script = handle.read()
        self.run_script(script, r"""
          const href = api.href;
          assert.strictEqual(href('', ['javascript:x.cpp', 1, 1, 1]), './javascript%3Ax.cpp.html');
          assert.strictEqual(href('../../', ['a b/c#d?e%.cpp', 1, 1, 1]), '../../a%20b/c%23d%3Fe%25.cpp.html');
          assert.strictEqual(href('', ['Source/odd dir', 1, 1, 2]), './Source/odd%20dir/index.html');
          assert.strictEqual(href('', ['d/index', 2, 1, 1, 'd/index.html.html']), './d/index.html.html');
          assert.strictEqual(href('../', ['na\u00efve.cpp', 1, 1, 1]), '../na%C3%AFve.cpp.html');
        """)

    def test_sorting_is_numeric_stable_and_natural(self):
        from webkitpy.coverage_directory_index import SORT_SCRIPT
        self.run_script(SORT_SCRIPT, r"""
          const order = api.order;
          same(order(['5', '10', '9', '-1'], true, true), [1, 2, 0, 3]);
          same(order(['5', '10', '9', '-1'], true, false), [3, 0, 2, 1]);
          same(order(['2', '1', '2', '1'], true, false), [1, 3, 0, 2]);
          same(order(['2', '1', '2', '1'], true, true), [0, 2, 1, 3]);
          same(order(['', 'abc', '3'], true, false), [0, 1, 2]);
          same(order(['file10', 'file2', 'File1'], false, false), [2, 1, 0]);
          same(order(['b', 'a', 'B'], false, true), [0, 2, 1]);
          same(order([], true, true), []);
          same(order(['9.5', '10.25', '100'], true, false), [0, 1, 2]);
        """)

    def test_the_filters_combine(self):
        from webkitpy.coverage_directory_index import FILTER_SCRIPT
        self.run_script(FILTER_SCRIPT, r"""
          const visible = api.visible;
          const none = {query: '', minimum: 0, hideCovered: false};
          const row = (name, lines, uncovered) => ({name, lines, uncovered});
          assert.ok(visible(row('vector.h', 100, 10), none));
          assert.ok(visible(row('vector.h', 100, 10), {query: 'vec', minimum: 0, hideCovered: false}));
          assert.ok(!visible(row('vector.h', 100, 10), {query: 'map', minimum: 0, hideCovered: false}));
          assert.ok(!visible(row('vector.h', 99, 10), {query: '', minimum: 100, hideCovered: false}));
          assert.ok(visible(row('vector.h', 100, 10), {query: '', minimum: 100, hideCovered: false}));
          assert.ok(!visible(row('done.h', 5, 0), {query: '', minimum: 0, hideCovered: true}));
          assert.ok(visible(row('empty.h', 0, 0), {query: '', minimum: 0, hideCovered: true}));
          assert.ok(!visible(row('empty.h', 0, 0), {query: '', minimum: 1, hideCovered: false}));
          assert.ok(visible(row('gone.cpp', null, null), {query: 'gone', minimum: 50, hideCovered: true}));
          assert.ok(!visible(row('gone.cpp', null, null), {query: 'x', minimum: 0, hideCovered: false}));
        """)

    def test_the_treemap_tiles_the_whole_area_without_overlap(self):
        from webkitpy.coverage_report_assets import TREEMAP_SCRIPT
        self.run_script(TREEMAP_SCRIPT, r"""
          let seed = 7;
          const random = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
          for (let trial = 0; trial < 120; trial++) {
            const count = 1 + Math.floor(random() * 120);
            const values = Array.from({length: count}, () => 1 + Math.floor(Math.pow(random(), 3) * 50000))
                                .sort((a, b) => b - a);
            const w = 280 + Math.floor(random() * 900), h = 200 + Math.floor(random() * 200);
            const rects = api.squarify(values, 0, 0, w, h);
            const total = values.reduce((a, b) => a + b, 0);
            assert.strictEqual(rects.length, count);
            let area = 0;
            rects.forEach((r, i) => {
              assert.ok(r.x >= -1e-6 && r.y >= -1e-6 && r.x + r.w <= w + 1e-6 && r.y + r.h <= h + 1e-6);
              assert.ok(Math.abs(r.w * r.h - values[i] / total * w * h) < 1e-6 * w * h);
              area += r.w * r.h;
            });
            assert.ok(Math.abs(area - w * h) < 1e-6 * w * h);
            for (let i = 0; i < count; i++) for (let j = i + 1; j < count; j++) {
              const a = rects[i], b = rects[j];
              const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
              const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
              assert.ok(!(ox > 1e-6 && oy > 1e-6));
            }
          }
          same(api.squarify([], 0, 0, 100, 100), []);
          same(api.squarify([5], 0, 0, 0, 100), []);
          same(api.squarify([4], 10, 20, 100, 50), [{x: 10, y: 20, w: 100, h: 50}]);
        """)

    def test_the_treemap_is_deterministic_and_keeps_big_tiles_square_ish(self):
        from webkitpy.coverage_report_assets import TREEMAP_SCRIPT
        self.run_script(TREEMAP_SCRIPT, r"""
          const values = [400, 300, 200, 100, 50, 40, 30, 20, 10, 5];
          const first = JSON.stringify(api.squarify(values, 0, 0, 600, 300));
          assert.strictEqual(first, JSON.stringify(api.squarify(values, 0, 0, 600, 300)));
          api.squarify(values, 0, 0, 600, 300).slice(0, 4).forEach(r => {
            assert.ok(Math.max(r.w / r.h, r.h / r.w) < 4);
          });
        """)

    def test_arrow_keys_move_to_the_nearest_tile_in_that_direction(self):
        from webkitpy.coverage_report_assets import TREEMAP_SCRIPT
        self.run_script(TREEMAP_SCRIPT, r"""
          const grid = [{x: 0, y: 0, w: 100, h: 100}, {x: 100, y: 0, w: 100, h: 50},
                        {x: 100, y: 50, w: 100, h: 50}, {x: 0, y: 100, w: 200, h: 50}];
          assert.strictEqual(api.neighbor(grid, 0, 'ArrowRight'), 1);
          assert.strictEqual(api.neighbor(grid, 1, 'ArrowDown'), 2);
          assert.strictEqual(api.neighbor(grid, 2, 'ArrowUp'), 1);
          assert.strictEqual(api.neighbor(grid, 2, 'ArrowLeft'), 0);
          assert.strictEqual(api.neighbor(grid, 0, 'ArrowDown'), 3);
          assert.strictEqual(api.neighbor(grid, 0, 'ArrowLeft'), -1);
          assert.strictEqual(api.neighbor(grid, 3, 'ArrowDown'), -1);
        """)

    def test_coverage_falls_into_five_bins_and_the_label_ink_follows_the_fill(self):
        from webkitpy.coverage_report_assets import TREEMAP_SCRIPT
        self.run_script(TREEMAP_SCRIPT, r"""
          same([0, 19.99, 20, 39.9, 40, 59.9, 60, 79.9, 80, 100].map(api.binOf),
                                 [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]);
          assert.strictEqual(api.inkFor('#1f5f9e'), '#ffffff');
          assert.strictEqual(api.inkFor('#d9c978'), '#111111');
          assert.strictEqual(api.inkFor(' #ffffff'), '#111111');
          assert.strictEqual(api.inkFor('#000'), '#ffffff');
          assert.strictEqual(api.inkFor('not a color'), '#111111');
        """)

    def test_items_too_small_to_see_become_one_tile_and_the_area_stays_honest(self):
        from webkitpy.coverage_report_assets import TREEMAP_SCRIPT
        self.run_script(TREEMAP_SCRIPT, r"""
          const big = (name) => ({name, href: name + '.html', directory: false, lines: 1000, covered: 500});
          const tiny = (name) => ({name, href: name + '.html', directory: false, lines: 1, covered: 1});
          const items = [tiny('t1'), big('b'), tiny('t2'), big('a'), tiny('t3'), big('c')];
          const sum = (list) => list.reduce((total, tile) => total + tile.lines, 0);
          const folded = api.arrange(items, 3000);
          same(folded.map(t => t.name), ['a', 'b', 'c', '3 smaller items']);
          assert.ok(folded[3].rest && folded[3].href === null);
          assert.strictEqual(folded[3].lines, 3);
          assert.strictEqual(folded[3].covered, 3);
          assert.strictEqual(sum(folded), sum(items));
          same(api.arrange(items, 3000000).map(t => t.name), ['a', 'b', 'c', 't1', 't2', 't3']);
          same(api.arrange(items.slice(0, 2), 100).map(t => t.name), ['b', 't1']);
        """)

    def test_a_directory_of_thousands_of_files_is_capped_without_losing_any_area(self):
        from webkitpy.coverage_report_assets import TREEMAP_MAX_TILES, TREEMAP_SCRIPT
        self.run_script(TREEMAP_SCRIPT, """
          const items = Array.from({length: %d}, (_, i) => ({name: 'f' + i, href: 'f' + i + '.html',
              directory: false, lines: 1000, covered: 1}));
          const tiles = api.arrange(items, 1e9);
          assert.strictEqual(tiles.length, %d + 1);
          assert.ok(tiles[0].rest);
          assert.strictEqual(tiles[0].name, '100 smaller items');
          const sum = (list) => list.reduce((total, tile) => total + tile.lines, 0);
          assert.strictEqual(sum(tiles), sum(items));
          assert.strictEqual(api.arrange([{name: 'b', lines: 5, covered: 0}, {name: 'a', lines: 5, covered: 0}], 1e6)[0].name, 'a');
        """ % (TREEMAP_MAX_TILES + 100, TREEMAP_MAX_TILES))

    def test_tooltips_and_labels_state_the_figures_and_the_lower_bound(self):
        from webkitpy.coverage_report_assets import TREEMAP_SCRIPT
        self.run_script(TREEMAP_SCRIPT, r"""
          const dir = api.describe({name: 'dom', directory: true, lines: 2000, covered: 1500}, '');
          assert.strictEqual(dir.name, 'dom/');
          assert.strictEqual(dir.percent, '75.0%');
          assert.strictEqual(dir.detail, '1,500 of 2,000 lines');
          assert.strictEqual(dir.label, 'dom/, 75.0% covered, 1,500 of 2,000 lines');
          assert.strictEqual(api.describe({name: 'a.h', lines: 4, covered: 1}, '≥ ').percent, '≥ 25.0%');
          assert.strictEqual(api.fit('short', 10), 'short');
          assert.strictEqual(api.fit('a-very-long-name.cpp', 8), 'a-very-…');
          assert.strictEqual(api.fit('abcdef', 2), '');
        """)


if __name__ == '__main__':
    unittest.main()

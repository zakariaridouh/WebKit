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

from webkitpy.coverage_directory_index import (
    effective_source_prefix, write_directory_index, write_report)


class _Report(unittest.TestCase):
    """A throwaway checkout, an lcov trace over it, and somewhere to write the report."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.output, ignore_errors=True)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)

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


class FileLinkTest(_Report):
    def test_a_file_links_to_its_sibling_line_view(self):
        # Not up and back down into html/coverage/<absolute path>.html, which is where
        # llvm-cov's pages were and is a 404 without them.
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('<a href="Vector.h.html">Vector.h</a>', page)
        self.assertNotIn('html/coverage/', page)
        self.assertTrue(os.path.isfile(os.path.join(self.output, 'Source/WTF/wtf/Vector.h.html')))

    def test_a_file_with_no_line_view_is_rendered_as_text(self):
        # No source on disk, so no page was written and a link would be a link to a 404.
        trace = self.write_trace('Source/WTF/wtf/Gone.h')
        report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(report.source_pages, 0)
        self.assertEqual(report.skipped_paths, {os.path.join(self.root, 'Source/WTF/wtf/Gone.h')})
        page = self.page('Source/WTF/wtf/index.html')
        self.assertNotIn('<a href="Gone.h.html">', page)
        self.assertIn('nosource', page)
        self.assertIn('Gone.h', page)

    def test_a_directory_and_a_file_of_the_same_name_do_not_collide(self):
        self.write_source('Source/WTF/wtf/Vector.h')
        self.write_source('Source/WTF/wtf/Vector/Extra.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h', 'Source/WTF/wtf/Vector/Extra.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('<a href="Vector/index.html">Vector</a>', page)
        self.assertIn('<a href="Vector.h.html">Vector.h</a>', page)


class BreadcrumbTest(_Report):
    def test_a_collapsed_chain_is_not_linked_to_a_page_that_was_never_written(self):
        # A directory whose only content is one subdirectory does not get a page of its own, so
        # WPEPlatform below is rendered as part of WPEPlatform/wpe. A breadcrumb linking to it
        # is a link to nothing, which was 140 broken links in a full-suite report.
        self.write_source('Source/WebKit/WPEPlatform/wpe/View.cpp')
        self.write_source('Source/WebKit/Shared/Cocoa/Thing.mm')
        trace = self.write_trace('Source/WebKit/WPEPlatform/wpe/View.cpp',
                                 'Source/WebKit/Shared/Cocoa/Thing.mm')
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertFalse(os.path.exists(os.path.join(self.output, 'Source/WebKit/WPEPlatform/index.html')))
        page = self.page('Source/WebKit/WPEPlatform/wpe/index.html')
        crumbs = page.split('<p class="crumbs">')[1].split('</p>')[0]
        # Source collapses into Source/WebKit here for the same reason, so the only linkable
        # ancestors are the root and Source/WebKit.
        self.assertIn('<a href="../../../../index.html">All source</a>', crumbs)
        self.assertIn('<a href="../../index.html">WebKit</a>', crumbs)
        self.assertIn('Source', crumbs)
        self.assertNotIn('>Source</a>', crumbs)
        self.assertIn('WPEPlatform', crumbs)
        self.assertNotIn('>WPEPlatform</a>', crumbs)

    def test_every_link_in_every_page_resolves(self):
        for relative in ('Source/WebKit/WPEPlatform/wpe/View.cpp',
                         'Source/WebKit/Shared/Cocoa/Thing.mm',
                         'Source/WTF/wtf/Vector.h',
                         'Source/WTF/wtf/text/AtomString.h'):
            self.write_source(relative)
        trace = self.write_trace('Source/WebKit/WPEPlatform/wpe/View.cpp',
                                 'Source/WebKit/Shared/Cocoa/Thing.mm',
                                 'Source/WTF/wtf/Vector.h',
                                 'Source/WTF/wtf/text/AtomString.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        broken = []
        for dirpath, _, filenames in os.walk(self.output):
            for name in filenames:
                if not name.endswith('.html'):
                    continue
                with open(os.path.join(dirpath, name)) as handle:
                    text = handle.read()
                for link in re.findall(r'(?:href)="([^"#]+)"', text):
                    if not os.path.exists(os.path.normpath(os.path.join(dirpath, link))):
                        broken.append((os.path.join(dirpath, name), link))
        self.assertEqual(broken, [])


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
        self.assertEqual(report.skipped_paths, set())
        self.assertTrue(report.source_bytes > 0)
        # index.html at the root, Source, Source/WTF and Source/WTF/wtf collapse to two pages.
        self.assertTrue(report.directory_pages >= 2)


class SuiteColumnTest(_Report):
    """One line-coverage column per suite, beside the combined one."""

    def suites(self):
        # Vector.h: covered by layout only. HashMap.h: covered by neither, and absent from
        # api's trace altogether, which is not the same thing as being uncovered there.
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
        # And the combined column says what it is, so it cannot be read as one of the suites.
        self.assertIn('>All suites %<', page)
        self.assertNotIn('>Lines %<', page)

    def test_the_columns_are_where_the_sort_script_says_they_are(self):
        # The sort script addresses cells by index, so a heading whose data-col does not match
        # its position sorts a different column than the one that was clicked.
        page = self.build()
        headings = re.findall(r'<th class="[^"]*" data-col="(\d+)" data-numeric="\d">([^<]*)</th>',
                              page)
        self.assertEqual([label for _, label in headings],
                         ['Name', 'Line coverage', 'All suites %', 'Lines', 'Uncovered',
                          'layout %', 'api %', 'Functions %', 'Branches %', 'Not built'])
        self.assertEqual([int(column) for column, _ in headings], list(range(len(headings))))
        row = re.search(r'<tr><td class="file" data-v="Vector\.h".*?</tr>', page).group(0)
        self.assertEqual(row.count('<td'), len(headings))

    def test_a_suite_with_no_record_for_a_file_shows_no_number(self):
        # Not 0.00%. A file that suite's profile says nothing about has no denominator there,
        # which is the same distinction the "not built" column exists to make.
        page = self.build()
        row = re.search(r'<tr><td class="file" data-v="HashMap\.h".*?</tr>', page).group(0)
        cells = re.findall(r'<td[^>]*>(?:<div[^>]*>(?:<i[^>]*></i>)?</div>)?([^<]*)', row)
        self.assertEqual(cells[5], '0.00%')   # layout, which has a record and covered nothing
        self.assertEqual(cells[6], '-')       # api, which has no record at all

    def test_a_directory_aggregates_each_suite_separately(self):
        page = self.build()
        totals = re.search(r'<tr class="totals">.*?</tr>', page).group(0)
        cells = re.findall(r'<td[^>]*>(?:<div[^>]*>(?:<i[^>]*></i>)?</div>)?([^<]*)', totals)
        # 4 instrumented lines over the two files, 2 executed by the merged profile; layout
        # executed 1 of its 4 and api 0 of the 2 it has records for.
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
        # It is last, so inserting columns before it moves it, and it is reported once rather
        # than per suite: every suite ran against the same binaries, so a file this
        # configuration never compiled was not built for any of them.
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


class EffectiveSourcePrefixTest(unittest.TestCase):
    def test_the_source_root_is_used_when_every_path_is_under_it(self):
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/wtf/Vector.h', '/a/b/Source/WebCore/dom/Node.cpp'], '/a/b'), '/a/b')

    def test_a_trailing_slash_on_the_source_root_is_ignored(self):
        self.assertEqual(effective_source_prefix(['/a/b/Source/x.cpp'], '/a/b/'), '/a/b')

    def test_the_common_prefix_is_used_when_a_path_is_outside_the_source_root(self):
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/wtf/Vector.h', '/other/generated/Foo.cpp'], '/a/b'), '/')

    def test_the_common_prefix_is_used_when_there_is_no_source_root(self):
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/x.h', '/a/b/Source/WTF/y.h']), '/a/b/Source/WTF')


if __name__ == '__main__':
    unittest.main()

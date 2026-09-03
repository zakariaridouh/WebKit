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
    effective_source_prefix, generated_source_totals, write_directory_index, write_report)


class _Report(unittest.TestCase):
    """A throwaway checkout, an lcov trace over it, and somewhere to write the report."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.output = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.output, ignore_errors=True)
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)
        # The line-view writer warns when it withholds a page, which one test here provokes
        # deliberately. Keep it off the console rather than disabling it: logging falls back to
        # lastResort and prints to stderr when a record reaches no handler at all.
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
        """The page without its inlined script or JSON payload.

        Both mention every path and every attribute name the markup does, so a substring
        assertion over the whole file answers a different question than it looks like it does.
        """
        return self.page(relative).split('<script>')[0]

    def card(self, relative, heading):
        """One card's table, from its heading to the end of that table."""
        markup = self.markup(relative)
        start = markup.index(heading)
        return markup[start:markup.index('</table>', start)]


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
        from webkitpy.coverage_source_view import UNREADABLE_SOURCE
        trace = self.write_trace('Source/WTF/wtf/Gone.h')
        report = write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertEqual(report.source_pages, 0)
        self.assertEqual(report.skipped_paths,
                         {os.path.join(self.root, 'Source/WTF/wtf/Gone.h'): UNREADABLE_SOURCE})
        page = self.page('Source/WTF/wtf/index.html')
        self.assertNotIn('<a href="Gone.h.html">', page)
        self.assertIn('nosource', page)
        self.assertIn('Gone.h', page)

    def test_the_index_says_why_a_file_has_no_line_view(self):
        # "There is coverage here but no page" is otherwise a dead end for whoever is reading
        # it, and the two reasons want different actions: one is a cleaned build directory, the
        # other is a tree that has moved on since the binaries were built.
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
        # --no-source-views writes the index and no line views at all, and passes the whole set
        # of paths. "The source could not be read" is untrue there, and a tooltip that lies is
        # worse than no tooltip.
        from webkitpy.coverage_source_view import LINE_VIEWS_NOT_WRITTEN
        self.write_source('Source/WTF/wtf/Vector.h')
        trace = self.write_trace('Source/WTF/wtf/Vector.h')
        write_directory_index(trace, self.output, source_root=self.root,
                              unlinkable={os.path.join(self.root, 'Source/WTF/wtf/Vector.h')},
                              unlinkable_reason=LINE_VIEWS_NOT_WRITTEN)
        page = self.page('Source/WTF/wtf/index.html')
        self.assertIn('title="No line view: {}"'.format(LINE_VIEWS_NOT_WRITTEN), page)
        self.assertNotIn('<a href="Vector.h.html">', page)

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
        self.assertEqual(report.skipped_paths, {})
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


class GeneratedSourcesTest(_Report):
    """The fourth state, which is bigger than the third.

    generate-coverage-report excludes /DerivedSources/ from the trace, so the implementation
    files the build generates are in none of the three states the report distinguishes: not
    reported on, not in the universe the not-built denominator comes from, and not in the
    not-built list. 2,217 files and 1,138,068 physical lines on the measured build, against the
    764,144 the not-built caveat carefully accounts for.
    """

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
        # A different question: these were built and instrumented, and the report filters them
        # out. Adding them to "not built" would make that word mean two things.
        page = self.report(self.build_directory())
        self.assertIn('Why 1 file of the tree is not in this report, plus 2 files the build '
                      'generated', page)

    def test_unified_source_bundles_are_not_counted_twice(self):
        # Each bundle is a list of #includes of files already counted, so counting the bundles
        # would count the same code twice: 1,219 bundles on the measured build.
        build = self.build_directory()
        bundles = os.path.join(build, 'DerivedSources', 'WebCore', 'unified-sources')
        os.makedirs(bundles)
        with open(os.path.join(bundles, 'UnifiedSource1.cpp'), 'w') as handle:
            handle.write('#include "JSDocument.cpp"\n')
        self.assertEqual(generated_source_totals(build), (2, 6))

    def test_a_pruned_build_directory_shows_no_row_rather_than_a_zero(self):
        # "None were generated" is not what was measured, and a zero would say it was.
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
        # This used to fall back to the common prefix, which is '/' as soon as the build
        # directory is on another volume -- and write_directory_index then dropped the whole
        # third state with no message.
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/wtf/Vector.h', '/Volumes/Scratch/Build/generated/Foo.h'], '/a/b'),
            '/a/b')

    def test_the_common_prefix_is_used_when_there_is_no_source_root(self):
        self.assertEqual(effective_source_prefix(
            ['/a/b/Source/WTF/x.h', '/a/b/Source/WTF/y.h']), '/a/b/Source/WTF')


class OutsideTheCheckoutTest(_Report):
    """A covered path that is not under the source root must not move the root.

    There is always some: 120 paths in the shipped report are copied framework headers and
    WebKitAdditions sources with no checkout path at all. When the build directory is inside
    the checkout they are under the root anyway; put it on another volume and the tree used to
    re-root at '/', which set absence to None and silently deleted the "23.4% of the tree is
    not built" caveat, the reason card and the Not-built column, while not-built.tsv went on
    being written. Any WEBKIT_OUTPUTDIR outside the checkout did it.
    """

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
        # One row, whose label begins at the synthetic node. Its single-child chain collapses
        # onto one page exactly as Source/WTF/wtf does, so there is no page per level of the
        # absolute path it came from.
        self.assertIn('data-v="{}/'.format(OUTSIDE_SOURCE_ROOT_DIRECTORY), self.page('index.html'))
        self.assertTrue(os.path.isdir(os.path.join(self.output, OUTSIDE_SOURCE_ROOT_DIRECTORY)))
        # And the real tree is still rooted where it was, not at '/'.
        self.assertTrue(os.path.isfile(os.path.join(self.output, 'Source/WTF/wtf/index.html')))

    def test_the_line_view_of_an_outside_path_is_beside_the_row_that_links_to_it(self):
        trace, _ = self.build()
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

    def test_the_totals_still_include_the_outside_path(self):
        # It is coverage the report has: those headers are product code whose only path is the
        # copy. Rooting it somewhere legible must not drop it from the denominator.
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
        # The whole point: the drill-down means the worst file in the tree is otherwise several
        # pages away, and each page only ever sorts its own directory.
        trace = self.write_ranked_trace(('Source/WTF/wtf/Small.h', 2),
                                        ('Source/WebCore/dom/Huge.cpp', 40),
                                        ('Source/JavaScriptCore/runtime/Middle.cpp', 9))
        write_report(trace, self.output, source_root=self.root, workers=1)
        card = self.card('index.html', 'Least-covered files')
        order = [card.index('Huge.cpp'), card.index('Middle.cpp'), card.index('Small.h')]
        self.assertEqual(order, sorted(order))

    def test_it_ranks_by_uncovered_lines_and_not_by_percentage(self):
        # A 40-line file at 2% has more untested code in it than a 2-line file at 33%, and
        # "where should a test go" is a question about lines rather than about ratios.
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
        # One project-wide answer, not the same fifty rows on 1,040 pages.
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
        # Same rule as the directory rows: a link to a page that was never written is a 404.
        # write_trace deliberately does not create the file, so no line view was written.
        trace = self.write_trace('Source/WTF/wtf/Gone.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        card = self.card('index.html', 'Least-covered files')
        self.assertIn('Gone.h', card)
        self.assertIn('nosource', card)
        self.assertNotIn('Gone.h.html', card)


class SearchTest(_RankedReport):
    def test_the_root_page_carries_every_file_so_the_filter_can_reach_one(self):
        # Sorting is not navigation. Without the payload, a file whose name you know is still a
        # guess at which of 1,040 directory pages it is on.
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4),
                                        ('Source/WebCore/dom/Document.cpp', 7))
        write_report(trace, self.output, source_root=self.root, workers=1)
        page = self.page('index.html')
        self.assertIn('window.COVERAGE_ALL_FILES=', page)
        self.assertIn('["Source/WebCore/dom/Document.cpp",8,1,1]', page)
        self.assertIn('["Source/WTF/wtf/Vector.h",5,1,1]', page)

    def test_the_payload_is_on_the_root_page_only(self):
        # It is the one page that needs it, and 780 KB on 1,040 pages would be 800 MB.
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertIn('window.COVERAGE_ALL_FILES=', self.page('index.html'))
        self.assertNotIn('window.COVERAGE_ALL_FILES=', self.page('Source/WTF/wtf/index.html'))

    def test_a_file_with_no_line_view_is_marked_unlinkable_in_the_payload(self):
        trace = self.write_trace('Source/WTF/wtf/Gone.h')
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertIn('["Source/WTF/wtf/Gone.h",2,1,0]', self.page('index.html'))

    def test_every_page_has_the_filter_input(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        for relative in ('index.html', 'Source/WTF/wtf/index.html'):
            self.assertIn('id="filter"', self.page(relative))
            self.assertIn('id="filter-count"', self.page(relative))

    def test_the_search_results_card_is_present_and_empty_until_used(self):
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        markup = self.markup('index.html')
        self.assertIn('<div id="search-results" hidden>', markup)
        self.assertIn('<tbody></tbody>', markup)

    def test_the_cards_the_search_replaces_are_marked(self):
        # Otherwise the least-covered fifty stay on screen beside the results and read as
        # matches that are not matches.
        trace = self.write_ranked_trace(('Source/WTF/wtf/Vector.h', 4))
        write_report(trace, self.output, source_root=self.root, workers=1)
        self.assertIn('data-hide-on-search', self.markup('index.html'))
        self.assertNotIn('data-hide-on-search', self.markup('Source/WTF/wtf/index.html'))

    def test_a_closing_script_tag_in_a_path_cannot_end_the_payload_early(self):
        from webkitpy.coverage_directory_index import _search_data
        data = _search_data([(('Source', 'a</script>b.h'), {'lines': (2, 1)})], '', {})
        self.assertNotIn('</script>', data)
        self.assertIn('<\\/script>', data)


if __name__ == '__main__':
    unittest.main()

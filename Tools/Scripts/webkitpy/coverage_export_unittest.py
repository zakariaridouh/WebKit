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

import csv
import gzip
import json
import os
import shutil
import tempfile
import tracemalloc
import unittest
import xml.etree.ElementTree as ElementTree
from unittest import mock

from webkitpy import coverage_export
from webkitpy.coverage_export import ExportError, FORMATS, Trace, export_trace, parse_formats
from webkitpy.coverage_lcov import parse_lcov, project_totals
from webkitpy.coverage_scope import CoverageScope

ROOT = '/checkout'

VECTOR = (
    'SF:/checkout/Source/WTF/wtf/Vector.h\n'
    'FN:10,_Z1av\n'
    'FN:10,_Z1aIiEvv\n'
    'FN:20,_Z1bv\n'
    'FNDA:5,_Z1av\n'
    'FNDA:0,_Z1aIiEvv\n'
    'FNDA:0,_Z1bv\n'
    'FNF:3\n'
    'FNH:1\n'
    'BRDA:11,0,0,3\n'
    'BRDA:11,0,1,-\n'
    'BRDA:12,e0,0,1\n'
    'BRDA:12,e0,1,2\n'
    'BRF:4\n'
    'BRH:3\n'
    'DA:10,5\n'
    'DA:11,5\n'
    'DA:12,3\n'
    'DA:20,0\n'
    'DA:21,0\n'
    'LF:5\n'
    'LH:3\n'
    'end_of_record\n')

ODD_NAME = 'Source/A & B/<odd>"q\'.cpp'
ODD = ('SF:/checkout/' + ODD_NAME + '\n'
       'FN:3,_Z1cv\nFNDA:2,_Z1cv\nDA:3,2\nDA:4,0\nend_of_record\n')

EMPTY = ('SF:/checkout/Source/WTF/wtf/Empty.h\nFNF:0\nFNH:0\nBRF:0\nBRH:0\nLF:0\nLH:0\n'
         'end_of_record\n')
OUTSIDE = 'SF:/usr/include/outside.h\nDA:1,1\nend_of_record\n'
AT_ROOT = 'SF:/checkout/Root.cpp\nDA:1,1\nDA:2,0\nend_of_record\n'

# A second record for Vector.h, as llvm-cov writes when it reports a path twice.
VECTOR_AGAIN = ('SF:/checkout/Source/WTF/wtf/Vector.h\n'
                'FN:20,_Z1bv\nFNDA:9,_Z1bv\nDA:20,7\nDA:30,1\nend_of_record\n')

TRACE = ODD + VECTOR + EMPTY + OUTSIDE + AT_ROOT


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.output = os.path.join(self.directory, 'export')

    def trace(self, text=TRACE, name='coverage.lcov'):
        path = os.path.join(self.directory, name)
        opener = gzip.open if name.endswith('.gz') else open
        with opener(path, 'wt', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def export(self, formats=FORMATS, text=TRACE, **arguments):
        arguments.setdefault('source_root', ROOT)
        return export_trace(self.trace(text), self.output, formats, **arguments)

    def path(self, name):
        return os.path.join(self.output, name)

    def read(self, name):
        with open(self.path(name), encoding='utf-8', newline='') as handle:
            return handle.read()

    def xml(self, name):
        return ElementTree.parse(self.path(name)).getroot()

    def json(self, name):
        with open(self.path(name), encoding='utf-8') as handle:
            return json.load(handle)


class FormatNamesTest(unittest.TestCase):
    def test_all_is_every_format(self):
        self.assertEqual(parse_formats(['all']), FORMATS)

    def test_names_are_comma_separated_or_repeated_and_come_back_in_a_fixed_order(self):
        self.assertEqual(parse_formats(['json,cobertura', 'csv']), ('cobertura', 'csv', 'json'))

    def test_a_format_given_twice_is_one_format(self):
        self.assertEqual(parse_formats(['csv', 'csv,all']), FORMATS)

    def test_an_unknown_format_is_named_and_the_choices_listed(self):
        with self.assertRaisesRegex(ExportError, "'lcov', which is not one of cobertura, "
                                    "istanbul, sonarqube, csv, json, or all"):
            parse_formats(['csv,lcov'])

    def test_an_empty_list_is_an_error_and_not_no_formats(self):
        for values in ([''], [','], [' ']):
            with self.assertRaisesRegex(ExportError, 'needs a format'):
                parse_formats(values)

    def test_export_trace_refuses_an_unknown_format_before_reading_anything(self):
        with self.assertRaisesRegex(ExportError, "Unknown export format 'xml'"):
            export_trace('/nonexistent.lcov', '/nonexistent/export', ['xml'])


class TraceTest(_Fixture):
    def test_totals_are_the_reports(self):
        path = self.trace(TRACE + VECTOR_AGAIN)
        with Trace(path, ROOT) as trace:
            self.assertEqual(trace.totals, project_totals(
                parse_lcov(path, keep_function_names=False)))

    def test_a_function_is_a_start_line_and_a_branch_is_a_taken_count(self):
        with Trace(self.trace(), ROOT) as trace:
            self.assertEqual(trace.file_totals['Source/WTF/wtf/Vector.h'],
                             {'lines': (5, 3), 'functions': (2, 1), 'branches': (4, 3)})

    def test_the_instantiation_that_ran_names_the_function(self):
        with Trace(self.trace(), ROOT) as trace:
            record = trace.file('Source/WTF/wtf/Vector.h')
            self.assertEqual(record.functions_sorted(), [(10, 5, '_Z1av'), (20, 0, '_Z1bv')])

    def test_equally_executed_instantiations_are_named_by_the_smallest_name(self):
        text = ('SF:/checkout/a.cpp\nFN:1,_Zb\nFN:1,_Za\nFNDA:0,_Zb\nFNDA:0,_Za\nDA:1,0\n'
                'end_of_record\n')
        with Trace(self.trace(text), ROOT) as trace:
            self.assertEqual(trace.file('a.cpp').functions_sorted(), [(1, 0, '_Za')])

    def test_paths_are_relative_to_the_root_inside_it_and_absolute_outside_it(self):
        with Trace(self.trace(), ROOT) as trace:
            self.assertEqual(sorted(trace.file_totals), sorted([
                ODD_NAME, 'Source/WTF/wtf/Vector.h', 'Source/WTF/wtf/Empty.h',
                '/usr/include/outside.h', 'Root.cpp']))

    def test_a_path_that_only_starts_like_the_root_is_outside_it(self):
        text = 'SF:/checkout-other/a.cpp\nDA:1,1\nend_of_record\n'
        with Trace(self.trace(text), ROOT) as trace:
            self.assertEqual(list(trace.file_totals), ['/checkout-other/a.cpp'])

    def test_without_a_root_every_path_stays_as_it_is(self):
        with Trace(self.trace(), None) as trace:
            self.assertIn('/checkout/Root.cpp', trace.file_totals)
            self.assertIn('/usr/include/outside.h', trace.file_totals)

    def test_two_records_for_one_path_are_unioned_by_maximum(self):
        with Trace(self.trace(VECTOR + VECTOR_AGAIN), ROOT) as trace:
            self.assertEqual(len(trace), 1)
            record = trace.file('Source/WTF/wtf/Vector.h')
            self.assertEqual(dict(record.lines()), {10: 5, 11: 5, 12: 3, 20: 7, 21: 0, 30: 1})
            self.assertEqual(record.functions_sorted(), [(10, 5, '_Z1av'), (20, 9, '_Z1bv')])
            self.assertEqual(trace.file_totals['Source/WTF/wtf/Vector.h']['lines'], (6, 5))

    def test_files_come_back_by_path_or_grouped_by_directory(self):
        text = ''.join('SF:/checkout/{}\nDA:1,1\nend_of_record\n'.format(name) for name in (
            'a/b.cpp', 'a/b/z.cpp', 'a/c.cpp', 'a-b/x.cpp'))
        with Trace(self.trace(text), ROOT) as trace:
            self.assertEqual([record.path for record in trace.files()],
                             ['a-b/x.cpp', 'a/b.cpp', 'a/b/z.cpp', 'a/c.cpp'])
            # '.' sorts before '/', so by path alone a/b.cpp and a/c.cpp are not adjacent.
            self.assertEqual([record.path for record in trace.files(directory_order=True)],
                             ['a/b.cpp', 'a/c.cpp', 'a-b/x.cpp', 'a/b/z.cpp'])

    def test_a_gzipped_trace_is_read_whatever_it_is_called(self):
        with Trace(self.trace(name='renamed.lcov.gz'), ROOT) as trace:
            self.assertEqual(len(trace), 5)

    def test_a_record_with_no_end_is_an_error_and_not_a_shorter_report(self):
        for text in (VECTOR[:-len('end_of_record\n')], VECTOR[:-len('end_of_record\n')] + ODD):
            with self.assertRaisesRegex(ExportError, 'no end_of_record'):
                Trace(self.trace(text), ROOT)

    def test_it_is_stricter_than_parse_lcov_which_drops_a_record_with_no_end(self):
        text = 'SF:/c/a.cpp\nDA:1,1\nSF:/c/b.cpp\nDA:1,0\nend_of_record\nSF:/c/d.cpp\nDA:1,1\n'
        path = self.trace(text)
        self.assertEqual(list(parse_lcov(path)), ['/c/b.cpp'])
        with self.assertRaisesRegex(ExportError, 'a.cpp has no end_of_record'):
            Trace(path, '/c')

    def test_an_empty_trace_is_an_error(self):
        for text in ('', '# nothing\nTN:\n'):
            with self.assertRaisesRegex(ExportError, 'contained no coverage records'):
                Trace(self.trace(text), ROOT)

    def test_function_lines_that_arrive_before_their_declaration_still_count(self):
        text = 'SF:/checkout/a.cpp\nFNDA:4,_Z1fv\nFN:7,_Z1fv\nDA:7,4\nend_of_record\n'
        with Trace(self.trace(text), ROOT) as trace:
            self.assertEqual(trace.file('a.cpp').functions_sorted(), [(7, 4, '_Z1fv')])

    def test_a_branch_that_names_no_line_is_dropped(self):
        text = 'SF:/checkout/a.cpp\nBRDA:x,0,0,1\nBRDA:3,0,0,1\nDA:3,1\nend_of_record\n'
        with Trace(self.trace(text), ROOT) as trace:
            self.assertEqual(trace.file_totals['a.cpp']['branches'], (1, 1))

    def test_branches_sort_by_line_then_block_then_branch_numerically(self):
        text = ('SF:/checkout/a.cpp\nBRDA:10,0,1,1\nBRDA:9,e10,0,1\nBRDA:9,e2,0,1\n'
                'BRDA:9,e2,10,1\nBRDA:9,e2,2,1\nDA:9,1\nDA:10,1\nend_of_record\n')
        with Trace(self.trace(text), ROOT) as trace:
            self.assertEqual([branch[:3] for branch in trace.file('a.cpp').branches()],
                             [(9, 'e2', 0), (9, 'e2', 2), (9, 'e2', 10), (9, 'e10', 0),
                              (10, '0', 1)])

    def test_blocks_that_differ_only_in_leading_zeros_stay_apart(self):
        text = ('SF:/checkout/a.cpp\nBRDA:9,e2,0,1\nBRDA:9,e02,0,1\nBRDA:9,e2,1,1\n'
                'BRDA:9,e02,1,1\nDA:9,1\nend_of_record\n')
        with Trace(self.trace(text), ROOT) as trace:
            self.assertEqual([branch[:3] for branch in trace.file('a.cpp').branches()],
                             [(9, 'e02', 0), (9, 'e02', 1), (9, 'e2', 0), (9, 'e2', 1)])


class CoberturaTest(_Fixture):
    def export_cobertura(self, **arguments):
        self.export(['cobertura'], **arguments)
        return self.xml('coverage.cobertura.xml')

    def classes(self, root):
        return {cls.get('filename'): cls for cls in root.iter('class')}

    def test_the_root_carries_the_reports_totals(self):
        root = self.export_cobertura()
        self.assertEqual(root.tag, 'coverage')
        self.assertEqual(root.attrib, {
            'line-rate': '0.6000', 'branch-rate': '0.7500', 'lines-covered': '6',
            'lines-valid': '10', 'branches-covered': '3', 'branches-valid': '4',
            'complexity': '0', 'version': '1.0', 'timestamp': '0'})
        self.assertEqual([source.text for source in root.iter('source')], ['/checkout'])

    def test_the_document_declares_the_cobertura_dtd(self):
        self.export(['cobertura'])
        self.assertIn('<!DOCTYPE coverage SYSTEM '
                      '"http://cobertura.sourceforge.net/xml/coverage-04.dtd">',
                      self.read('coverage.cobertura.xml'))

    def test_there_is_one_package_per_directory_named_with_dots(self):
        root = self.export_cobertura()
        packages = {package.get('name'): [cls.get('filename') for cls in package.iter('class')]
                    for package in root.iter('package')}
        self.assertEqual(packages, {
            '.': ['Root.cpp'],
            'Source.A & B': [ODD_NAME],
            'Source.WTF.wtf': ['Source/WTF/wtf/Empty.h', 'Source/WTF/wtf/Vector.h'],
            '.usr.include': ['/usr/include/outside.h'],
        })

    def test_elements_come_in_the_orders_the_dtd_requires(self):
        root = self.export_cobertura()
        self.assertEqual([child.tag for child in root], ['sources', 'packages'])
        for cls in root.iter('class'):
            self.assertEqual([child.tag for child in cls], ['methods', 'lines'])
        for method in root.iter('method'):
            self.assertEqual([child.tag for child in method], ['lines'])

    def test_a_class_has_a_line_per_da_record_and_a_method_per_function_start(self):
        cls = self.classes(self.export_cobertura())['Source/WTF/wtf/Vector.h']
        self.assertEqual(cls.get('name'), 'Vector.h')
        self.assertEqual([(line.get('number'), line.get('hits')) for line in cls.find('lines')],
                         [('10', '5'), ('11', '5'), ('12', '3'), ('20', '0'), ('21', '0')])
        methods = [(method.get('name'), method.get('line-rate'),
                    method.find('lines/line').attrib['number'],
                    method.find('lines/line').attrib['hits'])
                   for method in cls.find('methods')]
        self.assertEqual(methods, [('_Z1av', '1.0000', '10', '5'), ('_Z1bv', '0.0000', '20', '0')])

    def test_a_line_with_branches_says_how_many_were_taken(self):
        cls = self.classes(self.export_cobertura())['Source/WTF/wtf/Vector.h']
        lines = {line.get('number'): line for line in cls.find('lines')}
        self.assertEqual(lines['11'].attrib, {'number': '11', 'hits': '5', 'branch': 'true',
                                              'condition-coverage': '50% (1/2)'})
        self.assertEqual(lines['12'].get('condition-coverage'), '100% (2/2)')
        self.assertEqual(lines['10'].get('branch'), 'false')
        self.assertIsNone(lines['10'].get('condition-coverage'))

    def test_a_file_with_nothing_to_cover_is_a_class_whose_rates_are_one(self):
        cls = self.classes(self.export_cobertura())['Source/WTF/wtf/Empty.h']
        self.assertEqual((cls.get('line-rate'), cls.get('branch-rate')), ('1.0000', '1.0000'))
        self.assertEqual(len(cls.find('lines')), 0)
        self.assertEqual(len(cls.find('methods')), 0)

    def test_package_rates_are_over_the_directory(self):
        root = self.export_cobertura()
        wtf = [package for package in root.iter('package')
               if package.get('name') == 'Source.WTF.wtf'][0]
        self.assertEqual((wtf.get('line-rate'), wtf.get('branch-rate')), ('0.6000', '0.7500'))

    def test_special_characters_are_escaped_and_round_trip(self):
        self.export(['cobertura'])
        text = self.read('coverage.cobertura.xml')
        self.assertIn('Source/A &amp; B/&lt;odd&gt;&quot;q\'.cpp', text)
        self.assertIn(ODD_NAME, self.classes(self.xml('coverage.cobertura.xml')))

    def test_characters_xml_cannot_hold_are_replaced_and_not_written(self):
        text = 'SF:/checkout/bad\x01name\x0b.cpp\nDA:1,1\nend_of_record\n'
        self.export(['cobertura', 'sonarqube'], text=text)
        self.assertIn('bad�name�.cpp', self.classes(self.xml('coverage.cobertura.xml')))
        self.assertEqual(self.xml('sonar-generic-coverage.xml').find('file').get('path'),
                         'bad�name�.cpp')

    def test_a_tab_or_newline_in_a_path_survives_attribute_normalisation(self):
        text = 'SF:/checkout/a\tb.cpp\nDA:1,1\nend_of_record\n'
        self.export(['cobertura'], text=text)
        self.assertIn('a\tb.cpp', self.classes(self.xml('coverage.cobertura.xml')))

    def test_hits_saturate_at_what_java_and_dotnet_consumers_parse(self):
        text = ('SF:/checkout/a.cpp\nFN:1,_Z1fv\nFNDA:9999999999,_Z1fv\nDA:1,9999999999\n'
                'DA:2,2147483647\nDA:3,0\nend_of_record\n')
        self.export(['cobertura', 'sonarqube', 'json'], text=text)
        cls = self.classes(self.xml('coverage.cobertura.xml'))['a.cpp']
        self.assertEqual([line.get('hits') for line in cls.find('lines')],
                         ['2147483647', '2147483647', '0'])
        self.assertEqual(cls.find('methods/method/lines/line').get('hits'), '2147483647')
        self.assertEqual(self.json('coverage.json')['files']['a.cpp']['lines']['1'], 9999999999)

    def test_the_timestamp_is_zero_unless_asked_for(self):
        self.assertEqual(self.export_cobertura().get('timestamp'), '0')
        with mock.patch.dict(os.environ, {'SOURCE_DATE_EPOCH': '1700000000'}):
            self.assertEqual(self.export_cobertura().get('timestamp'), '1700000000000')
        for value in ('soon', '\u00b2', ''):
            with mock.patch.dict(os.environ, {'SOURCE_DATE_EPOCH': value}):
                self.assertEqual(self.export_cobertura().get('timestamp'), '0')
        self.assertEqual(self.export_cobertura(timestamp=42).get('timestamp'), '42')

    def test_the_output_is_byte_for_byte_repeatable(self):
        self.export(['cobertura'])
        first = self.read('coverage.cobertura.xml')
        self.export(['cobertura'], text=''.join(reversed([ODD, VECTOR, EMPTY, OUTSIDE, AT_ROOT])))
        self.assertEqual(self.read('coverage.cobertura.xml'), first)

    def test_without_a_source_root_every_path_is_absolute_and_the_source_is_the_root(self):
        self.export(['cobertura'], source_root=None)
        root = self.xml('coverage.cobertura.xml')
        self.assertIn('/checkout/Root.cpp', self.classes(root))
        self.assertEqual([source.text for source in root.iter('source')], ['/'])


class IstanbulTest(_Fixture):
    def setUp(self):
        super().setUp()
        self.export(['istanbul'])
        self.final = self.json('coverage-final.json')
        self.summary = self.json('coverage-summary.json')
        self.vector = self.final['Source/WTF/wtf/Vector.h']

    def test_a_file_has_the_seven_members_istanbul_lib_coverage_requires(self):
        for data in self.final.values():
            for key in ('path', 'statementMap', 's', 'fnMap', 'f', 'branchMap', 'b'):
                self.assertIn(key, data)
        self.assertEqual(self.vector['path'], 'Source/WTF/wtf/Vector.h')

    def test_statements_are_the_da_lines(self):
        lines = {self.vector['statementMap'][key]['start']['line']: count
                 for key, count in self.vector['s'].items()}
        self.assertEqual(lines, {10: 5, 11: 5, 12: 3, 20: 0, 21: 0})
        self.assertEqual(sorted(self.vector['s']), sorted(self.vector['statementMap']))
        self.assertEqual(self.vector['statementMap']['0'], {
            'start': {'line': 10, 'column': 0}, 'end': {'line': 10, 'column': 2 ** 31 - 1}})

    def test_functions_are_start_lines(self):
        self.assertEqual({key: (meta['name'], meta['line'], meta['decl']['start']['line'])
                          for key, meta in self.vector['fnMap'].items()},
                         {'0': ('_Z1av', 10, 10), '1': ('_Z1bv', 20, 20)})
        self.assertEqual(self.vector['f'], {'0': 5, '1': 0})

    def test_branches_group_by_line_and_block(self):
        self.assertEqual(self.vector['b'], {'0': [3, 0], '1': [1, 2]})
        for key, meta in self.vector['branchMap'].items():
            self.assertEqual(len(meta['locations']), len(self.vector['b'][key]))
            self.assertEqual(meta['line'], meta['loc']['start']['line'])
        self.assertEqual([meta['line'] for meta in self.vector['branchMap'].values()], [11, 12])

    def test_a_file_with_nothing_in_it_is_still_a_valid_file_coverage(self):
        empty = self.final['Source/WTF/wtf/Empty.h']
        for key in ('statementMap', 's', 'fnMap', 'f', 'branchMap', 'b'):
            self.assertEqual(empty[key], {})

    def test_the_summary_starts_with_the_total_and_has_every_file(self):
        self.assertEqual(list(self.summary)[0], 'total')
        self.assertEqual(sorted(self.summary), sorted(['total'] + list(self.final)))

    def test_summary_counts_and_percentages(self):
        self.assertEqual(self.summary['Source/WTF/wtf/Vector.h'], {
            'lines': {'total': 5, 'covered': 3, 'skipped': 0, 'pct': 60},
            'statements': {'total': 5, 'covered': 3, 'skipped': 0, 'pct': 60},
            'functions': {'total': 2, 'covered': 1, 'skipped': 0, 'pct': 50},
            'branches': {'total': 4, 'covered': 3, 'skipped': 0, 'pct': 75}})
        self.assertEqual(self.summary['total']['lines'],
                         {'total': 10, 'covered': 6, 'skipped': 0, 'pct': 60})

    def test_percentages_floor_as_istanbul_does(self):
        self.assertEqual(coverage_export._istanbul_percent(2, 3), 66.66)
        self.assertEqual(coverage_export._istanbul_percent(1, 3), 33.33)
        self.assertEqual(coverage_export._istanbul_percent(3, 3), 100)
        self.assertEqual(coverage_export._istanbul_percent(0, 3), 0)

    def test_nothing_to_cover_is_a_hundred_percent_as_istanbul_has_it(self):
        self.assertEqual(self.summary['Source/WTF/wtf/Empty.h']['branches'],
                         {'total': 0, 'covered': 0, 'skipped': 0, 'pct': 100})


class SonarQubeTest(_Fixture):
    def setUp(self):
        super().setUp()
        self.export(['sonarqube'])
        self.root = self.xml('sonar-generic-coverage.xml')
        self.files = {file.get('path'): file for file in self.root.findall('file')}

    def test_the_root_is_a_version_1_coverage_element(self):
        self.assertEqual((self.root.tag, self.root.attrib), ('coverage', {'version': '1'}))

    def test_every_file_is_listed_in_path_order(self):
        paths = [file.get('path') for file in self.root.findall('file')]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), 5)

    def test_lines_say_whether_they_ran(self):
        lines = [(line.get('lineNumber'), line.get('covered'))
                 for line in self.files['Source/WTF/wtf/Vector.h']]
        self.assertEqual(lines, [('10', 'true'), ('11', 'true'), ('12', 'true'),
                                 ('20', 'false'), ('21', 'false')])

    def test_a_line_with_branches_counts_them(self):
        lines = {line.get('lineNumber'): line.attrib
                 for line in self.files['Source/WTF/wtf/Vector.h']}
        self.assertEqual(lines['11']['branchesToCover'], '2')
        self.assertEqual(lines['11']['coveredBranches'], '1')
        self.assertNotIn('branchesToCover', lines['10'])

    def test_a_file_with_nothing_to_cover_is_an_empty_element(self):
        self.assertEqual(len(self.files['Source/WTF/wtf/Empty.h']), 0)

    def test_paths_are_escaped(self):
        self.assertIn(ODD_NAME, self.files)
        self.assertIn('Source/A &amp; B/&lt;odd&gt;&quot;q\'.cpp',
                      self.read('sonar-generic-coverage.xml'))

    def test_a_line_that_never_ran_is_a_line_to_cover_and_is_not_covered(self):
        self.export(['sonarqube'], text='SF:/checkout/a.cpp\nDA:1,0\nDA:2,3\nDA:3,0\nend_of_record\n'
                    'SF:/checkout/b.cpp\nDA:1,0\nend_of_record\n')
        files = {file.get('path'): [(line.get('lineNumber'), line.get('covered')) for line in file]
                 for file in self.xml('sonar-generic-coverage.xml').findall('file')}
        self.assertEqual(files, {'a.cpp': [('1', 'false'), ('2', 'true'), ('3', 'false')],
                                 'b.cpp': [('1', 'false')]})

    def test_line_zero_is_not_a_line_sonar_accepts(self):
        self.export(['sonarqube'], text='SF:/checkout/a.cpp\nDA:0,1\nDA:2,1\nend_of_record\n')
        lines = self.xml('sonar-generic-coverage.xml').findall('file/lineToCover')
        self.assertEqual([line.get('lineNumber') for line in lines], ['2'])


class CsvTest(_Fixture):
    def setUp(self):
        super().setUp()
        self.export(['csv'])
        self.text = self.read('coverage.csv')
        self.rows = list(csv.reader(self.text.splitlines(), strict=True))

    def test_the_header_is_fixed(self):
        self.assertEqual(','.join(self.rows[0]), 'path,lines_total,lines_covered,lines_pct,'
                         'functions_total,functions_covered,functions_pct,branches_total,'
                         'branches_covered,branches_pct')

    def test_rows_end_in_crlf_as_rfc_4180_has_it(self):
        self.assertTrue(self.text.endswith('\r\n'))
        self.assertEqual(self.text.count('\r\n'), self.text.count('\n'))

    def test_there_is_one_row_per_file_in_path_order(self):
        paths = [row[0] for row in self.rows[1:]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), 5)

    def test_counts_and_percentages(self):
        rows = {row[0]: row for row in self.rows[1:]}
        self.assertEqual(rows['Source/WTF/wtf/Vector.h'][1:],
                         ['5', '3', '60.00', '2', '1', '50.00', '4', '3', '75.00'])

    def test_nothing_to_cover_leaves_the_percentage_empty(self):
        rows = {row[0]: row for row in self.rows[1:]}
        self.assertEqual(rows['Source/WTF/wtf/Empty.h'][1:],
                         ['0', '0', '', '0', '0', '', '0', '0', ''])

    def test_a_path_with_a_comma_or_a_quote_is_quoted(self):
        self.assertIn('"Source/A & B/<odd>""q\'.cpp"', self.text)
        self.export(['csv'], text='SF:/checkout/a,b.cpp\nDA:1,1\nend_of_record\n')
        self.assertTrue(self.read('coverage.csv').splitlines()[1].startswith('"a,b.cpp",1,1,'))


class JsonTest(_Fixture):
    def setUp(self):
        super().setUp()
        self.export(['json'])
        self.document = self.json('coverage.json')

    def test_the_document_has_the_documented_members_and_no_timestamp(self):
        self.assertEqual(list(self.document), ['schema', 'scope', 'totals', 'files'])
        self.assertEqual((self.document['schema'], self.document['scope']), (1, 'full-suite'))

    def test_totals(self):
        self.assertEqual(self.document['totals'], {
            'files': 5,
            'lines': {'total': 10, 'covered': 6, 'percent': 60.0},
            'functions': {'total': 3, 'covered': 2, 'percent': 66.67},
            'branches': {'total': 4, 'covered': 3, 'percent': 75.0}})

    def test_lines_are_an_object_from_line_to_hits_in_numeric_order(self):
        lines = self.document['files']['Source/WTF/wtf/Vector.h']['lines']
        self.assertEqual(lines, {'10': 5, '11': 5, '12': 3, '20': 0, '21': 0})
        self.assertEqual(list(lines), ['10', '11', '12', '20', '21'])

    def test_functions_and_branches(self):
        record = self.document['files']['Source/WTF/wtf/Vector.h']
        self.assertEqual(record['functions'], [{'line': 10, 'hits': 5, 'name': '_Z1av'},
                                               {'line': 20, 'hits': 0, 'name': '_Z1bv'}])
        self.assertEqual(record['branches'][1], {'line': 11, 'block': '0', 'branch': 1, 'taken': 0})
        self.assertEqual(record['branches'][2],
                         {'line': 12, 'block': 'e0', 'branch': 0, 'taken': 1})
        self.assertEqual(record['totals']['branches'],
                         {'total': 4, 'covered': 3, 'percent': 75.0})

    def test_nothing_to_cover_is_a_null_percentage_and_empty_collections(self):
        record = self.document['files']['Source/WTF/wtf/Empty.h']
        self.assertEqual(record['lines'], {})
        self.assertEqual((record['functions'], record['branches']), ([], []))
        self.assertIsNone(record['totals']['lines']['percent'])

    def test_a_file_is_one_entry_per_line_so_that_a_diff_shows_which_line_changed(self):
        self.assertIn('        "11": 5,\n', self.read('coverage.json'))

    def test_the_output_is_repeatable(self):
        first = self.read('coverage.json')
        self.export(['json'])
        self.assertEqual(self.read('coverage.json'), first)


class ScopeAndFilesTest(_Fixture):
    def test_the_files_written_are_returned_in_format_order(self):
        written = self.export()
        self.assertEqual([os.path.basename(path) for path in written], [
            'coverage.cobertura.xml', 'coverage-final.json', 'coverage-summary.json',
            'sonar-generic-coverage.xml', 'coverage.csv', 'coverage.json'])
        self.assertEqual(sorted(os.listdir(self.output)),
                         sorted(os.path.basename(path) for path in written))

    def test_only_the_asked_for_formats_are_written(self):
        self.export(['csv', 'json'])
        self.assertEqual(sorted(os.listdir(self.output)), ['coverage.csv', 'coverage.json'])

    def test_a_selective_run_says_so_in_every_name_and_where_a_format_can_hold_it(self):
        scope = CoverageScope.selective(['fast/forms'], tests_run=3, tests_in_suite=100)
        written = self.export(scope=scope)
        self.assertEqual([os.path.basename(path) for path in written], [
            'coverage-selective.cobertura.xml', 'coverage-final-selective.json',
            'coverage-summary-selective.json', 'sonar-generic-coverage-selective.xml',
            'coverage-selective.csv', 'coverage-selective.json'])
        self.assertIn('lower bound', self.read('coverage-selective.cobertura.xml'))
        self.assertIn('lower bound', self.read('sonar-generic-coverage-selective.xml'))
        self.xml('coverage-selective.cobertura.xml')
        self.assertEqual(self.json('coverage-selective.json')['scope'], 'selective')

    def test_a_full_suite_run_adds_nothing_to_the_names(self):
        written = self.export(['csv'], scope=CoverageScope.full_suite())
        self.assertEqual([os.path.basename(path) for path in written], ['coverage.csv'])
        self.assertNotIn('lower bound', self.read('coverage.csv'))

    def test_the_formats_agree_with_each_other_on_every_total(self):
        self.export()
        root = self.xml('coverage.cobertura.xml')
        summary = self.json('coverage-summary.json')['total']
        document = self.json('coverage.json')['totals']
        rows = list(csv.reader(self.read('coverage.csv').splitlines()))[1:]
        self.assertEqual(
            (int(root.get('lines-valid')), int(root.get('lines-covered'))),
            (summary['lines']['total'], summary['lines']['covered']))
        self.assertEqual(summary['lines']['total'], document['lines']['total'])
        self.assertEqual(summary['branches']['covered'], document['branches']['covered'])
        self.assertEqual(sum(int(row[1]) for row in rows), document['lines']['total'])
        self.assertEqual(sum(int(row[5]) for row in rows), document['functions']['covered'])
        sonar = self.xml('sonar-generic-coverage.xml').findall('file/lineToCover')
        self.assertEqual(len(sonar), document['lines']['total'])
        self.assertEqual([line.get('covered') for line in sonar].count('true'),
                         document['lines']['covered'])
        self.assertEqual([line.get('covered') for line in sonar].count('false'),
                         document['lines']['total'] - document['lines']['covered'])
        self.assertGreater(document['lines']['total'] - document['lines']['covered'], 0)

    def test_a_file_outside_the_checkout_keeps_its_absolute_path_in_every_format(self):
        written = self.export()
        outside = '/usr/include/outside.h'
        self.assertIn(outside, self.json('coverage.json')['files'])
        self.assertIn(outside, self.json('coverage-final.json'))
        self.assertIn(outside, self.json('coverage-summary.json'))
        self.assertIn(outside, [cls.get('filename')
                                for cls in self.xml('coverage.cobertura.xml').iter('class')])
        self.assertIn(outside, [file.get('path') for file in self.xml('sonar-generic-coverage.xml')])
        self.assertIn(outside, self.read('coverage.csv'))
        for path in written:
            self.assertNotIn('outside-the-checkout', self.read(os.path.basename(path)))

    def test_the_files_of_an_earlier_run_are_removed_and_unknown_files_are_not(self):
        self.export()
        with open(self.path('notes.txt'), 'w') as handle:
            handle.write('mine')
        self.export(['csv'])
        self.assertEqual(sorted(os.listdir(self.output)), ['coverage.csv', 'notes.txt'])

    def test_the_selective_names_of_an_earlier_run_are_removed_too(self):
        scope = CoverageScope.selective(['fast/forms'], tests_run=3, tests_in_suite=100)
        self.export(scope=scope)
        self.export(['json'])
        self.assertEqual(os.listdir(self.output), ['coverage.json'])
        self.export(['csv'], scope=scope)
        self.assertEqual(os.listdir(self.output), ['coverage-selective.csv'])

    def test_a_partial_file_left_by_a_killed_run_is_removed(self):
        self.export(['csv'])
        for name in ('coverage.json.partial', 'coverage-selective.csv.partial'):
            with open(self.path(name), 'w') as handle:
                handle.write('x')
        self.export(['csv'])
        self.assertEqual(os.listdir(self.output), ['coverage.csv'])

    def test_a_trace_that_cannot_be_read_leaves_no_stale_export_behind(self):
        self.export()
        with self.assertRaises(ExportError):
            self.export(text='')
        self.assertEqual(os.listdir(self.output), [])

    def test_an_empty_trace_writes_nothing(self):
        with self.assertRaises(ExportError):
            self.export(text='')
        self.assertEqual(os.listdir(self.output), [])

    def test_a_truncated_gzip_is_an_export_error_naming_the_trace(self):
        path = self.trace(TRACE * 50, name='cut.lcov.gz')
        with open(path, 'rb') as handle:
            data = handle.read()
        with open(path, 'wb') as handle:
            handle.write(data[:len(data) // 2])
        with self.assertRaisesRegex(ExportError, 'cut.lcov.gz'):
            export_trace(path, self.output, FORMATS, source_root=ROOT)

    def test_a_failure_part_way_through_a_file_leaves_no_file_behind(self):
        with mock.patch.object(coverage_export, '_cobertura_class', side_effect=RuntimeError('x')):
            with self.assertRaises(RuntimeError):
                self.export(['cobertura'])
        self.assertEqual(os.listdir(self.output), [])

    def test_an_unwritable_destination_is_an_export_error(self):
        blocker = os.path.join(self.directory, 'file')
        with open(blocker, 'w'):
            pass
        with self.assertRaisesRegex(ExportError, 'Could not export'):
            export_trace(self.trace(), os.path.join(blocker, 'export'), FORMATS)

    def test_the_spool_leaves_nothing_in_the_destination(self):
        self.export()
        self.assertFalse([name for name in os.listdir(self.output) if name.endswith('.partial')])


class StreamingTest(_Fixture):
    def synthetic(self, files=120, lines=1200):
        pieces = []
        for index in range(files):
            pieces.append('SF:/checkout/Source/D{}/F{}.cpp\n'.format(index % 9, index))
            pieces.extend('FN:{0},_ZN1F{1}Ev{0}\nFNDA:{0},_ZN1F{1}Ev{0}\n'.format(n, index)
                          for n in range(1, 60))
            pieces.extend('BRDA:{0},0,{1},{0}\n'.format(n, k) for n in range(1, 200, 2)
                          for k in range(2))
            pieces.extend('DA:{},{}\n'.format(n, n % 3) for n in range(1, lines + 1))
            pieces.append('end_of_record\n')
        return ''.join(pieces)

    def peak(self, function):
        tracemalloc.start()
        try:
            function()
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    def test_memory_follows_the_largest_file_and_not_the_trace(self):
        path = self.trace(self.synthetic())
        exported = self.peak(lambda: export_trace(path, self.output, FORMATS, source_root=ROOT))
        parsed = self.peak(lambda: parse_lcov(path, keep_function_names=False))
        self.assertLess(exported * 4, parsed)

    def test_the_trace_is_not_held_after_it_is_read(self):
        with Trace(self.trace(self.synthetic(files=20)), ROOT) as trace:
            for entries in trace._index.values():
                self.assertTrue(all(isinstance(entry, tuple) and len(entry) == 2
                                    for entry in entries))
            self.assertEqual(len(trace), 20)

    def test_a_large_file_is_exported_whole(self):
        text = 'SF:/checkout/big.cpp\n' + ''.join('DA:{},1\n'.format(n) for n in range(1, 100001))
        self.export(text=text + 'end_of_record\n')
        self.assertEqual(self.json('coverage.json')['totals']['lines']['total'], 100000)
        self.assertEqual(len(self.xml('sonar-generic-coverage.xml').findall('file/lineToCover')),
                         100000)


if __name__ == '__main__':
    unittest.main()

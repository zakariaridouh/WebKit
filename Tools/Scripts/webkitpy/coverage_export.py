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

"""Write the report's canonical lcov trace in formats other coverage tools read.

Every total equals the report's. A function is one start line, named after its most executed
instantiation. Paths are relative to the checkout root when inside it, and absolute otherwise.
Output is sorted and has no timestamps, except Cobertura's, which is 0 unless SOURCE_DATE_EPOCH is set. Branches on a line with no DA
record appear only in the json and istanbul formats. Cobertura hits saturate at 2**31 - 1.

  cobertura   coverage.cobertura.xml: Cobertura 4, one package per directory, one class per file
  istanbul    coverage-final.json (statements are DA lines) and coverage-summary.json
  sonarqube   sonar-generic-coverage.xml: SonarQube "Generic Test Data" coverage, version 1
  csv         coverage.csv: one row per file; an empty percentage means nothing to cover
  json        coverage.json, below

coverage.json, where M is {"total": int, "covered": int, "percent": float or null}:

  {"schema": 1, "scope": "full-suite" | "selective",
   "totals": {"files": int, "lines": M, "functions": M, "branches": M},
   "files": {"<path>": {"totals": {"lines": M, "functions": M, "branches": M},
                        "lines": {"<line>": hits, ...},
                        "functions": [{"line": int, "hits": int, "name": str}, ...],
                        "branches": [{"line": int, "block": str, "branch": int,
                                      "taken": int}, ...]}}}
"""

import contextlib
import csv
import itertools
import json
import marshal
import os
import re
import tempfile
import zlib
from collections import defaultdict

from webkitpy.coverage_lcov import FileCoverage, open_lcov
from webkitpy.coverage_scope import CoverageScope

FORMATS = ('cobertura', 'istanbul', 'sonarqube', 'csv', 'json')

_OUTPUT_NAMES = {
    'cobertura': ('coverage.cobertura.xml',),
    'istanbul': ('coverage-final.json', 'coverage-summary.json'),
    'sonarqube': ('sonar-generic-coverage.xml',),
    'csv': ('coverage.csv',),
    'json': ('coverage.json',),
}

# istanbul-reports clamps a column past the end of a line, but reads a null one as column 0.
_LINE_END_COLUMN = 2 ** 31 - 1
_MAXIMUM_COBERTURA_HITS = 2 ** 31 - 1

_COBERTURA_DOCTYPE = ('<!DOCTYPE coverage SYSTEM '
                      '"http://cobertura.sourceforge.net/xml/coverage-04.dtd">')

_XML_ILLEGAL = re.compile('[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')
_XML_ATTRIBUTE_ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
                          '\t': '&#9;', '\n': '&#10;', '\r': '&#13;'}
_XML_ATTRIBUTE_SPECIALS = re.compile('[&<>"\t\n\r]')
_BLOCK = re.compile(r'(\D*)(\d*)')


class ExportError(Exception):
    pass


def parse_formats(values):
    """The formats --export names, in FORMATS order. `all` is every format."""
    names = [name.strip() for value in values for name in value.split(',')]
    if not any(names):
        raise ExportError('--export needs a format: one of {}, or all'.format(', '.join(FORMATS)))
    chosen = set()
    for name in names:
        if name == 'all':
            chosen.update(FORMATS)
        elif name in FORMATS:
            chosen.add(name)
        else:
            raise ExportError('--export names {!r}, which is not one of {}, or all'.format(
                name, ', '.join(FORMATS)))
    return tuple(name for name in FORMATS if name in chosen)


def _prefer(candidate, current):
    return candidate[0] > current[0] or (candidate[0] == current[0] and candidate[1] < current[1])


def _read_records(lcov_path):
    """Yield (path, lines, {start: (hits, name)}, branches) per record.

    Follows parse_lcov(keep_function_names=False), but is stricter: a record with no end_of_record raises where parse_lcov drops it.
    """
    path = None
    lines = branches = starts = counts = None
    with open_lcov(lcov_path) as handle:
        for line in handle:
            tag = line[:3]
            if tag == 'DA:':
                if lines is not None:
                    number, _, count = line[3:].partition(',')
                    try:
                        lines[int(number)] = int(count)
                    except ValueError:
                        pass
            elif tag == 'SF:':
                if lines is not None:
                    raise ExportError('{}: the record for {} has no end_of_record'.format(
                        lcov_path, path))
                path = line[3:].rstrip('\n')
                lines, branches, starts, counts = {}, {}, {}, {}
            elif tag == 'end':
                if lines is None or not line.startswith('end_of_record'):
                    continue
                functions = {}
                for name, start in starts.items():
                    candidate = (counts.get(name, 0), name)
                    current = functions.get(start)
                    if current is None or _prefer(candidate, current):
                        functions[start] = candidate
                yield path, lines, functions, branches
                lines = None
            elif lines is None:
                continue
            elif tag == 'FND':
                count, _, name = line[5:].rstrip('\n').partition(',')
                try:
                    count = int(count)
                except ValueError:
                    continue
                if count > counts.get(name, -1):
                    counts[name] = count
            elif tag == 'FN:':
                number, _, name = line[3:].rstrip('\n').partition(',')
                try:
                    starts[name] = int(number)
                except ValueError:
                    pass
            elif tag == 'BRD':
                parts = line[5:].rstrip('\n').split(',')
                if len(parts) != 4:
                    continue
                try:
                    int(parts[0])
                    int(parts[2])
                    taken = 0 if parts[3] == '-' else int(parts[3] or 0)
                except ValueError:
                    continue
                branches[(parts[0], parts[1], parts[2])] = taken
    if lines is not None:
        raise ExportError('{}: the record for {} has no end_of_record'.format(lcov_path, path))


def _checkout_path(path, source_root):
    """path relative to source_root when it is inside it, else as given."""
    if source_root and path.startswith(source_root + '/'):
        return path[len(source_root) + 1:]
    return path


def _branch_key(key):
    line, block, branch = key
    prefix, digits = _BLOCK.match(block).groups()
    return int(line), (prefix, int(digits) if digits else -1), block, int(branch)


class TraceFile:
    """One file's coverage: its FileCoverage, and a (hits, name) per function start line."""
    __slots__ = ('path', 'coverage', 'functions')

    def __init__(self, path, lines, functions, branches):
        self.path = path
        self.functions = functions
        self.coverage = FileCoverage()
        self.coverage.lines = lines
        self.coverage.function_lines = {start: hits for start, (hits, _) in functions.items()}
        self.coverage.branches = branches

    def merge(self, other):
        self.coverage.merge(other.coverage)
        for start, candidate in other.functions.items():
            current = self.functions.get(start)
            if current is None or _prefer(candidate, current):
                self.functions[start] = candidate

    def totals(self):
        return self.coverage.totals()

    def lines(self):
        return sorted(self.coverage.lines.items())

    def functions_sorted(self):
        return [(start, hits, name) for start, (hits, name) in sorted(self.functions.items())]

    def branches(self):
        """[(line, block, branch, taken)] in line, block, branch order."""
        keys = sorted(self.coverage.branches, key=_branch_key)
        return [(int(line), block, int(branch), self.coverage.branches[(line, block, branch)])
                for line, block, branch in keys]

    def branches_by_line(self):
        """{line: [branches, taken]}"""
        by_line = defaultdict(lambda: [0, 0])
        for key, taken in self.coverage.branches.items():
            entry = by_line[int(key[0])]
            entry[0] += 1
            entry[1] += 1 if taken else 0
        return by_line


class Trace:
    """An lcov trace read once into a spool file, then served one file at a time. Memory is bounded by the largest file, not the trace."""

    def __init__(self, lcov_path, source_root=None, spool_directory=None):
        self._spool = tempfile.TemporaryFile(dir=spool_directory)
        self._index = defaultdict(list)
        self._size = 0
        self.file_totals = {}
        try:
            self._read(lcov_path, source_root)
        except BaseException:
            self.close()
            raise
        if not self._index:
            self.close()
            raise ExportError('{} contained no coverage records'.format(lcov_path))
        self.totals = {metric: (0, 0) for metric in FileCoverage().totals()}
        for totals in self.file_totals.values():
            for metric, (count, covered) in totals.items():
                self.totals[metric] = (self.totals[metric][0] + count,
                                       self.totals[metric][1] + covered)

    def _read(self, lcov_path, source_root):
        for path, lines, functions, branches in _read_records(lcov_path):
            path = _checkout_path(path, source_root)
            blob = marshal.dumps((lines, functions, branches))
            self._index[path].append((self._size, len(blob)))
            self._spool.write(blob)
            self._size += len(blob)
            if len(self._index[path]) == 1:
                self.file_totals[path] = TraceFile(path, lines, functions, branches).totals()
        for path, entries in self._index.items():
            if len(entries) > 1:
                self.file_totals[path] = self.file(path).totals()

    def close(self):
        self._spool.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()

    def __len__(self):
        return len(self._index)

    def file(self, path):
        merged = None
        for offset, length in self._index[path]:
            self._spool.seek(offset)
            record = TraceFile(path, *marshal.loads(self._spool.read(length)))
            if merged is None:
                merged = record
            else:
                merged.merge(record)
        return merged

    def files(self, directory_order=False):
        """Every file, by path. directory_order keeps a directory's files together."""
        paths = self._index
        if directory_order:
            paths = sorted(paths, key=lambda path: (path.rpartition('/')[0], path))
        else:
            paths = sorted(paths)
        for path in paths:
            yield self.file(path)

    def directory_totals(self):
        """{directory: {metric: (count, covered)}}, with '' for files at the root."""
        directories = defaultdict(lambda: {metric: (0, 0) for metric in self.totals})
        for path, totals in self.file_totals.items():
            entry = directories[path.rpartition('/')[0]]
            for metric, (count, covered) in totals.items():
                entry[metric] = (entry[metric][0] + count, entry[metric][1] + covered)
        return directories


@contextlib.contextmanager
def _output(path, newline='\n'):
    """A text file that only appears under its name once it has been written completely."""
    temporary = path + '.partial'
    try:
        with open(temporary, 'w', encoding='utf-8', newline=newline, buffering=1 << 20) as handle:
            yield handle
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _xml_attribute(value):
    value = _XML_ILLEGAL.sub('\ufffd', value)
    return _XML_ATTRIBUTE_SPECIALS.sub(lambda match: _XML_ATTRIBUTE_ESCAPES[match.group()], value)


def _fraction(covered, count):
    return '{:.4f}'.format(covered / count if count else 1.0)


def _number(value):
    return int(value) if value == int(value) else value


def _percent(covered, count):
    return round(100.0 * covered / count, 2) if count else None


def _istanbul_percent(covered, count):
    return _number((100000 * covered // count) // 10 / 100) if count else 100


def _condition_coverage(taken, count):
    return '{}% ({}/{})'.format(100 * taken // count, taken, count)


def _scope_note(scope):
    if scope is None or not scope.is_selective:
        return ''
    return '<!-- Selective run: every figure here is a lower bound. -->\n'


def _write_cobertura(trace, path, source_root, scope, timestamp):
    lines_total, lines_covered = trace.totals['lines']
    branches_total, branches_covered = trace.totals['branches']
    directories = trace.directory_totals()
    with _output(path) as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n{}\n{}'.format(
            _COBERTURA_DOCTYPE, _scope_note(scope)))
        out.write('<coverage line-rate="{}" branch-rate="{}" lines-covered="{}" lines-valid="{}" '
                  'branches-covered="{}" branches-valid="{}" complexity="0" version="1.0" '
                  'timestamp="{}">\n'.format(
                      _fraction(lines_covered, lines_total),
                      _fraction(branches_covered, branches_total), lines_covered, lines_total,
                      branches_covered, branches_total, timestamp))
        out.write('  <sources>\n    <source>{}</source>\n  </sources>\n  <packages>\n'.format(
            _xml_attribute(source_root or '/')))
        grouped = itertools.groupby(trace.files(directory_order=True),
                                    key=lambda record: record.path.rpartition('/')[0])
        for directory, records in grouped:
            totals = directories[directory]
            out.write('    <package name="{}" line-rate="{}" branch-rate="{}" complexity="0">\n'
                      '      <classes>\n'.format(
                          _xml_attribute(directory.replace('/', '.') or '.'),
                          _fraction(totals['lines'][1], totals['lines'][0]),
                          _fraction(totals['branches'][1], totals['branches'][0])))
            for record in records:
                out.write(_cobertura_class(record))
            out.write('      </classes>\n    </package>\n')
        out.write('  </packages>\n</coverage>\n')


def _cobertura_class(record):
    totals = record.totals()
    branches_by_line = record.branches_by_line()
    rows = ['        <class name="{}" filename="{}" line-rate="{}" branch-rate="{}" '
            'complexity="0">\n          <methods>\n'.format(
                _xml_attribute(record.path.rpartition('/')[2]), _xml_attribute(record.path),
                _fraction(totals['lines'][1], totals['lines'][0]),
                _fraction(totals['branches'][1], totals['branches'][0]))]
    for start, hits, name in record.functions_sorted():
        hits = min(hits, _MAXIMUM_COBERTURA_HITS)
        rows.append('            <method name="{}" signature="" line-rate="{}" '
                    'branch-rate="1.0000"><lines><line number="{}" hits="{}" '
                    'branch="false"/></lines></method>\n'.format(
                        _xml_attribute(name), '1.0000' if hits else '0.0000', start, hits))
    rows.append('          </methods>\n          <lines>\n')
    for number, hits in record.lines():
        hits = min(hits, _MAXIMUM_COBERTURA_HITS)
        if number in branches_by_line:
            count, taken = branches_by_line[number]
            rows.append('            <line number="{}" hits="{}" branch="true" '
                        'condition-coverage="{}"/>\n'.format(
                            number, hits, _condition_coverage(taken, count)))
        else:
            rows.append('            <line number="{}" hits="{}" branch="false"/>\n'.format(
                number, hits))
    rows.append('          </lines>\n        </class>\n')
    return ''.join(rows)


def _istanbul_location(line):
    return ('{{"start":{{"line":{0},"column":0}},"end":{{"line":{0},"column":{1}}}}}'
            .format(line, _LINE_END_COLUMN))


def _write_istanbul(trace, final_path, summary_path):
    with _output(final_path) as out:
        out.write('{\n')
        separator = ''
        for record in trace.files():
            out.write(separator + json.dumps(record.path) + ':' + _istanbul_file(record))
            separator = ',\n'
        out.write('\n}\n')

    def metrics(totals):
        parts = []
        for name, metric in (('lines', 'lines'), ('statements', 'lines'),
                             ('functions', 'functions'), ('branches', 'branches')):
            count, covered = totals[metric]
            parts.append('"{}":{{"total":{},"covered":{},"skipped":0,"pct":{}}}'.format(
                name, count, covered, _istanbul_percent(covered, count)))
        return '{' + ','.join(parts) + '}'

    with _output(summary_path) as out:
        out.write('{\n"total":' + metrics(trace.totals))
        for path in sorted(trace.file_totals):
            out.write(',\n' + json.dumps(path) + ':' + metrics(trace.file_totals[path]))
        out.write('\n}\n')


def _istanbul_file(record):
    lines = record.lines()
    functions = record.functions_sorted()
    branches = record.branches()
    grouped = [(key, list(group)) for key, group in
               itertools.groupby(branches, key=lambda branch: branch[:2])]
    statement_map = ','.join('"{}":{}'.format(index, _istanbul_location(number))
                             for index, (number, _) in enumerate(lines))
    statements = ','.join('"{}":{}'.format(index, hits) for index, (_, hits) in enumerate(lines))
    function_map = ','.join(
        '"{}":{{"name":{},"decl":{},"loc":{},"line":{}}}'.format(
            index, json.dumps(name), _istanbul_location(start), _istanbul_location(start), start)
        for index, (start, _, name) in enumerate(functions))
    function_counts = ','.join('"{}":{}'.format(index, hits)
                               for index, (_, hits, _) in enumerate(functions))
    branch_map = ','.join(
        '"{}":{{"loc":{},"type":"branch","locations":[{}],"line":{}}}'.format(
            index, _istanbul_location(key[0]), ','.join([_istanbul_location(key[0])] * len(group)),
            key[0])
        for index, (key, group) in enumerate(grouped))
    branch_counts = ','.join(
        '"{}":[{}]'.format(index, ','.join(str(branch[3]) for branch in group))
        for index, (_, group) in enumerate(grouped))
    return ('{{"path":{},"statementMap":{{{}}},"s":{{{}}},"fnMap":{{{}}},"f":{{{}}},'
            '"branchMap":{{{}}},"b":{{{}}}}}'.format(
                json.dumps(record.path), statement_map, statements, function_map, function_counts,
                branch_map, branch_counts))


def _write_sonarqube(trace, path, scope):
    with _output(path) as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n{}<coverage version="1">\n'.format(
            _scope_note(scope)))
        for record in trace.files():
            branches_by_line = record.branches_by_line()
            rows = []
            for number, hits in record.lines():
                if number < 1:
                    continue
                if number in branches_by_line:
                    count, taken = branches_by_line[number]
                    rows.append('    <lineToCover lineNumber="{}" covered="{}" '
                                'branchesToCover="{}" coveredBranches="{}"/>\n'.format(
                                    number, 'true' if hits else 'false', count, taken))
                else:
                    rows.append('    <lineToCover lineNumber="{}" covered="{}"/>\n'.format(
                        number, 'true' if hits else 'false'))
            if rows:
                out.write('  <file path="{}">\n{}  </file>\n'.format(
                    _xml_attribute(record.path), ''.join(rows)))
            else:
                out.write('  <file path="{}"/>\n'.format(_xml_attribute(record.path)))
        out.write('</coverage>\n')


_CSV_HEADER = ('path', 'lines_total', 'lines_covered', 'lines_pct', 'functions_total',
               'functions_covered', 'functions_pct', 'branches_total', 'branches_covered',
               'branches_pct')


def _write_csv(trace, path):
    with _output(path, newline='') as out:
        writer = csv.writer(out, lineterminator='\r\n')
        writer.writerow(_CSV_HEADER)
        for file_path in sorted(trace.file_totals):
            row = [file_path]
            for metric in ('lines', 'functions', 'branches'):
                count, covered = trace.file_totals[file_path][metric]
                percent = _percent(covered, count)
                row += [count, covered, '' if percent is None else '{:.2f}'.format(percent)]
            writer.writerow(row)


def _json_metrics(totals):
    return '{{{}}}'.format(', '.join(
        '"{}": {{"total": {}, "covered": {}, "percent": {}}}'.format(
            metric, totals[metric][0], totals[metric][1],
            json.dumps(_percent(totals[metric][1], totals[metric][0])))
        for metric in ('lines', 'functions', 'branches')))


def _json_block(items, opening, closing):
    if not items:
        return opening + closing
    body = ',\n'.join('        ' + item for item in items)
    return '{}\n{}\n      {}'.format(opening, body, closing)


def _json_file(record):
    lines = _json_block(['"{}": {}'.format(number, hits) for number, hits in record.lines()],
                        '{', '}')
    functions = _json_block(
        ['{{"line": {}, "hits": {}, "name": {}}}'.format(start, hits, json.dumps(name))
         for start, hits, name in record.functions_sorted()], '[', ']')
    branches = _json_block(
        ['{{"line": {}, "block": {}, "branch": {}, "taken": {}}}'.format(
            line, json.dumps(block), branch, taken)
         for line, block, branch, taken in record.branches()], '[', ']')
    totals = _json_metrics(record.totals())
    return ('{{\n      "totals": {},\n      "lines": {},\n      "functions": {},\n'
            '      "branches": {}\n    }}'.format(totals, lines, functions, branches))


def _write_json(trace, path, scope):
    scope_name = 'selective' if scope is not None and scope.is_selective else 'full-suite'
    with _output(path) as out:
        out.write('{{\n  "schema": 1,\n  "scope": "{}",\n  "totals": {{"files": {}, {}}},\n'
                  '  "files": {{\n'.format(scope_name, len(trace),
                                           _json_metrics(trace.totals)[1:-1]))
        separator = ''
        for record in trace.files():
            out.write('{}    {}: {}'.format(separator, json.dumps(record.path),
                                            _json_file(record)))
            separator = ',\n'
        out.write('\n  }\n}\n')


def _timestamp_from_environment():
    try:
        return int(os.environ['SOURCE_DATE_EPOCH']) * 1000
    except (KeyError, ValueError):
        return 0


def _remove_previous_exports(directory):
    """Delete what an earlier run left under the names this module writes, in any format and either scope. Other files stay."""
    selective = CoverageScope.selective([])
    for names in _OUTPUT_NAMES.values():
        for name in names:
            for known in (name, selective.filename(name)):
                for stale in (known, known + '.partial'):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(os.path.join(directory, stale))


def export_trace(lcov_path, directory, formats, source_root=None, scope=None, timestamp=None):
    """Write the formats into directory and return the paths written, in FORMATS order. A selective scope puts its infix in every filename.

    The files an earlier run wrote there are removed first, so the directory never mixes runs.
    """
    unknown = [name for name in formats if name not in FORMATS]
    if unknown:
        raise ExportError('Unknown export format {!r}; the formats are {}'.format(
            unknown[0], ', '.join(FORMATS)))
    if timestamp is None:
        timestamp = _timestamp_from_environment()
    source_root = source_root.rstrip('/') if source_root else None

    def named(name):
        return os.path.join(directory, scope.filename(name) if scope is not None else name)

    try:
        os.makedirs(directory, exist_ok=True)
        _remove_previous_exports(directory)
        with Trace(lcov_path, source_root, spool_directory=directory) as trace:
            written = []
            for format_name in FORMATS:
                if format_name not in formats:
                    continue
                paths = [named(name) for name in _OUTPUT_NAMES[format_name]]
                if format_name == 'cobertura':
                    _write_cobertura(trace, paths[0], source_root, scope, timestamp)
                elif format_name == 'istanbul':
                    _write_istanbul(trace, *paths)
                elif format_name == 'sonarqube':
                    _write_sonarqube(trace, paths[0], scope)
                elif format_name == 'csv':
                    _write_csv(trace, paths[0])
                else:
                    _write_json(trace, paths[0], scope)
                written += paths
    except (OSError, EOFError, zlib.error) as error:
        raise ExportError('Could not export {}: {}'.format(lcov_path, error))
    return written

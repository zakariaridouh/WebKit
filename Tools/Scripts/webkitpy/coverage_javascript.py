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

"""Coverage of the JavaScript builtins inside JavaScriptCore, from JSC's control-flow profiler.
Maps $vm.dumpBasicBlockExecutionRanges() output back to files and lines under
Source/JavaScriptCore. See Tools/CodeCoverage/JavaScriptCoverage.md."""

import logging
import os
import re
import subprocess
from collections import namedtuple

logger = logging.getLogger(__name__)

# CMake and Xcode lay out DerivedSources differently; the first candidate that exists wins.
GENERATED_BUILTINS_CANDIDATES = (
    os.path.join('JavaScriptCore', 'DerivedSources', 'JSCBuiltins.cpp'),
    os.path.join('DerivedSources', 'JavaScriptCore', 'JSCBuiltins.cpp'),
)

# inspector/ holds InjectedScriptSource.js, which is also embedded as a builtin. Files the
# generated table does not name are ignored.
BUILTIN_SOURCE_DIRECTORIES = (
    os.path.join('Source', 'JavaScriptCore', 'builtins'),
    os.path.join('Source', 'JavaScriptCore', 'inspector'),
)

# debug() and dataLog() both write to stderr, so a marker cannot be reordered relative to the
# dump it delimits.
CLEAN_PHASE = 'WEBKIT-JS-COVERAGE-CLEAN'
ANCHOR_PHASE = 'WEBKIT-JS-COVERAGE-ANCHOR'
END_PHASE = 'WEBKIT-JS-COVERAGE-END'

# arrayPrototypeForEach is a public JS builtin whose callback runs once per element. The nonce
# is large enough to be implausible as a real block count.
ANCHOR_CODE_NAME = 'arrayPrototypeForEach'
ANCHOR_NONCE = 104729

# useDollarVM is what provides $vm.dumpBasicBlockExecutionRanges().
JSC_PROFILER_OPTIONS = ('--useControlFlowProfiler=true', '--useDollarVM=true')

_COMBINED_CODE_PATTERN = re.compile(
    r'constinit const char s_(?P<namespace>\w+)CombinedCode\[\] = \{ (?P<payload>[^}]*) \};')
_COMBINED_OFFSET_PATTERN = re.compile(
    r'const char\* const s_(?P<name>\w+)Code =\s*s_\w+CombinedCode \+ (?P<offset>\d+)\s*;')
_CODE_LENGTH_PATTERN = re.compile(r'const int s_(?P<name>\w+)CodeLength = (?P<length>\d+);')

# Copied verbatim from builtins_model.py (functionHeadRegExp and the two comment regexps).
_SINGLE_LINE_COMMENT = re.compile(r"\/\/.*?\n", re.MULTILINE | re.DOTALL)
_MULTILINE_COMMENT = re.compile(r"\/\*.*?\*\/", re.MULTILINE | re.DOTALL)
_FUNCTION_HEAD = re.compile(
    r"(?P<annotations>(?:@[\w|=\[\] \"\.]+\s*\n)*)(?:async\s+)?function\s+(?P<name>\w+)\s*\(",
    re.MULTILINE | re.DOTALL)
# Both annotations set is_constructor, which mangledNameForFunction() spells as a
# "Constructor" suffix.
_CONSTRUCTOR_ANNOTATION = re.compile(r'^@(?:naked)?[cC]onstructor', re.MULTILINE)

_DUMP_SOURCE_ID = re.compile(r'^SourceID: (?P<id>\d+)\s*$')
_DUMP_BASIC_BLOCK = re.compile(
    r'^\s*BasicBlock: \[(?P<start>-?\d+), (?P<end>-?\d+)\] '
    r'hasExecuted: (?P<executed>true|false), executionCount:(?P<count>\d+)\s*$')
_DUMP_MARKER = re.compile(r'^--> (?P<name>WEBKIT-JS-COVERAGE-[A-Z-]+)\s*$')


class AttributionError(Exception):
    """The dump could not be tied to the builtins source; raised rather than guessed."""


BasicBlock = namedtuple('BasicBlock', ('start', 'end', 'has_executed', 'execution_count'))


def _substitute_tracking_origins(text, pattern, replacement, origins):
    """Apply pattern -> replacement, tracking each character's original offset in origins.
    The generator's `/*...*/` rewrite deletes newlines, which would shift function line numbers."""
    output = []
    output_origins = []
    at = 0
    for match in pattern.finditer(text):
        output.append(text[at:match.start()])
        output_origins.extend(origins[at:match.start()])
        output.append(replacement)
        output_origins.extend([origins[match.start()]] * len(replacement))
        at = match.end()
    output.append(text[at:])
    output_origins.extend(origins[at:])
    return ''.join(output), output_origins


def _lower_first(name):
    """Mirrors WK_lcfirst in builtins_generator.py."""
    name = name[:1].lower() + name[1:]
    for wrong, right in (('dOM', 'dom'), ('uRL', 'url'), ('jS', 'js'), ('xML', 'xml'),
                         ('xSLT', 'xslt'), ('cSS', 'css'), ('rTC', 'rtc')):
        name = name.replace(wrong, right)
    return name


def _upper_first(name):
    """Mirrors WK_ucfirst in builtins_generator.py."""
    name = name[:1].upper() + name[1:]
    return name.replace('Xml', 'XML').replace('Svg', 'SVG')


def mangled_code_name(object_name, function_name, is_constructor=False):
    """Mirrors BuiltinsGenerator.mangledNameForFunction(): the s_<name>Code symbol of a builtin."""
    name = _upper_first(function_name)
    name = re.sub(r'\.[a-z]', lambda match: match.group(0)[1].upper(), name, flags=re.IGNORECASE)
    if is_constructor:
        name += 'Constructor'
    object_name = re.sub(r'\.[a-z]', lambda match: match.group(0)[1].upper(), object_name,
                         flags=re.IGNORECASE)
    return _lower_first(object_name + name)


def parse_combined_builtins(text):
    """(combined source, {code name: (offset, length)}) from a generated <NS>Builtins.cpp.
    Decoded as latin-1 so that one array element is always one offset."""
    combined_match = _COMBINED_CODE_PATTERN.search(text)
    if not combined_match:
        raise ValueError('No s_<namespace>CombinedCode array found; this is a separate-mode '
                         'builtins file, which has no combined offset space')
    payload = combined_match.group('payload')
    combined = bytes((int(element) & 0xff) for element in payload.split(',')).decode('latin-1')

    offsets = {match.group('name'): int(match.group('offset'))
               for match in _COMBINED_OFFSET_PATTERN.finditer(text)}
    lengths = {match.group('name'): int(match.group('length'))
               for match in _CODE_LENGTH_PATTERN.finditer(text)}
    windows = {}
    for name, offset in offsets.items():
        length = lengths.get(name)
        if length is None:
            logger.debug('%s has an offset but no length in the generated builtins', name)
            continue
        windows[name] = (offset, length)
    return combined, windows


SourceLocation = namedtuple('SourceLocation', ('path', 'function_name', 'line'))


def locate_builtin_sources(checkout_root, directories=BUILTIN_SOURCE_DIRECTORIES):
    """{code name: SourceLocation} for each builtin function in the .js files.
    Walks forward from file and function: a code name cannot be split back unambiguously."""
    claims = {}
    for directory in directories:
        absolute = os.path.join(checkout_root, directory)
        if not os.path.isdir(absolute):
            continue
        for name in sorted(os.listdir(absolute)):
            if not name.endswith('.js'):
                continue
            path = os.path.join(absolute, name)
            try:
                with open(path, 'r', encoding='utf-8') as handle:
                    raw = handle.read()
            except OSError as failure:
                logger.debug('Could not read %s: %s', path, failure)
                continue
            relative = os.path.relpath(path, checkout_root)
            for code_name, location in _locations_in_file(raw, relative).items():
                claims.setdefault(code_name, []).append(location)

    located = {}
    for code_name, locations in claims.items():
        if len(locations) == 1:
            located[code_name] = locations[0]
        else:
            logger.warning('%s is claimed by %d builtins (%s); leaving it unplaced', code_name,
                           len(locations), ', '.join(sorted(entry.path for entry in locations)))
    return located


def _locations_in_file(raw, relative_path):
    object_name = os.path.splitext(os.path.basename(relative_path))[0]
    origins = list(range(len(raw)))
    text, origins = _substitute_tracking_origins(raw, _SINGLE_LINE_COMMENT, '//\n', origins)
    text, origins = _substitute_tracking_origins(text, _MULTILINE_COMMENT, '/**/', origins)

    locations = {}
    for match in _FUNCTION_HEAD.finditer(text):
        annotations = match.group('annotations')
        code_name = mangled_code_name(
            object_name, match.group('name'),
            is_constructor=bool(_CONSTRUCTOR_ANNOTATION.search(annotations)))
        # The embedded source starts at the `function` keyword, not the first annotation.
        keyword = match.start() + len(annotations)
        line = raw.count('\n', 0, origins[keyword]) + 1
        locations[code_name] = SourceLocation(relative_path, match.group('name'), line)
    return locations


def is_code_line(text):
    """True if the line belongs in the denominator: not blank, comment-only or just punctuation.
    A function ending in `return` never runs its closing brace, so braces must not count."""
    text = text.strip()
    if not text or text.startswith(('//', '*', '/*')) or text == '*/':
        return False
    return bool(set(text) - set('{}()[];,'))


class TextLineIndex:
    """Maps profiler offsets to file lines. text[0] sits at base_offset in the blocks' coordinates
    and on file line first_line."""

    __slots__ = ('text', 'base_offset', 'first_line', 'line_starts')

    def __init__(self, text, first_line=1, base_offset=0):
        self.text = text
        self.base_offset = base_offset
        self.first_line = first_line
        self.line_starts = [0] + [index + 1 for index, character in enumerate(text)
                                  if character == '\n']

    @property
    def line_count(self):
        """Lines of text, ignoring a single trailing newline."""
        return self.text.rstrip('\n').count('\n') + 1

    @property
    def last_line(self):
        return self.first_line + self.line_count - 1

    def line_index_for_relative(self, relative):
        """0-based line of an offset within text. A newline belongs to the line it terminates."""
        relative = min(max(relative, 0), max(len(self.text) - 1, 0))
        low, high = 0, len(self.line_starts) - 1
        while low < high:
            middle = (low + high + 1) // 2
            if self.line_starts[middle] <= relative:
                low = middle
            else:
                high = middle - 1
        return low

    def line_for_offset(self, offset):
        return self.first_line + self.line_index_for_relative(offset - self.base_offset)

    def lines_with_text_in(self, start, end):
        """The file lines [start, end] holds real text on, in the blocks' coordinates.
        Blocks often end inside the next line's indentation, so whitespace alone must not count."""
        low = max(start - self.base_offset, 0)
        high = min(end - self.base_offset, len(self.text) - 1)
        if high < low:
            return ()
        first, last = self.line_index_for_relative(low), self.line_index_for_relative(high)
        lines = []
        for index in range(first, last + 1):
            line_start = self.line_starts[index]
            line_end = (self.line_starts[index + 1] - 1 if index + 1 < len(self.line_starts)
                        else len(self.text) - 1)
            if self.text[max(low, line_start):min(high, line_end) + 1].strip():
                lines.append(self.first_line + index)
        return tuple(lines)


class Builtin:
    """One embedded JS function: its window in the combined source and where it came from.
    Without line_mapping_verified, only function-level coverage is reported."""

    __slots__ = ('code_name', 'offset', 'length', 'embedded_source', 'path', 'function_name',
                 'first_line', 'line_mapping_verified', '_file_lines', '_lines')

    def __init__(self, code_name, offset, length, embedded_source, location=None,
                 file_lines=None):
        self.code_name = code_name
        self.offset = offset
        self.length = length
        self.embedded_source = embedded_source
        self.path = location.path if location else None
        self.function_name = location.function_name if location else None
        self.first_line = location.line if location else None
        self._file_lines = file_lines
        self._lines = TextLineIndex(embedded_source, first_line=self.first_line or 1,
                                    base_offset=offset)
        self.line_mapping_verified = self._verify_line_mapping()

    @property
    def end(self):
        return self.offset + self.length

    @property
    def embedded_line_count(self):
        return self._lines.line_count

    @property
    def last_line(self):
        if self.first_line is None:
            return None
        return self.first_line + self.embedded_line_count - 1

    def _verify_line_mapping(self):
        """Does the file's text agree that the builtin spans first_line..last_line?
        The generator's wrapper writes the closing line as `})` where the file has `}`."""
        if self.first_line is None or not self._file_lines:
            return False
        head_index = self.first_line - 1
        last_index = head_index + self.embedded_line_count - 1
        if last_index >= len(self._file_lines):
            return False
        if 'function ' + self.function_name not in self._file_lines[head_index]:
            return False
        return self._file_lines[last_index].strip() in ('}', '})')

    def file_line_for_offset(self, offset):
        """The file line a combined-source offset falls on, or None without a verified mapping."""
        if not self.line_mapping_verified:
            return None
        return self._lines.line_for_offset(offset)

    def lines_substantially_covered_by(self, start, end):
        if not self.line_mapping_verified:
            return ()
        return self._lines.lines_with_text_in(start, end)

    def code_lines(self):
        """The file lines holding code: the report's denominator."""
        if not self.line_mapping_verified:
            return []
        return [number for number in range(self.first_line, self.last_line + 1)
                if is_code_line(self._file_lines[number - 1])]


class BuiltinsIndex:
    """Every builtin the build embedded, placed in a file at a line.
    Built from the build's generated source; an edited .js with a stale build fails verification."""

    def __init__(self, builtins, combined_length, generated_source_path=None, unplaced=()):
        self.builtins = builtins
        self.combined_length = combined_length
        self.generated_source_path = generated_source_path
        self.unplaced = tuple(unplaced)
        self._windows = sorted((builtin.offset, builtin.end, builtin)
                               for builtin in builtins.values())

    def __len__(self):
        return len(self.builtins)

    def __iter__(self):
        return iter(sorted(self.builtins.values(), key=lambda builtin: builtin.offset))

    @classmethod
    def load(cls, checkout_root, generated_source_path,
             directories=BUILTIN_SOURCE_DIRECTORIES):
        with open(generated_source_path, 'r', encoding='utf-8') as handle:
            combined, windows = parse_combined_builtins(handle.read())
        locations = locate_builtin_sources(checkout_root, directories)
        return cls.from_parts(checkout_root, combined, windows, locations,
                              generated_source_path=generated_source_path)

    @classmethod
    def from_parts(cls, checkout_root, combined, windows, locations,
                   generated_source_path=None):
        file_lines = {}
        builtins = {}
        unplaced = []
        for code_name, (offset, length) in sorted(windows.items(), key=lambda item: item[1]):
            location = locations.get(code_name)
            if location is None:
                unplaced.append(code_name)
            elif location.path not in file_lines:
                file_lines[location.path] = _read_lines(checkout_root, location.path)
            builtins[code_name] = Builtin(
                code_name, offset, length, combined[offset:offset + length], location=location,
                file_lines=file_lines.get(location.path) if location else None)
        return cls(builtins, len(combined), generated_source_path=generated_source_path,
                   unplaced=unplaced)

    def owner_of(self, start, end):
        """The builtin whose window wholly contains [start, end], or None."""
        low, high = 0, len(self._windows) - 1
        while low <= high:
            middle = (low + high) // 2
            window_start, window_end, builtin = self._windows[middle]
            if start < window_start:
                high = middle - 1
            elif start >= window_end:
                low = middle + 1
            else:
                return builtin if end < window_end else None
        return None

    def verification_summary(self):
        """(placed, verified, total), for the banner that says how much of this is exact."""
        placed = sum(1 for builtin in self if builtin.path)
        verified = sum(1 for builtin in self if builtin.line_mapping_verified)
        return placed, verified, len(self)


def _read_lines(checkout_root, relative_path):
    try:
        with open(os.path.join(checkout_root, relative_path), 'r', encoding='utf-8') as handle:
            return handle.read().split('\n')
    except OSError as failure:
        logger.debug('Could not read %s: %s', relative_path, failure)
        return []


def parse_basic_block_dump(text):
    """{phase name: {source id: [BasicBlock]}} from jsc's stderr.
    Unrecognised lines are ignored; blocks before the first marker land under None."""
    phases = {}
    phase = None
    source_id = None
    for line in text.splitlines():
        marker = _DUMP_MARKER.match(line)
        if marker:
            phase = marker.group('name')
            phases.setdefault(phase, {})
            source_id = None
            continue
        header = _DUMP_SOURCE_ID.match(line)
        if header:
            source_id = int(header.group('id'))
            phases.setdefault(phase, {}).setdefault(source_id, [])
            continue
        block = _DUMP_BASIC_BLOCK.match(line)
        if block and source_id is not None:
            phases[phase][source_id].append(BasicBlock(
                start=int(block.group('start')), end=int(block.group('end')),
                has_executed=block.group('executed') == 'true',
                execution_count=int(block.group('count'))))
    return phases


def attribute_builtins_source(phases, index, anchor_code_name=ANCHOR_CODE_NAME,
                              nonce=ANCHOR_NONCE):
    """The SourceID that gained exactly `nonce` executions of a block in the anchor's window.
    A delta between the dumps, not an absolute count, so earlier calls to forEach cannot matter."""
    anchor = index.builtins.get(anchor_code_name)
    if anchor is None:
        raise AttributionError(
            'The anchor builtin {} is not in this build\'s generated builtins, so a dump '
            'cannot be attributed'.format(anchor_code_name))
    anchor_phase = phases.get(ANCHOR_PHASE)
    if not anchor_phase:
        raise AttributionError(
            'The dump has no {} phase, so jsc never reached the driver -- the run before it '
            'called quit(), crashed, or was not this tool\'s driver at all'.format(ANCHOR_PHASE))
    clean_phase = phases.get(CLEAN_PHASE) or {}

    carriers = []
    for source_id, blocks in sorted(anchor_phase.items()):
        before = {(block.start, block.end): block.execution_count
                  for block in clean_phase.get(source_id, ())}
        for block in blocks:
            if block.start < anchor.offset or block.end >= anchor.end:
                continue
            if block.execution_count - before.get((block.start, block.end), 0) == nonce:
                carriers.append(source_id)
                break
    if not carriers:
        raise AttributionError(
            'No source in the dump gained exactly {} executions of a block in {}\'s window '
            '[{}, {}) between the two dumps, so nothing identifies the builtins source'.format(
                nonce, anchor_code_name, anchor.offset, anchor.end))
    if len(carriers) > 1:
        raise AttributionError(
            'SourceIDs {} all gained exactly {} executions inside {}\'s window, so the nonce '
            'does not identify one source'.format(', '.join(str(one) for one in carriers), nonce,
                                                  anchor_code_name))
    return carriers[0]


class BuiltinsCoverage:
    """Basic-block coverage of the builtins, accumulated over jsc runs.
    Blocks merge by highest count, so the result does not depend on how runs were batched."""

    def __init__(self, index):
        self.index = index
        # (start, end) -> BasicBlock, over the builtins source only.
        self._blocks = {}
        self.runs_attributed = 0
        self.runs_unattributed = []      # [(label, reason)]
        self.blocks_outside_any_builtin = 0

    def add_dump(self, text, label=None):
        """Merge one run's dump. Returns False if it could not be attributed.
        Such a run is recorded, not raised: a few tests call quit() before the driver."""
        phases = parse_basic_block_dump(text)
        try:
            source_id = attribute_builtins_source(phases, self.index)
        except AttributionError as failure:
            self.runs_unattributed.append((label, str(failure)))
            return False
        self.runs_attributed += 1
        for block in phases.get(CLEAN_PHASE, {}).get(source_id, ()):
            self.add_block(block)
        return True

    def add_block(self, block):
        # An empty range such as [0, -1] at the head of a program owns no text.
        if block.end < block.start:
            return
        if self.index.owner_of(block.start, block.end) is None:
            self.blocks_outside_any_builtin += 1
            return
        key = (block.start, block.end)
        previous = self._blocks.get(key)
        if previous is None or block.execution_count > previous.execution_count:
            self._blocks[key] = block
        elif block.has_executed and not previous.has_executed:
            self._blocks[key] = previous._replace(has_executed=True)

    def blocks_by_builtin(self):
        """{code name: [BasicBlock]}, only for builtins some run compiled."""
        grouped = {}
        for block in self._blocks.values():
            owner = self.index.owner_of(block.start, block.end)
            grouped.setdefault(owner.code_name, []).append(block)
        return grouped


# blocks_known is 0 for a builtin nothing called: JSC generates builtin bytecode lazily, so
# "0 of 0 blocks" means it never ran.
BuiltinReport = namedtuple('BuiltinReport', (
    'builtin', 'executed', 'blocks_known', 'blocks_executed', 'lines_total', 'lines_covered',
    'uncovered_lines'))


def report_for_builtin(builtin, blocks):
    executed = any(block.has_executed for block in blocks)
    blocks_executed = sum(1 for block in blocks if block.has_executed)

    lines = builtin.code_lines()
    if not lines:
        return BuiltinReport(builtin, executed, len(blocks), blocks_executed, 0, 0, ())

    covered = set()
    with_data = set()
    for block in blocks:
        for number in builtin.lines_substantially_covered_by(block.start, block.end):
            with_data.add(number)
            if block.has_executed:
                covered.add(number)

    uncovered = []
    for number in lines:
        if number in with_data:
            if number not in covered:
                uncovered.append(number)
        # No block holds text on this line (a gap between blocks, or the builtin was never
        # compiled), so it takes the function's own verdict.
        elif not executed:
            uncovered.append(number)
    return BuiltinReport(builtin, executed, len(blocks), blocks_executed, len(lines),
                         len(lines) - len(uncovered), tuple(uncovered))


class FileReport:
    def __init__(self, path, builtin_reports):
        self.path = path
        self.builtins = sorted(builtin_reports, key=lambda report: report.builtin.first_line or 0)

    @property
    def functions_total(self):
        return len(self.builtins)

    @property
    def functions_executed(self):
        return sum(1 for report in self.builtins if report.executed)

    @property
    def lines_total(self):
        return sum(report.lines_total for report in self.builtins)

    @property
    def lines_covered(self):
        return sum(report.lines_covered for report in self.builtins)

    @property
    def line_percent(self):
        return percent(self.lines_covered, self.lines_total)


class JavaScriptCoverageReport:
    def __init__(self, coverage, jsc_path=None, test_count=None, scope=None):
        self.index = coverage.index
        self.jsc_path = jsc_path
        self.test_count = test_count
        self.scope = scope
        self.runs_attributed = coverage.runs_attributed
        self.runs_unattributed = list(coverage.runs_unattributed)
        self.blocks_outside_any_builtin = coverage.blocks_outside_any_builtin

        grouped = coverage.blocks_by_builtin()
        self.builtin_reports = [report_for_builtin(builtin, grouped.get(builtin.code_name, []))
                                for builtin in coverage.index]
        by_path = {}
        for report in self.builtin_reports:
            by_path.setdefault(report.builtin.path or '(unplaced)', []).append(report)
        self.files = [FileReport(path, reports) for path, reports in sorted(by_path.items())]

    @property
    def functions_total(self):
        return len(self.builtin_reports)

    @property
    def functions_executed(self):
        return sum(1 for report in self.builtin_reports if report.executed)

    @property
    def lines_total(self):
        return sum(report.lines_total for report in self.builtin_reports)

    @property
    def lines_covered(self):
        return sum(report.lines_covered for report in self.builtin_reports)

    def never_executed(self):
        return [report for report in
                sorted(self.builtin_reports,
                       key=lambda report: (report.builtin.path or '', report.builtin.first_line or 0))
                if not report.executed]

    def to_json(self):
        placed, verified, total = self.index.verification_summary()
        return {
            'schema': 'webkit-javascript-coverage-1',
            'mechanism': 'jsc --useControlFlowProfiler + $vm.dumpBasicBlockExecutionRanges',
            'jsc': self.jsc_path,
            'generated_builtins': self.index.generated_source_path,
            'combined_source_length': self.index.combined_length,
            'test_count': self.test_count,
            'runs_attributed': self.runs_attributed,
            'runs_unattributed': [{'test': label, 'reason': reason}
                                  for label, reason in self.runs_unattributed],
            'builtins_total': total,
            'builtins_placed_in_a_file': placed,
            'builtins_with_verified_line_mapping': verified,
            'functions': {'total': self.functions_total, 'executed': self.functions_executed},
            'lines': {'total': self.lines_total, 'covered': self.lines_covered},
            'files': [{
                'path': report.path,
                'functions': {'total': report.functions_total,
                              'executed': report.functions_executed},
                'lines': {'total': report.lines_total, 'covered': report.lines_covered},
                'builtins': [{
                    'code_name': entry.builtin.code_name,
                    'function': entry.builtin.function_name,
                    'first_line': entry.builtin.first_line,
                    'last_line': entry.builtin.last_line,
                    'executed': entry.executed,
                    'blocks': {'known': entry.blocks_known, 'executed': entry.blocks_executed},
                    'lines': {'total': entry.lines_total, 'covered': entry.lines_covered},
                    'uncovered_lines': list(entry.uncovered_lines),
                } for entry in report.builtins],
            } for report in self.files],
        }


def percent(covered, total):
    """A percentage, or None when there is no denominator -- never 0.0 for 0 of 0."""
    if not total:
        return None
    return 100.0 * covered / total


def format_percent(value, scope=None):
    if value is None:
        return '-'
    text = '{:.2f}%'.format(value)
    return scope.qualify(text) if scope is not None else text


def format_line_numbers(numbers, limit=12):
    """'116, 118-120, 123' -- runs collapsed, and truncated rather than unbounded."""
    runs = []
    for number in sorted(numbers):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    rendered = ['{}'.format(low) if low == high else '{}-{}'.format(low, high)
                for low, high in runs]
    if len(rendered) > limit:
        return ', '.join(rendered[:limit]) + ', ... ({} runs)'.format(len(rendered))
    return ', '.join(rendered)


def format_report(report, show_uncovered_lines=True):
    """The whole text report, never-executed functions first. Returns a list of lines."""
    scope = report.scope
    placed, verified, total = report.index.verification_summary()
    lines = []

    lines.append('JavaScript coverage of the builtins compiled into JavaScriptCore')
    lines.append('')
    if report.jsc_path:
        lines.append('  jsc                 {}'.format(report.jsc_path))
    if report.index.generated_source_path:
        lines.append('  builtins table      {}'.format(report.index.generated_source_path))
    lines.append('  combined source     {:,} bytes, {} builtins, {} placed in a .js file, '
                 '{} with a verified line mapping'.format(report.index.combined_length, total,
                                                          placed, verified))
    if report.test_count is not None:
        lines.append('  test files          {:,} run, {:,} dumps attributed to the builtins '
                     'source'.format(report.test_count, report.runs_attributed))
    lines.append('')

    functions_percent = percent(report.functions_executed, report.functions_total)
    lines_percent = percent(report.lines_covered, report.lines_total)
    lines.append('  Functions   {:>8}  {} of {} builtins executed'.format(
        format_percent(functions_percent, scope), report.functions_executed,
        report.functions_total))
    lines.append('  Lines       {:>8}  {} of {} body lines covered'.format(
        format_percent(lines_percent, scope), report.lines_covered, report.lines_total))
    lines.append('')

    never = report.never_executed()
    if never:
        lines.append('{} builtins were never executed:'.format(len(never)))
        lines.append('')
        current = None
        for entry in never:
            if entry.builtin.path != current:
                current = entry.builtin.path
                lines.append('  {}'.format(current))
            lines.append('      {:>5}  {:<44} {} lines'.format(
                entry.builtin.first_line or '?', entry.builtin.function_name or entry.builtin.code_name,
                entry.lines_total))
        lines.append('')
    else:
        lines.append('Every builtin in the index was executed.')
        lines.append('')

    lines.append('Per file:')
    lines.append('')
    lines.append('  {:>8}  {:>11}  {:>9}  {}'.format('lines', 'lines', 'functions', 'file'))
    for entry in report.files:
        lines.append('  {:>8}  {:>11}  {:>9}  {}'.format(
            format_percent(entry.line_percent, scope),
            '{}/{}'.format(entry.lines_covered, entry.lines_total),
            '{}/{}'.format(entry.functions_executed, entry.functions_total),
            entry.path))
    lines.append('')

    partial = [entry for entry in report.builtin_reports
               if entry.executed and entry.uncovered_lines]
    if partial and show_uncovered_lines:
        lines.append('{} builtins ran but have uncovered lines:'.format(len(partial)))
        lines.append('')
        for entry in sorted(partial, key=lambda item: (item.builtin.path or '',
                                                       item.builtin.first_line or 0)):
            lines.append('  {:>7}  {:<40} {}'.format(
                format_percent(percent(entry.lines_covered, entry.lines_total), scope),
                entry.builtin.function_name or entry.builtin.code_name, entry.builtin.path))
            lines.append('          uncovered lines {}'.format(
                format_line_numbers(entry.uncovered_lines)))
        lines.append('')

    if report.runs_unattributed:
        lines.append('{} runs produced no attributable dump:'.format(len(report.runs_unattributed)))
        for label, reason in report.runs_unattributed[:10]:
            lines.append('  {}: {}'.format(label, reason.split('.')[0]))
        if len(report.runs_unattributed) > 10:
            lines.append('  ... and {} more'.format(len(report.runs_unattributed) - 10))
        lines.append('')
    if report.index.unplaced:
        lines.append('{} builtins in the generated table matched no .js function and are '
                     'reported without a file: {}'.format(
                         len(report.index.unplaced), ', '.join(report.index.unplaced[:8])))
        lines.append('')
    return lines


class JSCDriver:
    """Runs jsc with the control-flow profiler on, and returns the dump."""

    def __init__(self, jsc_path, framework_path=None, profile_directory=None,
                 anchor_nonce=ANCHOR_NONCE, options=JSC_PROFILER_OPTIONS,
                 combined_source_length=0):
        self.jsc_path = jsc_path
        self.framework_path = framework_path or os.path.dirname(os.path.abspath(jsc_path))
        self.profile_directory = profile_directory
        self.anchor_nonce = anchor_nonce
        self.combined_source_length = combined_source_length
        self.options = tuple(options)

    def driver_source(self):
        """The JS appended to every run: dump, plant the nonce, dump again.
        Padded with a comment so the driver's own blocks sit past every builtin's window."""
        padding = max(self.combined_source_length + 64 - len(_DRIVER_PADDING_DELIMITERS), 0)
        return ('/*{}*/\n'.format(_DRIVER_PADDING_CHARACTER * padding)
                + _DRIVER_SOURCE_TEMPLATE.format(clean=CLEAN_PHASE, anchor=ANCHOR_PHASE,
                                                 end=END_PHASE, nonce=self.anchor_nonce))

    def environment(self, base=None):
        environment = dict(os.environ if base is None else base)
        # The build's JavaScriptCore must win over the system framework.
        existing = environment.get('DYLD_FRAMEWORK_PATH')
        environment['DYLD_FRAMEWORK_PATH'] = (
            self.framework_path + ':' + existing if existing else self.framework_path)
        # Keep thousands of runs from writing raw profiles into a native coverage run's directory.
        if self.profile_directory:
            environment['LLVM_PROFILE_FILE'] = os.path.join(
                self.profile_directory, 'jsc-javascript-coverage_%p_%m.profraw')
        else:
            environment['LLVM_PROFILE_FILE'] = os.devnull
        return environment

    def command(self, test_paths, driver_path):
        return [self.jsc_path] + list(self.options) + list(test_paths) + [driver_path]

    def run(self, test_paths, driver_path, timeout=120, cwd=None):
        """(returncode, dump text) for one jsc process. Markers and dump are both on stderr."""
        try:
            completed = subprocess.run(
                self.command(test_paths, driver_path), env=self.environment(), cwd=cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            return None, (expired.stderr or b'').decode('utf-8', 'replace')
        return completed.returncode, completed.stderr.decode('utf-8', 'replace')


# The comment delimiters around the padding count toward the padded length.
_DRIVER_PADDING_DELIMITERS = '/**/\n'
_DRIVER_PADDING_CHARACTER = ' '

# forEach comes from a fresh global because jsc evaluates every file into one global, and tests
# overwrite Array.prototype.forEach. The array is filled by a loop so only one builtin runs.
_DRIVER_SOURCE_TEMPLATE = '''// Generated by webkitpy/coverage_javascript.py. Appended to each jsc run.
debug("{clean}");
$vm.dumpBasicBlockExecutionRanges();
debug("{anchor}");
(function () {{
    var forEach = null;
    try {{
        forEach = $vm.createGlobalObject().Array.prototype.forEach;
    }} catch (exception) {{ }}
    if (typeof forEach !== "function")
        forEach = Array.prototype.forEach;
    var array = [];
    for (var index = 0; index < {nonce}; index++)
        array[index] = 0;
    forEach.call(array, function (element) {{ }});
}})();
$vm.dumpBasicBlockExecutionRanges();
debug("{end}");
'''


def find_generated_builtins(build_directory):
    for relative in GENERATED_BUILTINS_CANDIDATES:
        candidate = os.path.join(build_directory, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


def find_jsc(build_directory):
    for relative in ('jsc', os.path.join('bin', 'jsc')):
        candidate = os.path.join(build_directory, relative)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def expand_test_paths(paths, extension='.js'):
    """Files, with any directory walked. Sorted, so a run is reproducible and resumable."""
    found = []
    for path in paths:
        if os.path.isdir(path):
            for current, directories, files in os.walk(path):
                directories[:] = sorted(name for name in directories if not name.startswith('.'))
                for name in sorted(files):
                    if name.endswith(extension):
                        found.append(os.path.join(current, name))
        else:
            found.append(path)
    return found

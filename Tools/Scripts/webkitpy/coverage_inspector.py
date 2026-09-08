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

"""Coverage of WebKit's shipping JavaScript, from the same profiler over the Inspector protocol.

coverage_javascript.py measures the 102 builtins compiled into JavaScriptCore by driving JSC's
control-flow profiler from `jsc` plus `$vm`. That reaches everything jsc can execute and nothing
else, and the interesting JavaScript in WebKit is mostly not in jsc's reach: 199,850 lines of Web
Inspector front end, WebCore's injected scripts, and -- as that tool's own report says -- 1,049
of its own 2,737 lines, which are Inspector code no jsc test can structurally run.

InspectorRuntimeAgent wraps the identical JSC::ControlFlowProfiler, so the same measurement is
available to anything that speaks the Inspector protocol:

    Runtime.enableControlFlowProfiler         InspectorRuntimeAgent.cpp:487
    Runtime.getBasicBlocks(sourceID)          InspectorRuntimeAgent.cpp:530
    Debugger.scriptParsed -> {scriptId, url, startLine, endLine, endColumn}
    Debugger.getScriptSource(scriptId)

That last pair is what makes this *easier* than the jsc path rather than harder: scriptParsed
hands over the sourceID-to-URL mapping that the combined builtins source does not have, and
getScriptSource hands over the exact text the VM compiled, so no offset arithmetic is needed and
no line number is ever inferred.

WHAT `getBasicBlocks` ACTUALLY RETURNS, WHICH IS TWO THINGS

InspectorRuntimeAgent::getBasicBlocks concatenates two different records
(ControlFlowProfiler.cpp:72-102):

  * the basic blocks the BytecodeGenerator registered, one per control-flow edge, with a real
    execution count -- but only for code that was *compiled*, which for a function means called;
  * every range in VM::functionHasExecutedCache(), which is populated for the whole *text* of
    every function a compiled CodeBlock declares, whether or not it was ever called
    (CodeBlock.cpp:447 and :458, both guarded by wasCompiledWithControlFlowProfilerOpcodes), and
    flipped to executed when that function's own CodeBlock is created (CodeBlock.cpp:421).

The second record is why a function nothing called is visible at all, and it is the headline of
this report. Measured on the Web Inspector front end: `() => "done"` in a driver page appears as
exactly one range, hasExecuted false, containing no other range -- the signature of a function
that was declared, never called, and therefore never compiled.

It also creates the one trap in this file. A function's whole-text range has hasExecuted true as
soon as the function ran *once*, and it spans every line of the function including the ones that
never ran. Letting it claim lines reports any function that was entered as fully covered. So
before any line is attributed, every range that STRICTLY CONTAINS another range of the same
source is discarded (see drop_enclosing_ranges). That removes function extents and the outer
basic block that tiles over a nested function's text, and keeps the extent of a function nothing
compiled -- which contains nothing, and whose lines really are uncovered. Verified on a page
whose blocks were printed against their own source text: the arrow function at [699, 1022] tiles
exactly over its own blocks [699, 977], [978, 1014], [1014, 1014], [1015, 1016] and [1017, 1022],
and dropping the tile is what makes [1014, 1014] -- a `)` that never ran -- visible.

THE DRIVER, AND WHY IT IS WebKitTestRunner AND NOT A PROTOCOL SOCKET

Remote inspection on macOS is an XPC service, not a WebSocket, so there is no cheap out-of-process
protocol client to write. There is, however, one already inside the tree:
Internals::openDummyInspectorFrontend(url) attaches an InspectorStubFrontend to the calling page
and opens `url` as its front end, and WebInspectorUI ships TestStub.html -- eight small files and
`InspectorProtocol.awaitCommand` -- for exactly this. LayoutTests/inspector/* is driven this way.
So the driver is a generated HTML page run under WebKitTestRunner, which needs no new client and
no new build product.

Two targets are supported, and the second is the point of this file:

  * TARGET_PAGE: the driver page is the inspected page. Measures the page's own scripts and any
    JavaScript WebCore injects into it.
  * TARGET_FRONTEND: the driver page opens the real Main.html as its front end -- which is how the
    front end gets an InspectorFrontendHost and boots -- and then, from inside that front end,
    opens TestStub.html as *its* front end. The inspector inspecting the inspector. Measured on
    this checkout: 665 scriptParsed events, all 665 with block data, and 659 of them placed in a
    file under Source/WebInspectorUI/UserInterface once the text was verified.

BOTH TARGETS HAVE TO BE RELOADED

Runtime.enableControlFlowProfiler calls VM::enableControlFlowProfiler under vm.whenIdle() and
then deleteAllCode (InspectorRuntimeAgent.cpp:519). Everything the target compiled before that
point loses its blocks. Measured: enabling the profiler after the front end had booted left
1 source with block data out of the 653 front-end scripts reported. So the driver enables the
profiler, then reloads the target and waits for scriptParsed to go quiet. With the reload, every
one of the 662 front-end scripts has block data.
"""

import json
import logging
import os
import re
import subprocess
from collections import namedtuple

from webkitpy.coverage_javascript import TextLineIndex, is_code_line, percent

logger = logging.getLogger(__name__)

# The driver logs its payload with InspectorFrontendHost.unbufferedLog, which is WTFLogAlways
# (InspectorFrontendHost.cpp:634) and therefore the WebContent process's stderr. It is chunked
# because one call is not reliable at size: a 46,701-character payload came back truncated
# mid-string while a 4,096-character chunking of the same data came back whole.
COVERAGE_BEGIN = 'WEBKIT-INSPECTOR-COVERAGE-BEGIN'
COVERAGE_END = 'WEBKIT-INSPECTOR-COVERAGE-END'
CHUNK_PREFIX = 'COVERAGE-CHUNK:'
CHUNK_SIZE = 4096

TARGET_PAGE = 'page'
TARGET_FRONTEND = 'frontend'
TARGETS = (TARGET_PAGE, TARGET_FRONTEND)

# Where a resource the build copied out of the checkout came from. The Inspector front end is
# shipped as 717 separate unminified files in this build, one per source file, so the mapping is
# exact -- and it is verified per file by comparing the text, never assumed. See
# ScriptCoverage.path_verified.
RESOURCE_ROOTS = (
    (os.path.join('WebInspectorUI.framework', 'Resources'),
     os.path.join('Source', 'WebInspectorUI', 'UserInterface')),
    (os.path.join('WebInspectorUI.framework', 'Versions', 'A', 'Resources'),
     os.path.join('Source', 'WebInspectorUI', 'UserInterface')),
)

_CHUNK_LINE = re.compile(r'^' + re.escape(CHUNK_PREFIX) + r'(?P<text>.*)$')

# Keywords that are followed by `(...)` and `{`, and would otherwise read as a shorthand method.
# Without this list `if (x) {` is a function named `if`, which it is not.
_NOT_A_FUNCTION_NAME = frozenset((
    'if', 'for', 'while', 'switch', 'catch', 'with', 'do', 'else', 'return', 'typeof', 'new',
    'delete', 'void', 'in', 'of', 'case', 'try', 'finally', 'class', 'const', 'let', 'var',
    'function', 'await', 'yield', 'throw'))

# A function head. Not anchored with `^`: it is matched with `pattern.match(source, offset)`, which
# anchors at offset on its own, and `^` in a pattern without re.MULTILINE matches only at position
# 0 of the string -- so a `^` here silently matched nothing at all and reported 0 functions in
# 130,520 lines of Web Inspector.
#
# Deliberately narrow: the ranges this is asked about include ordinary basic blocks, and a pattern
# loose enough to match `controller.enqueue(chunk);` or `(controller) {` -- the first statement of a
# block, and a function's own first block, which starts at the parameter list rather than at the
# head -- would invent functions. Verified against a driver page's 23 ranges: the five real
# functions match and nothing else does.
_FUNCTION_HEAD = re.compile(r'''
    (?:
        (?:async\s+)?function\s*\*?\s*(?P<declared>[\w$]*)\s*\(
      | (?:get|set)\s+(?P<accessor>[\w$]+)\s*\(
      | (?:async\s+)?(?P<method>[\w$]+)\s*\([^()]*\)\s*\{
      | (?:async\s+)?\([^()]*\)\s*=>
      | (?:async\s+)?(?P<arrow>[\w$]+)\s*=>
    )
''', re.VERBOSE)


class PayloadError(Exception):
    """The driver produced no payload, or one that is not a coverage payload at all.

    Raised rather than reported as zero coverage, because "the page never loaded" and "the page
    ran nothing" are different answers and only one of them is about coverage.
    """


BasicBlock = namedtuple('BasicBlock', ('start', 'end', 'has_executed', 'execution_count'))


def basic_block_from_protocol(entry):
    """One Runtime.BasicBlock, as the protocol spells it (Runtime.json's BasicBlock type)."""
    return BasicBlock(start=int(entry['startOffset']), end=int(entry['endOffset']),
                      has_executed=bool(entry['hasExecuted']),
                      execution_count=int(entry.get('executionCount') or 0))


def extract_payload(text):
    """The driver's JSON payload, reassembled from a WebKitTestRunner stderr log.

    Everything outside the markers is ignored, because the same stream carries the WebContent
    process's own logging -- MallocStackLogging notices, CONSOLE MESSAGE lines from the front end
    complaining about localized strings it cannot find -- and a run that produced a payload should
    not be lost to any of it.
    """
    if COVERAGE_BEGIN not in text:
        raise PayloadError('The log has no {} marker, so the driver never reached the point of '
                           'reporting: the page did not load, the front end did not boot, or '
                           'WebKitTestRunner died first'.format(COVERAGE_BEGIN))
    body = text.split(COVERAGE_BEGIN, 1)[1]
    if COVERAGE_END not in body:
        raise PayloadError('The log has {} but no {}, so the payload is incomplete'.format(
            COVERAGE_BEGIN, COVERAGE_END))
    body = body.split(COVERAGE_END, 1)[0]
    chunks = []
    for line in body.splitlines():
        match = _CHUNK_LINE.match(line)
        if match:
            chunks.append(match.group('text'))
    if not chunks:
        raise PayloadError('No {} lines between the markers'.format(CHUNK_PREFIX))
    joined = ''.join(chunks)
    try:
        return json.loads(joined)
    except ValueError as failure:
        raise PayloadError('The payload between the markers is not JSON ({}); {} chunks, {} '
                           'characters'.format(failure, len(chunks), len(joined)))


def drop_enclosing_ranges(blocks):
    """Every range that does not strictly contain another range of the same source.

    See the module docstring: getBasicBlocks returns function extents alongside basic blocks, and
    an extent's hasExecuted says only that the function was entered. Keeping it would report every
    line of every function that ran once as covered.

    Identical ranges are folded first -- a single-expression arrow's extent and its one basic
    block have exactly the same offsets, and neither contains the other -- taking the highest
    execution count and executed if either is, for the same reason coverage_lcov merges by
    maximum: a range executed by either record was executed.

    O(n log n): after sorting by start ascending and end descending, a range contains another
    exactly when some later range ends no later than it does.
    """
    folded = {}
    for block in blocks:
        if block.end < block.start:
            # `[0, -1]` is real: an empty range at the head of a program. It owns no text.
            continue
        key = (block.start, block.end)
        previous = folded.get(key)
        if previous is None:
            folded[key] = block
        else:
            folded[key] = BasicBlock(
                block.start, block.end, previous.has_executed or block.has_executed,
                max(previous.execution_count, block.execution_count))

    ordered = sorted(folded.values(), key=lambda block: (block.start, -block.end))
    suffix_minimum_end = [None] * (len(ordered) + 1)
    for index in range(len(ordered) - 1, -1, -1):
        following = suffix_minimum_end[index + 1]
        suffix_minimum_end[index] = (ordered[index].end if following is None
                                     else min(ordered[index].end, following))
    kept = []
    for index, block in enumerate(ordered):
        following = suffix_minimum_end[index + 1]
        if following is not None and following <= block.end:
            continue
        kept.append(block)
    return kept


FunctionRecord = namedtuple('FunctionRecord', ('name', 'line', 'start', 'end', 'executed'))

# `WI.roleSelectorForNode = function(node)` and `foo: () => {}`: the head itself is anonymous, but
# the report is useless without the name. Looked up backwards from the head, over the assignment or
# the property colon. Measured: without this, 8 of the first 12 never-executed functions in the
# Web Inspector front end were reported as `(anonymous)`.
_ASSIGNED_NAME = re.compile(r'(?P<name>[\w$]+(?:\.[\w$]+)*)\s*(?:=|:)\s*$')


def _name_before(source, offset):
    """The identifier a function at `offset` was assigned to, or ''."""
    match = _ASSIGNED_NAME.search(source, max(offset - 120, 0), offset)
    return match.group('name') if match else ''


def function_records(source, blocks):
    """Every function the ranges name, with whether it ran. The report's headline.

    A range is treated as a function's extent when the text at its start offset is a function
    head; see _FUNCTION_HEAD for why that pattern is as narrow as it is. Where several ranges
    share a start offset the widest wins, because that is the extent and the narrower ones are its
    first basic block.

    A function counts as executed when its extent says so or when any range strictly inside it
    executed. The second half matters: an extent whose function was entered is already
    hasExecuted, but a function whose *enclosing* tile is the extent needs its own blocks
    consulted.

    Empty ranges are dropped first, and that is not tidying. `BasicBlock: [1356, 1355]` is real --
    a zero-width program-level marker at the offset a top-level function declaration begins -- and
    it is always hasExecuted, because the program ran. Counted as "a range inside the extent that
    executed" it reported createCodeMirrorTextMarkers as executed in a file where the tool
    simultaneously and correctly reported 0 of 117 lines covered.

    This is a lower bound on the function count, and says so in the report. Two shapes are known
    to be missed: a `function` expression whose extent is not itself a range (the range starts at
    the parameter list, which is not a function head), and any function whose extent never
    reached the profiler at all because its enclosing function was never compiled.
    """
    blocks = [block for block in blocks if block.end >= block.start]
    widest = {}
    for block in blocks:
        current = widest.get(block.start)
        if current is None or block.end > current.end:
            widest[block.start] = block

    records = []
    for start, block in sorted(widest.items()):
        match = _FUNCTION_HEAD.match(source, start)
        if not match:
            continue
        name = (match.group('declared') or match.group('accessor') or match.group('method')
                or match.group('arrow') or '')
        if match.group('method') and name in _NOT_A_FUNCTION_NAME:
            continue
        name = name or _name_before(source, start)
        executed = block.has_executed or any(
            inner.has_executed for inner in blocks
            if inner.start >= block.start and inner.end <= block.end
            and (inner.start, inner.end) != (block.start, block.end))
        records.append(FunctionRecord(name or '(anonymous)', None, block.start, block.end,
                                      executed))
    return records


class ScriptCoverage:
    """One source the backend named: where it came from, and which of its lines ran.

    Everything is computed from the text the VM compiled -- either fetched with
    Debugger.getScriptSource or read from the checkout file whose identity has been verified --
    so a line number in this report is never inferred from a stale artefact. That is the same
    distinction coverage_javascript.py draws with line_mapping_verified, reached from the other
    side: there the text is recovered from the build and checked against the checkout, here the
    text is authoritative and the *path* is what gets checked.
    """

    def __init__(self, source_id, url=None, source_url=None, start_line=0, start_column=0,
                 end_line=None, end_column=None, source=None, blocks=(), path=None,
                 path_verified=False, source_truncated=False):
        self.source_id = source_id
        self.url = url or ''
        self.source_url = source_url or ''
        self.start_line = start_line
        self.start_column = start_column
        self.end_line = end_line
        self.end_column = end_column
        self.source = source
        self.blocks = list(blocks)
        self.path = path
        self.path_verified = path_verified
        self.source_truncated = source_truncated

    @property
    def label(self):
        """What to call this source in a report, best available first."""
        if self.path:
            return self.path
        if self.url:
            return self.url
        if self.source_url:
            return '(injected) ' + self.source_url
        return '(sourceID {})'.format(self.source_id)

    @property
    def has_text(self):
        return bool(self.source) and not self.source_truncated

    def line_index(self):
        """Blocks-to-lines for this source, at the file's own line numbers.

        start_line is zero-based in the protocol (Debugger.cpp builds Script.startLine from
        startPosition().m_line.zeroBasedInt()), so an inline <script> beginning on file line 5
        arrives as 4 and its own first line is 5.
        """
        return TextLineIndex(self.source, first_line=self.start_line + 1, base_offset=0)

    def coverage(self):
        """(code lines, covered lines, uncovered lines) for this source.

        A line is in the denominator when is_code_line says so, which is the identical rule
        coverage_javascript.py applies to the builtins, and it is imported rather than copied.
        A code line with no range holding text on it is not counted either way: the ranges only
        exist for code some CodeBlock was built for, and a line inside a function whose enclosing
        function was never compiled has no record at all rather than a negative one.
        """
        if not self.has_text:
            return (), (), ()
        index = self.line_index()
        lines = index.text.split('\n')
        code_lines = tuple(index.first_line + offset for offset, text in enumerate(lines)
                           if is_code_line(text))
        if not code_lines:
            return (), (), ()

        covered = set()
        with_data = set()
        for block in drop_enclosing_ranges(self.blocks):
            for number in index.lines_with_text_in(block.start, block.end):
                with_data.add(number)
                if block.has_executed:
                    covered.add(number)
        covered &= set(code_lines)
        with_data &= set(code_lines)
        uncovered = tuple(number for number in code_lines
                          if number in with_data and number not in covered)
        return code_lines, tuple(sorted(covered)), uncovered

    def functions(self):
        """Every function this source's ranges name, with a file line number."""
        if not self.has_text:
            return []
        index = self.line_index()
        return [record._replace(line=index.line_for_offset(record.start))
                for record in function_records(self.source, self.blocks)]


def resolve_path(url, checkout_root, resource_roots=RESOURCE_ROOTS):
    """(checkout-relative path, or None) for a script URL.

    Only file: URLs resolve, and only to somewhere inside the checkout. A build's copy of a
    checkout file is remapped through resource_roots -- the Inspector front end is loaded from
    WebInspectorUI.framework/Resources, one file per source file -- and the result is a claim that
    the caller is expected to verify against the text, not a conclusion. See
    verify_path_against_text.
    """
    if not url.startswith('file://'):
        return None
    path = url[len('file://'):]
    path = path.split('?', 1)[0].split('#', 1)[0]
    if not path.startswith('/'):
        return None
    for build_relative, source_relative in resource_roots:
        marker = os.sep + build_relative + os.sep
        if marker in path:
            remainder = path.split(marker, 1)[1]
            return os.path.join(source_relative, remainder)
    checkout_root = os.path.abspath(checkout_root)
    if path == checkout_root or path.startswith(checkout_root + os.sep):
        return os.path.relpath(path, checkout_root)
    return None


def verify_path_against_text(checkout_root, path, source=None, end_line=None, end_column=None,
                             whole_file=True):
    """(verified, text) -- does the checkout file really hold the text the VM compiled?

    Two checks, in preference order.

      * If the driver fetched the source, compare it. Exact equality is the strongest statement
        available and it is what makes the line numbers in this report trustworthy.
      * Otherwise use the shape scriptParsed already reported, and only for a source that is the
        whole file. Debugger.cpp:411-419 derives endLine from the source's line count and endColumn
        from the length of its last line, so for a whole-file script those two numbers pin the
        file's line count and its last line's length. A file edited since the build is
        overwhelmingly likely to fail one of them.

    whole_file is False for an inline <script>, whose URL is the enclosing document: there the two
    numbers describe the script and the file on disk is HTML, so the fallback would be comparing a
    fragment's shape against a document's. Such a source is only placed in a file when its text was
    fetched and matches.

    Returns (False, None) for a file that is not there, which is the ordinary case for a build
    resource whose source moved.
    """
    absolute = os.path.join(checkout_root, path)
    try:
        with open(absolute, 'r', encoding='utf-8') as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as failure:
        logger.debug('Could not read %s: %s', path, failure)
        return False, None
    if source is not None:
        return source == text, text
    if end_line is None or end_column is None or not whole_file:
        return False, text
    last_newline = text.rfind('\n')
    measured_end_line = text.count('\n')
    measured_end_column = len(text) - (last_newline + 1)
    return (measured_end_line == end_line and measured_end_column == end_column), text


def scripts_from_payload(payload, checkout_root, resource_roots=RESOURCE_ROOTS):
    """[ScriptCoverage] from the driver's payload, paths resolved and verified.

    The payload's `scripts` are Debugger.scriptParsed parameters verbatim and its `sources` are
    Runtime.getBasicBlocks results keyed by the same scriptId, so nothing here has to guess which
    blocks belong to which URL: the backend said.
    """
    blocks_by_id = {}
    fetched = {}
    truncated = set()
    for entry in payload.get('sources', ()):
        source_id = str(entry['sourceID'])
        blocks_by_id[source_id] = [basic_block_from_protocol(block)
                                   for block in entry.get('basicBlocks', ())]
        if entry.get('source') is not None:
            fetched[source_id] = entry['source']
        if entry.get('sourceTruncated'):
            truncated.add(source_id)

    coverages = []
    for entry in payload.get('scripts', ()):
        source_id = str(entry['scriptId'])
        url = entry.get('url') or ''
        path = resolve_path(url, checkout_root, resource_roots=resource_roots)
        source = fetched.get(source_id)
        verified = False
        if path:
            verified, text = verify_path_against_text(
                checkout_root, path, source=source, end_line=entry.get('endLine'),
                end_column=entry.get('endColumn'),
                whole_file=not (entry.get('startLine') or entry.get('startColumn')))
            if source is None and verified:
                source = text
            elif not verified:
                # A path that does not verify is reported as a URL, never as a file: naming a
                # checkout file whose text is not what ran is how a report ends up pointing at
                # the right line number of the wrong text.
                path = None
        coverages.append(ScriptCoverage(
            source_id, url=url, source_url=entry.get('sourceURL'),
            start_line=entry.get('startLine') or 0, start_column=entry.get('startColumn') or 0,
            end_line=entry.get('endLine'), end_column=entry.get('endColumn'),
            source=source, blocks=blocks_by_id.get(source_id, ()), path=path,
            path_verified=verified, source_truncated=source_id in truncated))
    return coverages


class FileReport:
    """One source's numbers, ready to print."""

    def __init__(self, coverage):
        self.coverage = coverage
        code_lines, covered, uncovered = coverage.coverage()
        self.lines_total = len(code_lines)
        self.lines_covered = len(covered)
        self.uncovered_lines = uncovered
        self.functions = coverage.functions()

    @property
    def label(self):
        return self.coverage.label

    @property
    def path(self):
        return self.coverage.path

    @property
    def functions_total(self):
        return len(self.functions)

    @property
    def functions_executed(self):
        return sum(1 for record in self.functions if record.executed)

    @property
    def never_executed(self):
        return [record for record in self.functions if not record.executed]

    @property
    def line_percent(self):
        return percent(self.lines_covered, self.lines_total)


class InspectorCoverageReport:
    """The report: per file, per function, per line, with the never-run functions first."""

    def __init__(self, coverages, target=None, page=None, driver=None, scope=None):
        self.target = target
        self.page = page
        self.driver = driver
        self.scope = scope
        self.scripts = list(coverages)
        self.files = sorted((FileReport(coverage) for coverage in coverages
                             if coverage.has_text and coverage.blocks),
                            key=lambda report: (report.path is None, report.label))
        self.without_text = [coverage for coverage in coverages if not coverage.has_text]
        self.without_blocks = [coverage for coverage in coverages if not coverage.blocks]

    @property
    def files_resolved(self):
        return sum(1 for report in self.files if report.path)

    @property
    def lines_total(self):
        return sum(report.lines_total for report in self.files)

    @property
    def lines_covered(self):
        return sum(report.lines_covered for report in self.files)

    @property
    def functions_total(self):
        return sum(report.functions_total for report in self.files)

    @property
    def functions_executed(self):
        return sum(report.functions_executed for report in self.files)

    def never_executed(self):
        """[(FileReport, FunctionRecord)] for every function nothing called, in source order."""
        found = []
        for report in self.files:
            for record in sorted(report.never_executed, key=lambda item: item.start):
                found.append((report, record))
        return found

    def to_json(self):
        return {
            'schema': 'webkit-inspector-javascript-coverage-1',
            'mechanism': 'Runtime.enableControlFlowProfiler + Runtime.getBasicBlocks over the '
                         'Web Inspector protocol',
            'target': self.target,
            'page': self.page,
            'driver': self.driver,
            'scripts_reported': len(self.scripts),
            'scripts_with_blocks': len(self.scripts) - len(self.without_blocks),
            'scripts_resolved_to_a_checkout_file': self.files_resolved,
            'functions': {'total': self.functions_total, 'executed': self.functions_executed},
            'lines': {'total': self.lines_total, 'covered': self.lines_covered},
            'files': [{
                'path': report.path,
                'label': report.label,
                'url': report.coverage.url,
                'source_id': report.coverage.source_id,
                'path_verified': report.coverage.path_verified,
                'functions': {'total': report.functions_total,
                              'executed': report.functions_executed},
                'lines': {'total': report.lines_total, 'covered': report.lines_covered},
                'never_executed_functions': [
                    {'name': record.name, 'line': record.line} for record in
                    sorted(report.never_executed, key=lambda item: item.start)],
                'uncovered_lines': list(report.uncovered_lines),
            } for report in self.files],
            'unresolved': [{
                'source_id': coverage.source_id,
                'url': coverage.url,
                'source_url': coverage.source_url,
                'blocks': len(coverage.blocks),
            } for coverage in self.scripts if coverage.blocks and not coverage.path],
        }


def format_percent(value, scope=None):
    if value is None:
        return '-'
    text = '{:.2f}%'.format(value)
    return scope.qualify(text) if scope is not None else text


def format_report(report, limit=40, show_never_executed=True):
    """The whole text report. Returns a list of lines."""
    scope = report.scope
    lines = []
    lines.append('JavaScript coverage over the Web Inspector protocol')
    lines.append('')
    lines.append('  target              {}'.format(report.target))
    if report.page:
        lines.append('  page                {}'.format(report.page))
    if report.driver:
        lines.append('  driver              {}'.format(report.driver))
    lines.append('  sources             {} reported by Debugger.scriptParsed, {} with basic '
                 'blocks, {} placed in a checkout file'.format(
                     len(report.scripts), len(report.scripts) - len(report.without_blocks),
                     report.files_resolved))
    lines.append('')

    lines.append('  Functions   {:>8}  {} of {} functions executed'.format(
        format_percent(percent(report.functions_executed, report.functions_total), scope),
        report.functions_executed, report.functions_total))
    lines.append('  Lines       {:>8}  {} of {} code lines covered'.format(
        format_percent(percent(report.lines_covered, report.lines_total), scope),
        report.lines_covered, report.lines_total))
    lines.append('')

    never = report.never_executed()
    if never and show_never_executed:
        lines.append('{} functions were never executed. The first {}, by file:'.format(
            len(never), min(limit, len(never))))
        lines.append('')
        current = None
        for file_report, record in never[:limit]:
            if file_report.label != current:
                current = file_report.label
                lines.append('  {}'.format(current))
            lines.append('      {:>6}  {}'.format(record.line, record.name))
        lines.append('')
    elif not never:
        lines.append('Every function the profiler saw was executed.')
        lines.append('')

    lines.append('Per file, least covered first:')
    lines.append('')
    lines.append('  {:>8}  {:>13}  {:>11}  {}'.format('lines', 'lines', 'functions', 'file'))
    ranked = sorted((report_ for report_ in report.files if report_.lines_total),
                    key=lambda item: (item.line_percent, -item.lines_total))
    for entry in ranked[:limit]:
        lines.append('  {:>8}  {:>13}  {:>11}  {}'.format(
            format_percent(entry.line_percent, scope),
            '{}/{}'.format(entry.lines_covered, entry.lines_total),
            '{}/{}'.format(entry.functions_executed, entry.functions_total), entry.label))
    if len(ranked) > limit:
        lines.append('  ... and {} more files'.format(len(ranked) - limit))
    lines.append('')

    unplaced = [coverage for coverage in report.scripts if coverage.blocks and not coverage.path]
    if unplaced:
        lines.append('{} sources with block data could not be placed in a checkout file:'.format(
            len(unplaced)))
        for coverage in unplaced[:10]:
            lines.append('  sourceID {:<6} {:<44} {} blocks'.format(
                coverage.source_id,
                (coverage.url or coverage.source_url or '(no url)')[:44], len(coverage.blocks)))
        if len(unplaced) > 10:
            lines.append('  ... and {} more'.format(len(unplaced) - 10))
        lines.append('')
    return lines


# -- the driver ------------------------------------------------------------------------------

def driver_source(target=TARGET_PAGE, page=None, frontend_url=None, stub_url=None,
                  settle_ms=3000, quiet_polls=6, poll_ms=500, fetch_sources='unresolved',
                  source_limit=262144):
    """The HTML page WebKitTestRunner is pointed at.

    Kept as one template so that what the browser runs can be read here, the way
    coverage_javascript.JSCDriver keeps its jsc driver source. The front-end target needs
    frontend_url; the page target needs neither.
    """
    if target not in TARGETS:
        raise ValueError('Unknown target {!r}; expected one of {}'.format(target,
                                                                         ', '.join(TARGETS)))
    return _DRIVER_TEMPLATE.format(
        begin=json.dumps(COVERAGE_BEGIN), end=json.dumps(COVERAGE_END),
        chunk_prefix=json.dumps(CHUNK_PREFIX), chunk_size=CHUNK_SIZE,
        target=json.dumps(target), page=json.dumps(page or ''),
        frontend_url=json.dumps(frontend_url or ''), stub_url=json.dumps(stub_url or ''),
        settle_ms=int(settle_ms), quiet_polls=int(quiet_polls), poll_ms=int(poll_ms),
        fetch_sources=json.dumps(fetch_sources), source_limit=int(source_limit))


# The braces are doubled for str.format. Read it as ordinary JavaScript.
#
# The two things worth reading closely are the reload (see the module docstring: without it the
# profiler has deleted the code it was about to measure) and, for the front-end target, the
# double attach -- Main.html is opened as this page's front end so that it boots with an
# InspectorFrontendHost, and then TestStub.html is opened as Main.html's front end so that there
# is a protocol channel to the front end's own VM. window.opener is how the innermost frame ends
# the test, since the page WebKitTestRunner is waiting on is two hops away.
_DRIVER_TEMPLATE = '''<!DOCTYPE html>
<html>
<body>
<iframe id="workload" width="640" height="480" src={page}></iframe>
<script>
// Generated by webkitpy/coverage_inspector.py. Run under WebKitTestRunner.
const COVERAGE_BEGIN = {begin};
const COVERAGE_END = {end};
const CHUNK_PREFIX = {chunk_prefix};
const CHUNK_SIZE = {chunk_size};
const TARGET = {target};
const PAGE = {page};
const FRONTEND_URL = {frontend_url};
const STUB_URL = {stub_url};
const SETTLE_MS = {settle_ms};
const QUIET_POLLS = {quiet_polls};
const POLL_MS = {poll_ms};
const FETCH_SOURCES = {fetch_sources};
const SOURCE_LIMIT = {source_limit};

window.__coverageDone = function (summary) {{
    document.body.textContent = summary;
    testRunner.notifyDone();
}};

function frontendMain(options)
{{
    let scripts = [];
    InspectorProtocol.addEventListener("Debugger.scriptParsed", (message) => {{
        scripts.push(message.params);
    }});

    function log(text)
    {{
        InspectorFrontendHost.unbufferedLog(text);
    }}

    function finish(summary)
    {{
        InspectorProtocol.sendCommand({{method: "Runtime.evaluate", params: {{expression:
            (options.target === "frontend" ? "window.opener.__coverageDone(" : "window.__coverageDone(")
            + JSON.stringify(summary) + ")"}}}});
    }}

    async function settle()
    {{
        let quiet = 0;
        let last = -1;
        while (quiet < options.quietPolls) {{
            await new Promise((resolve) => setTimeout(resolve, options.pollMs));
            quiet = scripts.length === last ? quiet + 1 : 0;
            last = scripts.length;
        }}
    }}

    async function run() {{
        await InspectorProtocol.awaitCommand({{method: "Runtime.enableControlFlowProfiler", params: {{}}}});
        await InspectorProtocol.awaitCommand({{method: "Debugger.enable", params: {{}}}});

        // enableControlFlowProfiler deletes all compiled code when the VM next goes idle, so
        // anything the target already ran has no blocks. Reload and measure that instead. For
        // the page target it is the workload iframe that reloads, not the driver page: the
        // driver page's Internals owns the stub frontend and its own document has to survive.
        scripts.length = 0;
        InspectorProtocol.sendCommand({{method: "Runtime.evaluate", params: {{expression:
            options.target === "frontend"
                ? "location.reload()"
                : "document.getElementById('workload').contentWindow.location.reload()"}}}});
        await settle();

        let sources = [];
        for (let script of scripts.slice()) {{
            let result = await InspectorProtocol.awaitCommand({{
                method: "Runtime.getBasicBlocks",
                params: {{sourceID: script.scriptId}},
            }});
            let entry = {{sourceID: script.scriptId, basicBlocks: result.basicBlocks}};
            let wanted = options.fetchSources === "all"
                || (options.fetchSources === "unresolved" && !script.url.startsWith("file://"));
            if (wanted && result.basicBlocks.length) {{
                try {{
                    let content = await InspectorProtocol.awaitCommand({{
                        method: "Debugger.getScriptSource",
                        params: {{scriptId: script.scriptId}},
                    }});
                    if (content.scriptSource.length > options.sourceLimit)
                        entry.sourceTruncated = true;
                    else
                        entry.source = content.scriptSource;
                }} catch (error) {{
                    entry.sourceError = String(error);
                }}
            }}
            sources.push(entry);
        }}

        let payload = JSON.stringify({{target: options.target, page: options.page, scripts, sources}});
        log(options.begin);
        for (let at = 0; at < payload.length; at += options.chunkSize)
            log(options.chunkPrefix + payload.substr(at, options.chunkSize));
        log(options.end);
        finish("scripts=" + scripts.length + " sources=" + sources.length);
    }}

    run().catch((error) => {{
        log("COVERAGE DRIVER ERROR: " + error + " " + (error && error.stack));
        finish("error: " + error);
    }});
}}

function attachStub(hostWindow, options)
{{
    let stub = hostWindow.internals.openDummyInspectorFrontend(STUB_URL);
    if (!stub) {{
        window.__coverageDone("could not open the protocol test stub");
        return;
    }}
    stub.addEventListener("load", () => {{
        stub.postMessage("(" + frontendMain.toString() + ")(" + JSON.stringify(options) + ");", "*");
    }});
}}

window.onload = function () {{
    testRunner.dumpAsText();
    testRunner.waitUntilDone();
    // Everything frontendMain needs has to travel inside this object: the function is sent to
    // the stub page as a string and evaluated there, where none of this page's constants exist.
    let options = {{
        target: TARGET,
        page: TARGET === "frontend" ? "" : PAGE,
        quietPolls: QUIET_POLLS,
        pollMs: POLL_MS,
        fetchSources: FETCH_SOURCES,
        sourceLimit: SOURCE_LIMIT,
        begin: COVERAGE_BEGIN,
        end: COVERAGE_END,
        chunkPrefix: CHUNK_PREFIX,
        chunkSize: CHUNK_SIZE,
    }};

    if (TARGET !== "frontend") {{
        attachStub(window, options);
        return;
    }}

    // The real front end, opened as this page's inspector so that it boots with an
    // InspectorFrontendHost. Then it becomes the inspected page of a second stub frontend.
    let frontend = internals.openDummyInspectorFrontend(FRONTEND_URL);
    if (!frontend) {{
        window.__coverageDone("could not open " + FRONTEND_URL);
        return;
    }}
    frontend.addEventListener("load", () => {{
        setTimeout(() => {{
            if (!frontend.internals) {{
                window.__coverageDone("the front end has no window.internals, so it cannot be "
                    + "inspected from here (WI=" + !!frontend.WI + ")");
                return;
            }}
            attachStub(frontend, options);
        }}, SETTLE_MS);
    }});
}};
</script>
</body>
</html>
'''


def default_workload_source():
    """A page that touches WebCore's own JavaScript, for when the caller names no page.

    Streams because ReadableStream, WritableStream, TransformStream, TextEncoderStream and
    CompressionStream are all WebCore builtins; <video controls> because that is what makes
    WebCore inject its media controls, which is 161 KB of JavaScript in this build and the largest
    single script a page gets for free. It is a demonstration workload, not a suite: the point of
    --page is that the interesting workload is somebody else's page.
    """
    return _WORKLOAD_TEMPLATE


_WORKLOAD_TEMPLATE = '''<!DOCTYPE html>
<html>
<body>
<video controls width="320" height="240"></video>
<script>
// Generated by webkitpy/coverage_inspector.py.
(function () {
    let readable = new ReadableStream({
        start(controller) {
            for (let chunk of ["alpha", "beta", "gamma"])
                controller.enqueue(chunk);
            controller.close();
        }
    });
    let transform = new TransformStream({
        transform(chunk, controller) { controller.enqueue(chunk.toUpperCase()); }
    });
    let seen = [];
    let writable = new WritableStream({
        write(chunk) { seen.push(chunk); }
    });
    readable.pipeThrough(transform).pipeTo(writable).then(() => {
        let source = new ReadableStream({
            start(controller) { controller.enqueue("hello compression"); controller.close(); }
        });
        return source.pipeThrough(new TextEncoderStream())
            .pipeThrough(new CompressionStream("gzip")).getReader().read();
    }).then(() => {
        let video = document.querySelector("video");
        video.controls = true;
        video.getBoundingClientRect();
        document.title = "workload done: " + seen.join(",");
    });
})();
</script>
</body>
</html>
'''


class WebKitTestRunnerDriver:
    """Runs one WebKitTestRunner over the generated driver page and returns its log.

    Three things about the environment are load-bearing, and all three were learned the hard way.

    DYLD_FRAMEWORK_PATH and DYLD_LIBRARY_PATH, so that the build's WebKit is loaded instead of the
    system one -- and __XPC_DYLD_FRAMEWORK_PATH and __XPC_DYLD_LIBRARY_PATH as well, because the
    Network, GPU and WebContent processes are XPC services and only see the prefixed copies.
    Without them WebKitTestRunner starts and dies immediately with `WebKit framework version
    mismatch: 626.1.6 != 22626.1.3` followed by `com.apple.WebKit.Networking.Development
    terminated`. webkitpy.port.base.setup_environ_for_server copies exactly these four names for
    the same reason.

    LLVM_PROFILE_FILE and __XPC_LLVM_PROFILE_FILE, because every binary in a coverage build has
    /private/tmp/WebKitCoverage/<Product>_%8m%c.profraw baked into __llvm_profile_filename and
    that directory is machine-global. Setting only the unprefixed name is not enough and the
    failure is silent in the wrong direction: measured, one run with LLVM_PROFILE_FILE set and
    __XPC_LLVM_PROFILE_FILE unset left 20 .profraw files, 400 MB, in the shared directory in the
    middle of somebody else's coverage run. Both are always set.

    The driver page's own stderr is where the payload comes back, so stderr is captured and
    stdout -- the layout-test dump -- is kept only for the one-line summary.
    """

    def __init__(self, driver_path, build_directory, profile_destination=os.devnull,
                 extra_arguments=()):
        self.driver_path = driver_path
        self.build_directory = os.path.abspath(build_directory)
        self.profile_destination = profile_destination
        self.extra_arguments = tuple(extra_arguments)

    def environment(self, base=None):
        environment = dict(os.environ if base is None else base)
        for name in ('DYLD_FRAMEWORK_PATH', 'DYLD_LIBRARY_PATH'):
            existing = environment.get(name)
            environment[name] = (self.build_directory + ':' + existing if existing
                                 else self.build_directory)
            environment['__XPC_' + name] = environment[name]
        environment['LLVM_PROFILE_FILE'] = self.profile_destination
        environment['__XPC_LLVM_PROFILE_FILE'] = self.profile_destination
        return environment

    def command(self, page_path):
        return [self.driver_path, '--no-timeout'] + list(self.extra_arguments) + [page_path]

    def run(self, page_path, timeout=600):
        """(returncode, stdout, stderr) for one WebKitTestRunner process."""
        try:
            completed = subprocess.run(
                self.command(page_path), env=self.environment(), cwd=self.build_directory,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            return (None, (expired.stdout or b'').decode('utf-8', 'replace'),
                    (expired.stderr or b'').decode('utf-8', 'replace'))
        return (completed.returncode, completed.stdout.decode('utf-8', 'replace'),
                completed.stderr.decode('utf-8', 'replace'))


def find_test_runner(build_directory):
    """The build's WebKitTestRunner, or None."""
    for relative in ('WebKitTestRunner', os.path.join('bin', 'WebKitTestRunner')):
        candidate = os.path.join(build_directory, relative)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_inspector_resource(build_directory, name):
    """A file in the build's WebInspectorUI.framework Resources, or None.

    Both the flat and the versioned bundle layouts are tried because the CMake and Xcode builds
    produce different ones and neither is derivable from the other.
    """
    for relative in (os.path.join('WebInspectorUI.framework', 'Resources', name),
                     os.path.join('WebInspectorUI.framework', 'Versions', 'A', 'Resources',
                                  name)):
        candidate = os.path.join(build_directory, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


def file_url(path):
    return 'file://' + os.path.abspath(path)

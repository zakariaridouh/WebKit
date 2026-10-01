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

"""Coverage of WebKit's shipping JavaScript, from the profiler over the Inspector protocol.
Drives Runtime.getBasicBlocks from a generated page run under WebKitTestRunner, and reuses
TextLineIndex and is_code_line from coverage_javascript.py."""

import json
import logging
import os
import re
import subprocess
from collections import namedtuple

from webkitpy.coverage_javascript import TextLineIndex, is_code_line, percent

logger = logging.getLogger(__name__)

# The payload is logged with InspectorFrontendHost.unbufferedLog (the WebContent process's
# stderr), in chunks because one large call can come back truncated.
COVERAGE_BEGIN = 'WEBKIT-INSPECTOR-COVERAGE-BEGIN'
COVERAGE_END = 'WEBKIT-INSPECTOR-COVERAGE-END'
CHUNK_PREFIX = 'COVERAGE-CHUNK:'
CHUNK_SIZE = 4096

TARGET_PAGE = 'page'
TARGET_FRONTEND = 'frontend'
TARGETS = (TARGET_PAGE, TARGET_FRONTEND)

# Build copies of the Inspector front end, one file per source file. Each mapping is verified
# against the text, not assumed.
RESOURCE_ROOTS = (
    (os.path.join('WebInspectorUI.framework', 'Resources'),
     os.path.join('Source', 'WebInspectorUI', 'UserInterface')),
    (os.path.join('WebInspectorUI.framework', 'Versions', 'A', 'Resources'),
     os.path.join('Source', 'WebInspectorUI', 'UserInterface')),
)

_CHUNK_LINE = re.compile(r'^' + re.escape(CHUNK_PREFIX) + r'(?P<text>.*)$')

# Keywords that look like shorthand methods: `if (x) {` is not a function named `if`.
_NOT_A_FUNCTION_NAME = frozenset((
    'if', 'for', 'while', 'switch', 'catch', 'with', 'do', 'else', 'return', 'typeof', 'new',
    'delete', 'void', 'in', 'of', 'case', 'try', 'finally', 'class', 'const', 'let', 'var',
    'function', 'await', 'yield', 'throw'))

# Matched with pattern.match(source, offset), so it must not use `^`. Deliberately narrow: the
# ranges include ordinary blocks, and a looser pattern would invent functions from statements.
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
    """The driver produced no usable payload; that is not the same answer as zero coverage."""


BasicBlock = namedtuple('BasicBlock', ('start', 'end', 'has_executed', 'execution_count'))


def basic_block_from_protocol(entry):
    return BasicBlock(start=int(entry['startOffset']), end=int(entry['endOffset']),
                      has_executed=bool(entry['hasExecuted']),
                      execution_count=int(entry.get('executionCount') or 0))


def extract_payload(text):
    """The driver's JSON payload, reassembled from a WebKitTestRunner stderr log.
    Text outside the markers is ignored: the stream also carries other WebContent logging."""
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
    getBasicBlocks also returns function extents, which say only that the function was entered."""
    # Identical ranges (an arrow's extent and its one block) fold; neither contains the other.
    folded = {}
    for block in blocks:
        if block.end < block.start:
            # An empty range such as [0, -1] owns no text.
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

# `WI.foo = function(node)` and `foo: () => {}` have anonymous heads; the name is looked up
# backwards over the assignment or property colon.
_ASSIGNED_NAME = re.compile(r'(?P<name>[\w$]+(?:\.[\w$]+)*)\s*(?:=|:)\s*$')


def _name_before(source, offset):
    """The identifier a function at `offset` was assigned to, or ''."""
    match = _ASSIGNED_NAME.search(source, max(offset - 120, 0), offset)
    return match.group('name') if match else ''


def function_records(source, blocks):
    """Every function the ranges name, with whether it ran. A lower bound on the function count.
    The widest range at each start offset that reads as a function head is the extent."""
    # Empty ranges such as [1356, 1355] always report executed, which would mark functions called.
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
    Lines come from the text the VM compiled; it is the path that gets verified."""

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
        """Offsets to file lines. start_line is zero-based in the protocol."""
        return TextLineIndex(self.source, first_line=self.start_line + 1, base_offset=0)

    def coverage(self):
        """(code lines, covered lines, uncovered lines) for this source.
        A code line with no range holding text on it counts neither way: it has no record."""
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
        if not self.has_text:
            return []
        index = self.line_index()
        return [record._replace(line=index.line_for_offset(record.start))
                for record in function_records(self.source, self.blocks)]


def resolve_path(url, checkout_root, resource_roots=RESOURCE_ROOTS):
    """(checkout-relative path, or None) for a script URL.
    The result is a claim; callers must check it with verify_path_against_text."""
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
    """(verified, text): does the checkout file hold the text the VM compiled?
    Without the source, falls back to endLine/endColumn, valid only for whole-file scripts."""
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
    """[ScriptCoverage] from the driver's payload, paths resolved and verified."""
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
                # Never name a checkout file whose text is not what ran.
                path = None
        coverages.append(ScriptCoverage(
            source_id, url=url, source_url=entry.get('sourceURL'),
            start_line=entry.get('startLine') or 0, start_column=entry.get('startColumn') or 0,
            end_line=entry.get('endLine'), end_column=entry.get('endColumn'),
            source=source, blocks=blocks_by_id.get(source_id, ()), path=path,
            path_verified=verified, source_truncated=source_id in truncated))
    return coverages


class FileReport:
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


def driver_source(target=TARGET_PAGE, page=None, frontend_url=None, stub_url=None,
                  settle_ms=3000, quiet_polls=6, poll_ms=500, fetch_sources='unresolved',
                  source_limit=262144):
    """The HTML page WebKitTestRunner is pointed at. The front-end target needs frontend_url."""
    if target not in TARGETS:
        raise ValueError('Unknown target {!r}; expected one of {}'.format(
            target, ', '.join(TARGETS)))
    return _DRIVER_TEMPLATE.format(
        begin=json.dumps(COVERAGE_BEGIN), end=json.dumps(COVERAGE_END),
        chunk_prefix=json.dumps(CHUNK_PREFIX), chunk_size=CHUNK_SIZE,
        target=json.dumps(target), page=json.dumps(page or ''),
        frontend_url=json.dumps(frontend_url or ''), stub_url=json.dumps(stub_url or ''),
        settle_ms=int(settle_ms), quiet_polls=int(quiet_polls), poll_ms=int(poll_ms),
        fetch_sources=json.dumps(fetch_sources), source_limit=int(source_limit))


# Braces are doubled for str.format. The front-end target opens Main.html as this page's
# inspector and TestStub.html as Main.html's; window.opener ends the test from two hops away.
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
    """A page that exercises WebCore's builtin streams and injected media controls."""
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
    """Runs one WebKitTestRunner over the generated driver page and returns its log."""

    def __init__(self, driver_path, build_directory, profile_destination=os.devnull,
                 extra_arguments=()):
        self.driver_path = driver_path
        self.build_directory = os.path.abspath(build_directory)
        self.profile_destination = profile_destination
        self.extra_arguments = tuple(extra_arguments)

    def environment(self, base=None):
        environment = dict(os.environ if base is None else base)
        # The build's WebKit must load, not the system one; XPC services see only __XPC_ copies.
        for name in ('DYLD_FRAMEWORK_PATH', 'DYLD_LIBRARY_PATH'):
            existing = environment.get(name)
            environment[name] = (self.build_directory + ':' + existing if existing
                                 else self.build_directory)
            environment['__XPC_' + name] = environment[name]
        # Both names: XPC services ignore the unprefixed one and would write to a shared directory.
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
    for relative in ('WebKitTestRunner', os.path.join('bin', 'WebKitTestRunner')):
        candidate = os.path.join(build_directory, relative)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_inspector_resource(build_directory, name):
    """A resource in the build's WebInspectorUI.framework (flat or versioned layout), or None."""
    for relative in (os.path.join('WebInspectorUI.framework', 'Resources', name),
                     os.path.join('WebInspectorUI.framework', 'Versions', 'A', 'Resources',
                                  name)):
        candidate = os.path.join(build_directory, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


def file_url(path):
    return 'file://' + os.path.abspath(path)

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

"""Coverage of the JavaScript that ships inside JavaScriptCore, from JSC's own profiler.

LLVM source-based coverage instruments compiled native code, so the whole of
Source/JavaScriptCore/builtins is invisible to Tools/CodeCoverage's pipeline: 102 functions and
2,737 statement lines of shipping, security-relevant JavaScript -- Array iteration, Promise,
async-from-sync, the iterator helpers, the Inspector's injected script -- about which the
existing report says exactly nothing, in either direction. It cannot even say 0%.

JSC already has the instrument. `Options::useControlFlowProfiler` makes BytecodeGenerator emit
op_profile_control_flow at every basic-block boundary and register a BasicBlockLocation with
VM::controlFlowProfiler(); `$vm.dumpBasicBlockExecutionRanges()` prints every one of them with
its text range, whether it ran, and how many times. That is per-basic-block coverage of JS
source, and it applies to builtins because builtins *are* JS. This module is the harness and
the report model; it adds no instrumentation.

WHAT THE PROFILER GIVES, AND WHAT HAS TO BE RECOVERED

The dump is in text offsets, not files:

    SourceID: 5
        BasicBlock: [86019, 86056] hasExecuted: true, executionCount:3

Those offsets are into `s_JSCCombinedCode`, the single 140,940-byte string that
builtins_generate_combined_implementation.py concatenates every builtin's source into and that
BuiltinExecutables hands to one StringSourceProvider (BuiltinExecutables.cpp:45). So there is
one SourceID for all 102 builtins, and no filename or line number anywhere in the dump.

Both halves of the mapping are recovered from the build's own generated file rather than
guessed:

  * `<build>/JavaScriptCore/DerivedSources/JSCBuiltins.cpp` holds the combined string as a
    char array and, for each builtin, `s_<codeName>Code = s_JSCCombinedCode + <offset>` and
    `s_<codeName>CodeLength = <length>`. That partitions the offset space into 102 windows.
  * The codeName is BuiltinsGenerator.mangledNameForFunction()'s output -- lcfirst(file base
    name + ucfirst(function name), plus "Constructor" for an @constructor -- so walking the
    .js files forward and computing the same name places every window in a file, at a line.
    Measured on this checkout: 102 of 102, no ambiguity, nothing left over.

Line numbers are then exact, which is not obvious: the generator rewrites the source it embeds.
_parse_functions() replaces every `//...` comment with `//` and every `/*...*/` with `/**/`
before it finds the function bounds, and the wrapper turns `function forEach(callback)` into
`(function (callback)`. The first substitution preserves line count and the second collapses
it, so this module reproduces both while carrying an offset map back to the original text (see
_substitute_tracking_origins). Verified for all 102 builtins: the last line of the embedded
source lands on the file line the map predicts, so embedded line k is file line
first_line + k - 1 exactly.

ATTRIBUTING THE SOURCE ID, AND WHY IT NEEDS A NONCE

The dump's SourceIDs are assigned in order of source-provider creation, and the builtins
provider is created lazily, so its ID is not a constant -- measured as 3 in a run over one test
file and 5 in a run over two. Nor is it structurally obvious: a program source's blocks are
numbered from 0 and every 219-byte test file's blocks therefore fall inside the *first*
builtin's window, satisfying any "the offsets look like builtins" test.

So the driver identifies it, in two dumps taken from one process:

  1. dump the ranges (this is the measurement, and nothing has been perturbed yet);
  2. call Array.prototype.forEach over an array of ANCHOR_NONCE elements;
  3. dump again.

The builtins SourceID is the one that GAINED exactly ANCHOR_NONCE executions of a block inside
`arrayPrototypeForEach`'s window between the two dumps. A difference and not an absolute count,
because a test that already called forEach itself leaves the count at 6 and the anchor takes it
to 104,735 -- see attribute_builtins_source() for the wrong numbers that produced. Nothing but
the anchor runs between the dumps, so every other source's delta is zero and a test file's
offsets landing inside a builtin's window cannot mislead it.

Because the anchor runs *after* the first dump it does not appear in the numbers. The forEach
reference is taken from a fresh `$vm.createGlobalObject()`, so a test that has already
overwritten Array.prototype.forEach -- which the driver would otherwise be calling, since jsc
evaluates every file into one global object -- cannot silently move the anchor. Verified: with
a preceding file doing exactly that, the nonce still lands at 86019-86106.

If no SourceID carries the nonce, or more than one does, this module raises rather than
reporting whichever looked plausible.

WHAT IS NOT HERE

WebCore's builtins are generated one file at a time, not combined, so they have no offset table
to recover and, more to the point, they only run inside WebCore. The Web Inspector front end
and WebCore's injected scripts are the same shape of problem. Runtime.getBasicBlocks in the
Inspector protocol exposes this identical ControlFlowProfiler per sourceID, and
Debugger.scriptParsed supplies the sourceID-to-URL mapping the combined builtins source does
not have, so the mechanism generalizes -- but through a driven browser, not through jsc.
coverage_inspector.py does that, and reuses TextLineIndex and is_code_line from here so that
the two tools cannot drift apart on the two rules that were expensive to get right: a block
claims a line only where it overlaps non-whitespace, and punctuation-only lines are not in the
denominator. See Tools/CodeCoverage/JavaScriptCoverage.md.
"""

import logging
import os
import re
import subprocess
from collections import namedtuple

logger = logging.getLogger(__name__)

# Where <build>/.../JSCBuiltins.cpp lands, relative to the build directory. The CMake build
# writes DerivedSources under a per-project directory and Xcode writes one shared tree, and
# neither is derivable from the other, so both are tried and the first that exists wins.
GENERATED_BUILTINS_CANDIDATES = (
    os.path.join('JavaScriptCore', 'DerivedSources', 'JSCBuiltins.cpp'),
    os.path.join('DerivedSources', 'JavaScriptCore', 'JSCBuiltins.cpp'),
)

# The directories whose *.js the JSC builtins generator is fed, from
# JavaScriptCore_BUILTINS_SOURCES in Source/JavaScriptCore/CMakeLists.txt. inspector/ is in
# here because InjectedScriptSource.js is in that list: 3 of the 102 builtins are the
# Inspector's injected script, and they are 1,033 of the 2,737 measurable lines -- shipping JS
# built into JavaScriptCore like any other, and unreachable from jsc, which is why the report
# breaks the total down per file.
#
# Directories rather than the file list itself, because the two build systems keep two lists
# and a file added to one and not the other would be a build failure, not this tool's problem.
# Anything found here that the generated table does not name is ignored.
BUILTIN_SOURCE_DIRECTORIES = (
    os.path.join('Source', 'JavaScriptCore', 'builtins'),
    os.path.join('Source', 'JavaScriptCore', 'inspector'),
)

# jsc's debug() writes '--> <text>' to stderr (jsc.cpp:1729), which is the same stream
# dataLog() writes the dump to, so a marker printed with it cannot be reordered relative to
# the dump it delimits. Two dumps in one process is the whole attribution mechanism; see the
# module docstring.
CLEAN_PHASE = 'WEBKIT-JS-COVERAGE-CLEAN'
ANCHOR_PHASE = 'WEBKIT-JS-COVERAGE-ANCHOR'
END_PHASE = 'WEBKIT-JS-COVERAGE-END'

# The builtin the driver runs to plant the nonce, and the count it plants. A prime, purely so
# that a coincidence is easier to rule out by eye; nothing depends on its primality. It also
# has to be large enough to be implausible as a real block count in the same window, and small
# enough not to cost measurable time -- measured, the profiler plus the 141,634-byte padded
# driver plus 104,729 iterations of forEach add 3.4ms to jsc's 40.5ms of startup.
#
# arrayPrototypeForEach because it is public, is a JS builtin rather than a host function (so
# it has a FunctionExecutable in the combined source at all), takes a callback whose arity
# makes the loop body run once per element, and is at offset 85,636 -- three quarters of the
# way into the combined code, which is where a false positive would have to reach.
ANCHOR_CODE_NAME = 'arrayPrototypeForEach'
ANCHOR_NONCE = 104729

# jsc options. useControlFlowProfiler installs the profiler at VM construction
# (VM.cpp:500); useDollarVM is what makes $vm.dumpBasicBlockExecutionRanges() exist.
JSC_PROFILER_OPTIONS = ('--useControlFlowProfiler=true', '--useDollarVM=true')

_COMBINED_CODE_PATTERN = re.compile(
    r'constinit const char s_(?P<namespace>\w+)CombinedCode\[\] = \{ (?P<payload>[^}]*) \};')
_COMBINED_OFFSET_PATTERN = re.compile(
    r'const char\* const s_(?P<name>\w+)Code =\s*s_\w+CombinedCode \+ (?P<offset>\d+)\s*;')
_CODE_LENGTH_PATTERN = re.compile(r'const int s_(?P<name>\w+)CodeLength = (?P<length>\d+);')

# Copied from builtins_model.py rather than imported, because importing it would mean importing
# the generator's model of a builtin as well and this tool needs only its lexical rules. Kept
# byte-identical to the originals so that a divergence is a one-line diff:
#   singleLineCommentRegExp   builtins_model.py:60
#   multilineCommentRegExp    builtins_model.py:59
#   functionHeadRegExp        builtins_model.py:45
_SINGLE_LINE_COMMENT = re.compile(r"\/\/.*?\n", re.MULTILINE | re.DOTALL)
_MULTILINE_COMMENT = re.compile(r"\/\*.*?\*\/", re.MULTILINE | re.DOTALL)
_FUNCTION_HEAD = re.compile(
    r"(?P<annotations>(?:@[\w|=\[\] \"\.]+\s*\n)*)(?:async\s+)?function\s+(?P<name>\w+)\s*\(",
    re.MULTILINE | re.DOTALL)
# @constructor and @nakedConstructor both set is_constructor, which mangledNameForFunction()
# spells as a "Constructor" suffix (builtins_generator.py:301). One builtin in this checkout
# needs it -- PromiseConstructor.js's `function Promise` is promiseConstructorPromiseConstructor
# -- and without it that one builtin is the only one of 102 that fails to place.
_CONSTRUCTOR_ANNOTATION = re.compile(r'^@(?:naked)?[cC]onstructor', re.MULTILINE)

_DUMP_SOURCE_ID = re.compile(r'^SourceID: (?P<id>\d+)\s*$')
_DUMP_BASIC_BLOCK = re.compile(
    r'^\s*BasicBlock: \[(?P<start>-?\d+), (?P<end>-?\d+)\] '
    r'hasExecuted: (?P<executed>true|false), executionCount:(?P<count>\d+)\s*$')
_DUMP_MARKER = re.compile(r'^--> (?P<name>WEBKIT-JS-COVERAGE-[A-Z-]+)\s*$')


class AttributionError(Exception):
    """The dump could not be tied to the builtins source, so it has no meaning at all.

    Raised rather than skipped, because the failure mode this guards against is reporting
    another source's block counts as builtin coverage, which looks like an answer.
    """


BasicBlock = namedtuple('BasicBlock', ('start', 'end', 'has_executed', 'execution_count'))


def _substitute_tracking_origins(text, pattern, replacement, origins):
    """Apply pattern -> replacement, and carry every character's original offset with it.

    The generator rewrites a builtins file twice before it decides where a function begins, and
    the second rewrite -- `/*...*/` to `/**/` -- deletes newlines. Without this map, the line
    number of a function head is computed in the rewritten text and is wrong by however many
    lines the licence block above it occupied: measured, arrayPrototypeForEach came out at line
    91 of ArrayPrototype.js instead of 115, and 93 of 102 builtins were misplaced.

    origins is a list as long as text, holding the offset in the *original* text that each
    character came from. Replacement characters all inherit the offset the match started at,
    which is the right answer for the only question asked of them: which line was this on.
    """
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
    """WK_lcfirst, builtins_generator.py:39."""
    name = name[:1].lower() + name[1:]
    for wrong, right in (('dOM', 'dom'), ('uRL', 'url'), ('jS', 'js'), ('xML', 'xml'),
                         ('xSLT', 'xslt'), ('cSS', 'css'), ('rTC', 'rtc')):
        name = name.replace(wrong, right)
    return name


def _upper_first(name):
    """WK_ucfirst, builtins_generator.py:50."""
    name = name[:1].upper() + name[1:]
    return name.replace('Xml', 'XML').replace('Svg', 'SVG')


def mangled_code_name(object_name, function_name, is_constructor=False):
    """BuiltinsGenerator.mangledNameForFunction()'s output for one builtin.

    The object name is the .js file's base name (builtins_model.py:215), so this is the whole
    of the map from a file and a function to the s_<name>Code symbol the build generates -- and
    therefore to the offset window in the combined source.
    """
    name = _upper_first(function_name)
    name = re.sub(r'\.[a-z]', lambda match: match.group(0)[1].upper(), name, flags=re.IGNORECASE)
    if is_constructor:
        name += 'Constructor'
    object_name = re.sub(r'\.[a-z]', lambda match: match.group(0)[1].upper(), object_name,
                         flags=re.IGNORECASE)
    return _lower_first(object_name + name)


def parse_combined_builtins(text):
    """(combined source, {code name: (offset, length)}) from a generated <NS>Builtins.cpp.

    Decoded as latin-1 rather than utf-8 on purpose: the generator writes one array element per
    Python character, so one element is one offset, and a multi-byte decode would renumber
    every offset in the dump. Everything in the file is ASCII today; a stray high byte should
    move nothing.
    """
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
        # A declared offset with no declared length cannot be turned into a window, and a
        # window is the only thing this tool wants. Never seen; the generator emits both from
        # one loop over one list.
        if length is None:
            logger.debug('%s has an offset but no length in the generated builtins', name)
            continue
        windows[name] = (offset, length)
    return combined, windows


SourceLocation = namedtuple('SourceLocation', ('path', 'function_name', 'line'))


def locate_builtin_sources(checkout_root, directories=BUILTIN_SOURCE_DIRECTORIES):
    """{code name: SourceLocation} for every builtin function the .js files declare.

    Walks forward -- from a file and a function to the name the generator would give it --
    rather than trying to split a codeName back into an object and a function, which is
    ambiguous: arrayPrototypeForEach could be ArrayPrototype + forEach or Array +
    prototypeForEach, and nothing in the name says which.

    A name claimed by two files would be a generator collision and is dropped with a warning
    rather than resolved, because either answer would be a guess. Measured on this checkout:
    102 names, no collisions.
    """
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
    """{code name: SourceLocation} for one .js file, at the file's own line numbers."""
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
        # The `function` keyword, not the first annotation: the embedded source starts at the
        # keyword, so that is the offset a line number has to be counted to.
        keyword = match.start() + len(annotations)
        line = raw.count('\n', 0, origins[keyword]) + 1
        locations[code_name] = SourceLocation(relative_path, match.group('name'), line)
    return locations


def is_code_line(text):
    """Does this line of JavaScript carry a coverage record worth putting in a denominator?

    Non-blank, not comment-only, and not punctuation-only. The last exclusion is the rule
    Tools/CodeCoverage/README.md already states for the native pipeline -- "added lines that carry
    no coverage record -- comments, blank lines, braces, declarations -- are excluded from the
    denominator rather than counted against you" -- applied here for the same reason. A function
    that ends in an explicit `return` never executes the bytecode at its closing brace, so counting
    `}` would report an uncovered line in every fully exercised function. Array.prototype.forEach
    spans 17 lines of ArrayPrototype.js, of which 10 are statements, 4 are blank and 3 are `{`, `}`
    and `}` alone; calling it 76.92% covered when every statement in it ran would be a false
    negative that recurs 102 times.

    Shared with coverage_inspector.py, which needs the identical denominator: the two tools report
    on overlapping bodies of JavaScript and a divergence here would show up as one of them being
    mysteriously stricter than the other.
    """
    text = text.strip()
    if not text or text.startswith(('//', '*', '/*')) or text == '*/':
        return False
    return bool(set(text) - set('{}()[];,'))


class TextLineIndex:
    """Offsets into one JS source text, turned into line numbers of the file it came from.

    Three things are separated on purpose, because the callers need different combinations:

      * `text` is what the profiler's offsets index into -- the whole file for a script the
        Inspector reports, one function's embedded copy for a JSC builtin.
      * `base_offset` is the offset that text[0] has in the coordinate space the blocks use. It is
        0 for a script and the builtin's window offset for a builtin, whose blocks are numbered
        from the start of the 140,940-byte combined source rather than from the start of the
        function.
      * `first_line` is the file line text[0] sits on, so a builtin embedded at ArrayPrototype.js
        line 115, or an inline <script> starting at line 5 of an HTML document, reports the file's
        own line numbers rather than its own.

    Verified against the JSC builtins: all 102 place, and the last line of each embedded source
    lands on the file line this predicts.
    """

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
        """0-based line of an offset within text.

        A newline belongs to the line it terminates, which is what makes a block that starts on
        the newline ending line 285 report as starting on 285 rather than 286.
        """
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
        """The file line an offset in the blocks' coordinate space falls on."""
        return self.first_line + self.line_index_for_relative(offset - self.base_offset)

    def lines_with_text_in(self, start, end):
        """The file lines a range [start, end] holds real text on, in the blocks' coordinates.

        "Real text" is the whole point. JSC's basic blocks are contiguous over a function body and
        each one begins at the newline that ended the previous one, so a block's range routinely
        reaches into the *indentation* of the next statement's line without containing any of the
        statement. Crediting that line to the block is how a never-taken throw came out covered:
        in flatIntoArray, block [82860, 82953] executed 12 times and ends inside line 288's
        leading whitespace, while the block that is actually
        `@throwTypeError("flatten array exceeds 2**53 - 1");` on that same line never ran. Line
        288 was reported covered by the first and the tool named only the closing brace as
        uncovered -- which is the wrong line and the wrong conclusion.

        So a range claims a line only when it overlaps a non-whitespace character of it.
        """
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
    """One embedded JS function: its window in the combined source, and where it came from.

    line_mapping_verified is the load-bearing field. It is not an assumption that embedded line
    k is file line first_line + k - 1; it is checked, per builtin, against the file's own text,
    and a builtin that fails the check reports its function-level coverage and no line detail.
    """

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
        """One past the last offset in this builtin's window."""
        return self.offset + self.length

    @property
    def embedded_line_count(self):
        """Lines of the embedded source, ignoring the trailing newline the generator adds."""
        return self._lines.line_count

    @property
    def last_line(self):
        if self.first_line is None:
            return None
        return self.first_line + self.embedded_line_count - 1

    def _verify_line_mapping(self):
        """Does the file's text agree that this builtin occupies first_line..last_line?

        Checked at both ends. The head line must still name the function -- which catches a
        codeName placed in the wrong file or at the wrong function -- and the file line the
        embedded source's last line is predicted to land on must be the closing brace. The
        generator's wrapper writes that line as `})` and the file has `}`, so the two are
        compared with the wrapper's paren allowed for.

        Measured on this checkout: 102 of 102 builtins pass, which is what makes the line
        numbers in this report exact rather than approximate.
        """
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
        """The file lines a block [start, end] holds real text on, in combined coordinates."""
        if not self.line_mapping_verified:
            return ()
        return self._lines.lines_with_text_in(start, end)

    def code_lines(self):
        """The file lines of this builtin that hold code, which is the report's denominator.

        See is_code_line for the rule and for why punctuation-only lines are excluded.
        """
        if not self.line_mapping_verified:
            return []
        return [number for number in range(self.first_line, self.last_line + 1)
                if is_code_line(self._file_lines[number - 1])]


class BuiltinsIndex:
    """Every builtin the build embedded, placed in a file at a line.

    Built from the build directory's own generated source, so it describes the binary that is
    about to be run rather than the checkout's current idea of what the builtins are. That
    distinction is the same one Tools/CodeCoverage/README.md draws about line views rendered
    from a moved working tree, and it has the same failure mode: an edited .js file and a stale
    JSCBuiltins.cpp disagree, which this notices as a line-mapping verification failure instead
    of reporting the wrong lines.
    """

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
        """The index, given already-parsed pieces. The seam the unit tests build on."""
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

    Everything that is not a marker, a SourceID header or a BasicBlock line is ignored, because
    stderr also carries whatever the tests under measurement printed with debug() and whatever
    warnings the VM emitted. Blocks before the first marker land under None, so a dump produced
    without this module's driver still parses.
    """
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
    """The SourceID that is the combined builtins source, by finding the planted nonce.

    Positive identification, and by a DIFFERENCE between the two dumps rather than by an
    absolute count. Between the measurement dump and the anchor dump the only JavaScript that
    runs is the driver's anchor closure and the one builtin it calls `nonce` times, so:

      * a test file's source has a delta of zero on every block -- it finished before the first
        dump -- and can never be mistaken for the builtins source, however its offsets happen to
        line up;
      * the builtins source has a block inside the anchor builtin's window whose delta is
        exactly `nonce`.

    An absolute count does not work and this is the bug it caused. A test that itself calls
    Array.prototype.forEach six times leaves that block at 6, the anchor takes it to 104,735,
    and `count == 104729` matches nothing in the builtins source -- while the driver's OWN
    source, whose loop body did run exactly 104,729 times and whose offsets start at 0, sits
    inside the first builtin's window and matches instead. Measured on
    JSTests/stress/array-species-functions.js: the run reported 0 of 102 builtins executed when
    the dump plainly showed forEach at count 6, and Array.prototype.forEach came out as never
    executed over 5,926 stress tests.

    The driver's own source is kept out structurally as well, by padding it past the end of the
    combined source so that none of its offsets can fall in a builtin's window at all. See
    JSCDriver.driver_source().

    Raises AttributionError when nothing carries the nonce (the anchor did not run: a crash
    before the driver, a jsc without $vm, an option that turned the profiler off) and when more
    than one source does, which would mean the nonce is not unique.
    """
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
    """Basic-block coverage of the builtins, accumulated over any number of jsc runs.

    Merged per block range by taking the highest execution count, for the same reason
    coverage_lcov.FileCoverage.merge() does: a block executed by any run is executed, and
    summing would make the number depend on how the runs were batched.
    """

    def __init__(self, index):
        self.index = index
        # (start, end) -> BasicBlock, over the builtins source only.
        self._blocks = {}
        self.runs_attributed = 0
        self.runs_unattributed = []      # [(label, reason)]
        self.blocks_outside_any_builtin = 0

    def add_dump(self, text, label=None):
        """Merge one run's dump. Returns True if it was attributed, False if it was skipped.

        A run that cannot be attributed is recorded with its reason rather than raised through,
        because over five thousand test files a handful will call quit() before the driver and
        losing the whole report to them would be worse than reporting the shortfall.
        """
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
        # `BasicBlock: [0, -1]` appears in real dumps: an empty range at the head of a program.
        # It owns no text, so it can neither be placed in a window nor cover a line.
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


# executed: some block of this builtin ran, which for a JS function means it was called.
# blocks_known is 0 for a builtin nothing called, because the profiler creates a
# BasicBlockLocation during bytecode generation and JSC generates a builtin's bytecode lazily,
# on the first call. So "0 of 0 blocks" is not missing data; it is the strongest possible
# statement that the function never ran.
BuiltinReport = namedtuple('BuiltinReport', (
    'builtin', 'executed', 'blocks_known', 'blocks_executed', 'lines_total', 'lines_covered',
    'uncovered_lines'))


def report_for_builtin(builtin, blocks):
    """One builtin's coverage, from the blocks the runs recorded for it."""
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
        # A code line no block holds text on. The blocks are contiguous over a builtin's body,
        # so with braces already out of the denominator this is rare -- but a line can still
        # fall in the gap between the end of one block and the start of the next, and a line in
        # a builtin nothing compiled has no blocks at all. Both take the function's own verdict,
        # which for the second case is the exact answer and for the first is the only one
        # available.
        elif not executed:
            uncovered.append(number)
    return BuiltinReport(builtin, executed, len(blocks), blocks_executed, len(lines),
                         len(lines) - len(uncovered), tuple(uncovered))


class FileReport:
    """Every builtin one .js file contributes, and the file's totals."""

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
    """The report: per file, per builtin function, per line, with the never-run ones first."""

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
        """Every builtin no run called, in source order. The headline."""
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
    """Runs jsc with the control-flow profiler on, and returns the dump.

    Two things about the environment are not incidental.

    DYLD_FRAMEWORK_PATH: the CMake build's jsc links against
    /System/Library/Frameworks/JavaScriptCore.framework, so run from a shell it loads the
    system framework and dies on the first symbol the build added -- observed as `Symbol not
    found: __ZN3JSC17DeferredWorkTimer24scheduleWorkSoonIfActiveE...`. The build directory has
    to be ahead of it.

    LLVM_PROFILE_FILE: every binary in a coverage build has
    /private/tmp/WebKitCoverage/<Product>_%8m%c.profraw baked into __llvm_profile_filename, and
    that directory is machine-global -- Tools/CodeCoverage/README.md's "two coverage runs
    interfere" entry is about exactly this. Running jsc thousands of times would write
    thousands of raw profiles into whatever native coverage run is in flight. So the driver
    always overrides it into a directory of its own, and it is not optional: a caller that
    passes no profile directory gets os.devnull, which the profile runtime writes to and
    discards.
    """

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
        """The JS the driver appends to every run: dump, plant the nonce, dump again.

        Padded with a comment long enough that the driver's first statement sits past the end of
        the combined builtins source. The driver runs its anchor loop 104,729 times, so its OWN
        source has a block with that execution count -- and unpadded, that block is at offset
        ~400, which is inside the first builtin's window and indistinguishable from the thing
        being looked for. Measured before the padding:
        JSTests/stress/array-species-functions.js attributed the driver's source instead of the
        builtins source and reported 0 of 102 builtins executed.

        The padding is a comment, so it costs one lexer pass over 141 KB per jsc run and
        produces no bytecode and no basic blocks of its own.
        """
        padding = max(self.combined_source_length + 64 - len(_DRIVER_PADDING_DELIMITERS), 0)
        return ('/*{}*/\n'.format(_DRIVER_PADDING_CHARACTER * padding)
                + _DRIVER_SOURCE_TEMPLATE.format(clean=CLEAN_PHASE, anchor=ANCHOR_PHASE,
                                                 end=END_PHASE, nonce=self.anchor_nonce))

    def environment(self, base=None):
        environment = dict(os.environ if base is None else base)
        existing = environment.get('DYLD_FRAMEWORK_PATH')
        environment['DYLD_FRAMEWORK_PATH'] = (
            self.framework_path + ':' + existing if existing else self.framework_path)
        if self.profile_directory:
            environment['LLVM_PROFILE_FILE'] = os.path.join(
                self.profile_directory, 'jsc-javascript-coverage_%p_%m.profraw')
        else:
            environment['LLVM_PROFILE_FILE'] = os.devnull
        return environment

    def command(self, test_paths, driver_path):
        return [self.jsc_path] + list(self.options) + list(test_paths) + [driver_path]

    def run(self, test_paths, driver_path, timeout=120, cwd=None):
        """(returncode, dump text) for one jsc process.

        The dump is on stderr, because that is where WTF's dataLog writes and where jsc's
        debug() writes the phase markers -- keeping them on one stream is what makes the two
        dumps separable. stdout is whatever the tests printed and is discarded.
        """
        try:
            completed = subprocess.run(
                self.command(test_paths, driver_path), env=self.environment(), cwd=cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            return None, (expired.stderr or b'').decode('utf-8', 'replace')
        return completed.returncode, completed.stderr.decode('utf-8', 'replace')


# The `/*`, `*/` and newline around the padding, counted so that the driver's first statement
# lands past combined_source_length rather than 3 bytes short of it.
_DRIVER_PADDING_DELIMITERS = '/**/\n'
_DRIVER_PADDING_CHARACTER = ' '

# Kept as one string rather than assembled, so that what jsc evaluates can be read here.
#
# The forEach reference comes from a fresh global object because jsc evaluates every file it is
# given into ONE global, so a test file that assigns to Array.prototype.forEach -- and tests do
# -- would otherwise be the function the anchor calls, the nonce would land in the test's own
# source, and attribution would fail. $vm.createGlobalObject() gives a global whose
# Array.prototype is pristine while its builtins are the same VM's, which is the property
# needed. Verified against a file that overwrites forEach: the nonce still lands at 86019.
#
# The array is filled by a plain loop rather than with Array.prototype.fill or new Array(n),
# because both of those are themselves builtins or intrinsics and the point of the anchor is to
# touch exactly one builtin, after the measurement has already been printed.
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
    """The build's JSCBuiltins.cpp, or None. See GENERATED_BUILTINS_CANDIDATES."""
    for relative in GENERATED_BUILTINS_CANDIDATES:
        candidate = os.path.join(build_directory, relative)
        if os.path.isfile(candidate):
            return candidate
    return None


def find_jsc(build_directory):
    """The build's jsc, or None."""
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

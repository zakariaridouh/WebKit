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

"""Render a line-by-line coverage view for every file in an lcov trace, in place of llvm-cov show's much larger pages.
Coverage data records no revision, so a page that could describe a file other than the one compiled is withheld or flagged.
"""

import bisect
import datetime
import html
import itertools
import json
import logging
import os
import posixpath
import textwrap
from collections import defaultdict, namedtuple
from concurrent.futures import ProcessPoolExecutor

from webkitpy.coverage_lcov import compiled_copy_candidates
from webkitpy.coverage_report_assets import search_box_html, source_page_path
from webkitpy.coverage_report_theme import (
    THEME_CSS, THEME_SCRIPT_NAME, THEME_TOGGLE_HTML, write_theme_script)

logger = logging.getLogger(__name__)

# One stylesheet and one script at the report root, shared by every page: inlining them would cost more than the source text.
STYLESHEET_NAME = 'coverage-source.css'
SCRIPT_NAME = 'coverage-source.js'

# Rows are grouped into <ol> chunks that the browser skips laying out until scrolled near (content-visibility:auto).
# Every row is exactly ROW_HEIGHT_PX tall, so a skipped chunk keeps its real height: #L123 lands exactly and the
# script can place any line by arithmetic.
CHUNK_LINES = 500
ROW_HEIGHT_PX = 17

# How far past the end of a file a record may sit and still be llvm-cov's rather than a sign of drift. For a file
# #included into another translation unit, llvm-cov maps a region ending one past the last line and the lcov export
# emits a DA: for it.
END_OF_FILE_SEGMENT_LINES = 1

# Why a file has no line view, shown in the index. Each reason calls for a different action.
UNREADABLE_SOURCE = 'the source could not be read'
RECORDS_PAST_END_OF_FILE = ('the coverage records run past the end of the file on disk, so '
                            'this is not the text that was compiled')
LINE_VIEWS_NOT_WRITTEN = 'this report was written with --no-source-views'

# Where a covered path outside the source root goes in the report tree. Hyphenated and lowercase so it cannot collide with a real top-level directory.
OUTSIDE_SOURCE_ROOT_DIRECTORY = 'outside-the-checkout'

_PageResult = namedtuple('_PageResult',
                         ('path', 'size', 'reason', 'newer_than_build', 'rendered_from'))

# The states a rendered line can be in. The row loop is the hot path and asks line_state() for one, so a new state is a
# constant, a ROW_CLASS entry, a branch in line_state() and a stylesheet rule.
NOT_INSTRUMENTED = 'not-instrumented'
UNCOVERED = 'uncovered'
COVERED = 'covered'
PARTIALLY_TAKEN = 'partially-taken'

# The class attribute per state, with its leading space, so states with no class cost nothing per row.
ROW_CLASS = {
    NOT_INSTRUMENTED: '',
    UNCOVERED: ' class=u',
    COVERED: '',
    PARTIALLY_TAKEN: ' class=p',
}


def line_state(count, taken):
    """The state of one rendered line. count is None with no trace record; taken is (branches taken, total) when a branch was never taken."""
    if count is None:
        return NOT_INSTRUMENTED
    if not count:
        return UNCOVERED
    return COVERED if taken is None else PARTIALLY_TAKEN


FOLD_MIN_LINES = 8

_VIEW_STYLE = r"""
html { scroll-padding-top: 96px; }
body {
  --h: var(--cov-heat-4);
  --hatch: repeating-linear-gradient(90deg, transparent 0 3px, var(--cov-border) 3px 4px);
  margin: 0; width: max-content; min-width: 100%;
  background: var(--cov-bg); color: var(--cov-fg);
  font: 12px/@ROW@px ui-monospace, SFMono-Regular, Menlo, monospace;
}
a { color: var(--cov-accent); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--cov-accent); outline-offset: 2px; }
header {
  position: sticky; top: 0; left: 0; z-index: 4;
  box-sizing: border-box; max-width: 100vw; contain: inline-size;
  padding: 6px 14px; background: var(--cov-surface); border-bottom: 1px solid var(--cov-border);
  font: 12px/1.4 system-ui, -apple-system, sans-serif; color: var(--cov-muted);
}
@media (max-height: 480px) { header { position: static; } }
.top, .sub { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; }
.sub { min-height: 24px; margin-top: 4px; }
.crumbs { flex: 1 1 260px; min-width: 0; }
.crumbs ol { display: flex; flex-wrap: wrap; margin: 0; padding: 0; list-style: none; }
.crumbs li + li::before { content: "/"; margin: 0 6px; color: var(--cov-muted); }
.crumbs [aria-current] { color: var(--cov-fg); font-weight: 600; overflow-wrap: anywhere; }
.report-search { position: relative; flex: 0 1 300px; }
.report-search input {
  box-sizing: border-box; width: 100%; padding: 4px 9px;
  font: 12px/1.4 system-ui, -apple-system, sans-serif; color: var(--cov-fg);
  background: var(--cov-bg); border: 1px solid var(--cov-border); border-radius: 6px;
}
.stats { display: flex; flex-wrap: wrap; gap: 2px 18px; margin: 0; padding: 0; list-style: none; }
.stats li { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
.stats b { color: var(--cov-fg); }
.stats .pct { color: var(--cov-fg); font-variant-numeric: tabular-nums; }
.stats .warn { color: var(--cov-fg); text-decoration: underline dotted; cursor: help; }
.stats .warn::before { content: "\26A0\FE0E"; margin-right: 4px; }
.bar {
  width: 56px; height: 6px; border-radius: 3px; outline: 1px solid var(--cov-outline);
  background: linear-gradient(to right, var(--cov-covered) var(--v), var(--cov-uncovered) var(--v));
}
.tools { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px; margin-left: auto; }
:root:not(.js) .tools { display: none; }
.tools button {
  font: inherit; padding: 2px 8px; color: var(--cov-fg); background: var(--cov-bg);
  border: 1px solid var(--cov-border); border-radius: 5px; cursor: pointer;
}
.tools button:hover:not(:disabled) { border-color: var(--cov-muted); }
.tools button:disabled { opacity: 0.45; cursor: default; }
.tools button[aria-pressed="true"] {
  background: color-mix(in oklab, var(--cov-accent) 16%, var(--cov-surface));
  border-color: var(--cov-accent);
}
.info {
  position: sticky; left: 0; box-sizing: border-box; max-width: 100vw; contain: inline-size;
  padding: 8px 14px 6px; font: 12px/1.5 system-ui, -apple-system, sans-serif;
  color: var(--cov-muted);
}
.info h1 {
  margin: 0; font: 600 13px/1.4 system-ui, -apple-system, sans-serif; color: var(--cov-fg);
  overflow-wrap: anywhere;
}
.info p { margin: 0; }
.key { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; margin-top: 6px; }
.key > span { white-space: nowrap; }
.key .u::before, .key .p::before, .key .x::before {
  content: "\a0"; display: inline-block; box-sizing: border-box; min-width: 22px; height: 16px;
  margin-right: 5px; padding: 0 3px; vertical-align: -4px; text-align: center; color: var(--cov-fg);
  font: 700 11px/14px ui-monospace, SFMono-Regular, Menlo, monospace;
  border: 1px solid var(--cov-border); border-radius: 3px;
}
.key .u::before {
  content: "0 !"; border-color: var(--cov-uncovered);
  background: color-mix(in srgb, var(--cov-uncovered) 30%, transparent);
}
.key .p::before {
  content: "~"; border-color: var(--cov-partial);
  background: color-mix(in srgb, var(--cov-partial) 30%, transparent);
}
.key .x::before { background-image: var(--hatch); }
.heat { display: inline-flex; align-items: center; }
.heat > span { margin-right: 6px; }
.heat i { padding: 0 7px 0 5px; font-style: normal; color: var(--cov-fg); }
.h2 { --h: color-mix(in srgb, var(--cov-heat-4) 75%, var(--cov-heat-5)); }
.h3 { --h: color-mix(in srgb, var(--cov-heat-4) 50%, var(--cov-heat-5)); }
.h4 { --h: color-mix(in srgb, var(--cov-heat-4) 25%, var(--cov-heat-5)); }
.h5 { --h: var(--cov-heat-5); }
main b, .heat i {
  background: color-mix(in srgb, var(--h) 50%, var(--cov-surface));
  box-shadow: inset -3px 0 var(--h);
}
main {
  --gutter: calc((var(--lw) + var(--bw) + var(--cw) + 6) * 1ch);
  --sel: color-mix(in srgb, var(--cov-accent) 22%, transparent);
  width: max-content; min-width: max(100%, calc(var(--gutter) + var(--w) * 1ch + 14px));
  padding: 6px 0 40vh;
}
main ol {
  margin: 0; padding: 0; list-style: none;
  content-visibility: auto; contain-intrinsic-size: none calc(var(--n, @CHUNK@) * @ROW@px);
}
main li {
  position: relative; height: @ROW@px; padding: 0 14px 0 var(--gutter);
  white-space: pre; counter-increment: ln; scroll-margin-top: calc(3 * @ROW@px);
}
main li::before {
  content: counter(ln); position: absolute; left: 0; box-sizing: border-box;
  width: calc((var(--lw) + 2) * 1ch); padding-right: 1ch; text-align: right;
  color: var(--cov-muted); border-right: 1px solid var(--cov-border); cursor: pointer;
  user-select: none;
}
main li:not(.u, .s, .f, .fold):not(:has(> b)) {
  background-image: var(--hatch); background-repeat: no-repeat;
  background-size: calc((var(--bw) + var(--cw) + 4) * 1ch) 100%;
  background-position: calc((var(--lw) + 2) * 1ch + 2px) 0;
}
main b, main i, main li.u::after, main li.p::after {
  position: absolute; top: 0; box-sizing: border-box; font-weight: 400; font-style: normal;
  user-select: none;
}
main i {
  left: calc((var(--lw) + 2) * 1ch); width: calc(var(--bw) * 1ch); text-align: right;
  color: var(--cov-fg); cursor: help;
}
main b {
  left: calc((var(--lw) + var(--bw) + 2) * 1ch); width: calc((var(--cw) + 1) * 1ch);
  padding-right: 4px; text-align: right; color: var(--cov-fg);
}
main li.u {
  background-color: color-mix(in srgb, var(--cov-uncovered) 13%, transparent);
  box-shadow: inset 3px 0 var(--cov-uncovered);
}
main li.u::after {
  content: "0 !"; left: calc((var(--lw) + var(--bw) + var(--cw) + 2) * 1ch);
  color: var(--cov-fg); font-weight: 700;
  background: color-mix(in srgb, var(--cov-uncovered) 30%, transparent);
}
main li.p {
  background-color: color-mix(in srgb, var(--cov-partial) 15%, transparent);
  box-shadow: inset 3px 0 var(--cov-partial);
}
main li.p::after {
  content: "~"; left: calc((var(--lw) + var(--bw) + var(--cw) + 4) * 1ch);
  color: var(--cov-fg); font-weight: 700;
  background: color-mix(in srgb, var(--cov-partial) 30%, transparent);
}
main mark {
  color: inherit; border-radius: 2px;
  background: color-mix(in srgb, var(--cov-uncovered) 26%, transparent);
  text-decoration: underline dotted var(--cov-uncovered); text-underline-offset: 3px;
}
:root:not(.js) main li:target, main li.s { background-image: linear-gradient(var(--sel), var(--sel)); }
:root:not(.js) main li:target::before, main li.s::before {
  color: var(--cov-bg); background: var(--cov-accent);
}
main li.f { height: 0; overflow: hidden; visibility: hidden; }
main li.fold {
  overflow: hidden; counter-increment: none; cursor: pointer; background: var(--cov-surface);
  box-shadow: inset 0 1px var(--cov-border), inset 0 -1px var(--cov-border);
}
main li.fold::before { content: none; }
main li.fold button {
  display: block; box-sizing: border-box; width: 100%; height: 15px; margin: 1px 0; padding: 0 8px;
  font: 11px/13px system-ui, -apple-system, sans-serif; color: var(--cov-muted);
  background: none; border: 0; border-radius: 3px; cursor: pointer; text-align: left;
}
main li.fold button:hover { color: var(--cov-fg); }
.conditions {
  position: absolute; z-index: 5; padding: 5px 9px; white-space: pre;
  color: var(--cov-fg); background: var(--cov-surface);
  border: 1px solid var(--cov-border); border-radius: 6px; box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
}
.minimap {
  position: fixed; top: 0; right: 0; bottom: 0; z-index: 3; box-sizing: border-box; width: 14px;
  background-color: var(--cov-surface); background-image: var(--map); background-repeat: no-repeat;
  background-size: 100% 100%, 50% 100%; border-left: 1px solid var(--cov-border);
  cursor: pointer; touch-action: none;
}
.minimap[hidden], .outline[hidden] { display: none; }
.minimap .view {
  position: absolute; left: 0; right: 0; top: 0; min-height: 8px; pointer-events: none;
  background: color-mix(in srgb, var(--cov-fg) 14%, transparent);
  border-top: 1px solid var(--cov-muted); border-bottom: 1px solid var(--cov-muted);
}
.outline {
  position: fixed; top: 0; right: 14px; bottom: 0; z-index: 3; display: flex;
  flex-direction: column; box-sizing: border-box; width: min(340px, calc(100vw - 14px));
  background: var(--cov-surface); border-left: 1px solid var(--cov-border);
  font: 12px/1.4 system-ui, -apple-system, sans-serif; color: var(--cov-fg);
}
.outline .head { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px; padding: 8px 10px; }
.outline .head strong { margin-right: auto; }
.outline button, dialog.help button {
  font: inherit; padding: 1px 8px; color: var(--cov-fg); background: var(--cov-bg);
  border: 1px solid var(--cov-border); border-radius: 5px; cursor: pointer;
}
.outline button[aria-pressed="true"] {
  background: color-mix(in oklab, var(--cov-accent) 16%, var(--cov-surface));
  border-color: var(--cov-accent);
}
.fns { flex: 1; margin: 0; padding: 0; overflow: auto; list-style: none; border-top: 1px solid var(--cov-border); }
.fns a {
  display: grid; grid-template-columns: minmax(0, 1fr) auto auto auto; gap: 2px 10px;
  align-items: baseline; padding: 4px 10px; color: var(--cov-fg);
  border-bottom: 1px solid var(--cov-border);
}
.fns a:hover { text-decoration: none; background: color-mix(in srgb, var(--cov-accent) 10%, transparent); }
.fns .n {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.fns .l, .fns .c, .fns .t { color: var(--cov-muted); font-variant-numeric: tabular-nums; }
.fns .z { color: var(--cov-fg); font-weight: 700; }
.fns .bar { grid-column: 1 / -1; width: auto; height: 3px; }
dialog.help {
  max-width: min(560px, 94vw); padding: 0; color: var(--cov-fg); background: var(--cov-surface);
  border: 1px solid var(--cov-border); border-radius: 10px; font: 13px/1.5 system-ui, -apple-system, sans-serif;
}
dialog.help::backdrop { background: rgba(0, 0, 0, 0.45); }
dialog.help > div { padding: 16px 20px; }
dialog.help h2 { margin: 0 0 8px; font-size: 14px; }
dialog.help table { border-collapse: collapse; margin: 8px 0 12px; }
dialog.help td { padding: 2px 14px 2px 0; vertical-align: top; }
kbd {
  padding: 0 5px; font: 11px/16px ui-monospace, SFMono-Regular, Menlo, monospace;
  border: 1px solid var(--cov-border); border-radius: 3px; background: var(--cov-bg);
}
.toast {
  position: fixed; left: 14px; bottom: 14px; z-index: 5; box-sizing: border-box;
  max-width: min(560px, calc(100vw - 56px)); padding: 6px 10px; opacity: 0; pointer-events: none;
  font: 12px/1.4 system-ui, -apple-system, sans-serif; color: var(--cov-fg);
  background: var(--cov-surface); border: 1px solid var(--cov-border); border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
}
.toast.on { opacity: 1; }
@media (prefers-reduced-motion: no-preference) {
  .toast { transition: opacity 0.15s; }
  main li.s { animation: flash 1.2s ease-out; }
  @keyframes flash { from { background-color: var(--sel); } }
}
@media (max-width: 720px) {
  header { padding: 6px 10px; }
  .report-search { flex-basis: 100%; }
  .tools { margin-left: 0; }
  .minimap { width: 10px; }
  .outline { right: 10px; }
}
@media (forced-colors: active) {
  main b { border-right: 3px solid CanvasText; box-shadow: none; }
  main li.u, main li.p { box-shadow: none; border-left: 3px solid CanvasText; }
}
@media print {
  .minimap, .outline, .toast, dialog.help, .tools, .report-search, main li.fold { display: none; }
  header, .info { position: static; max-width: none; contain: none; }
  body { width: auto; min-width: 0; font-size: 9pt; }
  main { width: auto; min-width: 0; padding: 0; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
  main ol { content-visibility: visible; }
  main li {
    height: auto; min-height: @ROW@px; white-space: pre-wrap; overflow-wrap: anywhere;
    break-inside: avoid;
  }
  main li.f { height: auto; overflow: visible; visibility: visible; }
}
"""

SOURCE_VIEW_STYLE = (THEME_CSS + _VIEW_STYLE).replace('@ROW@', str(ROW_HEIGHT_PX)).replace(
    '@CHUNK@', str(CHUNK_LINES))

# The script logic that needs no document, so node can test it. State arrays are indexed by line number; slot 0 is unused.
SOURCE_VIEW_CORE_SCRIPT = r"""
var NONE = 0, COVERED = 1, PARTIAL = 2, UNCOVERED = 3;

function formatCount(count) {
  var digits = String(count), length = digits.length;
  if (length <= 3) { return digits; }
  var whole = length % 3 || 3;
  var text = digits.slice(0, whole);
  if (whole !== 3) { text += '.' + digits.slice(whole, 3); }
  return text + ' kMGTPEZY'.charAt(Math.floor((length - 1) / 3));
}

// How many digits the count had, at most five, read back from formatCount(): 2.53k is four
// and 10.0k is five.
function heatLevel(text) {
  var last = text.charAt(text.length - 1);
  if (last >= '0' && last <= '9') { return text.length; }
  return last === 'k' && text.charAt(1) === '.' ? 4 : 5;
}

function parseHash(hash, lineCount) {
  var match = /^#L(\d+)(?:-L?(\d+))?$/.exec(hash);
  if (!match) { return null; }
  var first = +match[1], last = match[2] ? +match[2] : first;
  if (first > last) { var swap = first; first = last; last = swap; }
  first = Math.max(first, 1);
  last = Math.min(last, lineCount);
  return first <= last ? [first, last] : null;
}

function formatHash(first, last) {
  return '#L' + first + (last > first ? '-L' + last : '');
}

function foldRuns(states, minimum) {
  var runs = [], start = 0, end = states.length;
  for (var line = 1; line <= end; line++) {
    var open = line < end && states[line] !== PARTIAL && states[line] !== UNCOVERED;
    if (open && !start) {
      start = line;
    } else if (!open && start) {
      if (line - start >= minimum) { runs.push([start, line - 1]); }
      start = 0;
    }
  }
  return runs;
}

// A fold is the collapsed runs, sorted, each drawn as one row; saved[i] is the rows the first i remove.
function foldIndex(runs) {
  var saved = [0];
  for (var i = 0; i < runs.length; i++) { saved.push(saved[i] + runs[i][1] - runs[i][0]); }
  return {runs: runs, saved: saved};
}

function runAt(fold, line) {
  var low = 0, high = fold.runs.length;
  while (low < high) {
    var middle = (low + high) >> 1;
    if (fold.runs[middle][0] <= line) { low = middle + 1; } else { high = middle; }
  }
  return low - 1;
}

function rowsThrough(fold, line) {
  var i = runAt(fold, line);
  if (i < 0) { return line; }
  return line - fold.saved[i] - Math.min(line, fold.runs[i][1]) + fold.runs[i][0];
}

// The zero-based row a line is drawn in; every line of a collapsed run is its placeholder's.
function visualRow(fold, line) {
  var i = runAt(fold, line);
  if (i >= 0 && line <= fold.runs[i][1]) { return rowsThrough(fold, fold.runs[i][0] - 1); }
  return rowsThrough(fold, line - 1);
}

function lineAtRow(fold, row, lineCount) {
  var low = 1, high = lineCount + 1;
  while (low < high) {
    var middle = (low + high) >> 1;
    if (visualRow(fold, middle) >= row) { high = middle; } else { low = middle + 1; }
  }
  return Math.min(low, lineCount);
}

// Where the uncovered and the partial lines are, as [from, to) ranges of buckets. A bucket
// holds many lines in a long file, and a range is at least two buckets so a lone line shows.
function bucketRanges(marks, minimum) {
  var ranges = [], i = 0;
  while (i < marks.length) {
    if (!marks[i]) { i++; continue; }
    var from = i;
    while (i < marks.length && marks[i]) { i++; }
    var to = Math.min(marks.length, Math.max(i, from + minimum));
    from = Math.max(0, Math.min(from, to - minimum));
    var last = ranges[ranges.length - 1];
    if (last && from <= last[1]) { last[1] = Math.max(last[1], to); } else { ranges.push([from, to]); }
  }
  return ranges;
}

function minimapSegments(states, buckets) {
  var lines = states.length - 1;
  var count = Math.min(buckets, lines);
  var partial = new Uint8Array(count), uncovered = new Uint8Array(count);
  for (var line = 1; line <= lines; line++) {
    var state = states[line];
    if (state === PARTIAL || state === UNCOVERED) {
      (state === PARTIAL ? partial : uncovered)[Math.floor((line - 1) * count / lines)] = 1;
    }
  }
  return {count: count, partial: bucketRanges(partial, 2), uncovered: bucketRanges(uncovered, 2)};
}

// Runs of lines in one state, as {first, last, fn}. Another state or a function start ends a
// run, a line with no record does not; fn counts the function starts at or before the run.
function findBlocks(states, state, starts) {
  var blocks = [], current = null, f = 0;
  for (var line = 1; line < states.length; line++) {
    while (f < starts.length && starts[f] <= line) { f++; }
    if (states[line] === state) {
      if (current && current.fn === f) {
        current.last = line;
      } else {
        current = {first: line, last: line, fn: f};
        blocks.push(current);
      }
    } else if (states[line] !== NONE) {
      current = null;
    }
  }
  return blocks;
}

function pickBlock(blocks, from, delta) {
  if (!blocks.length) { return -1; }
  var i;
  if (delta > 0) {
    for (i = 0; i < blocks.length; i++) { if (blocks[i].first > from) { return i; } }
    return 0;
  }
  for (i = blocks.length - 1; i >= 0; i--) { if (blocks[i].first < from) { return i; } }
  return blocks.length - 1;
}

function describeBlock(state, block, index, total, hits, figure) {
  var where = block.last > block.first ? 'lines ' + block.first + '-' + block.last
                                       : 'line ' + block.first;
  var head = (state === PARTIAL ? 'Partially taken ' : 'Uncovered ') + (index + 1) + ' of '
      + total + ' at ' + where + ': ';
  if (state === PARTIAL) {
    return head + (figure ? figure + ' branches taken; ' : '') + 'the line ran but a branch never did';
  }
  if (hits === undefined) { return head + 'no function in the trace encloses it'; }
  if (hits === 0) { return head + 'the enclosing function was never entered: look for a caller'; }
  return head + 'the function ran ' + formatCount(hits) + (hits === 1 ? ' time' : ' times')
      + ' but this block was skipped: look at the condition';
}

// data-fns is "line:hits:covered lines:instrumented lines", semicolon-separated.
function parseFunctions(text) {
  var functions = [];
  (text || '').split(';').forEach(function (entry) {
    var match = /^(\d+):(\d+):(\d+):(\d+)$/.exec(entry);
    if (match) {
      functions.push({line: +match[1], hits: +match[2], covered: +match[3], total: +match[4]});
    }
  });
  return functions;
}

var SORT_VALUE = {
  line: function (f) { return f.line; },
  hits: function (f) { return f.hits; },
  uncovered: function (f) { return f.total - f.covered; }
};
var SORT_DESCENDING = {line: false, hits: true, uncovered: true};

function sortFunctions(functions, key, descending) {
  var value = SORT_VALUE[key];
  return functions.slice().sort(function (a, b) {
    return (descending ? value(b) - value(a) : value(a) - value(b)) || a.line - b.line;
  });
}

var NOT_NAMES = {'if': 1, 'for': 1, 'while': 1, 'switch': 1, 'catch': 1, 'return': 1, 'sizeof': 1};

// The name before the first parenthesis of a function's first line, or the line itself.
function labelFromLine(text) {
  var line = text.slice(0, 300).replace(/\s+/g, ' ').trim();
  var pattern = /((?:~?\w+::)*(?:operator ?[^\s(]+|~?\w+)) ?\(/g, match;
  while ((match = pattern.exec(line))) {
    if (!Object.prototype.hasOwnProperty.call(NOT_NAMES, match[1])) { return match[1]; }
  }
  return line.length > 60 ? line.slice(0, 59) + '\u2026' : line;
}
"""

# Lines are placed by arithmetic, never by asking a row where it is: a row in an unlaid-out chunk would force layout.
SOURCE_VIEW_PAGE_SCRIPT = r"""
var ROW = @ROW@;
var CHUNK = @CHUNK@;
var MIN_FOLD = @FOLD@;
var CONTEXT_ROWS = 3;
var MINIMAP_BUCKETS = 512;
var root = document.documentElement;
var main = document.querySelector('main');
var header = document.querySelector('body > header');
var tools = header && header.querySelector('.tools');
if (!main || !main.firstElementChild || !tools) { return; }
root.classList.add('js');

function recall(key) {
  try { return sessionStorage.getItem(key); } catch (error) { return null; }
}
function remember(key, value) {
  try {
    if (value) { sessionStorage.setItem(key, value); } else { sessionStorage.removeItem(key); }
  } catch (error) {}
}

// Row i is line i + 1, and chunks never change, so each row's state is read once, here.
// The heat of its count is the one thing the page does not carry, to keep a row's markup small.
var rows = main.querySelectorAll('li');
var lineCount = rows.length;
var chunks = Array.prototype.slice.call(main.children);
var states = new Uint8Array(lineCount + 1);
for (var i = 0; i < lineCount; i++) {
  var classes = ' ' + rows[i].className + ' ';
  states[i + 1] = classes.indexOf(' u ') !== -1 ? UNCOVERED
      : classes.indexOf(' p ') !== -1 ? PARTIAL : rows[i].firstElementChild ? COVERED : NONE;
  if (states[i + 1] === COVERED || states[i + 1] === PARTIAL) {
    var count = rows[i].firstElementChild;
    var level = heatLevel((count.tagName === 'I' ? count.nextElementSibling : count).textContent);
    if (level > 1) { rows[i].classList.add('h' + level); }
  }
}
var functions = parseFunctions(main.dataset.fns).filter(function (f) { return f.line <= lineCount; });
var starts = functions.map(function (f) { return f.line; });

var fold = foldIndex([]);
var holders = [];
var folding = false;
var barHeight = 0;

function chunkTop() { return main.firstElementChild.getBoundingClientRect().top; }
function lineTop(line) { return chunkTop() + visualRow(fold, line) * ROW; }
function isVisible(line) {
  var top = lineTop(line);
  return top >= barHeight && top + ROW <= window.innerHeight;
}
function firstVisibleLine() {
  return lineAtRow(fold, Math.max(0, Math.ceil((barHeight - chunkTop()) / ROW)), lineCount);
}
function lastVisibleLine() {
  var row = Math.floor((window.innerHeight - chunkTop()) / ROW);
  return row >= rowsThrough(fold, lineCount) ? lineCount
      : Math.max(firstVisibleLine(), lineAtRow(fold, row, lineCount) - 1);
}
// The same offset scroll-padding and scroll-margin give a native #L123 navigation.
function scrollToLine(line, rowsAbove) {
  var above = rowsAbove === undefined ? CONTEXT_ROWS : rowsAbove;
  window.scrollTo(window.scrollX, window.scrollY + lineTop(line) - barHeight - above * ROW);
}

var toast = document.createElement('output');
var toastTimer = 0;
toast.className = 'toast';
document.body.appendChild(toast);
function say(text) {
  toast.textContent = text;
  toast.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { toast.classList.remove('on'); }, 7000);
}

function setChunkHeights() {
  chunks.forEach(function (chunk, c) {
    var first = c * CHUNK + 1;
    var last = Math.min(first + CHUNK - 1, lineCount);
    var shown = String(rowsThrough(fold, last) - rowsThrough(fold, first - 1));
    if (chunk.style.getPropertyValue('--n') !== shown) { chunk.style.setProperty('--n', shown); }
  });
}
function placeholder(run) {
  var item = document.createElement('li');
  var button = document.createElement('button');
  item.className = 'fold';
  item.dataset.first = run[0];
  button.type = 'button';
  button.textContent = '\u25B8 ' + (run[1] - run[0] + 1) + ' lines covered';
  button.title = 'Show lines ' + run[0] + '-' + run[1] + ', which ran or have no code';
  item.appendChild(button);
  return item;
}
function setRows(run, hidden) {
  for (var line = run[0]; line <= run[1]; line++) { rows[line - 1].classList.toggle('f', hidden); }
}
function expandRun(index) {
  var runs = fold.runs.slice();
  setRows(runs[index], false);
  holders[index].remove();
  holders.splice(index, 1);
  runs.splice(index, 1);
  fold = foldIndex(runs);
  setChunkHeights();
}
// Opens whatever collapsed runs touch first..last, so a linked line is never inside one.
function reveal(first, last) {
  var index = Math.max(0, runAt(fold, first));
  while (index < fold.runs.length && fold.runs[index][0] <= last) {
    if (fold.runs[index][1] >= first) { expandRun(index); } else { index++; }
  }
}
function setFold(on) {
  var anchorLine = firstVisibleLine();
  var before = lineTop(anchorLine);
  fold.runs.forEach(function (run) { setRows(run, false); });
  holders.forEach(function (item) { item.remove(); });
  holders = [];
  fold = foldIndex(on ? foldRuns(states, MIN_FOLD) : []);
  fold.runs.forEach(function (run) {
    var item = placeholder(run);
    setRows(run, true);
    rows[run[0] - 1].parentNode.insertBefore(item, rows[run[0] - 1]);
    holders.push(item);
  });
  var linked = hashRange();
  if (linked) { reveal(linked[0], linked[1]); }
  setChunkHeights();
  window.scrollBy(0, lineTop(anchorLine) - before);
  folding = on;
  remember('cov-fold', on ? '1' : '');
  buttons.fold.setAttribute('aria-pressed', on ? 'true' : 'false');
}

var selected = [];
var anchor = 0;
function select(first, last) {
  selected.forEach(function (row) { row.classList.remove('s'); });
  selected = [];
  for (var line = first; line <= last; line++) {
    var row = document.getElementById('L' + line);
    if (row) { row.classList.add('s'); selected.push(row); }
  }
}
function hashRange() { return parseHash(location.hash, lineCount); }
function setHash(first, last) {
  var hash = formatHash(first, last);
  reveal(first, last);
  try {
    history.replaceState(null, '', hash);
  } catch (error) {
    location.replace(hash);
    return;
  }
  select(first, last);
}
function showHash() {
  var range = hashRange();
  if (range) { reveal(range[0], range[1]); }
  select(range ? range[0] : 1, range ? range[1] : 0);
  if (range) {
    anchor = range[0];
    scrollToLine(range[0]);
  }
}

function inGutter(event, row) {
  return event.clientX - row.getBoundingClientRect().left
      < parseFloat(getComputedStyle(row).paddingLeft);
}
main.addEventListener('mousedown', function (event) {
  // A shift-click in the gutter extends the linked range, not the text selection.
  var row = event.target.closest('li');
  if (event.shiftKey && row && inGutter(event, row)) { event.preventDefault(); }
});
main.addEventListener('click', function (event) {
  var target = event.target;
  var collapsed = target.closest('li.fold');
  if (collapsed) {
    expandRun(runAt(fold, +collapsed.dataset.first));
    return;
  }
  if (target.tagName === 'I' && target.dataset.b) {
    toggleConditions(target);
    return;
  }
  var row = target.closest('li');
  if (!row || !row.id || !inGutter(event, row)) { return; }
  var line = +row.id.slice(1);
  if (event.shiftKey && anchor) {
    setHash(Math.min(anchor, line), Math.max(anchor, line));
  } else {
    anchor = line;
    setHash(line, line);
  }
});

var blockCache = {};
function blocksOf(state) {
  return blockCache[state] || (blockCache[state] = findBlocks(states, state, starts));
}
function step(state, delta) {
  var blocks = blocksOf(state);
  if (!blocks.length) {
    say('No ' + (state === PARTIAL ? 'partially taken' : 'uncovered') + ' lines in this file');
    return;
  }
  // From the linked range while it is on screen, otherwise from the top of the viewport.
  var range = hashRange();
  var from = range && isVisible(range[0]) ? range[0] : firstVisibleLine() - (delta > 0 ? 1 : 0);
  var index = pickBlock(blocks, from, delta);
  var block = blocks[index];
  setHash(block.first, block.last);
  if (!isVisible(block.first) || !isVisible(Math.min(block.last, block.first + CONTEXT_ROWS))) {
    scrollToLine(block.first);
  }
  var figure = state === PARTIAL ? rows[block.first - 1].querySelector('i') : null;
  say(describeBlock(state, block, index, blocks.length,
                    block.fn ? functions[block.fn - 1].hits : undefined,
                    figure ? figure.textContent : ''));
}

var buttons = {};
function addButton(act, label, title, keys) {
  var button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.title = title + ' (' + keys + ')';
  button.setAttribute('aria-keyshortcuts', keys);
  button.dataset.act = act;
  tools.appendChild(button);
  buttons[act] = button;
}
tools.setAttribute('role', 'toolbar');
tools.setAttribute('aria-label', 'Navigation');
addButton('pu', '\u25B2 Uncovered', 'Previous uncovered block', 'p');
addButton('nu', '\u25BC Uncovered', 'Next uncovered block', 'n');
addButton('pp', '\u25B2 Partial', 'Previous partially taken line', 'B');
addButton('np', '\u25BC Partial', 'Next partially taken line', 'b');
addButton('fold', 'Fold covered', 'Fold runs of covered code', 'f');
if (functions.length) { addButton('outline', 'Functions', 'Show the function outline', 'o'); }
addButton('help', '?', 'Keyboard shortcuts', '?');
buttons.pu.disabled = buttons.nu.disabled = !blocksOf(UNCOVERED).length;
buttons.pp.disabled = buttons.np.disabled = !blocksOf(PARTIAL).length;
['fold', 'outline'].forEach(function (name) {
  if (buttons[name]) { buttons[name].setAttribute('aria-pressed', 'false'); }
});
tools.addEventListener('click', function (event) {
  var button = event.target.closest('button');
  if (button) { act(button.dataset.act); }
});

var STEPS = {nu: [UNCOVERED, 1], pu: [UNCOVERED, -1], np: [PARTIAL, 1], pp: [PARTIAL, -1]};
function act(name) {
  if (STEPS[name]) {
    step(STEPS[name][0], STEPS[name][1]);
  } else if (name === 'fold') {
    setFold(!folding);
  } else if (name === 'outline') {
    setOutline(!panel || panel.hidden);
  } else if (name === 'help') {
    showHelp();
  }
}
var KEYS = {n: 'nu', j: 'nu', p: 'pu', k: 'pu', b: 'np', B: 'pp', f: 'fold', o: 'outline', '?': 'help'};
document.addEventListener('keydown', function (event) {
  var target = event.target;
  if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) { return; }
  if (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) { return; }
  if (event.key === 'Escape') {
    hideConditions();
  } else if (Object.prototype.hasOwnProperty.call(KEYS, event.key) && buttons[KEYS[event.key]]
             && !(help && help.open)) {
    event.preventDefault();
    act(KEYS[event.key]);
  }
});

var SHORTCUTS = [
  [['n', 'j'], 'Next uncovered block'], [['p', 'k'], 'Previous uncovered block'],
  [['b'], 'Next partially taken line'], [['B'], 'Previous partially taken line'],
  [['f'], 'Fold runs of covered code'], [['o'], 'Function outline'],
  [['/'], 'Search the report'], [['?'], 'This help'], [['Esc'], 'Close']
];
var help = null;
function buildHelp() {
  help = document.createElement('dialog');
  help.className = 'help';
  var body = document.createElement('div');
  var title = document.createElement('h2');
  var table = document.createElement('table');
  var tip = document.createElement('p');
  var close = document.createElement('button');
  title.textContent = 'Keyboard shortcuts';
  tip.textContent = 'Click a line number to link to that line. Shift-click another to link the range.';
  close.type = 'button';
  close.textContent = 'Close';
  SHORTCUTS.forEach(function (entry) {
    var row = table.insertRow();
    var cell = row.insertCell();
    entry[0].forEach(function (key) {
      var kbd = document.createElement('kbd');
      kbd.textContent = key;
      cell.appendChild(kbd);
      cell.appendChild(document.createTextNode(' '));
    });
    row.insertCell().textContent = entry[1];
  });
  [title, table, tip].forEach(function (node) { body.appendChild(node); });
  var key = document.querySelector('.key');
  if (key) { body.appendChild(key.cloneNode(true)); }
  body.appendChild(close);
  help.appendChild(body);
  close.addEventListener('click', closeHelp);
  help.addEventListener('click', function (event) { if (event.target === help) { closeHelp(); } });
  document.body.appendChild(help);
}
function showHelp() {
  if (!help) { buildHelp(); }
  if (typeof help.showModal === 'function') { help.showModal(); } else { help.setAttribute('open', ''); }
}
function closeHelp() {
  if (typeof help.close === 'function') { help.close(); } else { help.removeAttribute('open'); }
}

// data-b is "true false" per condition, comma-separated, in llvm-cov's order.
function describe(figure) {
  var pairs = figure.dataset.b.split(',');
  return pairs.map(function (pair, index) {
    var counts = pair.split(' ');
    return (pairs.length > 1 ? 'Condition ' + (index + 1) + ': ' : '')
        + 'T ' + counts[0] + ' / F ' + counts[1];
  }).join('\n');
}
main.addEventListener('mouseover', function (event) {
  var target = event.target;
  if (target.tagName === 'I' && target.dataset.b && !target.title) {
    target.title = describe(target);
  }
});
var popover = null;
function hideConditions() {
  if (popover) {
    popover.remove();
    popover = null;
  }
}
function toggleConditions(figure) {
  var owner = popover && popover.owner;
  hideConditions();
  if (owner === figure) { return; }
  popover = document.createElement('div');
  popover.className = 'conditions';
  popover.setAttribute('role', 'tooltip');
  popover.textContent = describe(figure);
  popover.owner = figure;
  var box = figure.getBoundingClientRect();
  popover.style.left = (box.left + window.scrollX) + 'px';
  popover.style.top = (box.bottom + window.scrollY + 2) + 'px';
  document.body.appendChild(popover);
}
document.addEventListener('click', function (event) {
  if (popover && event.target !== popover.owner && !popover.contains(event.target)) {
    hideConditions();
  }
});

var SORT_LABELS = {line: 'Line', hits: 'Hits', uncovered: 'Uncovered first'};
var panel = null;
var outlineList = null;
var sortButtons = {};
var sortKey = 'line';
var sortDescending = SORT_DESCENDING.line;
function buildOutline() {
  panel = document.createElement('aside');
  panel.className = 'outline';
  panel.setAttribute('aria-label', 'Functions');
  panel.style.top = barHeight + 'px';
  var head = document.createElement('div');
  var title = document.createElement('strong');
  head.className = 'head';
  title.textContent = 'Functions (' + functions.length + ')';
  head.appendChild(title);
  Object.keys(SORT_LABELS).forEach(function (key) {
    var button = document.createElement('button');
    button.type = 'button';
    button.dataset.sort = key;
    head.appendChild(button);
    sortButtons[key] = button;
  });
  outlineList = document.createElement('ol');
  outlineList.className = 'fns';
  panel.appendChild(head);
  panel.appendChild(outlineList);
  document.body.appendChild(panel);
  functions.forEach(function (f) {
    var row = rows[f.line - 1], skip = 0;
    for (var node = row.firstElementChild; node && /^[BI]$/.test(node.tagName);
         node = node.nextElementSibling) {
      skip += node.textContent.length;
    }
    f.text = row.textContent.slice(skip).trim();
    f.label = labelFromLine(f.text);
  });
  head.addEventListener('click', function (event) {
    var button = event.target.closest('button');
    if (!button) { return; }
    var key = button.dataset.sort;
    sortDescending = key === sortKey ? !sortDescending : SORT_DESCENDING[key];
    sortKey = key;
    renderOutline();
  });
  outlineList.addEventListener('click', function (event) {
    var link = event.target.closest('a');
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey) { return; }
    event.preventDefault();
    anchor = +link.dataset.line;
    setHash(anchor, anchor);
    scrollToLine(anchor);
  });
  renderOutline();
}
function renderOutline() {
  var fragment = document.createDocumentFragment();
  sortFunctions(functions, sortKey, sortDescending).forEach(function (f) {
    var item = document.createElement('li');
    var link = document.createElement('a');
    link.href = formatHash(f.line, f.line);
    link.dataset.line = f.line;
    link.title = f.text;
    [['n', f.label], ['l', 'L' + f.line], [f.hits ? 't' : 't z', f.hits ? formatCount(f.hits) : '0 !'],
     ['c', f.total ? f.covered + '/' + f.total : '-']].forEach(function (entry) {
      var cell = document.createElement('span');
      cell.className = entry[0];
      cell.textContent = entry[1];
      link.appendChild(cell);
    });
    if (f.total) {
      var bar = document.createElement('span');
      bar.className = 'bar';
      bar.style.setProperty('--v', (100 * f.covered / f.total) + '%');
      link.appendChild(bar);
    }
    item.appendChild(link);
    fragment.appendChild(item);
  });
  outlineList.replaceChildren(fragment);
  Object.keys(sortButtons).forEach(function (key) {
    var on = key === sortKey;
    sortButtons[key].setAttribute('aria-pressed', on ? 'true' : 'false');
    sortButtons[key].textContent = SORT_LABELS[key] + (on ? (sortDescending ? ' \u25BC' : ' \u25B2') : '');
  });
}
function setOutline(on) {
  if (on && !panel) { buildOutline(); }
  if (panel) { panel.hidden = !on; }
  remember('cov-outline', on ? '1' : '');
  buttons.outline.setAttribute('aria-pressed', on ? 'true' : 'false');
}

var minimap = document.createElement('div');
var view = document.createElement('div');
var dragging = false;
function gradient(ranges, count, color) {
  var stops = [], at = 0;
  ranges.forEach(function (range) {
    var from = (100 * range[0] / count).toFixed(3), to = (100 * range[1] / count).toFixed(3);
    stops.push('transparent ' + at + '% ' + from + '%', color + ' ' + from + '% ' + to + '%');
    at = to;
  });
  stops.push('transparent ' + at + '% 100%');
  return 'linear-gradient(to bottom,' + stops.join(',') + ')';
}
function buildMinimap() {
  var map = minimapSegments(states, MINIMAP_BUCKETS);
  minimap.className = 'minimap';
  minimap.setAttribute('aria-hidden', 'true');
  minimap.style.setProperty('--map', gradient(map.uncovered, map.count, 'var(--cov-uncovered)')
                            + ',' + gradient(map.partial, map.count, 'var(--cov-partial)'));
  view.className = 'view';
  minimap.appendChild(view);
  document.body.appendChild(minimap);
}
function jump(event) {
  var box = minimap.getBoundingClientRect();
  var fraction = Math.min(1, Math.max(0, (event.clientY - box.top) / box.height));
  scrollToLine(Math.min(lineCount, Math.floor(fraction * lineCount) + 1),
               Math.floor((window.innerHeight - barHeight) / ROW / 2));
}
minimap.addEventListener('pointerdown', function (event) {
  dragging = true;
  minimap.setPointerCapture(event.pointerId);
  jump(event);
});
minimap.addEventListener('pointermove', function (event) { if (dragging) { jump(event); } });
['pointerup', 'pointercancel'].forEach(function (type) {
  minimap.addEventListener(type, function () { dragging = false; });
});

var framePending = false;
function updateView() {
  framePending = false;
  var first = firstVisibleLine(), last = lastVisibleLine();
  view.style.top = (100 * (first - 1) / lineCount) + '%';
  view.style.height = (100 * (last - first + 1) / lineCount) + '%';
}
function schedule() {
  if (!framePending) {
    framePending = true;
    requestAnimationFrame(updateView);
  }
}
// Scroll padding rather than a custom property on every row, so that a header that wraps
// differently restyles one element, not every line of the file.
function syncHeader() {
  barHeight = getComputedStyle(header).position === 'sticky' ? header.getBoundingClientRect().height : 0;
  root.style.scrollPaddingTop = barHeight + 'px';
  minimap.style.top = barHeight + 'px';
  minimap.hidden = lineCount * ROW <= window.innerHeight - barHeight;
  if (panel) { panel.style.top = barHeight + 'px'; }
  schedule();
}

buildMinimap();
syncHeader();
window.addEventListener('scroll', schedule, {passive: true});
window.addEventListener('resize', syncHeader);
if (window.ResizeObserver) { new ResizeObserver(syncHeader).observe(header); }
if (recall('cov-fold') === '1') { setFold(true); }
if (buttons.outline && recall('cov-outline') === '1') { setOutline(true); }
window.addEventListener('hashchange', showHash);
if (location.hash) { showHash(); }
"""

SOURCE_VIEW_SCRIPT = ("(function () {\n  'use strict';\n" + textwrap.indent(
    SOURCE_VIEW_CORE_SCRIPT + SOURCE_VIEW_PAGE_SCRIPT.replace('@ROW@', str(ROW_HEIGHT_PX)).replace(
        '@CHUNK@', str(CHUNK_LINES)).replace('@FOLD@', str(FOLD_MIN_LINES)), '  ') + "})();\n")

_HEADER_LEGEND = (
    '<div class=key><span class=u>uncovered</span><span class=p>partially taken</span>'
    '<span class=x>not instrumented</span><span class=heat><span>hits</span><i>1+</i>'
    '<i class=h2>10+</i><i class=h3>100+</i><i class=h4>1k+</i><i class=h5>10k+</i></span></div>')


def _format_count(count):
    """llvm-cov's formatCount (three significant digits, SI suffix), reproduced so pages compare cell by cell: 40912345 is '40.9M'."""
    digits = str(count)
    length = len(digits)
    if length <= 3:
        return digits
    integer_length = length % 3 or 3
    text = digits[:integer_length]
    if integer_length != 3:
        text += '.' + digits[integer_length:3]
    return text + ' kMGTPEZY'[(length - 1) // 3]


def _read_source(path):
    """The file's lines, or None if it cannot be read.

    Split on newlines and drop one trailing empty line, as llvm-cov does. Not str.splitlines(), which also splits on
    form feed, U+2028 and others, and would number lines differently (XPathGrammar.cpp has form feeds).
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            lines = handle.read().split('\n')
    except OSError:
        return None
    if lines and not lines[-1]:
        lines.pop()
    return lines


def partial_branch_lines(branches, conditions=False):
    """{line: (taken, total)} for the lines where some branch was never taken.

    Only partial lines, because only those are marked and this map crosses a process boundary once per file.
    With conditions, each value gains ((true count, false count), ...) per condition. BRDA branches number in pairs, even
    true and odd false; a direction with no record is None. A line with no branch taken has no pairs.
    """
    totals = {}
    for (line, _, _), count in branches.items():
        entry = totals.get(line)
        if entry is None:
            totals[line] = entry = [0, 0]
        entry[1] += 1
        if count:
            entry[0] += 1
    partial = {}
    detailed = {}
    for line, (taken, total) in totals.items():
        if taken < total:
            try:
                number = int(line)
            except ValueError:
                continue
            partial[number] = (taken, total)
            if taken:
                detailed[line] = number
    if not conditions:
        return partial

    pairs = defaultdict(dict)
    if detailed:
        for (line, block, branch), count in branches.items():
            if line in detailed:
                try:
                    branch = int(branch)
                    pair = pairs[line].setdefault((int(block), branch >> 1), [None, None])
                except ValueError:
                    continue
                pair[branch & 1] = count
    ordered = {detailed[line]: tuple(tuple(found[key]) for key in sorted(found))
               for line, found in pairs.items()}
    return {number: entry + (ordered.get(number, ()),) for number, entry in partial.items()}


def branch_counts(branches):
    """(branches taken, branches) over every BRDA record of a file."""
    return sum(1 for count in branches.values() if count), len(branches)


def function_summaries(lines, function_lines, last_line):
    """[(start, hits, lines that ran, instrumented lines)]; a function owns lines up to the next start."""
    starts = sorted(start for start in function_lines if 1 <= start <= last_line)
    numbers = sorted(number for number in lines if number <= last_line)
    ran = list(itertools.accumulate((1 if lines[number] else 0 for number in numbers), initial=0))
    edges = [bisect.bisect_left(numbers, start) for start in starts] + [len(numbers)]
    return [(start, function_lines[start], ran[edges[index + 1]] - ran[edges[index]],
             edges[index + 1] - edges[index]) for index, start in enumerate(starts)]


def _functions_attribute(summaries):
    """function_summaries() as the script reads it."""
    return ';'.join('%d:%d:%d:%d' % summary for summary in summaries)


def _stat_html(label, covered, total):
    if not total:
        return '<li><b>%s</b> <span>no data</span>' % label
    percent = 100.0 * covered / total
    return ('<li><b>%s</b> <span class=pct>%.2f%%</span> <span>%s/%s</span>'
            '<span class=bar style=--v:%.2f%%></span>' % (
                label, percent, format(covered, ','), format(total, ','), percent))


def _branch_figure(entry):
    """The gutter's taken/total for a partial line, with each condition's counts when known."""
    figure = '%d/%d</i>' % entry[:2]
    if len(entry) < 3 or not entry[2]:
        return '<i>' + figure
    return '<i data-b="%s">%s' % (','.join(
        ' '.join('-' if count is None else _format_count(count) for count in pair)
        for pair in entry[2]), figure)


def _mark_regions(text, spans):
    """text, escaped, with spans of 1-based UTF-8 byte columns wrapped in <mark>. An end of None is the end of the line."""
    length = len(text)
    if text.isascii():
        def offset(column):
            return min(max(column - 1, 0), length)
    else:
        boundaries = list(itertools.accumulate(
            (len(character.encode('utf-8')) for character in text), initial=0))

        def offset(column):
            return min(bisect.bisect_left(boundaries, max(column - 1, 0)), length)

    merged = []
    for start, end in sorted((offset(start or 1), length if end is None else offset(end))
                             for start, end in spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    escape = html.escape
    out = []
    position = 0
    for start, end in merged:
        out.append(escape(text[position:start], False))
        out.append('<mark>')
        out.append(escape(text[start:end], False))
        out.append('</mark>')
        position = end
    out.append(escape(text[position:], False))
    return ''.join(out)


def fitting_source(path, highest_line, build_directory=None):
    """(text, where it came from, why there is none). Exactly one of the first and last is set.

    The file itself if its length accounts for the highest recorded line, else the build directory's copy (what was compiled,
    even for a header since deleted), else nothing: a page against the wrong text shows no symptom. Where it came from is
    None for the file itself.
    """
    source = _read_source(path)
    if source is not None and highest_line <= len(source) + END_OF_FILE_SEGMENT_LINES:
        return source, None, None
    for candidate in compiled_copy_candidates(path, build_directory):
        copied = _read_source(candidate)
        if copied is not None and highest_line <= len(copied) + END_OF_FILE_SEGMENT_LINES:
            return copied, candidate, None
    if source is None:
        return None, None, UNREADABLE_SOURCE
    return None, None, RECORDS_PAST_END_OF_FILE


def built_at_from_provenance(output_directory):
    """The newest reported binary's modification time, or None when there is none to read.

    A source newer than every binary cannot be the text that was compiled, and nothing else catches that. Read from
    coverage-provenance.json, so a missing or newer-schema record just skips the check.
    """
    from webkitpy.coverage_provenance import PROVENANCE_FILENAME

    try:
        with open(os.path.join(output_directory, PROVENANCE_FILENAME)) as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    stamps = []
    for state in record.get('objects') or ():
        stamp = state.get('modified_at') if isinstance(state, dict) else None
        if not stamp:
            continue
        try:
            stamps.append(datetime.datetime.fromisoformat(
                stamp.replace('Z', '+00:00')).timestamp())
        except (AttributeError, ValueError):
            continue
    return max(stamps) if stamps else None


def render_source_view(relative_path, lines, partial, source, up, rendered_from=None,
                       function_lines=None, regions=None, branch_totals=None):
    """One page. lines is {line: count} and partial is partial_branch_lines()'s.

    function_lines is {start line: count}. regions is {line: [(start column, end column or None)]} in llvm-cov's 1-based
    UTF-8 byte columns, end exclusive. branch_totals is (taken, total) over the file. Any of the three can be None.
    """
    escape = html.escape
    directory, _, name = relative_path.rpartition('/')
    crumbs = ['<li><a href="{}index.html">All source</a>'.format(up)]
    if directory:
        crumbs.append('<li><a href="index.html">{}</a>'.format(escape(directory)))
    crumbs.append('<li aria-current=page>' + escape(name))

    instrumented = len(lines)
    missed = sum(1 for count in lines.values() if not count)
    subtitle = '{:,} of {:,} instrumented lines never executed'.format(missed, instrumented)
    shown_partial = [entry for line, entry in partial.items() if lines.get(line)]
    partly_taken = len(shown_partial)
    if partly_taken:
        subtitle += '; {:,} executed {} a branch that was never taken'.format(
            partly_taken, 'line has' if partly_taken == 1 else 'lines have')
    # Said on the page: a row count that does not match the instrumented count would otherwise be a silent gap.
    unplaceable = sum(1 for number in lines if number > len(source))
    notices = []
    if unplaceable:
        subtitle += ('; {:,} {} past line {:,}, the end of this text, and {} not shown'.format(
            unplaceable, 'record sits' if unplaceable == 1 else 'records sit', len(source),
            'is' if unplaceable == 1 else 'are'))
        notices.append('{:,} not shown'.format(unplaceable))
    if rendered_from:
        subtitle += ('. Rendered from {}, because the checkout\'s copy of this file no longer '
                     'accounts for the coverage records: the build directory\'s copy is the '
                     'text that was compiled'.format(rendered_from))
        notices.append('rendered from a copy')

    stats = [_stat_html('Lines', instrumented - missed, instrumented)]
    if function_lines:
        stats.append(_stat_html('Functions', sum(1 for count in function_lines.values() if count),
                                len(function_lines)))
    if branch_totals and branch_totals[1]:
        stats.append(_stat_html('Branches', *branch_totals))
    stats.extend('<li class=warn title="%s">%s' % (escape(subtitle), notice) for notice in notices)

    # The gutter's column widths, in ch. _format_count() is at most five characters wide.
    highest = max(lines.values(), default=None)
    count_width = 0 if highest is None else len(str(highest)) if highest < 1000 else 5
    branch_width = max((len('%d/%d' % entry[:2]) + 1 for entry in shown_partial), default=0)
    outline = _functions_attribute(function_summaries(lines, function_lines or {}, len(source)))

    out = ['<!DOCTYPE html><html lang=en><head><meta charset=utf-8>'
           '<meta name=viewport content="width=device-width,initial-scale=1"><title>',
           escape(relative_path),
           '</title><script src="', up, THEME_SCRIPT_NAME, '"></script><link rel=stylesheet href="',
           up, STYLESHEET_NAME, '"><script defer src="', up, SCRIPT_NAME,
           '"></script></head><body><header><div class=top><nav class=crumbs '
           'aria-label=Breadcrumb><ol>', ''.join(crumbs), '</ol></nav>', search_box_html(up),
           THEME_TOGGLE_HTML, '</div><div class=sub><ul class=stats>', ''.join(stats),
           '</ul><div class=tools></div></div></header><section class=info><h1>',
           escape(relative_path), '</h1><p>', escape(subtitle, False), '</p>', _HEADER_LEGEND,
           '</section><main style="--lw:%d;--bw:%d;--cw:%d;--w:%d"%s>' % (
               len(str(len(source))), branch_width, count_width,
               max(map(len, source), default=0), ' data-fns="%s"' % outline if outline else '')]
    append = out.append
    get = lines.get
    get_partial = partial.get
    get_regions = regions.get if regions else {}.get
    row_class = ROW_CLASS
    format_count = _format_count
    total = len(source)
    for chunk_start in range(0, total, CHUNK_LINES):
        chunk_end = min(total, chunk_start + CHUNK_LINES)
        # Only a short last chunk says how tall it is; the stylesheet assumes CHUNK_LINES.
        append('<ol>' if chunk_end - chunk_start == CHUNK_LINES
               else '<ol style=--n:%d>' % (chunk_end - chunk_start))
        # content-visibility contains style, which scopes the counter to the chunk, so every chunk after the first
        # restates where it starts.
        first = chunk_start + 1 if chunk_start else 0
        for number in range(chunk_start + 1, chunk_end + 1):
            text = source[number - 1]
            count = get(number)
            start = ' style="counter-set:ln %d"' % number if number == first else ''
            # The newline leads the row so the page has one line per row; white-space:pre keeps it as the line break.
            if count is None:
                append('\n<li id=L%d%s>%s' % (number, start, escape(text, False)))
                continue
            # An uncovered row's count is always 0, so the stylesheet draws it from the class.
            if not count:
                append('\n<li id=L%d%s%s>%s' % (
                    number, row_class[line_state(count, None)], start, escape(text, False)))
                continue
            taken = get_partial(number)
            spans = get_regions(number)
            append('\n<li id=L%d%s%s>%s<b>%s</b>%s' % (
                number, row_class[line_state(count, taken)], start,
                '' if taken is None else _branch_figure(taken), format_count(count),
                escape(text, False) if spans is None else _mark_regions(text, spans)))
        append('</ol>')
    append('</main></body></html>\n')
    return ''.join(out)


def relative_source_path(path, source_root):
    """Where a page for path goes, relative to the report root.

    The same rule write_directory_index uses, so a file's page sits beside the index that links to it. A path outside the root
    goes under OUTSIDE_SOURCE_ROOT_DIRECTORY, not at its absolute depth (see effective_source_prefix()). A page is written
    only where is_confined() says so.
    """
    path = posixpath.normpath(path)
    root = source_root.rstrip('/') if source_root else ''
    if root and path.startswith(root + '/'):
        return path[len(root) + 1:]
    if root:
        return OUTSIDE_SOURCE_ROOT_DIRECTORY + '/' + path.lstrip('/')
    return path.lstrip('/')


def is_confined(relative):
    """Whether the page for a relative_source_path() stays inside the report directory."""
    return '\0' not in relative and all(part not in ('', '.', '..') for part in relative.split('/'))


def drop_unconfined(coverage_by_path, source_root):
    """coverage_by_path without the files whose page would fall outside the report directory. Warns once, with the count."""
    kept = {path: coverage for path, coverage in coverage_by_path.items()
            if is_confined(relative_source_path(path, source_root))}
    refused = len(coverage_by_path) - len(kept)
    if refused:
        logger.warning('%d files have a path that would put their page outside the report directory, so they '
                       'have no page and no row. Their coverage is still in the trace. For example: %s',
                       refused, next(path for path in coverage_by_path if path not in kept))
    return kept


def _write_one(job):
    (path, lines, partial, function_lines, regions, totals, output_directory, source_root,
     build_directory, built_at) = job
    source, origin, reason = fitting_source(path, max(lines) if lines else 0, build_directory)
    if reason is not None:
        return _PageResult(path, None, reason, False, None)
    newer = False
    if built_at is not None:
        try:
            newer = os.path.getmtime(origin or path) > built_at
        except OSError:
            pass
    relative = relative_source_path(path, source_root)
    target = os.path.join(output_directory, source_page_path(relative))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    page = render_source_view(relative, lines, partial, source, '../' * relative.count('/'),
                              rendered_from=origin, function_lines=function_lines,
                              regions=regions, branch_totals=totals).encode('utf-8')
    with open(target, 'wb') as handle:
        handle.write(page)
    return _PageResult(path, len(page), None, newer, origin)


def write_source_views(coverage_by_path, output_directory, source_root, workers=None,
                       build_directory=None, built_at=None, regions=None):
    """Write one page per file. Returns (pages, bytes written, {path: why it has no page}).

    Takes the parsed trace, which the directory index needs too. A file with no page (unreadable source, or records past its
    end) is returned with the reason so the index can say why; its coverage still counts. build_directory lets a copied
    header render from the build directory's copy. built_at is the newest binary's mtime. regions is {path: {line: [...]}}.
    """
    os.makedirs(output_directory, exist_ok=True)
    for name, contents in ((STYLESHEET_NAME, SOURCE_VIEW_STYLE), (SCRIPT_NAME, SOURCE_VIEW_SCRIPT)):
        with open(os.path.join(output_directory, name), 'w', encoding='utf-8') as handle:
            handle.write(contents.lstrip('\n'))
    write_theme_script(output_directory)
    coverage_by_path = drop_unconfined(coverage_by_path, source_root)

    # A generator, so the pool starts on the first pages while the main process prepares the rest (partial_branch_lines() is serial).
    regions = regions or {}
    jobs = ((path, coverage.lines, partial_branch_lines(coverage.branches, conditions=True),
             coverage.function_lines, regions.get(path), branch_counts(coverage.branches),
             output_directory, source_root, build_directory, built_at)
            for path, coverage in coverage_by_path.items())

    # Eight rather than every core: the work is short either way, and each worker holds a whole file's line map and rendered page.
    if workers is None:
        workers = min(8, os.cpu_count() or 1)

    written_bytes = 0
    pages = 0
    skipped = {}
    stale = []
    from_copy = []
    if workers > 1 and len(coverage_by_path) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_write_one, jobs, chunksize=64))
    else:
        results = [_write_one(job) for job in jobs]
    for result in results:
        if result.size is None:
            skipped[result.path] = result.reason
        else:
            pages += 1
            written_bytes += result.size
            if result.rendered_from:
                from_copy.append((result.path, result.rendered_from))
            if result.newer_than_build:
                stale.append(result.path)

    _log_fit(pages, skipped, stale, from_copy)
    return pages, written_bytes, skipped


def _log_fit(pages, skipped, stale, from_copy):
    if from_copy:
        logger.info('%d line views were rendered from the build directory\'s copy of the file, '
                    'because the checkout\'s copy no longer accounts for the coverage records. '
                    'For a copied header the build directory\'s copy is the text that was '
                    'compiled.', len(from_copy))
        for path, origin in sorted(from_copy)[:5]:
            logger.debug('    %s rendered from %s', path, origin)

    by_reason = defaultdict(list)
    for path, reason in skipped.items():
        by_reason[reason].append(path)

    past_end = sorted(by_reason.get(RECORDS_PAST_END_OF_FILE, ()))
    if past_end:
        logger.warning('%d files have coverage records past the end of the file on disk, so '
                       'they have no line view: the tree has moved on since the binaries were '
                       'built, and every row would be beside the wrong line. Their coverage is '
                       'still counted -- only the line-by-line page is withheld.', len(past_end))
        for path in past_end[:10]:
            logger.warning('    %s', path)
        if len(past_end) > 10:
            logger.warning('    ... and %d more', len(past_end) - 10)

    unreadable = sorted(by_reason.get(UNREADABLE_SOURCE, ()))
    if unreadable:
        logger.info('%d files have no line view because their source could not be read; '
                    'they are listed in the index without a link', len(unreadable))
        for path in sorted(unreadable)[:5]:
            logger.debug('    unreadable: %s', path)

    if stale:
        logger.warning('%d of the %d line views are rendered from a file that is newer than '
                       'every binary in this report, so the text shown may not be the text that '
                       'was compiled. Coverage data cannot detect that at all when the line '
                       'numbers still fit -- no row is missing and no count is wrong -- so the '
                       'file times are the only signal there is.', len(stale), pages)
        for path in sorted(stale)[:10]:
            logger.warning('    %s', path)
        if len(stale) > 10:
            logger.warning('    ... and %d more', len(stale) - 10)

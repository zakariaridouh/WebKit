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

"""Render a line-by-line coverage view for every file in an lcov trace.

llvm-cov show renders the same thing and is by far the largest part of the report: measured on
a full-suite WebKit run, 909,014,282 bytes across 18,238 pages, plus an 8,296,644-byte flat
index. That is not a formatting quibble at that size -- it is most of a 1.7 GB report, and it
is generated from data the report generator has already parsed and thrown away.

Rendering from the trace instead is 287.8 MB for the same 16,149 files, a 68% saving, and it
takes 2.2s against llvm-cov show's 30.5s. The markup is where the size went, not the content:
llvm-cov emits about 1.4 KB of table scaffolding per hundred lines (an <a name> and a nested
<pre> in every cell of every row), while this emits `<li id=L36><b>12</b>code`, numbers the
lines with a CSS counter, and leans on HTML5's optional end tags and one shared stylesheet and
script. A line with no coverage record is just `<li id=L36>code`, and one that never ran is
`<li id=L36 class=u>code`, its 0 drawn by the stylesheet.

lcov cannot express sub-line detail: llvm-cov marks the individual uncovered *regions* inside a
line, and on the measured run 23,081 lines are covered but contain a region that never
executed. Two things recover it here. lcov's branch records, which llvm-cov show does not
display at all, mark a covered line with an untaken branch and give its taken/total and each
condition's true and false counts. And when the caller has llvm-cov's JSON export, the
uncovered regions of a line that ran are highlighted in place; see render_source_view().
generate-coverage-report --llvm-cov-html still produces llvm-cov's own pages alongside these.

Rendering from the trace also means rendering against whatever is on disk *now*, and nothing
in a profile or a Mach-O records the revision it came from. So a page can describe a file it
is not describing, and this module's job is to refuse to do that:

  * A record past the end of the file is proof that the text is not the text that was
    compiled. Measured on the shipped report: 29 files, 858 rows, and wtf/Expected.h with 394
    instrumented lines against a 31-line file. Those rows used to be dropped by lines.get()
    while the subtitle went on quoting the full count.

  * When the checkout's copy of a header no longer accounts for the records, the build
    directory's copy is tried, because for a copied header that copy *is* the text the
    compiler saw. That recovers 8 of the 29 exactly.

  * Where neither fits, the file gets no line view at all and the index says why. Its coverage
    is still counted -- only the misleading page is withheld.

  * A file grew, or its lines merely moved, and every row is against the wrong line with no
    symptom whatsoever: no row is missing, no count is wrong. Coverage data cannot detect
    that. File times can, so every rendered file is checked against the newest binary in the
    report and the count is reported. On the measured tree that is 256 of 15,976 files, and it
    covers 22 of the 29 the length check catches plus 234 it cannot.
"""

import bisect
import datetime
import html
import itertools
import json
import logging
import os
from collections import defaultdict, namedtuple
from concurrent.futures import ProcessPoolExecutor

from webkitpy.coverage_lcov import compiled_copy_candidates
from webkitpy.coverage_report_assets import search_box_html

logger = logging.getLogger(__name__)

# One stylesheet and one script at the report root, shared by every page. Inlining them would
# cost more than the source text does: 1.5 KB times 16,149 pages is 24 MB.
STYLESHEET_NAME = 'coverage-source.css'
SCRIPT_NAME = 'coverage-source.js'

# Rows are grouped into <ol> chunks of CHUNK_LINES that the browser does not lay out until
# they are scrolled near (content-visibility:auto). Every row is exactly ROW_HEIGHT_PX tall, so
# a skipped chunk's placeholder is its real height: #L123 lands exactly, and the script can
# place any line by arithmetic instead of forcing layout.
CHUNK_LINES = 500
ROW_HEIGHT_PX = 17

# How far past the end of a file a record may sit and still be llvm-cov's rather than a sign
# that the file has changed. llvm-cov's mapping for a file that is #included into another
# translation unit carries a region ending at "one past the last line, column 1", and the lcov
# export emits a DA: for it. Measured over the shipped report: exactly 5 files overshoot by
# exactly one line, all of them .def macro lists, and the build directory's own copy of three
# of them is byte-identical to the checkout's -- pas_heap_config_kind.def is 80 lines and
# sha256 23b93160... on both sides, with a DA:81 in the trace -- which proves the overshoot is
# llvm-cov's and not drift. Everything beyond one line in that report was drift.
END_OF_FILE_SEGMENT_LINES = 1

# Why a file has no line view, shown in the index. "This file has coverage data but no page"
# is otherwise a dead end for whoever is reading it, and the reasons want different actions:
# one is a build directory that has been cleaned, one is a tree that has moved on since the
# binaries were built, and one is a flag the reader passed themselves.
UNREADABLE_SOURCE = 'the source could not be read'
RECORDS_PAST_END_OF_FILE = ('the coverage records run past the end of the file on disk, so '
                            'this is not the text that was compiled')
LINE_VIEWS_NOT_WRITTEN = 'this report was written with --no-source-views'

# Where a covered path that is not under the source root goes in the report tree. Hyphenated
# and lowercase so that it cannot collide with a real top-level directory of the checkout, and
# so that it needs no escaping in a URL.
OUTSIDE_SOURCE_ROOT_DIRECTORY = 'outside-the-checkout'

_PageResult = namedtuple('_PageResult',
                         ('path', 'size', 'reason', 'newer_than_build', 'rendered_from'))

# The states a rendered line can be in. The row loop asks line_state() for one and looks its
# markup up, so adding a state is a constant, a ROW_CLASS entry, one branch in line_state() and
# one rule in the stylesheet -- not another rewrite of the loop, which is the hot path over the
# 1.9 million rows of a full-suite report.
#
# A selective run -- one that reports on the whole tree but only ran a subset of the tests --
# needs a fourth: a line in a file this configuration compiled, in a binary that no test in the
# selected scope loaded. Today that is indistinguishable from NOT_INSTRUMENTED, because both
# are "no record in the trace", and it must not be read as either of the two states that would
# otherwise absorb it: NOT_INSTRUMENTED understates the gap by hiding it, and UNCOVERED
# overstates it by blaming the tests for code no test in scope could have reached. That state
# is deliberately not implemented here; what is implemented is that it can be added without
# touching anything else.
NOT_INSTRUMENTED = 'not-instrumented'
UNCOVERED = 'uncovered'
COVERED = 'covered'
PARTIALLY_TAKEN = 'partially-taken'

# The class attribute for each state, including the leading space, so that the states with no
# class of their own cost nothing per row. li.u and li.p are the stylesheet's names.
ROW_CLASS = {
    NOT_INSTRUMENTED: '',
    UNCOVERED: ' class=u',
    COVERED: '',
    PARTIALLY_TAKEN: ' class=p',
}


def line_state(count, taken):
    """The state of one rendered line.

    count is None when the trace has no record for the line at all, and taken is its
    (branches taken, branches total) when some branch on it was never taken.
    """
    if count is None:
        return NOT_INSTRUMENTED
    if not count:
        return UNCOVERED
    return COVERED if taken is None else PARTIALLY_TAKEN


# The gutter holds, left to right: the line number (a CSS counter), the taken/total of a line
# with an untaken branch, the count, and a marker that does not depend on color -- "!" for a
# line that never ran, "~" for one with an untaken branch. Its columns are sized per page, in
# ch, from the widest value each holds: --lw, --bw and --cw on <main>, and --w for the widest
# line. The palette is the one coverage_directory_index.py uses, so the two halves of the
# report match. The partial and uncovered tints differ in hue, not just strength, so that they
# can be told apart in dark mode, where both used to read as the same brown.
_DARK_PALETTE = """
    color-scheme: dark;
    --page: #0d0d0d;
    --surface-1: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --gridline: #2c2c2a;
    --link: #3987e5;
    --missed: #f0776a;
    --missed-row: rgba(240, 119, 106, 0.17);
    --partial: #e9c46a;
    --partial-row: rgba(233, 196, 106, 0.16);
    --region: rgba(240, 119, 106, 0.36);
    --target: rgba(57, 135, 229, 0.30);
    --shadow: rgba(0, 0, 0, 0.5);
"""

SOURCE_VIEW_STYLE = """
:root {
  color-scheme: light;
  --page: #f9f9f7;
  --surface-1: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --gridline: #e1e0d9;
  --link: #2a78d6;
  --missed: #c0392b;
  --missed-row: rgba(192, 57, 43, 0.10);
  --partial: #8c5a00;
  --partial-row: rgba(222, 170, 20, 0.18);
  --region: rgba(192, 57, 43, 0.22);
  --target: rgba(42, 120, 214, 0.16);
  --shadow: rgba(0, 0, 0, 0.12);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {@DARK@  }
}
:root[data-theme="dark"] {@DARK@}
html { scroll-padding-top: 120px; }
body {
  margin: 0; width: max-content; min-width: 100%;
  background: var(--page); color: var(--text-primary);
  font: 12px/@ROW@px ui-monospace, SFMono-Regular, Menlo, monospace;
}
header {
  position: sticky; top: 0; left: 0; z-index: 2;
  box-sizing: border-box; max-width: 100vw; contain: inline-size;
  display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 16px;
  padding: 10px 14px 7px; background: var(--page); border-bottom: 1px solid var(--gridline);
  font: 12px/1.5 system-ui, -apple-system, sans-serif; color: var(--text-secondary);
}
@media (max-height: 480px) { header { position: static; } }
header > * { grid-column: 1 / -1; }
header > h1, header > .crumbs { grid-column: 1; }
h1 {
  font: 600 13px/1.4 system-ui, -apple-system, sans-serif; margin: 0;
  color: var(--text-primary); overflow-wrap: anywhere;
}
header p { margin: 0; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
/* The search script styles its own box; this only places it, and sizes the input as that
   script will, so that the header does not move when it loads. */
.report-search { position: relative; grid-column: 2; grid-row: 1 / span 2; width: 320px; }
@media (max-width: 640px) { .report-search { grid-column: 1 / -1; grid-row: auto; width: auto; } }
.report-search input {
  box-sizing: border-box; width: 100%; padding: 5px 9px;
  font: 12px/1.4 system-ui, -apple-system, sans-serif; color: var(--text-primary);
  background: var(--surface-1); border: 1px solid var(--gridline); border-radius: 6px;
}
/* min-height is the gap buttons' height, so that adding them does not move the page. */
.key {
  display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px;
  min-height: 22px; margin-top: 6px;
}
.key span { white-space: nowrap; }
.key span::before {
  content: "\\a0"; display: inline-block; width: 16px; margin-right: 5px; text-align: center;
  font: 700 11px/14px ui-monospace, SFMono-Regular, Menlo, monospace;
  border: 1px solid var(--gridline); border-radius: 3px;
}
.key .u::before { content: "!"; color: var(--missed); background: var(--missed-row); }
.key .p::before { content: "~"; color: var(--partial); background: var(--partial-row); }
nav { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-left: 12px; }
nav button {
  font: inherit; padding: 1px 9px; color: var(--text-primary); background: var(--surface-1);
  border: 1px solid var(--gridline); border-radius: 5px; cursor: pointer;
}
nav button:disabled { opacity: 0.5; cursor: default; }
main {
  --gutter: calc((var(--lw) + var(--bw) + var(--cw) + 6) * 1ch);
  width: max-content; min-width: max(100%, calc(var(--gutter) + var(--w) * 1ch + 14px));
  padding: 6px 0 40vh;
}
main ol {
  margin: 0; padding: 0; list-style: none;
  content-visibility: auto; contain-intrinsic-size: none auto calc(var(--n, @CHUNK@) * @ROW@px);
}
main li {
  position: relative; height: @ROW@px; padding: 0 14px 0 var(--gutter);
  white-space: pre; counter-increment: ln; scroll-margin-top: calc(3 * @ROW@px);
}
main li::before {
  content: counter(ln); position: absolute; left: 0; box-sizing: border-box;
  width: calc((var(--lw) + 2) * 1ch); padding-right: 1ch; text-align: right;
  color: var(--muted); border-right: 1px solid var(--gridline); cursor: pointer; user-select: none;
}
main b, main i, main li.u::after, main li.p::after {
  position: absolute; top: 0; font-weight: 400; font-style: normal; user-select: none;
}
main i {
  left: calc((var(--lw) + 2) * 1ch); width: calc(var(--bw) * 1ch); text-align: right;
  color: var(--partial); cursor: help;
}
main b {
  left: calc((var(--lw) + var(--bw) + 2) * 1ch); width: calc((var(--cw) + 1) * 1ch);
  text-align: right; color: var(--text-secondary);
}
main li.u { background-color: var(--missed-row); }
main li.u::after {
  content: "0 !"; left: calc((var(--lw) + var(--bw) + var(--cw) + 2) * 1ch);
  color: var(--missed); font-weight: 700;
}
main li.p { background-color: var(--partial-row); }
main li.p::after {
  content: "~"; left: calc((var(--lw) + var(--bw) + var(--cw) + 4) * 1ch);
  color: var(--partial); font-weight: 700;
}
main mark {
  color: inherit; background: var(--region); border-radius: 2px;
  text-decoration: underline dotted var(--missed); text-underline-offset: 3px;
}
:root:not(.js) main li:target, main li.s {
  background-image: linear-gradient(var(--target), var(--target));
  box-shadow: inset 3px 0 var(--link);
}
.conditions {
  position: absolute; z-index: 3; padding: 5px 9px; white-space: pre;
  color: var(--text-primary); background: var(--surface-1);
  border: 1px solid var(--gridline); border-radius: 6px; box-shadow: 0 4px 16px var(--shadow);
}
""".replace('@DARK@', _DARK_PALETTE).replace('@ROW@', str(ROW_HEIGHT_PX)).replace(
    '@CHUNK@', str(CHUNK_LINES))

# Line links, gap navigation and the per-condition branch counts. Everything it needs is on
# the page already -- the gaps in <main data-gaps>, the counts in <i data-b> -- so it adds
# nothing per row. It places lines by arithmetic, never by asking a row for its position,
# because a row in a chunk that has not been laid out would force that layout.
SOURCE_VIEW_SCRIPT = """
(function () {
  'use strict';
  var ROW = @ROW@;
  var CONTEXT_ROWS = 3;
  var root = document.documentElement;
  var main = document.querySelector('main');
  var header = document.querySelector('body > header');
  if (!main || !main.firstElementChild) { return; }
  root.classList.add('js');
  var lastRow = main.lastElementChild.lastElementChild;
  var lineCount = lastRow ? +lastRow.id.slice(1) : 0;

  function headerHeight() {
    return header && getComputedStyle(header).position === 'sticky'
        ? header.getBoundingClientRect().height : 0;
  }
  // Scroll padding rather than a custom property on every row, so that a header that wraps
  // differently restyles one element, not every line of the file. The browser's own #L123
  // scroll uses it too, and may run after this script does.
  function syncHeader() { root.style.scrollPaddingTop = headerHeight() + 'px'; }

  function lineTop(line) {
    return main.firstElementChild.getBoundingClientRect().top + (line - 1) * ROW;
  }
  function isVisible(line) {
    var top = lineTop(line);
    return top >= headerHeight() && top + ROW <= window.innerHeight;
  }
  function firstVisibleLine() {
    var line = Math.ceil((headerHeight() - lineTop(1)) / ROW) + 1;
    return Math.min(lineCount, Math.max(1, line));
  }
  // The same offset scroll-padding and scroll-margin give a native #L123 navigation.
  function scrollToLine(line) {
    window.scrollTo(window.scrollX,
                    window.scrollY + lineTop(line) - headerHeight() - CONTEXT_ROWS * ROW);
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
  function hashRange() {
    var match = /^#L(\\d+)(?:-L?(\\d+))?$/.exec(location.hash);
    if (!match) { return null; }
    var first = +match[1], last = match[2] ? +match[2] : first;
    if (first > last) { var swap = first; first = last; last = swap; }
    first = Math.max(first, 1);
    last = Math.min(last, lineCount);
    return first <= last ? [first, last] : null;
  }
  function showHash() {
    var range = hashRange();
    select(range ? range[0] : 1, range ? range[1] : 0);
    if (range) {
      anchor = range[0];
      scrollToLine(range[0]);
    }
  }
  function setHash(first, last) {
    var hash = '#L' + first + (last > first ? '-L' + last : '');
    try {
      history.replaceState(null, '', hash);
    } catch (error) {
      location.replace(hash);
      return;
    }
    select(first, last);
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
    if (target.tagName === 'I' && target.dataset.b) {
      toggleConditions(target);
      return;
    }
    var row = target.closest('li');
    if (!row || !inGutter(event, row)) { return; }
    var line = +row.id.slice(1);
    if (event.shiftKey && anchor) {
      setHash(Math.min(anchor, line), Math.max(anchor, line));
    } else {
      anchor = line;
      setHash(line, line);
    }
  });

  // data-gaps is "first[+length][:count of the enclosing function]", comma-separated.
  var gaps = [];
  (main.dataset.gaps || '').split(',').forEach(function (entry) {
    var match = /^(\\d+)(?:\\+(\\d+))?(?::(.+))?$/.exec(entry);
    if (match) {
      gaps.push({first: +match[1], last: +match[1] + (+match[2] || 0), ran: match[3]});
    }
  });
  var status = document.createElement('output');
  function reason(gap) {
    if (gap.ran === undefined) { return 'no function in the trace encloses it'; }
    if (gap.ran === '0') { return 'the enclosing function was never entered: look for a caller'; }
    return 'the function ran ' + gap.ran + (gap.ran === '1' ? ' time' : ' times')
        + ' but this block was skipped: look at the condition';
  }
  function step(delta) {
    if (!gaps.length) { return; }
    // From the linked range while it is on screen, otherwise from the top of the viewport.
    var range = hashRange();
    var from = range && isVisible(range[0]) ? range[0] : firstVisibleLine() - (delta > 0 ? 1 : 0);
    var index = delta > 0 ? gaps.length : -1;
    for (var i = 0; i < gaps.length; i++) {
      if (delta > 0 && gaps[i].first > from) { index = i; break; }
      if (delta < 0 && gaps[i].first < from) { index = i; }
    }
    index = (index + gaps.length) % gaps.length;
    var gap = gaps[index];
    setHash(gap.first, gap.last);
    if (!isVisible(gap.first) || !isVisible(Math.min(gap.last, gap.first + CONTEXT_ROWS))) {
      scrollToLine(gap.first);
    }
    status.textContent = 'Gap ' + (index + 1) + ' of ' + gaps.length + ' at line ' + gap.first
        + ': ' + reason(gap);
  }
  var nav = document.createElement('nav');
  nav.setAttribute('aria-label', 'Coverage gaps');
  [[-1, 'Previous gap', 'p or k'], [1, 'Next gap', 'n or j']].forEach(function (control) {
    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = control[1];
    button.title = control[2];
    button.disabled = !gaps.length;
    button.addEventListener('click', function () { step(control[0]); });
    nav.appendChild(button);
  });
  status.textContent = gaps.length
      ? gaps.length + (gaps.length === 1 ? ' gap' : ' gaps') + '; n and p (or j and k) step'
      : 'No gaps';
  nav.appendChild(status);
  var key = header && header.querySelector('.key');
  if (key) { key.appendChild(nav); }
  syncHeader();
  if (header && window.ResizeObserver) { new ResizeObserver(syncHeader).observe(header); }

  var STEPS = {n: 1, j: 1, p: -1, k: -1};
  document.addEventListener('keydown', function (event) {
    var target = event.target;
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) { return; }
    if (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) { return; }
    if (event.key === 'Escape') {
      hideConditions();
    } else if (Object.prototype.hasOwnProperty.call(STEPS, event.key)) {
      event.preventDefault();
      step(STEPS[event.key]);
    }
  });

  // data-b is "true false" per condition, comma-separated, in llvm-cov's order.
  function describe(figure) {
    var pairs = figure.dataset.b.split(',');
    return pairs.map(function (pair, index) {
      var counts = pair.split(' ');
      return (pairs.length > 1 ? 'Condition ' + (index + 1) + ': ' : '')
          + 'T ' + counts[0] + ' / F ' + counts[1];
    }).join('\\n');
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

  window.addEventListener('hashchange', showHash);
  if (location.hash) { showHash(); }
})();
""".replace('@ROW@', str(ROW_HEIGHT_PX))

# The legend, the same on every page; its swatches come from the stylesheet. The gap controls
# beside it are added by the script, because without the script they would do nothing, and at
# about 200 bytes a page they would be 3.3 MB of the synthetic full-scale report.
_HEADER_LEGEND = ('<div class=key><span class=u>uncovered</span>'
                  '<span class=p>partially taken</span><span>not instrumented</span></div>')


def _format_count(count):
    """llvm-cov's formatCount: at most three significant digits and an SI suffix.

    Reproduced rather than improved on, so that a page rendered here and a page rendered by
    llvm-cov show are comparable cell by cell. 40912345 is '40.9M', not '40.91M'.
    """
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

    Split on newlines and drop one trailing empty line, because that is what llvm-cov does: a
    file whose last line ends in a newline has that many lines, not one more, and an extra
    empty row at the end of every page would be 16,000 rows of noise as well as wrong.

    Deliberately not str.splitlines(), which also splits on form feed, vertical tab, U+2028,
    U+2029 and U+0085. Source/WebCore/xml/XPathGrammar.cpp is bison output with three form
    feeds in it, and splitting on those numbered every line after them differently from
    llvm-cov's page for the same file -- found by comparing all 7,966 implementation files,
    and it was the only such file. Text mode folds CRLF, so the only remaining difference from
    llvm-cov would be a file using bare CRs as line endings, of which there are none here.
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

    Only the partial lines, because those are the only ones the page marks, and because this
    crosses a process boundary once per file: the full branch map is 1,043,499 entries on a
    full-suite run and pickling all of it to say nothing about most of it is pure cost.

    With conditions, each value is (taken, total, ((true count, false count), ...)), one pair per
    condition in the order llvm-cov numbers them. Its lcov export writes one BRDA per direction,
    BRDA:line,condition,branch,count, and numbers the branches on a line in pairs: even is the
    true direction and odd the false one. A direction with no record is None. A line none of
    whose branches was taken has no pairs: it did not run, or every one of them would read 0/0,
    and on a full-suite trace those are most of the partial lines.
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


def coverage_gaps(lines, partial, function_lines, last_line):
    """[(first line, last line, count of the enclosing function or None)], in line order.

    A gap is a run of lines that never ran or have a branch that was never taken. A line with
    no record does not end one; a covered line does, and so does the start of a function,
    because what a gap says about itself is about its function: never entered, or entered and
    this block skipped. The enclosing function is the nearest one starting at or before the
    gap -- lcov records where a function starts and not where it ends.
    """
    starts = sorted(function_lines or ())
    gaps = []
    current = None
    for number in sorted(lines):
        if number > last_line:
            break
        if lines[number] and number not in partial:
            current = None
            continue
        enclosing = bisect.bisect_right(starts, number)
        if current is not None and current[2] == enclosing:
            current[1] = number
        else:
            current = [number, number, enclosing]
            gaps.append(current)
    return [(first, last, function_lines[starts[enclosing - 1]] if enclosing else None)
            for first, last, enclosing in gaps]


def _gaps_attribute(gaps):
    """coverage_gaps() as the script reads it: first[+length][:count], comma-separated."""
    entries = []
    for first, last, ran in gaps:
        entry = str(first)
        if last > first:
            entry += '+%d' % (last - first)
        if ran is not None:
            entry += ':' + _format_count(ran)
        entries.append(entry)
    return ','.join(entries)


def _branch_figure(entry):
    """The gutter's taken/total for a partial line, with each condition's counts when known."""
    figure = '%d/%d</i>' % entry[:2]
    if len(entry) < 3 or not entry[2]:
        return '<i>' + figure
    return '<i data-b="%s">%s' % (','.join(
        ' '.join('-' if count is None else _format_count(count) for count in pair)
        for pair in entry[2]), figure)


def _mark_regions(text, spans):
    """text, escaped, with spans of 1-based UTF-8 byte columns wrapped in <mark>.

    An end of None is the end of the line. The columns are bytes and the text is characters, so
    anything non-ASCII before a region would shift it if the two were mixed up.
    """
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

    The file itself when its length accounts for the highest line the trace has a record for;
    failing that the build directory's copy of it, when that does; and nothing when neither
    does, because a page rendered against text the records are not about has no symptom at
    all -- every row is present, every count is right, and every row is beside the wrong line.

    "where it came from" is None when it is the file itself, so a caller can say so on the page
    rather than quietly rendering somebody else's text.

    A missing file is tried against the build directory too: a header deleted from the checkout
    since the build still has its copy, and that copy is what was compiled.
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

    A source file newer than every binary in the report cannot be the text that was compiled
    into any of them. That is the only signal there is for the direction --check-binary-ids
    does not cover: it catches "the binaries are newer than the profile", and nothing catches
    "the source is newer than the binaries", which is guaranteed the moment anybody keeps
    working after a run -- i.e. always, in a per-patch workflow.

    Read from the provenance record beside the report rather than taken as a parameter, so that
    this needs no new command-line flag: generate-coverage-report already writes
    coverage-provenance.json into the output directory before it writes the report. Every field
    is optional here, so a record from a newer schema, or no record at all, degrades to not
    making the check rather than to failing.
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
                       function_lines=None, regions=None):
    """One page. lines is {line: count} and partial is partial_branch_lines()'s.

    function_lines is {start line: count}, which says why each gap is one. regions is
    {line: [(start column, end column or None)]}: the uncovered parts of lines that ran, in
    llvm-cov's 1-based UTF-8 byte columns with the end exclusive and None for the end of the
    line. Either can be None.
    """
    escape = html.escape
    directory, _, name = relative_path.rpartition('/')
    crumbs = ['<a href="{}index.html">All source</a>'.format(up)]
    if directory:
        crumbs.append('<a href="index.html">{}</a>'.format(escape(directory)))
    crumbs.append(escape(name))

    instrumented = len(lines)
    missed = sum(1 for count in lines.values() if not count)
    subtitle = '{:,} of {:,} instrumented lines never executed'.format(missed, instrumented)
    shown_partial = [entry for line, entry in partial.items() if lines.get(line)]
    partly_taken = len(shown_partial)
    if partly_taken:
        subtitle += '; {:,} executed {} a branch that was never taken'.format(
            partly_taken, 'line has' if partly_taken == 1 else 'lines have')
    # Said out loud rather than left to be inferred from a row count that does not match the
    # instrumented count. This is what used to be silent: lines.get() dropped these rows and
    # the subtitle went on quoting the full total.
    unplaceable = sum(1 for number in lines if number > len(source))
    if unplaceable:
        subtitle += ('; {:,} {} past line {:,}, the end of this text, and {} not shown'.format(
            unplaceable, 'record sits' if unplaceable == 1 else 'records sit', len(source),
            'is' if unplaceable == 1 else 'are'))
    if rendered_from:
        subtitle += ('. Rendered from {}, because the checkout\'s copy of this file no longer '
                     'accounts for the coverage records: the build directory\'s copy is the '
                     'text that was compiled'.format(rendered_from))

    # The gutter's column widths, in ch. _format_count() is at most five characters wide.
    highest = max(lines.values(), default=None)
    count_width = 0 if highest is None else len(str(highest)) if highest < 1000 else 5
    branch_width = max((len('%d/%d' % entry[:2]) + 1 for entry in shown_partial), default=0)
    gaps = _gaps_attribute(coverage_gaps(lines, partial, function_lines, len(source)))

    out = ['<!DOCTYPE html><html lang=en><head><meta charset=utf-8>'
           '<meta name=viewport content="width=device-width,initial-scale=1"><title>',
           escape(relative_path),
           '</title><link rel=stylesheet href="', up, STYLESHEET_NAME,
           '"><script defer src="', up, SCRIPT_NAME,
           '"></script></head><body><header><h1>', escape(relative_path),
           '</h1><p class=crumbs>', ' / '.join(crumbs), '</p>', search_box_html(up),
           '<p>', escape(subtitle, False), '</p>', _HEADER_LEGEND,
           '</header><main style="--lw:%d;--bw:%d;--cw:%d;--w:%d"%s>' % (
               len(str(len(source))), branch_width, count_width,
               max(map(len, source), default=0), ' data-gaps="%s"' % gaps if gaps else '')]
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
        # content-visibility contains style, which scopes the counter to the chunk, so every
        # chunk after the first restates where it starts.
        first = chunk_start + 1 if chunk_start else 0
        for number in range(chunk_start + 1, chunk_end + 1):
            text = source[number - 1]
            count = get(number)
            start = ' style="counter-set:ln %d"' % number if number == first else ''
            # The newline leads the row, so the page has one line per row and it ends the
            # previous row's text, where white-space:pre keeps it as the line break it is.
            if count is None:
                append('\n<li id=L%d%s>%s' % (number, start, escape(text, False)))
                continue
            # An uncovered row's count is always 0, so the stylesheet draws it from the class:
            # 453,035 rows and 3.6 MB of <b>0</b> on the synthetic full-scale report.
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

    The same rule write_directory_index uses, so that a file's page is a sibling of the
    directory index that links to it and the link is just the file name.

    A path that is not under the root goes under OUTSIDE_SOURCE_ROOT_DIRECTORY rather than
    being spliced into the tree at whatever depth its absolute path happens to have. There is
    always some of that residue -- 120 paths in the shipped report are copied framework headers
    and WebKitAdditions sources with no checkout path at all -- and where it lands decides
    whether the tree can be rooted at the checkout. See effective_source_prefix().
    """
    root = source_root.rstrip('/') if source_root else ''
    if root and path.startswith(root + '/'):
        return path[len(root) + 1:]
    if root:
        return OUTSIDE_SOURCE_ROOT_DIRECTORY + '/' + path.lstrip('/')
    return path.lstrip('/')


def _write_one(job):
    (path, lines, partial, function_lines, regions, output_directory, source_root,
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
    target = os.path.join(output_directory, relative + '.html')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    page = render_source_view(relative, lines, partial, source, '../' * relative.count('/'),
                              rendered_from=origin, function_lines=function_lines,
                              regions=regions).encode('utf-8')
    with open(target, 'wb') as handle:
        handle.write(page)
    return _PageResult(path, len(page), None, newer, origin)


def write_source_views(coverage_by_path, output_directory, source_root, workers=None,
                       build_directory=None, built_at=None, regions=None):
    """Write one page per file. Returns (pages, bytes written, {path: why it has no page}).

    Takes the already-parsed trace rather than a path to one, because the directory index
    needs the same parse and parsing a full-suite trace twice costs more than rendering every
    page in it does.

    A file with no page is returned instead, with the reason, so the index can render it as
    text rather than as a link to a 404 and say why. Two things cause it: source that cannot
    be read at all -- a file generated into a build directory that has since been cleaned, or
    renamed since the run -- and source whose coverage records run past its end, which is
    proof that it is not the text that was compiled. Only the page is withheld in either case;
    the file's coverage still counts everywhere it is aggregated.

    build_directory, when given, lets a copied header be rendered from the build directory's
    copy when the checkout's has moved on. built_at is the newest reported binary's
    modification time, used only to count how many files are newer than the build; see
    built_at_from_provenance().

    regions, when given, is {path: {line: [(start column, end column or None)]}}, as
    coverage_lcov.uncovered_regions() returns it; see render_source_view().
    """
    os.makedirs(output_directory, exist_ok=True)
    for name, contents in ((STYLESHEET_NAME, SOURCE_VIEW_STYLE), (SCRIPT_NAME, SOURCE_VIEW_SCRIPT)):
        with open(os.path.join(output_directory, name), 'w') as handle:
            handle.write(contents.lstrip('\n'))

    # A generator rather than a list, so that the pool starts on the first pages while the main
    # process is still preparing the rest: partial_branch_lines() is serial, and 0.36s of it on
    # the synthetic full-scale trace.
    regions = regions or {}
    jobs = ((path, coverage.lines, partial_branch_lines(coverage.branches, conditions=True),
             coverage.function_lines, regions.get(path), output_directory, source_root,
             build_directory, built_at)
            for path, coverage in coverage_by_path.items())

    # Eight rather than every core: the work is a couple of seconds either way, and each
    # worker holds a whole file's line map plus its rendered page, which measured 1,285 MB of
    # peak resident memory across the group at eight.
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
    """Say what was withheld and what was rendered from somewhere else.

    Counted and named rather than left in the data, because the whole failure mode being fixed
    here is a report that describes a file it cannot prove it is describing, and a fix that is
    itself silent about how often it fired would be the same mistake one level up.
    """
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

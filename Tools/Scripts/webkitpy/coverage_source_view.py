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
<pre> in every cell of every row), while this emits `<tr id=L36 class=u><td>36<td>0<td>code`
and leans on HTML5's optional end tags and one shared stylesheet.

What is lost is sub-line detail. llvm-cov marks the individual uncovered *regions* inside a
line, which lcov cannot express: on the measured run, 23,081 lines are covered but contain a
region that never executed, and no line-granular format can say so. Some of that is recovered
here from lcov's branch records, which llvm-cov show does not display at all -- a covered line
with an untaken branch is marked and shows its taken/total. For the rest, generate-coverage-
report --llvm-cov-html still produces llvm-cov's own pages alongside these.
"""

import html
import logging
import os
from concurrent.futures import ProcessPoolExecutor

logger = logging.getLogger(__name__)

# One stylesheet at the report root, shared by every page. Inlining it would cost more than
# the source text does: 1.5 KB times 16,149 pages is 24 MB.
STYLESHEET_NAME = 'coverage-source.css'

# Columns are addressed by position so that a row costs `<td>` and nothing else. The palette
# is the one coverage_directory_index.py uses, so the two halves of the report match.
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
  --missed-row: rgba(192, 57, 43, 0.09);
  --partial: #a0620a;
  --partial-row: rgba(160, 98, 10, 0.09);
  --target: rgba(42, 120, 214, 0.18);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d;
    --surface-1: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --gridline: #2c2c2a;
    --link: #3987e5;
    --missed: #f0776a;
    --missed-row: rgba(240, 119, 106, 0.13);
    --partial: #e0a458;
    --partial-row: rgba(224, 164, 88, 0.13);
    --target: rgba(57, 135, 229, 0.26);
  }
}
body {
  margin: 0;
  background: var(--page);
  color: var(--text-primary);
  font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
}
h1 {
  font: 600 13px/1.4 system-ui, -apple-system, sans-serif;
  margin: 0; padding: 12px 14px 2px;
}
p {
  font: 12px/1.5 system-ui, -apple-system, sans-serif;
  color: var(--text-secondary);
  margin: 0; padding: 0 14px;
}
p.crumbs { padding-bottom: 10px; border-bottom: 1px solid var(--gridline); }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
td { padding: 0 10px; white-space: pre; vertical-align: top; }
td:nth-child(1) {
  width: 1%; text-align: right; color: var(--muted); user-select: none;
  border-right: 1px solid var(--gridline);
}
td:nth-child(2) {
  width: 1%; text-align: right; color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
td:nth-child(4) {
  width: 1%; text-align: right; color: var(--partial);
  font-variant-numeric: tabular-nums;
}
tr.u { background: var(--missed-row); }
tr.u td:nth-child(2) { color: var(--missed); font-weight: 600; }
tr.p { background: var(--partial-row); }
tr:target { background: var(--target); }
"""


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


def partial_branch_lines(branches):
    """{line: (taken, total)} for the lines where some branch was never taken.

    Only the partial lines, because those are the only ones the page marks, and because this
    crosses a process boundary once per file: the full branch map is 1,043,499 entries on a
    full-suite run and pickling all of it to say nothing about most of it is pure cost.
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
    for line, (taken, total) in totals.items():
        if taken < total:
            try:
                partial[int(line)] = (taken, total)
            except ValueError:
                pass
    return partial


def render_source_view(relative_path, lines, partial, source, up):
    """One page. lines is {line: count}, partial is {line: (taken, total)}."""
    escape = html.escape
    directory, _, name = relative_path.rpartition('/')
    crumbs = ['<a href="{}index.html">All source</a>'.format(up)]
    if directory:
        crumbs.append('<a href="index.html">{}</a>'.format(escape(directory)))
    crumbs.append(escape(name))

    instrumented = len(lines)
    missed = sum(1 for count in lines.values() if not count)
    subtitle = '{:,} of {:,} instrumented lines never executed'.format(missed, instrumented)
    partly_taken = sum(1 for line in partial if lines.get(line))
    if partly_taken:
        subtitle += '; {:,} executed {} a branch that was never taken'.format(
            partly_taken, 'line has' if partly_taken == 1 else 'lines have')

    out = ['<!DOCTYPE html><html lang=en><head><meta charset=utf-8>'
           '<meta name=viewport content="width=device-width,initial-scale=1"><title>',
           escape(relative_path),
           '</title><link rel=stylesheet href="', up, STYLESHEET_NAME,
           '"></head><body><h1>', escape(relative_path),
           '</h1><p class=crumbs>', ' / '.join(crumbs),
           '</p><p>', subtitle, '</p><table>']
    append = out.append
    for number, text in enumerate(source, 1):
        count = lines.get(number)
        # The newline leads the row rather than trailing it: trailing it would put it inside
        # the code cell, which is white-space:pre, and render every row two lines tall.
        if count is None:
            append('\n<tr id=L%d><td>%d<td><td>%s' % (number, number, escape(text, False)))
        elif not count:
            append('\n<tr id=L%d class=u><td>%d<td>0<td>%s' % (number, number, escape(text, False)))
        else:
            taken = partial.get(number)
            if taken is None:
                append('\n<tr id=L%d><td>%d<td>%s<td>%s' % (
                    number, number, _format_count(count), escape(text, False)))
            else:
                append('\n<tr id=L%d class=p><td>%d<td>%s<td>%s<td>%d/%d' % (
                    number, number, _format_count(count), escape(text, False),
                    taken[0], taken[1]))
    append('\n</table></body></html>\n')
    return ''.join(out)


def relative_source_path(path, source_root):
    """Where a page for path goes, relative to the report root.

    The same rule write_directory_index uses, so that a file's page is a sibling of the
    directory index that links to it and the link is just the file name.
    """
    root = source_root.rstrip('/') if source_root else ''
    if root and path.startswith(root + '/'):
        return path[len(root) + 1:]
    return path.lstrip('/')


def _write_one(job):
    path, lines, partial, output_directory, source_root = job
    source = _read_source(path)
    if source is None:
        return path, None
    relative = relative_source_path(path, source_root)
    target = os.path.join(output_directory, relative + '.html')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    page = render_source_view(relative, lines, partial, source,
                              '../' * relative.count('/')).encode('utf-8')
    with open(target, 'wb') as handle:
        handle.write(page)
    return path, len(page)


def write_source_views(coverage_by_path, output_directory, source_root, workers=None):
    """Write one page per file. Returns (pages, bytes written, paths with no page).

    Takes the already-parsed trace rather than a path to one, because the directory index
    needs the same parse and parsing a full-suite trace twice costs more than rendering every
    page in it does.

    A file whose source cannot be read gets no page and is returned instead, so the index can
    render it as text rather than as a link to a 404. That is not hypothetical: a file can be
    generated into a build directory that has since been cleaned, or renamed since the run.
    """
    os.makedirs(output_directory, exist_ok=True)
    with open(os.path.join(output_directory, STYLESHEET_NAME), 'w') as handle:
        handle.write(SOURCE_VIEW_STYLE.lstrip('\n'))

    jobs = [(path, coverage.lines, partial_branch_lines(coverage.branches),
             output_directory, source_root)
            for path, coverage in coverage_by_path.items()]

    # Eight rather than every core: the work is a couple of seconds either way, and each
    # worker holds a whole file's line map plus its rendered page, which measured 1,285 MB of
    # peak resident memory across the group at eight.
    if workers is None:
        workers = min(8, os.cpu_count() or 1)

    written_bytes = 0
    pages = 0
    skipped = set()
    if workers > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_write_one, jobs, chunksize=64))
    else:
        results = [_write_one(job) for job in jobs]
    for path, size in results:
        if size is None:
            skipped.add(path)
        else:
            pages += 1
            written_bytes += size

    if skipped:
        logger.info('%d files have no line view because their source could not be read; '
                    'they are listed in the index without a link', len(skipped))
        for path in sorted(skipped)[:5]:
            logger.debug('    unreadable: %s', path)
    return pages, written_bytes, skipped

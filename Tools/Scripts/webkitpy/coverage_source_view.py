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

import datetime
import html
import json
import logging
import os
from collections import defaultdict, namedtuple
from concurrent.futures import ProcessPoolExecutor

from webkitpy.coverage_lcov import compiled_copy_candidates

logger = logging.getLogger(__name__)

# One stylesheet at the report root, shared by every page. Inlining it would cost more than
# the source text does: 1.5 KB times 16,149 pages is 24 MB.
STYLESHEET_NAME = 'coverage-source.css'

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
# class of their own cost nothing per row. tr.u and tr.p are the stylesheet's names.
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


def render_source_view(relative_path, lines, partial, source, up, rendered_from=None):
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

    out = ['<!DOCTYPE html><html lang=en><head><meta charset=utf-8>'
           '<meta name=viewport content="width=device-width,initial-scale=1"><title>',
           escape(relative_path),
           '</title><link rel=stylesheet href="', up, STYLESHEET_NAME,
           '"></head><body><h1>', escape(relative_path),
           '</h1><p class=crumbs>', ' / '.join(crumbs),
           '</p><p>', escape(subtitle, False), '</p><table>']
    append = out.append
    for number, text in enumerate(source, 1):
        count = lines.get(number)
        taken = partial.get(number) if count else None
        # The newline leads the row rather than trailing it: trailing it would put it inside
        # the code cell, which is white-space:pre, and render every row two lines tall.
        append('\n<tr id=L%d%s><td>%d<td>%s<td>%s%s' % (
            number, ROW_CLASS[line_state(count, taken)], number,
            '' if count is None else _format_count(count), escape(text, False),
            '' if taken is None else '<td>%d/%d' % taken))
    append('\n</table></body></html>\n')
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
    path, lines, partial, output_directory, source_root, build_directory, built_at = job
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
                              rendered_from=origin).encode('utf-8')
    with open(target, 'wb') as handle:
        handle.write(page)
    return _PageResult(path, len(page), None, newer, origin)


def write_source_views(coverage_by_path, output_directory, source_root, workers=None,
                       build_directory=None, built_at=None):
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
    """
    os.makedirs(output_directory, exist_ok=True)
    with open(os.path.join(output_directory, STYLESHEET_NAME), 'w') as handle:
        handle.write(SOURCE_VIEW_STYLE.lstrip('\n'))

    jobs = [(path, coverage.lines, partial_branch_lines(coverage.branches),
             output_directory, source_root, build_directory, built_at)
            for path, coverage in coverage_by_path.items()]

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
    if workers > 1 and len(jobs) > 1:
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

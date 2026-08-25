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

"""Build a drill-down directory index over an lcov coverage trace.

llvm-cov's own index lists every source file in one page. For WebKit that is ~18,000
rows and roughly 8MB of HTML, which is slow to render and impossible to skim. This
writes one small page per directory instead: each lists only its immediate
subdirectories (with aggregated coverage) and its immediate files, linking to the
line-by-line view coverage_source_view.py writes beside it.

It also reports the third state llvm-cov cannot: a file that was never compiled in this
configuration has no coverage mapping, so it is absent from the trace rather than present
at 0%. Those files are listed per directory in a table of their own, with a reason, and
they are deliberately kept out of every percentage on the page -- 73 of them have no
executable lines at all, so giving them a denominator would invent one.

When the report has more than one test suite, each gets a line-coverage column of its own
beside the combined one, so that a gap can be attributed to the suite that should have closed
it. See coverage_suites.py for what the combined column means.
"""

import html
import os
from collections import defaultdict, namedtuple

# lcov carries line, function and branch data but not regions; llvm-cov's own
# summary.txt still reports regions for anyone who wants them.
METRICS = ('lines', 'functions', 'branches')


def suite_metric(name):
    """The totals key for one suite's line coverage.

    Per-suite columns are carried as extra metrics on the same node totals as lines,
    functions and branches, so that a directory aggregates its descendants' per-suite
    coverage by the same code path that aggregates everything else. The prefix keeps a suite
    called "lines" from colliding with the metric of that name.
    """
    return 'suite:' + name


ReportPages = namedtuple('ReportPages',
                         ('directory_pages', 'source_pages', 'source_bytes', 'skipped_paths',
                          'totals'))

# Roles from the reference data-visualization palette. Bar length carries the
# magnitude, so this is a single series: one constant fill, and no legend.
#
# REPORT_STYLE, SORT_SCRIPT, meter_html() and format_percent() are shared with
# coverage_delta.py, so that the delta report looks like part of the same tool rather
# than a second one. Keep them public.
REPORT_STYLE = """
:root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --page: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --gridline: #e1e0d9;
  --border: rgba(11, 11, 11, 0.10);
  --meter-track: #e1e0d9;
  --meter-fill: #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --page: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --gridline: #2c2c2a;
    --border: rgba(255, 255, 255, 0.10);
    --meter-track: #2c2c2a;
    --meter-fill: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --page: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --muted: #898781;
  --gridline: #2c2c2a;
  --border: rgba(255, 255, 255, 0.10);
  --meter-track: #2c2c2a;
  --meter-fill: #3987e5;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  background: var(--page);
  color: var(--text-primary);
  font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 17px; font-weight: 600; margin: 0 0 2px; }
.sub { color: var(--text-secondary); margin: 0 0 18px; font-size: 12px; }
.crumbs { margin: 0 0 14px; font-size: 12px; color: var(--text-secondary); }
.crumbs a { color: var(--meter-fill); text-decoration: none; }
.crumbs a:hover { text-decoration: underline; }
.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}
table { width: 100%; border-collapse: collapse; }
th, td { padding: 7px 12px; text-align: left; }
th {
  font-size: 11px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em;
  border-bottom: 1px solid var(--gridline); cursor: pointer; user-select: none;
  white-space: nowrap;
}
th.n, td.n { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr { border-bottom: 1px solid var(--gridline); }
tbody tr:last-child { border-bottom: 0; }
tbody tr:hover { background: color-mix(in oklab, var(--meter-fill) 7%, transparent); }
td a { color: var(--text-primary); text-decoration: none; }
td a:hover { color: var(--meter-fill); text-decoration: underline; }
.dir a { font-weight: 600; }
.dir a::before { content: "\\2192  "; color: var(--muted); font-weight: 400; }
.meter {
  position: relative; width: 132px; height: 8px;
  background: var(--meter-track); border-radius: 4px; overflow: hidden;
}
.meter > i {
  position: absolute; inset: 0 auto 0 0; display: block;
  background: var(--meter-fill); border-radius: 4px;
}
.pct { width: 62px; }
.totals { background: color-mix(in oklab, var(--meter-fill) 6%, var(--surface-1)); font-weight: 600; }
.hint { color: var(--muted); font-size: 11px; margin: 14px 0 0; }
.hint a { color: var(--meter-fill); }
h2 { font-size: 13px; font-weight: 600; margin: 22px 0 2px; }
.caveat {
  margin: 0 0 18px; padding: 9px 12px; font-size: 12px;
  color: var(--text-secondary); background: var(--surface-1);
  border: 1px solid var(--border); border-left: 3px solid var(--muted); border-radius: 6px;
}
td.reason, td.detail { color: var(--text-secondary); }
td.detail { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
td.nosource { color: var(--text-secondary); cursor: help; }
"""

SORT_SCRIPT = """
document.querySelectorAll('th[data-col]').forEach(function (th) {
  th.addEventListener('click', function () {
    var table = th.closest('table');
    var body = table.tBodies[0];
    var col = +th.dataset.col;
    var numeric = th.dataset.numeric === '1';
    var descending = th.dataset.dir !== 'desc';
    th.dataset.dir = descending ? 'desc' : 'asc';
    var rows = Array.prototype.slice.call(body.rows).filter(function (r) {
      return !r.classList.contains('totals');
    });
    rows.sort(function (a, b) {
      var x = a.cells[col].dataset.v, y = b.cells[col].dataset.v;
      if (numeric) { x = parseFloat(x) || 0; y = parseFloat(y) || 0; return descending ? y - x : x - y; }
      return descending ? String(y).localeCompare(String(x)) : String(x).localeCompare(String(y));
    });
    rows.forEach(function (r) { body.appendChild(r); });
  });
});
"""


class _Node:
    __slots__ = ('name', 'children', 'files', 'totals', 'absent', 'absent_totals')

    def __init__(self, name):
        self.name = name
        self.children = {}
        self.files = []
        # [count, covered] per metric, and a metric is whatever the files carry: the three
        # llvm-cov metrics, plus one per suite when the report has suites.
        self.totals = defaultdict(lambda: [0, 0])
        self.absent = []                            # AbsentFile for this directory itself
        self.absent_totals = [0, 0]                 # [files, physical lines] with descendants

    def add(self, metric, count, covered):
        entry = self.totals[metric]
        entry[0] += count
        entry[1] += covered


def _percent(count, covered):
    return (100.0 * covered / count) if count else None


def format_percent(value):
    return '-' if value is None else '{:.2f}%'.format(value)


def meter_html(value):
    if value is None:
        return '<div class="meter"></div>'
    return '<div class="meter"><i style="width:{:.2f}%"></i></div>'.format(max(value, 0.0))


def build_tree(files):
    """files: iterable of (path_components, {metric: (count, covered)}). Returns the root _Node."""
    root = _Node('')
    for components, totals in files:
        node = root
        nodes = [root]
        for component in components[:-1]:
            node = node.children.setdefault(component, _Node(component))
            nodes.append(node)
        node.files.append((components[-1], totals))
        for ancestor in nodes:
            # Over the file's own metrics rather than over METRICS, so that a per-suite metric
            # aggregates up the tree without this needing to know that suites exist.
            for metric, (count, covered) in totals.items():
                ancestor.add(metric, count, covered)
    return root


def attach_absent_files(root, absent_files, source_prefix):
    """Hang the not-built files off the coverage tree, creating directories as needed.

    A directory can hold nothing but not-built files -- Source/WebCore/platform/gtk is
    entirely another port's -- so this has to be able to create nodes the coverage data
    never mentioned, or the biggest gaps would be the ones with no page to show them on.
    """
    prefix = source_prefix.rstrip('/') + '/' if source_prefix else ''
    attached = 0
    for absent in absent_files:
        path = absent.path
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
        components = path.split('/')
        node = root
        nodes = [root]
        for component in components[:-1]:
            node = node.children.setdefault(component, _Node(component))
            nodes.append(node)
        node.absent.append(absent)
        for ancestor in nodes:
            ancestor.absent_totals[0] += 1
            ancestor.absent_totals[1] += absent.physical_lines
        attached += 1
    return attached


def _collapse_single_child_chain(node):
    """A directory whose only content is one subdirectory is not worth a page of its own."""
    prefix = [node.name] if node.name else []
    while not node.files and not node.absent and len(node.children) == 1:
        only = next(iter(node.children.values()))
        prefix.append(only.name)
        node = only
    return prefix, node


def _percent_cell(count, covered):
    value = _percent(count, covered)
    return '<td class="n pct" data-v="{}">{}</td>'.format(
        -1 if value is None else '{:.4f}'.format(value), format_percent(value))


def _row(label, link, node_or_summary, is_directory, suite_names=()):
    cells = []
    kind = 'dir' if is_directory else 'file'
    if link is None:
        # A file with no line view. Rendering it as a link anyway would be a link to a 404,
        # and leaving it out would hide coverage data the report does have.
        cells.append('<td class="{} nosource" data-v="{}" title="No line view: the source '
                     'could not be read">{}</td>'.format(
                         kind, html.escape(label), html.escape(label)))
    else:
        cells.append('<td class="{}" data-v="{}"><a href="{}">{}</a></td>'.format(
            kind, html.escape(label), html.escape(link), html.escape(label)))

    def totals_for(metric):
        if is_directory:
            return node_or_summary.totals[metric]
        return node_or_summary.get(metric, (0, 0))

    for metric in METRICS:
        count, covered = totals_for(metric)
        value = _percent(count, covered)
        if metric == 'lines':
            cells.append('<td data-v="{}">{}</td>'.format(
                -1 if value is None else '{:.4f}'.format(value), meter_html(value)))
        cells.append(_percent_cell(count, covered))
        if metric == 'lines':
            cells.append('<td class="n" data-v="{}">{:,}</td>'.format(count, count))
            cells.append('<td class="n" data-v="{}">{:,}</td>'.format(count - covered, count - covered))
            # Beside the combined figure, because the combined one is what they are read
            # against: the question is which suite is not reaching this code.
            for name in suite_names:
                cells.append(_percent_cell(*totals_for(suite_metric(name))))
    # Deliberately a file count and not a percentage: these files have no coverage mapping,
    # so they have no denominator to be a percentage of. And deliberately not per suite: every
    # suite ran against the same binaries, so a file that was not built was not built for any
    # of them.
    not_built = node_or_summary.absent_totals[0] if is_directory else 0
    cells.append('<td class="n" data-v="{}">{}</td>'.format(
        not_built, '{:,}'.format(not_built) if not_built else '-'))
    return '<tr>' + ''.join(cells) + '</tr>'


def _headers(suite_names=()):
    """(label, css class, column index, sorts numerically) per column, in order."""
    headers = [
        ('Name', '', False),
        ('Line coverage', '', True),
        ('Lines %', 'n', True),
        ('Lines', 'n', True),
        ('Uncovered', 'n', True),
    ]
    headers += [('{} %'.format(name), 'n', True) for name in suite_names]
    if suite_names:
        headers[2] = ('All suites %', 'n', True)
    headers += [
        ('Functions %', 'n', True),
        ('Branches %', 'n', True),
        ('Not built', 'n', True),
    ]
    return tuple((label, css, column, numeric)
                 for column, (label, css, numeric) in enumerate(headers))


_ABSENT_HEADERS = (
    ('File', '', 0, False),
    ('Physical lines', 'n', 1, True),
    ('Why it is not in this build', '', 2, False),
    ('Detail', '', 3, False),
)


def _files(count):
    return '{:,} file'.format(count) if count == 1 else '{:,} files'.format(count)


def _headers_html(headers):
    cells = []
    for label, css, column, numeric in headers:
        cells.append('<th class="{}" data-col="{}" data-numeric="{}">{}</th>'.format(
            css, column, '1' if numeric else '0', html.escape(label)))
    return ''.join(cells)


def _absent_card(absent_files, reason_labels):
    """The third state, in a table of its own so its counts cannot be read as coverage."""
    if not absent_files:
        return ''
    rows = []
    for absent in sorted(absent_files, key=lambda entry: (-entry.physical_lines, entry.path)):
        name = absent.path.rsplit('/', 1)[-1]
        rows.append(
            '<tr><td data-v="{name}">{name}</td>'
            '<td class="n" data-v="{lines}">{lines:,}</td>'
            '<td class="reason" data-v="{reason}">{reason}</td>'
            '<td class="detail" data-v="{detail}">{detail}</td></tr>'.format(
                name=html.escape(name), lines=absent.physical_lines,
                reason=html.escape(reason_labels.get(absent.reason, absent.reason)),
                detail=html.escape(absent.detail)))
    total = sum(absent.physical_lines for absent in absent_files)
    return """<h2>Not built in this configuration &mdash; {count}, {total:,} physical lines</h2>
<div class="card">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
""".format(count=_files(len(absent_files)), total=total,
           headers=_headers_html(_ABSENT_HEADERS), rows='\n'.join(rows))


def _reason_card(reason_rows, reason_explanations):
    if not reason_rows:
        return ''
    rows = []
    for reason, label, files, lines in reason_rows:
        rows.append(
            '<tr><td data-v="{label}">{label}</td>'
            '<td class="n" data-v="{files}">{files:,}</td>'
            '<td class="n" data-v="{lines}">{lines:,}</td>'
            '<td class="reason" data-v="">{why}</td></tr>'.format(
                label=html.escape(label), files=files, lines=lines,
                why=html.escape(reason_explanations.get(reason, ''))))
    headers = _headers_html((('Why a file is not built', '', 0, False),
                             ('Files', 'n', 1, True),
                             ('Physical lines', 'n', 2, True),
                             ('What it means', '', 3, False)))
    return """<h2>Why {count} are not in this build</h2>
<div class="card">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
""".format(count=_files(sum(row[2] for row in reason_rows)),
           headers=headers, rows='\n'.join(rows))


def _page(title, subtitle, crumbs_html, rows_html, totals_row, note,
          caveat='', extra_cards='', suite_names=()):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
<div class="wrap">
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
<p class="crumbs">{crumbs}</p>
{caveat}<div class="card">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>
{totals}
{rows}
</tbody>
</table>
</div>
{extra_cards}<p class="hint">{note}</p>
</div>
<script>{script}</script>
</body>
</html>
""".format(title=html.escape(title), subtitle=html.escape(subtitle), crumbs=crumbs_html,
           headers=_headers_html(_headers(suite_names)), rows=rows_html, totals=totals_row,
           style=REPORT_STYLE, script=SORT_SCRIPT, note=note,
           caveat='<p class="caveat">{}</p>\n'.format(html.escape(caveat)) if caveat else '',
           extra_cards=extra_cards)


def _write_node(node, output_root, full_parts, source_prefix, index_link, written,
                absence=None, unlinkable=(), ancestor_pages=frozenset(), suite_names=()):
    """Write one index.html per directory node.

    full_parts is the path from the source root, so it is also the on-disk location and the
    number of ../ hops back to the top of the report.

    ancestor_pages holds the full_parts of every ancestor that has a page, which is not all of
    them: a chain of single-child directories is collapsed onto one page, so the levels it
    swallowed have no index.html to link a breadcrumb to.
    """
    directory = os.path.join(output_root, *full_parts)
    os.makedirs(directory, exist_ok=True)
    depth = len(full_parts)
    up = '../' * depth

    def uncovered_lines(totals):
        count, covered = totals['lines'] if isinstance(totals, dict) else (0, 0)
        return count - covered

    directories = []
    for name in sorted(node.children):
        child_prefix, child = _collapse_single_child_chain(node.children[name])
        directories.append(('/'.join(child_prefix), child_prefix, child))
    # Biggest gaps first: the point of the page is to show where coverage is missing. A
    # directory with no coverage data at all still sorts by how much code is not built in
    # it, so an entirely-not-built directory is not pushed to the bottom by having no rows.
    directories.sort(key=lambda entry: (-(entry[2].totals['lines'][0] - entry[2].totals['lines'][1]),
                                        -entry[2].absent_totals[1], entry[0]))

    rows = []
    for label, child_prefix, child in directories:
        rows.append(_row(label, label + '/index.html', child, True, suite_names))
        _write_node(child, output_root, full_parts + tuple(child_prefix),
                    source_prefix, index_link, written, absence, unlinkable,
                    ancestor_pages | {full_parts}, suite_names)

    for name, totals in sorted(node.files, key=lambda entry: (-uncovered_lines(entry[1]), entry[0])):
        # The line view is written beside this page by coverage_source_view, so the link is
        # just the file name. No collision with a subdirectory of the same name: a file's page
        # is Foo.html and a directory's is Foo/index.html.
        source_path = os.path.join(source_prefix, *full_parts, name)
        rows.append(_row(name, None if source_path in unlinkable else name + '.html',
                         totals, False, suite_names))

    crumbs = ['<a href="{}index.html">All source</a>'.format(up)] if depth else ['All source']
    for index in range(depth):
        piece = html.escape(full_parts[index])
        # Only a link if that level has a page: a collapsed single-child chain has one page
        # for several levels, and linking the levels it swallowed is a link to nothing.
        if index == depth - 1 or full_parts[:index + 1] not in ancestor_pages:
            crumbs.append(piece)
        else:
            crumbs.append('<a href="{}index.html">{}</a>'.format('../' * (depth - index - 1), piece))

    display = '/'.join(full_parts) if full_parts else 'All source'
    totals = '<tr class="totals">' + _row('Total', '#', node, True, suite_names)[len('<tr>'):]
    totals = totals.replace('<td class="dir" data-v="Total"><a href="#">Total</a></td>',
                            '<td data-v="">Total</td>')
    note = ('Directories aggregate their descendants. Click a column heading to sort. '
            'A file name links to its line-by-line coverage. "Not built" is a file count, '
            'not a percentage: those files have no coverage mapping to be a percentage of.')
    if suite_names:
        note += (' The per-suite columns are one profile each and "All suites" is their merge, '
                 'so it is the union of the lines they executed and not the sum: a line two '
                 'suites reach counts once. "Not built" has no per-suite column because every '
                 'suite ran against the same binaries, so a file that was not built was not '
                 'built for any of them.')
    if index_link:
        note += ' <a href="{}{}">Flat llvm-cov index</a>'.format(up, index_link)
    subtitle = '{:,} lines, {:,} uncovered'.format(
        node.totals['lines'][0], node.totals['lines'][0] - node.totals['lines'][1])
    if node.absent_totals[0]:
        subtitle += ', {} not built here ({:,} physical lines)'.format(
            _files(node.absent_totals[0]), node.absent_totals[1])

    caveat = ''
    extra_cards = ''
    if absence is not None:
        if not depth:
            caveat = absence.denominator_sentence()
            extra_cards += _reason_card(absence.reasons(), absence.explanations)
        extra_cards += _absent_card(node.absent, absence.labels)

    page = _page('Coverage: ' + display, subtitle, ' / '.join(crumbs), '\n'.join(rows), totals,
                 note, caveat=caveat, extra_cards=extra_cards, suite_names=suite_names)
    with open(os.path.join(directory, 'index.html'), 'w') as handle:
        handle.write(page)
    written.append(directory)


def effective_source_prefix(paths, source_root=None):
    """The directory the report's tree is rooted at.

    source_root when every covered path is under it, so the hierarchy is the same shape
    whichever files a given run happened to cover. Otherwise the common prefix, which is what
    a run over one component with no --source-root gets.
    """
    prefix = source_root.rstrip('/') if source_root else None
    paths = list(paths)
    if prefix and all(path.startswith(prefix + '/') for path in paths):
        return prefix
    return os.path.dirname(os.path.commonprefix(paths))


def write_directory_index(lcov_path, output_directory, source_root=None, index_link=None,
                          absence=None, coverage_by_path=None, unlinkable=(),
                          build_directory=None, suite_line_totals=()):
    """Write the drill-down index from an lcov trace. Returns the number of pages written.

    source_root anchors the tree, so the hierarchy is the same shape whichever files a
    given run happened to cover, and it is also what copied-header paths are rewritten
    relative to. Without it the tree would be rooted at the common prefix of the covered
    paths, and a run touching only WebCore would silently lose the Source/WebCore levels.

    absence, when given, is a coverage_build_inventory.AbsenceReport, and adds the
    never-compiled files to every page as a separate table.

    coverage_by_path lets a caller that has already parsed the trace pass it in; see
    write_report. index_link, when given, adds a link to llvm-cov's own flat index, which
    exists only when generate-coverage-report was passed --llvm-cov-html. unlinkable is the
    set of paths that have no line view, which are listed without a link.

    suite_line_totals is [(suite name, {path: (lines, covered)})], in the order the columns
    should appear. The trace itself is the merge of those suites, so the Lines % column is
    their union and each suite column is one of them.
    """
    from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov

    if coverage_by_path is None:
        canonicalizer = (PathCanonicalizer(source_root, build_directory=build_directory)
                         if source_root else None)
        coverage_by_path = parse_lcov(lcov_path, canonicalizer)
        if canonicalizer:
            canonicalizer.log_summary()
    if not coverage_by_path:
        raise RuntimeError('{} contained no coverage records'.format(lcov_path))

    suite_names = tuple(name for name, _ in suite_line_totals)
    entries = []
    for path, coverage in coverage_by_path.items():
        totals = coverage.totals()
        for name, per_path in suite_line_totals:
            # A file with no record in a suite's trace is not zero-covered there; it is a file
            # that suite's profile says nothing about, and the column shows '-' for it.
            if path in per_path:
                totals[suite_metric(name)] = per_path[path]
        entries.append((path, totals))
    source_prefix = effective_source_prefix((path for path, _ in entries), source_root)

    stripped = []
    for path, totals in entries:
        relative = os.path.relpath(path, source_prefix)
        stripped.append((tuple(relative.split(os.sep)), totals))

    root = build_tree(stripped)
    if absence is not None:
        # The absent paths are relative to the checkout root, so they can only be hung off
        # the tree when the tree is rooted there too. It is not when the run covered a
        # single component and source_root was not supplied.
        if source_root and source_prefix == source_root.rstrip('/'):
            attach_absent_files(root, absence.files, '')
        else:
            absence = None
    written = []
    _write_node(root, output_directory, (), source_prefix, index_link, written,
                absence, unlinkable, suite_names=suite_names)
    return len(written)


def write_report(lcov_path, output_directory, source_root=None, absence=None,
                 index_link=None, workers=None, build_directory=None, suite_line_totals=(),
                 coverage_by_path=None):
    """Write the whole HTML report: the directory index and every file's line view.

    One entry point because both halves need the same parse, and parsing a full-suite trace
    takes longer than rendering all 16,149 line views does. coverage_by_path lets a caller
    that has already parsed the trace -- to gate on it, or to check it against the per-suite
    traces -- hand it over rather than pay for that twice. Returns a ReportPages.

    The line views are deliberately combined-only even when there are suites. They are the
    largest part of the report -- 288 MB for 16,149 files -- so one set per suite would
    multiply that by the number of suites to answer a question the index already answers at
    file granularity, which is the granularity at which somebody decides which suite to
    extend.
    """
    from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov, project_totals
    from webkitpy.coverage_source_view import write_source_views

    if coverage_by_path is None:
        canonicalizer = (PathCanonicalizer(source_root, build_directory=build_directory)
                         if source_root else None)
        coverage_by_path = parse_lcov(lcov_path, canonicalizer)
        if canonicalizer:
            canonicalizer.log_summary()
    if not coverage_by_path:
        raise RuntimeError('{} contained no coverage records'.format(lcov_path))

    # The line views are laid out against the same prefix as the index, so that a file's page
    # is a sibling of the index page that links to it.
    source_prefix = effective_source_prefix(coverage_by_path, source_root)
    source_pages, source_bytes, skipped = write_source_views(
        coverage_by_path, output_directory, source_prefix, workers=workers)
    directory_pages = write_directory_index(
        lcov_path, output_directory, source_root=source_root, index_link=index_link,
        absence=absence, coverage_by_path=coverage_by_path, unlinkable=skipped,
        suite_line_totals=suite_line_totals)
    return ReportPages(directory_pages, source_pages, source_bytes, skipped,
                       project_totals(coverage_by_path))

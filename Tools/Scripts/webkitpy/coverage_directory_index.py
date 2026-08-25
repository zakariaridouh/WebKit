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

"""Build a drill-down directory index over an llvm-cov HTML report.

llvm-cov's own index lists every source file in one page. For WebKit that is ~18,000
rows and roughly 8MB of HTML, which is slow to render and impossible to skim. This
writes one small page per directory instead: each lists only its immediate
subdirectories (with aggregated coverage) and its immediate files, linking down into
llvm-cov's existing per-file pages.
"""

import html
import os
from collections import defaultdict

# lcov carries line, function and branch data but not regions; llvm-cov's own
# summary.txt still reports regions for anyone who wants them.
METRICS = ('lines', 'functions', 'branches')

# Roles from the reference data-visualization palette. Bar length carries the
# magnitude, so this is a single series: one constant fill, and no legend.
_STYLE = """
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
"""

_SORT_SCRIPT = """
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
    __slots__ = ('name', 'children', 'files', 'totals')

    def __init__(self, name):
        self.name = name
        self.children = {}
        self.files = []
        self.totals = {m: [0, 0] for m in METRICS}  # [count, covered]

    def add(self, metric, count, covered):
        entry = self.totals[metric]
        entry[0] += count
        entry[1] += covered


def _percent(count, covered):
    return (100.0 * covered / count) if count else None


def _format_percent(value):
    return '-' if value is None else '{:.2f}%'.format(value)


def _meter(value):
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
            for metric in METRICS:
                count, covered = totals.get(metric, (0, 0))
                ancestor.add(metric, count, covered)
    return root


def _collapse_single_child_chain(node):
    """A directory whose only content is one subdirectory is not worth a page of its own."""
    prefix = [node.name] if node.name else []
    while not node.files and len(node.children) == 1:
        only = next(iter(node.children.values()))
        prefix.append(only.name)
        node = only
    return prefix, node


def _row(label, link, node_or_summary, is_directory, index_of_sort_column=0):
    cells = []
    kind = 'dir' if is_directory else 'file'
    cells.append('<td class="{}" data-v="{}"><a href="{}">{}</a></td>'.format(
        kind, html.escape(label), html.escape(link), html.escape(label)))
    for metric in METRICS:
        if is_directory:
            count, covered = node_or_summary.totals[metric]
        else:
            count, covered = node_or_summary.get(metric, (0, 0))
        value = _percent(count, covered)
        if metric == 'lines':
            cells.append('<td data-v="{}">{}</td>'.format(
                -1 if value is None else '{:.4f}'.format(value), _meter(value)))
        cells.append('<td class="n pct" data-v="{}">{}</td>'.format(
            -1 if value is None else '{:.4f}'.format(value), _format_percent(value)))
        if metric == 'lines':
            cells.append('<td class="n" data-v="{}">{:,}</td>'.format(count, count))
            cells.append('<td class="n" data-v="{}">{:,}</td>'.format(count - covered, count - covered))
    return '<tr>' + ''.join(cells) + '</tr>'


# (label, css class, column index, sorts numerically)
_HEADERS = (
    ('Name', '', 0, False),
    ('Line coverage', '', 1, True),
    ('Lines %', 'n', 2, True),
    ('Lines', 'n', 3, True),
    ('Uncovered', 'n', 4, True),
    ('Functions %', 'n', 5, True),
    ('Branches %', 'n', 6, True),
)


def _page(title, subtitle, crumbs_html, rows_html, totals_row, depth, note):
    up = '../' * depth
    header_cells = []
    for label, css, column, numeric in _HEADERS:
        header_cells.append('<th class="{}" data-col="{}" data-numeric="{}">{}</th>'.format(
            css, column, '1' if numeric else '0', html.escape(label)))
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
<div class="card">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>
{totals}
{rows}
</tbody>
</table>
</div>
<p class="hint">{note}</p>
</div>
<script>{script}</script>
</body>
</html>
""".format(title=html.escape(title), subtitle=html.escape(subtitle), crumbs=crumbs_html,
           headers=''.join(header_cells), rows=rows_html, totals=totals_row,
           style=_STYLE, script=_SORT_SCRIPT, note=note)


def _write_node(node, output_root, full_parts, source_prefix, file_prefix, index_link, written):
    """Write one index.html per directory node.

    full_parts is the path from the source root, so it is also the on-disk location and the
    number of ../ hops back to the top of the report.
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
    # Biggest gaps first: the point of the page is to show where coverage is missing.
    directories.sort(key=lambda entry: (-(entry[2].totals['lines'][0] - entry[2].totals['lines'][1]),
                                        entry[0]))

    rows = []
    for label, child_prefix, child in directories:
        rows.append(_row(label, label + '/index.html', child, True))
        _write_node(child, output_root, full_parts + tuple(child_prefix),
                    source_prefix, file_prefix, index_link, written)

    for name, totals in sorted(node.files, key=lambda entry: (-uncovered_lines(entry[1]), entry[0])):
        source_path = os.path.join(source_prefix, *full_parts, name)
        rows.append(_row(name, up + file_prefix + source_path.lstrip('/') + '.html', totals, False))

    crumbs = ['<a href="{}index.html">All source</a>'.format(up)] if depth else ['All source']
    for index in range(depth):
        hop = '../' * (depth - index - 1)
        piece = html.escape(full_parts[index])
        crumbs.append('<a href="{}index.html">{}</a>'.format(hop, piece) if index < depth - 1 else piece)

    display = '/'.join(full_parts) if full_parts else 'All source'
    totals = '<tr class="totals">' + _row('Total', '#', node, True)[len('<tr>'):]
    totals = totals.replace('<td class="dir" data-v="Total"><a href="#">Total</a></td>',
                            '<td data-v="">Total</td>')
    note = ('Directories aggregate their descendants. Click a column heading to sort. '
            'File names link into the full llvm-cov listing. '
            '<a href="{}{}">Flat llvm-cov index</a>'.format(up, index_link))
    subtitle = '{:,} lines, {:,} uncovered'.format(
        node.totals['lines'][0], node.totals['lines'][0] - node.totals['lines'][1])
    page = _page('Coverage: ' + display, subtitle, ' / '.join(crumbs), '\n'.join(rows), totals, depth, note)
    with open(os.path.join(directory, 'index.html'), 'w') as handle:
        handle.write(page)
    written.append(directory)


def write_directory_index(lcov_path, output_directory, source_root=None,
                          file_prefix='html/coverage/', index_link='html/index.html'):
    """Write the drill-down index from an lcov trace. Returns the number of pages written.

    source_root anchors the tree, so the hierarchy is the same shape whichever files a
    given run happened to cover, and it is also what copied-header paths are rewritten
    relative to. Without it the tree would be rooted at the common prefix of the covered
    paths, and a run touching only WebCore would silently lose the Source/WebCore levels.
    """
    from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov

    canonicalizer = PathCanonicalizer(source_root) if source_root else None
    coverage_by_path = parse_lcov(lcov_path, canonicalizer)
    if not coverage_by_path:
        raise RuntimeError('{} contained no coverage records'.format(lcov_path))
    if canonicalizer:
        canonicalizer.log_summary()

    entries = [(path, coverage.totals()) for path, coverage in coverage_by_path.items()]

    source_prefix = source_root.rstrip('/') if source_root else None
    if not source_prefix or not all(path.startswith(source_prefix + '/') for path, _ in entries):
        source_prefix = os.path.dirname(os.path.commonprefix([path for path, _ in entries]))

    stripped = []
    for path, totals in entries:
        relative = os.path.relpath(path, source_prefix)
        stripped.append((tuple(relative.split(os.sep)), totals))

    root = build_tree(stripped)
    written = []
    _write_node(root, output_directory, (), source_prefix, file_prefix, index_link, written)
    return len(written)

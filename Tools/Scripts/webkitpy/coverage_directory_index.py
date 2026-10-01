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

"""A drill-down directory index over an lcov trace: one small page per directory, linking to the
line views that coverage_source_view.py writes beside it. Files absent from the trace are listed apart from every percentage.
"""

import html
import logging
import os
from collections import defaultdict, namedtuple
from collections.abc import Mapping

from webkitpy.coverage_report_assets import (
    DIRECTORY, FILE, TREEMAP_SCRIPT, TREEMAP_STYLE, UNLINKED_FILE, relative_link,
    search_box_html, source_page_path, treemap_html, write_search_assets)
from webkitpy.coverage_report_theme import (
    THEME_CSS, THEME_SCRIPT_NAME, THEME_TOGGLE_HTML, write_theme_script)

logger = logging.getLogger(__name__)

# lcov carries no region data; summary.txt reports regions.
METRICS = ('lines', 'functions', 'branches')


def suite_metric(name):
    """The totals key for one suite's line coverage. The prefix keeps a suite called "lines" from colliding with that metric."""
    return 'suite:' + name


ReportPages = namedtuple('ReportPages',
                         ('directory_pages', 'source_pages', 'source_bytes', 'skipped_paths',
                          'totals'))

# One stylesheet and one script at the report root serve every directory page, as for the line views.
REPORT_STYLESHEET_NAME = 'coverage-report.css'
REPORT_SCRIPT_NAME = 'coverage-report.js'

# Below 50% is low and 80% or more is good, unless a --fail-under threshold says otherwise.
COVERAGE_BANDS = (50.0, 80.0)

# Shared with coverage_delta.py and coverage_patch.py, so keep REPORT_STYLE, SORT_SCRIPT,
# meter_html() and format_percent() public. The old property names alias the theme's.
REPORT_STYLE = THEME_CSS + r"""
:root {
  --surface-1: var(--cov-surface);
  --page: var(--cov-bg);
  --text-primary: var(--cov-fg);
  --text-secondary: var(--cov-muted);
  --muted: var(--cov-muted);
  --gridline: var(--cov-border);
  --border: var(--cov-border);
  --meter-track: var(--cov-border);
  --meter-fill: var(--cov-covered);
  --link: var(--cov-accent);
}
[hidden] { display: none !important; }
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  background: var(--page);
  color: var(--text-primary);
  font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
:focus-visible { outline: 2px solid var(--cov-accent); outline-offset: 2px; }
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 17px; font-weight: 600; margin: 0 0 2px; overflow-wrap: anywhere; }
.sub { color: var(--text-secondary); margin: 0 0 18px; font-size: 12px; }
.crumbs { margin: 0 0 14px; font-size: 12px; color: var(--text-secondary); }
.crumbs a { color: var(--link); text-decoration: none; }
.crumbs a:hover { text-decoration: underline; }
.vh {
  position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; border: 0;
  overflow: hidden; clip-path: inset(50%); white-space: nowrap;
}
.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow-x: auto;
}
table { width: 100%; border-collapse: collapse; }
th, td { padding: 7px 12px; text-align: left; }
td:first-child { overflow-wrap: anywhere; }
th {
  font-size: 11px; font-weight: 600; color: var(--muted); background: var(--surface-1);
  text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; user-select: none;
  box-shadow: inset 0 -1px 0 var(--gridline);
}
th button {
  position: relative; font: inherit; letter-spacing: inherit; text-transform: inherit;
  color: inherit; padding: 0; background: none; border: 0; cursor: pointer;
}
th button:hover, th[aria-sort] button { color: var(--text-primary); }
th[aria-sort] button::after {
  position: absolute; left: 100%; top: 50%; margin: -5px 0 0 3px; font-size: 8px; line-height: 10px;
}
th[aria-sort="ascending"] button::after { content: "\25B2"; }
th[aria-sort="descending"] button::after { content: "\25BC"; }
th.n, td.n { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr { border-bottom: 1px solid var(--gridline); }
tbody tr:last-child { border-bottom: 0; }
tbody tr:hover { background: color-mix(in oklab, var(--meter-fill) 7%, transparent); }
td a { color: var(--text-primary); text-decoration: none; }
td a:hover { color: var(--link); text-decoration: underline; }
.dir a { font-weight: 600; }
.dir a::before { content: "\2192  "; color: var(--muted); font-weight: 400; }
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
.hint a { color: var(--link); }
h2 { font-size: 13px; font-weight: 600; margin: 22px 0 2px; }
.caveat {
  margin: 0 0 18px; padding: 9px 12px; font-size: 12px;
  color: var(--text-secondary); background: var(--surface-1);
  border: 1px solid var(--border); border-left: 3px solid var(--muted); border-radius: 6px;
}
td.reason, td.detail { color: var(--text-secondary); }
td.detail { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
td.nosource { color: var(--text-secondary); cursor: help; }
.toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin: 0 0 10px; }
.toolbar input[type="search"], .toolbar input[type="number"] {
  min-width: 0; padding: 6px 10px; font: 12px/1.4 system-ui, -apple-system, sans-serif;
  color: var(--text-primary); background: var(--surface-1);
  border: 1px solid var(--muted); border-radius: 6px;
}
.toolbar input[type="search"] { flex: 1 1 220px; }
.toolbar input[type="number"] { width: 84px; padding: 5px 8px; }
.toolbar label {
  display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
  color: var(--text-secondary); font-size: 12px;
}
.toolbar input[type="checkbox"] { width: 14px; height: 14px; margin: 0; accent-color: var(--cov-accent); }
.toolbar .count {
  margin-left: auto; color: var(--text-secondary); font-size: 11px;
  white-space: nowrap; font-variant-numeric: tabular-nums;
}
td.path { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
td.path .dim { color: var(--muted); }
@media (max-width: 700px) {
  body { padding: 12px; }
  .meter { width: 64px; }
  th, td { padding: 6px 8px; }
  .c-fn, .c-br, .c-nb, .c-meter, .c-lines { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  *, ::before, ::after { transition: none !important; animation: none !important; scroll-behavior: auto !important; }
}
@media (forced-colors: active) {
  .meter, .gauge { border: 1px solid CanvasText; }
  .meter > i, .gauge i { forced-color-adjust: none; background: Highlight; }
}
@media print {
  body { padding: 0; background: #fff; }
  .card { overflow: visible; }
  th { position: static !important; }
  thead { display: table-header-group; }
  tr { break-inside: avoid; }
  .meter, .gauge, .gauge i, .meter > i { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
}
"""

# Only index.html and the directory pages carry these; the delta and patch reports do not.
INDEX_STYLE = r"""
.top { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.top .tools { display: flex; align-items: flex-start; gap: 8px; }
.top .report-search { flex: 0 1 300px; width: 300px; }
.dash {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px;
  margin: 0 0 16px; padding: 0; list-style: none;
}
.stat { padding: 11px 14px 12px; background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; }
.stat .k {
  margin: 0; font-size: 11px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em;
}
.stat .v { margin: 2px 0 7px; font-size: 30px; line-height: 1.15; font-weight: 600; }
.stat .s { margin: 5px 0 0; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.gauge { position: relative; height: 6px; background: var(--border); border-radius: 3px; }
.gauge i { position: absolute; inset: 0 auto 0 0; background: var(--cov-covered); border-radius: 3px; }
.gauge .gate {
  position: absolute; top: -3px; bottom: -3px; width: 2px; margin-left: -1px;
  background: var(--cov-fg); border-radius: 1px;
}
.stat.mid .gauge i { background: var(--cov-partial); }
.stat.low .gauge i { background: var(--cov-uncovered); }
.card.sticky th { position: sticky; top: 0; z-index: 1; }
:root:not(.cov-js) .toolbar { display: none; }
:root:not(.cov-js) th button { cursor: default; }
@media (min-width: 1150px) { .card.sticky { overflow: clip; } }
@media (max-width: 700px) {
  .top, .top .tools { flex-direction: column; gap: 8px; }
  .top .tools { width: 100%; }
  .top .report-search { flex: none; width: 100%; }
  .stat .v { font-size: 24px; }
  .toolbar .count { margin-left: 0; }
}
@media print {
  .top .tools, .toolbar { display: none; }
  .stat, .chart { break-inside: avoid; }
}
""" + TREEMAP_STYLE

SORT_SCRIPT = r"""
(function () {
  'use strict';
  var collator = typeof Intl === 'object' ? new Intl.Collator(undefined, {numeric: true, sensitivity: 'base'}) : null;

  function compareText(a, b) {
    if (collator) { return collator.compare(a, b); }
    return a < b ? -1 : a > b ? 1 : 0;
  }

  function toNumber(text) {
    var number = parseFloat(text);
    return isNaN(number) ? -1 : number;
  }

  // The permutation that sorts values. Ties keep their order, whichever way the sort runs.
  function order(values, numeric, descending) {
    var items = values.map(function (value, index) {
      return {value: numeric ? toNumber(value) : String(value), index: index};
    });
    items.sort(function (a, b) {
      var difference = numeric ? a.value - b.value : compareText(a.value, b.value);
      if (difference === 0) { return a.index - b.index; }
      return descending ? -difference : difference;
    });
    return items.map(function (item) { return item.index; });
  }

  function recall(key) {
    try { return (sessionStorage.getItem(key) || '').split(':'); } catch (error) { return []; }
  }

  function remember(key, column, descending) {
    try { sessionStorage.setItem(key, column + ':' + (descending ? 'desc' : 'asc')); } catch (error) {}
  }

  function sortTable(table, heading, descending) {
    var body = table.tBodies[0];
    var column = +heading.dataset.col;
    var rows = Array.prototype.filter.call(body.rows, function (row) {
      return !row.classList.contains('totals');
    });
    var values = rows.map(function (row) {
      var cell = row.cells[column];
      return cell.dataset.v !== undefined ? cell.dataset.v : cell.textContent;
    });
    var fragment = document.createDocumentFragment();
    order(values, heading.dataset.numeric === '1', descending).forEach(function (index) {
      fragment.appendChild(rows[index]);
    });
    body.appendChild(fragment);
    Array.prototype.forEach.call(table.querySelectorAll('th[data-col]'), function (other) {
      if (other === heading) { other.setAttribute('aria-sort', descending ? 'descending' : 'ascending'); }
      else { other.removeAttribute('aria-sort'); }
    });
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll('table'), function (table, position) {
      if (!table.tBodies.length) { return; }
      var key = 'cov-sort:' + location.pathname + ':' + position;
      var headings = table.querySelectorAll('th[data-col]');
      Array.prototype.forEach.call(headings, function (heading) {
        heading.addEventListener('click', function () {
          var current = heading.getAttribute('aria-sort');
          var descending = current ? current === 'ascending' : heading.dataset.numeric === '1';
          sortTable(table, heading, descending);
          remember(key, heading.dataset.col, descending);
        });
      });
      var saved = recall(key);
      Array.prototype.forEach.call(headings, function (heading) {
        if (heading.dataset.col === saved[0]) { sortTable(table, heading, saved[1] === 'desc'); }
      });
    });
  }

  if (typeof document !== 'undefined') { init(); }
  if (typeof module !== 'undefined' && module.exports) { module.exports = {order: order}; }
})();
"""

# Substring filter (not fuzzy, on purpose) over tables that opt in with data-filterable.
FILTER_SCRIPT = r"""
(function () {
  'use strict';

  // A row with no line counts, such as a not-built file, is only ever filtered by name.
  function visible(row, state) {
    if (state.query && row.name.indexOf(state.query) === -1) { return false; }
    if (row.lines === null) { return true; }
    if (state.minimum > 0 && row.lines < state.minimum) { return false; }
    return !(state.hideCovered && row.lines > 0 && row.uncovered === 0);
  }

  function init() {
    var input = document.getElementById('filter');
    if (!input) { return; }
    var status = document.getElementById('filter-count');
    var minimum = document.getElementById('min-lines');
    var hideCovered = document.getElementById('hide-covered');
    var rows = [];
    document.querySelectorAll('table[data-filterable="1"]').forEach(function (table) {
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        if (row.classList.contains('totals')) { return; }
        var lines = row.querySelector('.c-lines'), uncovered = row.querySelector('.c-unc');
        var cell = row.cells[0];
        rows.push({
          element: row, name: (cell.dataset.v || cell.textContent).toLowerCase(),
          lines: lines && uncovered ? parseFloat(lines.dataset.v) : null,
          uncovered: uncovered ? parseFloat(uncovered.dataset.v) : null
        });
      });
    });
    var total = rows.length;
    function apply() {
      var state = {
        query: input.value.trim().toLowerCase(),
        minimum: minimum ? parseInt(minimum.value, 10) || 0 : 0,
        hideCovered: !!(hideCovered && hideCovered.checked)
      };
      var shown = 0;
      rows.forEach(function (row) {
        var match = visible(row, state);
        row.element.hidden = !match;
        if (match) { shown++; }
      });
      var filtered = state.query || state.minimum > 0 || state.hideCovered;
      status.textContent = filtered
        ? shown.toLocaleString() + ' of ' + total.toLocaleString() + ' shown'
        : total.toLocaleString() + (total === 1 ? ' row' : ' rows');
    }
    [input, minimum, hideCovered].forEach(function (control) {
      if (control) { control.addEventListener('input', apply); control.addEventListener('change', apply); }
    });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { input.value = ''; apply(); }
    });
    apply();
  }

  if (typeof document !== 'undefined') { init(); }
  if (typeof module !== 'undefined' && module.exports) { module.exports = {visible: visible}; }
})();
"""

# Capped, because the top of the list is the point. The heading says what the cap left out.
WORST_FILES_LIMIT = 50


class _Node:
    __slots__ = ('name', 'children', 'files', 'file_count', 'totals', 'absent', 'absent_totals')

    def __init__(self, name):
        self.name = name
        self.children = {}
        self.files = []
        self.file_count = 0                         # with descendants
        # [count, covered] per metric: the llvm-cov metrics, plus one per suite.
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
            ancestor.file_count += 1
            # Over the file's own metrics, not METRICS, so per-suite metrics aggregate without special-casing.
            for metric, (count, covered) in totals.items():
                ancestor.add(metric, count, covered)
    return root


def attach_absent_files(root, absent_files, source_prefix):
    """Hang the not-built files off the coverage tree, creating directories the coverage data never mentioned.

    A directory can hold nothing but another port's files, and would otherwise have no page to show them on.
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


def _percent_cell(count, covered, scope=None, css=''):
    """One percentage cell. scope, when given, puts the >= on a lower bound. data-v stays the bare number so the column sorts numerically."""
    value = _percent(count, covered)
    text = format_percent(value) if scope is None else scope.format_percent(value)
    return '<td class="n pct{}" data-v="{}">{}</td>'.format(
        ' ' + css if css else '', -1 if value is None else '{:.4f}'.format(value), text)


def _row(label, link, node_or_summary, is_directory, suite_names=(), no_link_reason='',
         scope=None):
    cells = []
    kind = 'dir' if is_directory else 'file'
    if link is None:
        # No line view: show the reason rather than a link to a 404, and keep the coverage data visible.
        cells.append('<td class="{} nosource" data-v="{}" title="No line view: {}">{}</td>'.format(
            kind, html.escape(label), html.escape(no_link_reason or 'unknown'),
            html.escape(label)))
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
            cells.append('<td class="c-meter" data-v="{}">{}</td>'.format(
                -1 if value is None else '{:.4f}'.format(value), meter_html(value)))
        cells.append(_percent_cell(count, covered, scope, _METRIC_COLUMN_CLASS.get(metric, '')))
        if metric == 'lines':
            cells.append('<td class="n c-lines" data-v="{}">{:,}</td>'.format(count, count))
            cells.append('<td class="n c-unc" data-v="{}">{:,}</td>'.format(
                count - covered, count - covered))
            # Beside the combined figure: the question is which suite is not reaching this code.
            for name in suite_names:
                cells.append(_percent_cell(*totals_for(suite_metric(name)), scope=scope))
    # A file count, not a percentage (no coverage mapping, so no denominator), and not per suite: every suite
    # ran the same binaries.
    not_built = node_or_summary.absent_totals[0] if is_directory else 0
    cells.append('<td class="n c-nb" data-v="{}">{}</td>'.format(
        not_built, '{:,}'.format(not_built) if not_built else '-'))
    return '<tr>' + ''.join(cells) + '</tr>'


# The columns a narrow screen drops; see REPORT_STYLE.
_METRIC_COLUMN_CLASS = {'functions': 'c-fn', 'branches': 'c-br'}


def _headers(suite_names=()):
    """(label, css class, column index, sorts numerically) per column, in order."""
    headers = [
        ('Name', '', False),
        ('Line coverage', 'c-meter', True),
        ('Lines %', 'n', True),
        ('Lines', 'n c-lines', True),
        ('Uncovered', 'n', True),
    ]
    headers += [('{} %'.format(name), 'n', True) for name in suite_names]
    if suite_names:
        headers[2] = ('All suites %', 'n', True)
    headers += [
        ('Functions %', 'n c-fn', True),
        ('Branches %', 'n c-br', True),
        ('Not built', 'n c-nb', True),
    ]
    return tuple(headers)


_ABSENT_HEADERS = (
    ('File', '', False),
    ('Physical lines', 'n', True),
    ('Why it is not in this build', '', False),
    ('Detail', '', False),
)

# /DerivedSources/ is excluded from the trace and from the not-built universe, so generated implementation
# files (JS bindings, IPC serializers, CSS tables) are in neither state. They are counted from the build
# directory because llvm-cov never emitted a record for them.
GENERATED_SOURCES_REASON = 'generated'
GENERATED_SOURCES_LABEL = 'Generated by the build, excluded from the report'
GENERATED_SOURCES_EXPLANATION = (
    'Written into the build directory by the IDL, IPC, CSS and other generators, then '
    'compiled and instrumented like any other file. The report excludes /DerivedSources/, so '
    'these are neither reported on nor listed as not built. Physical lines, counted in the '
    'build directory; llvm-cov emitted no record for them to count.')


def generated_source_totals(build_directory):
    """(files, physical lines) for the implementation files the build generated, or None without a DerivedSources tree.

    Unified-source bundles are skipped: they only #include files already counted here.
    """
    from webkitpy.coverage_build_inventory import IMPLEMENTATION_SUFFIXES, physical_line_count

    derived = os.path.join(build_directory, 'DerivedSources')
    if not os.path.isdir(derived):
        return None
    files = 0
    lines = 0
    for current, directories, names in os.walk(derived):
        if os.path.basename(current) == 'unified-sources':
            directories[:] = []
            continue
        for name in names:
            if name.endswith(IMPLEMENTATION_SUFFIXES) and not name.startswith('UnifiedSource'):
                files += 1
                lines += physical_line_count(os.path.join(current, name))
    return (files, lines) if files else None


def _files(count):
    return '{:,} file'.format(count) if count == 1 else '{:,} files'.format(count)


def headers_html(headers):
    """The header row for a sortable table. headers are (label, css class, sorts numerically).

    The column index is assigned here to agree with SORT_SCRIPT's data-col. Shared with coverage_delta and coverage_patch; keep it public.
    """
    cells = []
    for column, (label, css, numeric) in enumerate(headers):
        cells.append('<th class="{}" data-col="{}" data-numeric="{}" scope="col">'
                     '<button type="button">{}</button></th>'.format(
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
    return """<div>
<h2>Not built in this configuration &mdash; {count}, {total:,} physical lines</h2>
<div class="card">
<table data-filterable="1">
<thead><tr>{headers}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</div>
""".format(count=_files(len(absent_files)), total=total,
           headers=headers_html(_ABSENT_HEADERS), rows='\n'.join(rows))


_WORST_HEADERS = (
    ('File', '', False),
    ('Line coverage', 'c-meter', True),
    ('Lines %', 'n', True),
    ('Lines', 'n c-lines', True),
    ('Uncovered', 'n', True),
)


def _worst_headers(selective):
    """Same columns, and the last one is not called "Uncovered" on a selective run."""
    if not selective:
        return _WORST_HEADERS
    return _WORST_HEADERS[:-1] + (('Not executed', 'n', True),)


def _worst_files_card(all_files, source_prefix, unlinkable, limit=WORST_FILES_LIMIT, scope=None):
    """The project-wide least-covered files, ranked by uncovered lines, not percentage.

    all_files is the list the tree is built from: [(path components relative to source_prefix, {metric: (count, covered)})].
    """
    ranked = []
    for components, totals in all_files:
        count, covered = totals.get('lines', (0, 0))
        uncovered = count - covered
        if uncovered <= 0:
            continue
        ranked.append((uncovered, '/'.join(components), components, totals))
    if not ranked:
        return ''
    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    shown = ranked[:limit]

    rows = []
    for uncovered, relative, components, totals in shown:
        source_path = os.path.join(source_prefix, *components)
        reason = unlinkable.get(source_path)
        directory, _, name = relative.rpartition('/')
        # Dim the directory rather than drop it: same-named files from different trees would look identical.
        label = ('<span class="dim">{}/</span>{}'.format(html.escape(directory), html.escape(name))
                 if directory else html.escape(name))
        if reason:
            first = ('<td class="path nosource" data-v="{v}" title="No line view: {why}">{label}'
                     '</td>').format(v=html.escape(relative), why=html.escape(reason), label=label)
        else:
            href = relative_link(source_page_path(relative))
            first = '<td class="path" data-v="{v}"><a href="{href}">{label}</a></td>'.format(
                v=html.escape(relative), href=html.escape(href), label=label)
        count, covered = totals.get('lines', (0, 0))
        value = _percent(count, covered)
        rows.append(
            '<tr>{first}<td class="c-meter" data-v="{sort}">{meter}</td>{pct}'
            '<td class="n c-lines" data-v="{count}">{count:,}</td>'
            '<td class="n c-unc" data-v="{uncovered}">{uncovered:,}</td></tr>'.format(
                first=first, sort=-1 if value is None else '{:.4f}'.format(value),
                meter=meter_html(value), pct=_percent_cell(count, covered, scope),
                count=count, uncovered=uncovered))

    total_uncovered = sum(entry[0] for entry in ranked)
    shown_uncovered = sum(entry[0] for entry in shown)
    # "Uncovered" is a false label under selection: a line no test executed may simply not have been asked.
    selective = scope is not None and scope.is_selective
    lines_phrase = 'lines no test that ran executed' if selective else 'uncovered lines'
    heading = 'Least-covered files &mdash; the {:,} with the most {}'.format(
        len(shown), lines_phrase)
    if len(ranked) > len(shown):
        heading += ', of {:,} with any'.format(len(ranked))
    note = ('These {shown:,} hold {shown_lines:,} of the {total_lines:,} {phrase} in the '
            'report, {share:.1f}%. Ranked by line count, not by percentage: one uncovered line '
            'in a two-line file is 50% and the column is there for that reading. Search above '
            'to reach a file that is not on this list.').format(
                shown=len(shown), shown_lines=shown_uncovered, total_lines=total_uncovered,
                phrase=lines_phrase, share=100.0 * shown_uncovered / total_uncovered)
    if selective:
        note += (' Each count is an upper bound on what is genuinely untested, never a lower '
                 'one, so every file listed here is still worth looking at.')
    return """<div>
<h2>{heading}</h2>
<div class="card">
<table data-filterable="1">
<thead><tr>{headers}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
<p class="hint">{note}</p>
</div>
""".format(heading=heading, headers=headers_html(_worst_headers(selective)),
           rows='\n'.join(rows), note=html.escape(note))


def _reason_card(reason_rows, reason_explanations, generated=None):
    """The reason table, plus a row for the generated sources, kept out of the total. generated is (files, physical lines)."""
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
    total_files = sum(row[2] for row in reason_rows)
    heading = 'Why {} of the tree {} not in this report'.format(
        _files(total_files), 'is' if total_files == 1 else 'are')
    if generated:
        rows.append(
            '<tr><td data-v="{label}">{label}</td>'
            '<td class="n" data-v="{files}">{files:,}</td>'
            '<td class="n" data-v="{lines}">{lines:,}</td>'
            '<td class="reason" data-v="">{why}</td></tr>'.format(
                label=html.escape(GENERATED_SOURCES_LABEL), files=generated[0],
                lines=generated[1], why=html.escape(GENERATED_SOURCES_EXPLANATION)))
        heading += ', plus {} the build generated'.format(_files(generated[0]))
    headers = headers_html((('Why a file is not in this report', '', False),
                            ('Files', 'n', True),
                            ('Physical lines', 'n', True),
                            ('What it means', '', False)))
    return """<div>
<h2>{heading}</h2>
<div class="card">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</div>
""".format(heading=html.escape(heading), headers=headers, rows='\n'.join(rows))


_DASHBOARD_METRICS = (
    ('lines', 'Lines', 'lines'),
    ('functions', 'Functions', 'functions'),
    ('branches', 'Branches', 'branches'),
    ('regions', 'Regions', 'regions'),
)


def _band(value, threshold):
    """'ok', 'mid' or 'low' for a percentage, against the gate when there is one."""
    if value is None:
        return ''
    if threshold is not None:
        return 'ok' if value >= threshold else 'low'
    if value >= COVERAGE_BANDS[1]:
        return 'ok'
    return 'mid' if value >= COVERAGE_BANDS[0] else 'low'


def _stat_html(title, noun, count, covered, scope=None, threshold=None):
    """One headline figure with a thin meter. A lower bound is never judged against a band."""
    value = _percent(count, covered)
    selective = scope is not None and scope.is_selective
    if value is None:
        shown = '-'
    elif scope is not None:
        shown = scope.format_percent(value, places=1)
    else:
        shown = '{:.1f}%'.format(value)
    band = '' if selective else _band(value, threshold)
    if value is None:
        gauge = '<div class="gauge" aria-hidden="true">'
    else:
        gauge = ('<div class="gauge" role="meter" aria-label="{} coverage" aria-valuemin="0" '
                 'aria-valuemax="100" aria-valuenow="{:.1f}" aria-valuetext="{}">'.format(
                     title, value, html.escape(shown)))
        gauge += '<i style="width:{:.2f}%"></i>'.format(max(min(value, 100.0), 0.0))
        if threshold is not None and not selective:
            gauge += '<span class="gate" style="left:{:.2f}%"></span>'.format(threshold)
    gauge += '</div>'
    if count:
        detail = '{:,} of {:,} {} {}'.format(
            covered, count, noun, 'executed' if selective else 'covered')
    else:
        detail = 'None instrumented'
    if threshold is not None and not selective and value is not None:
        detail += '<br>Gate {:g}%: {}'.format(threshold, 'met' if value >= threshold else 'not met')
    return ('<li class="stat{band}"><p class="k">{title}</p><p class="v">{shown}</p>{gauge}'
            '<p class="s">{detail}</p></li>').format(
                band=' ' + band if band else '', title=title, shown=html.escape(shown),
                gauge=gauge, detail=detail)


def _dashboard_html(node, scope=None, thresholds=None):
    """The headline strip: coverage by metric, then how much code this is."""
    thresholds = thresholds or {}
    tiles = []
    for metric, title, noun in _DASHBOARD_METRICS:
        count, covered = node.totals.get(metric, (0, 0))
        if metric == 'regions' and not count:
            continue
        tiles.append(_stat_html(title, noun, count, covered, scope, thresholds.get(metric)))
    size = '<p class="s">{:,} executable lines</p>'.format(node.totals['lines'][0])
    if node.absent_totals[0]:
        size += '<p class="s">{} not built</p>'.format(_files(node.absent_totals[0]))
    tiles.append('<li class="stat"><p class="k">Files</p><p class="v">{:,}</p>{}</li>'.format(
        node.file_count, size))
    return '<section aria-label="Coverage summary"><ul class="dash">{}</ul></section>\n'.format(
        ''.join(tiles))


def _page(title, subtitle, crumbs_html, rows_html, totals_row, note, up,
          caveat='', extra_cards='', suite_names=(), filter_label='', dashboard='', chart='',
          caption=''):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<link rel="stylesheet" href="{up}{stylesheet}">
<script src="{up}{theme}"></script>
<script defer src="{up}{script}"></script>
</head>
<body>
<div class="wrap">
<header class="top"><div>
<h1>{title}</h1>
<p class="sub">{subtitle}</p>
</div><div class="tools">{toggle}{search}</div></header>
<main>
<nav aria-label="Breadcrumb"><p class="crumbs">{crumbs}</p></nav>
{caveat}{dashboard}{chart}{toolbar}<div class="card{sticky}">
<table id="entries" data-filterable="1">
<caption class="vh">{caption}</caption>
<thead><tr>{headers}</tr></thead>
<tbody>
{totals}
{rows}
</tbody>
</table>
</div>
{extra_cards}<p class="hint">{note}</p>
</main>
</div>
</body>
</html>
""".format(title=html.escape(title), subtitle=html.escape(subtitle), crumbs=crumbs_html,
           headers=headers_html(_headers(suite_names)), rows=rows_html, totals=totals_row,
           up=up, stylesheet=REPORT_STYLESHEET_NAME, script=REPORT_SCRIPT_NAME,
           theme=THEME_SCRIPT_NAME, toggle=THEME_TOGGLE_HTML, search=search_box_html(up),
           sticky=' sticky' if len(suite_names) <= 2 else '',
           note=note, caption=html.escape(caption or title), dashboard=dashboard, chart=chart,
           caveat='<p class="caveat">{}</p>\n'.format(html.escape(caveat)) if caveat else '',
           toolbar=_toolbar(filter_label), extra_cards=extra_cards)


def _toolbar(label):
    """The filters. label names what a name match is measured against on this page."""
    return ("""<div class="toolbar">
<input id="filter" type="search" autocomplete="off" spellcheck="false"
 placeholder="{placeholder}" aria-label="{placeholder}">
<label>Min lines
 <input id="min-lines" type="number" min="0" step="10" value="0" inputmode="numeric"></label>
<label><input id="hide-covered" type="checkbox"> Hide fully covered</label>
<span class="count" id="filter-count" role="status"></span>
</div>
""").format(placeholder=html.escape(label or 'Filter by name'))


def _write_node(node, output_root, full_parts, source_prefix, index_link, written,
                absence=None, unlinkable=None, ancestor_pages=frozenset(), suite_names=(),
                generated=None, scope=None, all_files=(), thresholds=None):
    """Write one index.html per directory node.

    ancestor_pages holds the full_parts of ancestors that have a page: a collapsed single-child chain has fewer pages than levels.
    all_files and thresholds are read only at depth 0 and not passed down: the least-covered card is project-wide, and the
    gate is on the project total.
    """
    unlinkable = unlinkable or {}
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
    # Biggest gaps first. A directory with no coverage data still sorts by its not-built lines, so an
    # entirely-not-built directory does not sink for having no rows.
    directories.sort(key=lambda entry: (-(entry[2].totals['lines'][0] - entry[2].totals['lines'][1]),
                                        -entry[2].absent_totals[1], entry[0]))

    rows = []
    for label, child_prefix, child in directories:
        rows.append(_row(label, relative_link(label + '/index.html'), child, True, suite_names))
        _write_node(child, output_root, full_parts + tuple(child_prefix),
                    source_prefix, index_link, written, absence, unlinkable,
                    ancestor_pages | {full_parts}, suite_names, generated, scope)

    for name, totals in sorted(node.files, key=lambda entry: (-uncovered_lines(entry[1]), entry[0])):
        # The line view sits beside this page. A file's page is Foo.html and a directory's is Foo/index.html,
        # so they cannot collide; source_page_path() keeps a file called index off this directory's own page.
        source_path = os.path.join(source_prefix, *full_parts, name)
        reason = unlinkable.get(source_path)
        href = None if reason else relative_link(source_page_path(name))
        rows.append(_row(name, href, totals, False, suite_names, reason or ''))

    crumbs = ['<a href="{}index.html">All source</a>'.format(up)] if depth else ['All source']
    for index in range(depth):
        piece = html.escape(full_parts[index])
        # Link only a level that has a page: a collapsed single-child chain has one page for several levels.
        if index == depth - 1 or full_parts[:index + 1] not in ancestor_pages:
            crumbs.append(piece)
        else:
            crumbs.append('<a href="{}index.html">{}</a>'.format('../' * (depth - index - 1), piece))

    display = '/'.join(full_parts) if full_parts else 'All source'
    totals = '<tr class="totals">' + _row('Total', '#', node, True, suite_names,
                                          scope=scope)[len('<tr>'):]
    totals = totals.replace('<td class="dir" data-v="Total"><a href="#">Total</a></td>',
                            '<td data-v="">Total</td>')
    note = ('Directories aggregate their descendants. Click a column heading to sort, type in '
            'the filter to narrow this page, or press / to search the whole report. A file name '
            'links to its line-by-line coverage. '
            '"Not built" is a file count, not a percentage: those files have no coverage '
            'mapping to be a percentage of.')
    if suite_names:
        note += (' The per-suite columns are one profile each and "All suites" is their merge, '
                 'so it is the union of the lines they executed and not the sum: a line two '
                 'suites reach counts once. "Not built" has no per-suite column because every '
                 'suite ran against the same binaries, so a file that was not built was not '
                 'built for any of them.')
    if scope is not None and scope.is_selective:
        note += (' Every percentage on this page is a lower bound, including the per-file rows, '
                 'which are not marked individually to keep the table readable -- the Total row '
                 'is. A row at 0% means no test that ran reached that file, which is not the '
                 'same as no test reaching it.')
    if index_link:
        note += ' <a href="{}{}">Flat llvm-cov index</a>'.format(up, index_link)
    if scope is not None and scope.is_selective:
        # "Uncovered" is a false label here: a line no test executed may simply not have been asked.
        subtitle = '{:,} lines, {:,} not executed by the tests that ran'.format(
            node.totals['lines'][0], node.totals['lines'][0] - node.totals['lines'][1])
    else:
        subtitle = '{:,} lines, {:,} uncovered'.format(
            node.totals['lines'][0], node.totals['lines'][0] - node.totals['lines'][1])
    if node.absent_totals[0]:
        subtitle += ', {} not built here ({:,} physical lines)'.format(
            _files(node.absent_totals[0]), node.absent_totals[1])

    caveat = ''
    extra_cards = ''
    # Before the not-built cards: it is the answer the report was opened for, and those are its caveat.
    if not depth and all_files:
        extra_cards += _worst_files_card(all_files, source_prefix, unlinkable, scope=scope)
    if absence is not None:
        if not depth:
            caveat = absence.denominator_sentence()
            if generated:
                # Same sentence as the other caveat, being the larger of the two.
                caveat += (' A further {} of generated implementation files ({:,} physical '
                           'lines) are excluded from this report altogether and are in neither '
                           'count; see the table below.'.format(_files(generated[0]),
                                                                generated[1]))
            extra_cards += _reason_card(absence.reasons(), absence.explanations, generated)
        extra_cards += _absent_card(node.absent, absence.labels)

    title = 'Coverage: ' + display
    if scope is not None:
        # On every page, not just the root: the caveat has to travel with the number.
        caveat = ' '.join(scope.banner_lines() + ([caveat] if caveat else [])) or caveat
        title = scope.qualify_title(title)

    tiles = sum(1 for _, _, child in directories if child.totals['lines'][0] > 0)
    tiles += sum(1 for _, file_totals in node.files if file_totals.get('lines', (0, 0))[0] > 0)
    chart = treemap_html(tiles, scope.qualify('') if scope is not None else '')

    page = _page(title, subtitle, ' / '.join(crumbs), '\n'.join(rows), totals,
                 note, up, caveat=caveat, extra_cards=extra_cards, suite_names=suite_names,
                 filter_label='Filter this directory by name',
                 dashboard=_dashboard_html(node, scope, None if depth else thresholds), chart=chart,
                 caption='Coverage of the contents of ' + display)
    with open(os.path.join(directory, 'index.html'), 'w') as handle:
        handle.write(page)
    written.append((full_parts, node.totals['lines']))


def effective_source_prefix(paths, source_root=None):
    """The directory the report's tree is rooted at: source_root whenever there is one, else the common prefix.

    Not the common prefix when a covered path is outside source_root (copied headers hang off a synthetic top-level node, see
    coverage_source_view.relative_source_path()): that collapses to '/' and silently drops the not-built caveat.
    """
    prefix = source_root.rstrip('/') if source_root else None
    if prefix:
        return prefix
    return os.path.dirname(os.path.commonprefix(list(paths)))


def write_directory_index(lcov_path, output_directory, source_root=None, index_link=None,
                          absence=None, coverage_by_path=None, unlinkable=(),
                          unlinkable_reason=None, build_directory=None, suite_line_totals=(),
                          scope=None, thresholds=None, source_prefix=None):
    """Write the drill-down index from an lcov trace. Returns the number of pages written.

    source_root anchors the tree, so a run touching only WebCore keeps its Source/WebCore levels.
    unlinkable is {path: reason} or an iterable of paths, which get unlinkable_reason; suite_line_totals is
    [(suite name, {path: (lines, covered)})] in column order. thresholds are judged on the root page only.
    source_prefix pins the tree's root when the caller laid the line views out by it already.
    """
    from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov
    from webkitpy.coverage_source_view import (
        UNREADABLE_SOURCE, drop_unconfined, relative_source_path)

    default_reason = unlinkable_reason or UNREADABLE_SOURCE
    if isinstance(unlinkable, Mapping):
        unlinkable = {path: reason or default_reason for path, reason in unlinkable.items()}
    else:
        unlinkable = {path: default_reason for path in unlinkable}

    if coverage_by_path is None:
        canonicalizer = (PathCanonicalizer(source_root, build_directory=build_directory)
                         if source_root else None)
        coverage_by_path = parse_lcov(lcov_path, canonicalizer, keep_function_names=False)
        if canonicalizer:
            canonicalizer.log_summary()
    if not coverage_by_path:
        raise RuntimeError('{} contained no coverage records'.format(lcov_path))

    if source_prefix is None:
        source_prefix = effective_source_prefix(coverage_by_path, source_root)
    coverage_by_path = drop_unconfined(coverage_by_path, source_prefix)

    suite_names = tuple(name for name, _ in suite_line_totals)
    entries = []
    for path, coverage in coverage_by_path.items():
        totals = coverage.totals()
        for name, per_path in suite_line_totals:
            # No record in a suite's trace means that suite says nothing about the file, not zero coverage; the column shows '-'.
            if path in per_path:
                totals[suite_metric(name)] = per_path[path]
        entries.append((path, totals))

    stripped = []
    for path, totals in entries:
        # The same rule the line views are laid out by, so a file's row links to a page beside it (paths outside
        # the source root go under one synthetic node).
        stripped.append((tuple(relative_source_path(path, source_prefix).split('/')), totals))

    root = build_tree(stripped)
    generated = None
    if absence is not None:
        # Absent paths are relative to the checkout root, so they attach only to a tree rooted there.
        if source_root:
            attach_absent_files(root, absence.files, '')
            if build_directory:
                generated = generated_source_totals(build_directory)
        else:
            logger.warning('No source root, so the tree is rooted at the common prefix of the '
                           'covered paths and the %d files this configuration never compiled '
                           'cannot be placed in it. Those files are in not-built.tsv, and this '
                           "report's percentages are over an unstated fraction of the tree.",
                           len(absence.files))
            absence = None
    written = []
    _write_node(root, output_directory, (), source_prefix, index_link, written,
                absence, unlinkable, suite_names=suite_names, generated=generated, scope=scope,
                all_files=stripped, thresholds=thresholds)
    _write_assets(output_directory, written, stripped, source_prefix, unlinkable)
    return len(written)


def _write_assets(output_directory, pages, files, source_prefix, unlinkable):
    """The shared stylesheet and script, and the search index over every page in the report."""
    with open(os.path.join(output_directory, REPORT_STYLESHEET_NAME), 'w') as handle:
        handle.write(REPORT_STYLE.lstrip('\n') + INDEX_STYLE)
    with open(os.path.join(output_directory, REPORT_SCRIPT_NAME), 'w') as handle:
        handle.write(SORT_SCRIPT.lstrip('\n') + FILTER_SCRIPT + TREEMAP_SCRIPT)
    write_theme_script(output_directory)
    entries = []
    for components, totals in files:
        count, covered = totals.get('lines', (0, 0))
        linked = os.path.join(source_prefix, *components) not in unlinkable
        path = '/'.join(components)
        entry = (path, count, covered, FILE if linked else UNLINKED_FILE)
        if linked and source_page_path(path) != path + '.html':
            entry += (source_page_path(path),)
        entries.append(entry)
    for full_parts, (count, covered) in pages:
        if full_parts:
            entries.append(('/'.join(full_parts), count, covered, DIRECTORY))
    write_search_assets(output_directory, entries)


def write_report(lcov_path, output_directory, source_root=None, absence=None,
                 index_link=None, workers=None, build_directory=None, suite_line_totals=(),
                 coverage_by_path=None, built_at=None, scope=None, regions=None,
                 thresholds=None):
    """Write the whole HTML report: the directory index and every file's line view. Returns a ReportPages.

    Both halves need the same parse, so coverage_by_path lets a caller pass one it already has. built_at defaults to
    the provenance record's. Line views are combined-only: one set per suite would multiply the largest part of the report.
    """
    from webkitpy.coverage_lcov import PathCanonicalizer, parse_lcov, project_totals
    from webkitpy.coverage_source_view import (
        built_at_from_provenance, drop_unconfined, write_source_views)

    if coverage_by_path is None:
        canonicalizer = (PathCanonicalizer(source_root, build_directory=build_directory)
                         if source_root else None)
        coverage_by_path = parse_lcov(lcov_path, canonicalizer, keep_function_names=False)
        if canonicalizer:
            canonicalizer.log_summary()
    if not coverage_by_path:
        raise RuntimeError('{} contained no coverage records'.format(lcov_path))

    if built_at is None:
        built_at = built_at_from_provenance(output_directory)

    # Same prefix as the index, so a file's page is a sibling of the index page that links to it.
    source_prefix = effective_source_prefix(coverage_by_path, source_root)
    reportable = drop_unconfined(coverage_by_path, source_prefix)
    source_pages, source_bytes, skipped = write_source_views(
        reportable, output_directory, source_prefix, workers=workers,
        build_directory=build_directory, built_at=built_at, regions=regions)
    directory_pages = write_directory_index(
        lcov_path, output_directory, source_root=source_root, index_link=index_link,
        absence=absence, coverage_by_path=reportable, unlinkable=skipped,
        build_directory=build_directory, suite_line_totals=suite_line_totals, scope=scope,
        thresholds=thresholds, source_prefix=source_prefix)
    return ReportPages(directory_pages, source_pages, source_bytes, skipped,
                       project_totals(coverage_by_path))

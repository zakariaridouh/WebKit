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

"""The report-wide search box and the treemap, shared by the directory pages and the line views.
The index is loaded by a script tag, not fetch(): fetch() is blocked for file:// URLs.
"""

import html
import json
import os
import re
from urllib.parse import quote

SEARCH_SCRIPT_NAME = 'coverage-search.js'
SEARCH_DATA_NAME = 'coverage-files.js'

# Kinds in the index. A file with no line view is listed but not linked.
FILE = 1
UNLINKED_FILE = 0
DIRECTORY = 2

# Rows shown at once. The index is ranked, so the first rows are the ones wanted.
RESULT_LIMIT = 50

_INDEX_NAME = re.compile(r'(?:^|/)index(?:\.html)*$')


def source_page_path(relative):
    """Where the line view of relative goes, under the report root.

    A file called index would land on its directory's page, so index, index.html and so on get one more .html.
    """
    page = relative + '.html'
    return page + '.html' if _INDEX_NAME.search(relative) else page


def relative_link(path):
    """A relative URL for a '/'-separated path, one encoded segment at a time.

    The leading ./ keeps a first segment such as `javascript:x` from parsing as a scheme.
    """
    return './' + '/'.join(quote(segment, safe='') for segment in path.split('/'))


def search_box_html(up):
    """The search box for a page `up` levels below the report root ('' at the root, '../' ...)."""
    return ('<div class=report-search><input id=report-search type=search autocomplete=off '
            'spellcheck=false placeholder="Search files and directories (/)" '
            'aria-label="Search files and directories"><ol id=report-search-results hidden></ol>'
            '</div><script defer src="{0}{1}" data-root="{0}"></script>').format(up, SEARCH_SCRIPT_NAME)


def write_search_assets(output_directory, entries):
    """Write the search script and its index. entries are (path, lines, covered, kind[, page]).

    Paths are relative to the report root, and page is the line view when it is not path + '.html'. The index is
    ordered by uncovered lines, so among equal matches the one with most to test comes first.
    """
    ordered = sorted(entries, key=lambda entry: (-(entry[1] - entry[2]), entry[0]))
    payload = json.dumps([list(entry) for entry in ordered], separators=(',', ':'))
    with open(os.path.join(output_directory, SEARCH_DATA_NAME), 'w') as handle:
        handle.write('window.COVERAGE_FILES={};\n'.format(payload.replace('</', '<\\/')))
    with open(os.path.join(output_directory, SEARCH_SCRIPT_NAME), 'w') as handle:
        script = SEARCH_SCRIPT.lstrip('\n').replace('@LIMIT@', str(RESULT_LIMIT)).replace(
            '@DATA@', SEARCH_DATA_NAME)
        for placeholder, value in _SEARCH_COLORS:
            script = script.replace(placeholder, value)
        handle.write(script)


# Line views that predate the shared theme use other color names, so each color falls back to them.
_SEARCH_COLORS = (
    ('@FG@', 'var(--cov-fg,var(--text-primary,#0b0b0b))'),
    ('@SURFACE@', 'var(--cov-surface,var(--surface-1,#fcfcfb))'),
    ('@BORDER@', 'var(--cov-border,var(--gridline,#e1e0d9))'),
    ('@ACCENT@', 'var(--cov-accent,var(--link,#2a78d6))'),
    ('@MUTED@', 'var(--cov-muted,var(--text-secondary,#52514e))'),
)

# Ranked: exact file name, then name prefix, then name substring, then path substring. Ties keep index order.
SEARCH_SCRIPT = r"""
(function () {
  // entry is [path, lines, covered, kind[, page]]; page is the line view when it is not path + '.html'. Each segment
  // is encoded, and ./ keeps a first segment such as javascript:x from parsing as a scheme.
  function href(root, entry) {
    var page = entry[4] || entry[0] + (entry[3] === 2 ? '/index.html' : '.html');
    return (root || './') + page.split('/').map(encodeURIComponent).join('/');
  }

  if (typeof module !== 'undefined' && module.exports) { module.exports = {href: href}; }
  if (typeof document === 'undefined') { return; }
  var input = document.getElementById('report-search');
  var list = document.getElementById('report-search-results');
  if (!input || !list) { return; }
  var tag = document.currentScript || document.querySelector('script[data-root]');
  var root = tag ? tag.getAttribute('data-root') || '' : '';
  var limit = @LIMIT@;
  var entries = null, waiting = null, matches = [], active = -1;

  var style = document.createElement('style');
  style.textContent =
    '.report-search{position:relative;font:12px/1.4 system-ui,-apple-system,sans-serif}' +
    '.report-search input{width:100%;box-sizing:border-box;padding:5px 9px;font:inherit;' +
    'color:@FG@;background:@SURFACE@;border:1px solid @BORDER@;border-radius:6px}' +
    '.report-search ol{position:absolute;right:0;top:100%;z-index:20;margin:4px 0 0;' +
    'width:max(100%,min(560px,94vw));box-sizing:border-box;' +
    'padding:4px 0;list-style:none;max-height:60vh;overflow:auto;' +
    'background:@SURFACE@;border:1px solid @BORDER@;' +
    'border-radius:6px;box-shadow:0 6px 24px rgba(0,0,0,.18)}' +
    '.report-search li{display:flex;gap:10px;padding:3px 10px;white-space:nowrap;cursor:pointer}' +
    '.report-search li.on{background:color-mix(in oklab,@ACCENT@ 16%,transparent)}' +
    '.report-search li a{display:flex;min-width:0;flex:1 1 auto;color:@FG@;' +
    'text-decoration:none;font-family:ui-monospace,Menlo,monospace}' +
    '.report-search li .d{color:@MUTED@;min-width:0;overflow:hidden;' +
    'text-overflow:ellipsis}' +
    '.report-search li .n{flex:none}' +
    '.report-search li .p{color:@MUTED@;font-variant-numeric:tabular-nums}' +
    '.report-search li.note{cursor:default;color:@MUTED@}';
  document.head.appendChild(style);

  // Everything that asks while the index is still loading runs once it arrives.
  function load(then) {
    if (entries) { then(); return; }
    if (window.COVERAGE_FILES) { entries = window.COVERAGE_FILES; then(); return; }
    if (waiting) { waiting.push(then); return; }
    waiting = [then];
    var script = document.createElement('script');
    script.src = root + '@DATA@';
    script.onload = function () {
      entries = window.COVERAGE_FILES || [];
      var callbacks = waiting;
      waiting = null;
      callbacks.forEach(function (callback) { callback(); });
    };
    script.onerror = function () { waiting = null; note('The search index could not be loaded.'); };
    document.head.appendChild(script);
  }

  function rank(path, query) {
    var slash = path.lastIndexOf('/', path.length - 2);
    var name = path.slice(slash + 1).toLowerCase().replace(/\/$/, '');
    var bare = name.replace(/\.[^.]*$/, '');
    if (name === query || bare === query) { return 0; }
    if (name.indexOf(query) === 0) { return 1; }
    if (name.indexOf(query) !== -1) { return 2; }
    return path.toLowerCase().indexOf(query) !== -1 ? 3 : -1;
  }

  function note(text) {
    var item = document.createElement('li');
    item.className = 'note';
    item.textContent = text;
    list.replaceChildren(item);
    list.hidden = false;
  }

  function render(query) {
    var buckets = [[], [], [], []];
    for (var i = 0; i < entries.length; i++) {
      var r = rank(entries[i][0], query);
      if (r >= 0) { buckets[r].push(entries[i]); }
    }
    matches = buckets[0].concat(buckets[1], buckets[2], buckets[3]);
    var total = matches.length;
    matches = matches.slice(0, limit);
    active = matches.length ? 0 : -1;
    if (!total) { note('Nothing in this report matches.'); return; }
    var fragment = document.createDocumentFragment();
    matches.forEach(function (entry, index) {
      var item = document.createElement('li');
      var link = document.createElement('a');
      var path = entry[0], slash = path.lastIndexOf('/');
      var dim = document.createElement('span');
      dim.className = 'd';
      dim.textContent = path.slice(0, slash + 1);
      var name = document.createElement('span');
      name.className = 'n';
      name.textContent = path.slice(slash + 1) + (entry[3] === 2 ? '/' : '');
      link.appendChild(dim);
      link.appendChild(name);
      link.title = path;
      if (entry[3]) { link.href = href(root, entry); }
      else { link.title = path + ' has no line view'; }
      var percent = document.createElement('span');
      percent.className = 'p';
      percent.textContent = entry[1] ? (100 * entry[2] / entry[1]).toFixed(1) + '%' : '-';
      item.appendChild(link);
      item.appendChild(percent);
      item.addEventListener('mousemove', function () { select(index); });
      item.addEventListener('mousedown', function (event) { event.preventDefault(); open(index); });
      fragment.appendChild(item);
    });
    if (total > limit) {
      var more = document.createElement('li');
      more.className = 'note';
      more.textContent = (total - limit).toLocaleString() + ' more; keep typing to narrow it down.';
      fragment.appendChild(more);
    }
    list.replaceChildren(fragment);
    list.hidden = false;
    select(active);
  }

  function select(index) {
    var items = list.querySelectorAll('li:not(.note)');
    items.forEach(function (item, i) { item.classList.toggle('on', i === index); });
    active = index;
    if (items[index]) { items[index].scrollIntoView({block: 'nearest'}); }
  }

  function open(index) {
    var entry = matches[index];
    if (entry && entry[3]) { location.href = href(root, entry); }
  }

  function update() {
    var query = input.value.trim().toLowerCase();
    if (!query) { list.hidden = true; matches = []; return; }
    load(function () { if (input.value.trim().toLowerCase() === query) { render(query); } });
  }

  input.addEventListener('input', update);
  input.addEventListener('focus', function () { load(function () {}); if (input.value) { update(); } });
  input.addEventListener('blur', function () { list.hidden = true; });
  input.addEventListener('keydown', function (event) {
    if (event.key === 'ArrowDown' && matches.length) { select(Math.min(active + 1, matches.length - 1)); event.preventDefault(); }
    else if (event.key === 'ArrowUp' && matches.length) { select(Math.max(active - 1, 0)); event.preventDefault(); }
    else if (event.key === 'Enter') { open(active < 0 ? 0 : active); event.preventDefault(); }
    else if (event.key === 'Escape') { input.value = ''; list.hidden = true; input.blur(); }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) { return; }
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName)) { return; }
    input.focus();
    input.select();
    event.preventDefault();
  });
})();
"""

# Five equal bins of line coverage, lowest first, matching --cov-heat-1 .. --cov-heat-5.
TREEMAP_BINS = 5
TREEMAP_MAX_TILES = 1500
TREEMAP_MIN_AREA = 14


def treemap_html(tile_count, prefix=''):
    """The empty chart TREEMAP_SCRIPT fills in from the page's table, or '' under two tiles."""
    if tile_count < 2:
        return ''
    step = 100 // TREEMAP_BINS
    legend = ''.join(
        '<li><i class="sw h{0}"></i>{1}&ndash;{2}%</li>'.format(
            index + 1, index * step, (index + 1) * step)
        for index in range(TREEMAP_BINS))
    return ('<section class="chart" id="treemap" aria-labelledby="treemap-title" '
            'data-prefix="{prefix}" hidden>\n'
            '<h2 id="treemap-title">Size and coverage</h2>\n'
            '<p class="s">Each tile is a file or directory listed below. Its area is its '
            'executable lines and its color is its line coverage.{bound} Select a tile to open '
            'it.</p>\n'
            '<div class="tm-wrap"><svg class="tm" role="group" aria-label="Treemap of this '
            'directory. The table below has the same figures."></svg>'
            '<div class="tip" aria-hidden="true" hidden></div></div>\n'
            '<ul class="legend" aria-label="Line coverage color scale">{legend}</ul>\n'
            '</section>\n').format(prefix=html.escape(prefix), legend=legend,
                                   bound=' Every percentage is a lower bound.' if prefix else '')


TREEMAP_STYLE = r"""
.chart {
  margin: 0 0 18px; padding: 12px 14px 10px; background: var(--cov-surface);
  border: 1px solid var(--cov-border); border-radius: 8px;
}
.chart h2 { margin: 0; font-size: 13px; font-weight: 600; }
.chart .s { margin: 2px 0 0; font-size: 12px; color: var(--cov-muted); }
.tm-wrap { position: relative; margin: 10px 0 8px; }
.tm { display: block; width: 100%; height: auto; }
.tm .t { cursor: pointer; outline: none; }
.tm .t.rest { cursor: default; }
.tm .t rect { stroke: var(--cov-outline); stroke-width: 1px; transition: stroke .12s; }
.tm .t:hover rect, .tm .t:focus-visible rect { stroke: var(--cov-fg); stroke-width: 2px; }
.tm .t:focus-visible rect { stroke-width: 3px; }
.tm text { font: 11px/1.2 system-ui, -apple-system, "Segoe UI", sans-serif; pointer-events: none; }
.tm .nm { font-weight: 600; }
.tm .h1 rect { fill: var(--cov-heat-1); }
.tm .h2 rect { fill: var(--cov-heat-2); }
.tm .h3 rect { fill: var(--cov-heat-3); }
.tm .h4 rect { fill: var(--cov-heat-4); }
.tm .h5 rect { fill: var(--cov-heat-5); }
.tm .rest rect { fill: var(--cov-border); }
.tm .rest text { fill: var(--cov-muted); }
.tip {
  position: absolute; z-index: 5; max-width: 300px; padding: 6px 9px; pointer-events: none;
  color: var(--cov-fg); background: var(--cov-surface); font-size: 12px;
  border: 1px solid var(--cov-border); border-radius: 6px; box-shadow: 0 4px 16px rgba(0, 0, 0, .18);
}
.tip b, .tip em, .tip span { display: block; }
.tip b { font-weight: 600; overflow-wrap: anywhere; }
.tip em { font-size: 20px; font-style: normal; font-weight: 600; line-height: 1.2; }
.tip span { color: var(--cov-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.legend {
  display: flex; flex-wrap: wrap; gap: 2px 14px; margin: 0; padding: 0; list-style: none;
  font-size: 11px; color: var(--cov-muted); font-variant-numeric: tabular-nums;
}
.sw {
  display: inline-block; width: 16px; height: 10px; margin-right: 5px; vertical-align: -1px;
  border: 1px solid var(--cov-outline); border-radius: 2px;
}
.sw.h1 { background: var(--cov-heat-1); }
.sw.h2 { background: var(--cov-heat-2); }
.sw.h3 { background: var(--cov-heat-3); }
.sw.h4 { background: var(--cov-heat-4); }
.sw.h5 { background: var(--cov-heat-5); }
@media print { .tip { display: none; } .tm rect, .sw { print-color-adjust: exact; -webkit-print-color-adjust: exact; } }
@media (prefers-reduced-motion: reduce) { .tm .t rect { transition: none; } }
"""

TREEMAP_SCRIPT = r"""
(function () {
  'use strict';
  var BINS = @BINS@;
  var MAX_TILES = @MAX_TILES@;
  var MIN_AREA = @MIN_AREA@;
  var LIGHT_INK = '#ffffff';
  var DARK_INK = '#111111';

  function binOf(percent) {
    return Math.min(BINS - 1, Math.max(0, Math.floor(percent / (100 / BINS))));
  }

  function worstRatio(values, start, end, side, scale) {
    var sum = 0;
    for (var i = start; i < end; i++) { sum += values[i] * scale; }
    var largest = values[start] * scale, smallest = values[end - 1] * scale, square = side * side;
    return Math.max(square * largest / (sum * sum), sum * sum / (square * smallest));
  }

  // Squarified treemap (Bruls, Huizing, van Wijk). values are positive and sorted descending.
  function squarify(values, x, y, w, h) {
    var total = 0, i;
    for (i = 0; i < values.length; i++) { total += values[i]; }
    var rects = [];
    if (!(total > 0) || !(w > 0) || !(h > 0)) { return rects; }
    var scale = w * h / total;
    var start = 0;
    while (start < values.length) {
      var side = Math.min(w, h);
      if (!(side > 1e-9)) {
        for (i = start; i < values.length; i++) { rects[i] = {x: x, y: y, w: 0, h: 0}; }
        break;
      }
      var end = start + 1;
      var worst = worstRatio(values, start, end, side, scale);
      while (end < values.length) {
        var next = worstRatio(values, start, end + 1, side, scale);
        if (next > worst) { break; }
        worst = next;
        end++;
      }
      var sum = 0;
      for (i = start; i < end; i++) { sum += values[i] * scale; }
      var thickness = sum / side, offset = 0;
      for (i = start; i < end; i++) {
        var length = values[i] * scale / thickness;
        rects[i] = w >= h
          ? {x: x, y: y + offset, w: thickness, h: length}
          : {x: x + offset, y: y, w: length, h: thickness};
        offset += length;
      }
      if (w >= h) { x += thickness; w -= thickness; } else { y += thickness; h -= thickness; }
      start = end;
    }
    return rects;
  }

  // The tile nearest in the direction of an arrow key, preferring ones in line with this one.
  function neighbor(rects, from, key) {
    var a = rects[from], ax = a.x + a.w / 2, ay = a.y + a.h / 2;
    var best = -1, bestScore = Infinity;
    for (var i = 0; i < rects.length; i++) {
      if (i === from) { continue; }
      var dx = rects[i].x + rects[i].w / 2 - ax, dy = rects[i].y + rects[i].h / 2 - ay;
      var ahead, across;
      if (key === 'ArrowRight') { ahead = dx; across = dy; }
      else if (key === 'ArrowLeft') { ahead = -dx; across = dy; }
      else if (key === 'ArrowDown') { ahead = dy; across = dx; }
      else { ahead = -dy; across = dx; }
      if (ahead <= 0.5) { continue; }
      var score = ahead + 2 * Math.abs(across);
      if (score < bestScore) { bestScore = score; best = i; }
    }
    return best;
  }

  function parseColor(text) {
    var match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(text).trim());
    if (!match) { return null; }
    var hex = match[1];
    if (hex.length === 3) { hex = hex.replace(/./g, '$&$&'); }
    return [parseInt(hex.substr(0, 2), 16), parseInt(hex.substr(2, 2), 16), parseInt(hex.substr(4, 2), 16)];
  }

  function luminance(rgb) {
    var c = rgb.map(function (v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }

  // White or near-black, whichever reads better on this fill.
  function inkFor(color) {
    var rgb = parseColor(color);
    if (!rgb) { return DARK_INK; }
    var l = luminance(rgb);
    return 1.05 / (l + 0.05) > (l + 0.05) / (luminance(parseColor(DARK_INK)) + 0.05) ? LIGHT_INK : DARK_INK;
  }

  // Largest first. Items too small to see in this area become one tile, so the area stays honest.
  function arrange(items, area) {
    var sorted = items.slice().sort(function (a, b) {
      return b.lines - a.lines || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
    });
    var total = 0;
    sorted.forEach(function (item) { total += item.lines; });
    var keep = Math.min(sorted.length, MAX_TILES);
    while (keep > 0 && area * sorted[keep - 1].lines / total < MIN_AREA) { keep--; }
    var rest = sorted.slice(keep);
    if (rest.length < 2) { return sorted; }
    var lines = 0, covered = 0;
    rest.forEach(function (item) { lines += item.lines; covered += item.covered; });
    var tiles = sorted.slice(0, keep), at = keep;
    while (at > 0 && tiles[at - 1].lines < lines) { at--; }
    tiles.splice(at, 0, {name: rest.length.toLocaleString() + ' smaller items', href: null,
                         directory: false, rest: true, lines: lines, covered: covered});
    return tiles;
  }

  function describe(tile, prefix) {
    var percent = prefix + (100 * tile.covered / tile.lines).toFixed(1) + '%';
    var detail = tile.covered.toLocaleString() + ' of ' + tile.lines.toLocaleString() + ' lines';
    var name = tile.directory ? tile.name + '/' : tile.name;
    return {name: name, percent: percent, detail: detail,
            label: name + ', ' + percent + ' covered, ' + detail};
  }

  function fit(text, chars) {
    if (text.length <= chars) { return text; }
    return chars < 3 ? '' : text.slice(0, chars - 1) + '\u2026';
  }

  function collect(table) {
    var items = [];
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      var lines = row.querySelector('.c-lines'), uncovered = row.querySelector('.c-unc');
      if (row.classList.contains('totals') || !lines || !uncovered) { return; }
      var count = parseFloat(lines.dataset.v);
      if (!(count > 0)) { return; }
      var cell = row.cells[0], link = cell.querySelector('a');
      items.push({name: cell.dataset.v || cell.textContent, href: link ? link.getAttribute('href') : null,
                  directory: cell.classList.contains('dir'), lines: count,
                  covered: count - parseFloat(uncovered.dataset.v)});
    });
    return items;
  }

  function init() {
    var section = document.getElementById('treemap');
    var table = document.getElementById('entries');
    if (!section || !table) { return; }
    var wrap = section.querySelector('.tm-wrap'), svg = section.querySelector('svg');
    var tip = section.querySelector('.tip');
    var prefix = section.getAttribute('data-prefix') || '';
    var items = collect(table);
    if (items.length < 2) { return; }
    section.hidden = false;
    var tiles = [], rects = [], nodes = [], active = 0, drawnWidth = 0;

    function make(name, attributes) {
      var node = document.createElementNS(svg.namespaceURI, name);
      for (var key in attributes) { node.setAttribute(key, attributes[key]); }
      return node;
    }

    function draw() {
      var width = Math.max(280, Math.floor(wrap.clientWidth));
      var height = Math.max(200, Math.min(400, Math.round(width * 0.36)));
      drawnWidth = width;
      tiles = arrange(items, width * height);
      active = Math.min(active, tiles.length - 1);
      rects = squarify(tiles.map(function (tile) { return tile.lines; }), 0, 0, width, height);
      var style = getComputedStyle(document.documentElement);
      var inks = [];
      for (var b = 1; b <= BINS; b++) { inks.push(inkFor(style.getPropertyValue('--cov-heat-' + b))); }
      svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
      svg.setAttribute('width', width);
      svg.setAttribute('height', height);
      while (svg.firstChild) { svg.removeChild(svg.firstChild); }
      nodes = tiles.map(function (tile, index) {
        var r = rects[index], info = describe(tile, prefix);
        var bin = binOf(100 * tile.covered / tile.lines);
        var a = make('a', {'class': 't ' + (tile.rest ? 'rest' : 'h' + (bin + 1)),
                           'tabindex': index === active ? '0' : '-1', 'aria-label': info.label});
        if (tile.href) { a.setAttribute('href', tile.href); } else { a.setAttribute('role', 'img'); }
        a.appendChild(make('rect', {x: r.x + 1, y: r.y + 1, width: Math.max(0, r.w - 2),
                                    height: Math.max(0, r.h - 2), rx: 2}));
        var room = r.w - 2 - 12;
        if (room >= 36 && r.h >= 24) {
          var name = make('text', {'class': 'nm', x: r.x + 7, y: r.y + 17});
          name.textContent = fit(info.name, Math.floor(room / 6.4));
          a.appendChild(name);
          if (r.h >= 40) {
            var percent = make('text', {x: r.x + 7, y: r.y + 31});
            percent.textContent = info.percent;
            a.appendChild(percent);
          }
          if (!tile.rest) { a.setAttribute('style', 'fill:' + inks[bin]); }
        }
        svg.appendChild(a);
        return a;
      });
    }

    function showTip(index, x, y) {
      var info = describe(tiles[index], prefix);
      var parts = [['b', info.name], ['em', info.percent], ['span', info.detail]];
      tip.textContent = '';
      parts.forEach(function (part) {
        var line = document.createElement(part[0]);
        line.textContent = part[1];
        tip.appendChild(line);
      });
      tip.hidden = false;
      var left = Math.min(Math.max(0, x + 14), Math.max(0, wrap.clientWidth - tip.offsetWidth));
      var top = y + 18 + tip.offsetHeight > wrap.clientHeight ? y - tip.offsetHeight - 10 : y + 18;
      tip.style.left = left + 'px';
      tip.style.top = Math.max(0, top) + 'px';
    }

    function hideTip() { tip.hidden = true; }

    function indexOf(event) {
      var node = event.target.closest ? event.target.closest('.t') : null;
      return node ? nodes.indexOf(node) : -1;
    }

    function focusTile(index) {
      nodes[active].setAttribute('tabindex', '-1');
      active = index;
      nodes[active].setAttribute('tabindex', '0');
      nodes[active].focus();
    }

    svg.addEventListener('pointermove', function (event) {
      var index = indexOf(event);
      if (index < 0) { hideTip(); return; }
      var box = wrap.getBoundingClientRect();
      showTip(index, event.clientX - box.left, event.clientY - box.top);
    });
    svg.addEventListener('pointerleave', hideTip);
    svg.addEventListener('focusin', function (event) {
      var index = indexOf(event);
      if (index < 0) { return; }
      active = index;
      var r = rects[index];
      showTip(index, r.x + 6, r.y + r.h - 24);
    });
    svg.addEventListener('focusout', hideTip);
    svg.addEventListener('keydown', function (event) {
      var index = indexOf(event), next = -1;
      if (index < 0) { return; }
      if (/^Arrow/.test(event.key)) { next = neighbor(rects, index, event.key); }
      else if (event.key === 'Home') { next = 0; }
      else if (event.key === 'End') { next = tiles.length - 1; }
      else if (event.key === 'Escape') { hideTip(); return; }
      else if (event.key === 'Enter' && tiles[index].href) { location.href = tiles[index].href; return; }
      else { return; }
      event.preventDefault();
      if (next >= 0) { focusTile(next); }
    });

    var pending = false;
    function redraw(force) {
      if (pending || (!force && Math.floor(wrap.clientWidth) === drawnWidth)) { return; }
      pending = true;
      requestAnimationFrame(function () { pending = false; draw(); });
    }
    draw();
    if (typeof ResizeObserver === 'function') { new ResizeObserver(function () { redraw(false); }).observe(wrap); }
    else { window.addEventListener('resize', function () { redraw(false); }); }
    document.addEventListener('covthemechange', function () { redraw(true); });
    if (window.matchMedia) {
      var scheme = window.matchMedia('(prefers-color-scheme: dark)');
      if (scheme.addEventListener) { scheme.addEventListener('change', function () { redraw(true); }); }
    }
  }

  if (typeof document !== 'undefined') { init(); }
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {binOf: binOf, squarify: squarify, neighbor: neighbor, inkFor: inkFor,
                      arrange: arrange, describe: describe, fit: fit};
  }
})();
""".replace('@BINS@', str(TREEMAP_BINS)).replace('@MAX_TILES@', str(TREEMAP_MAX_TILES)).replace(
    '@MIN_AREA@', str(TREEMAP_MIN_AREA))

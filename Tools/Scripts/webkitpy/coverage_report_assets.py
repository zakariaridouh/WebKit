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

"""The report-wide search box, shared by the directory pages and the line views.

Every page carries the same box. The index of files and directories it searches is written
once, to SEARCH_DATA_NAME at the report root, and loaded by a script tag the first time the box
is used, so pages stay small and the index is cached. A script tag rather than fetch(), because
fetch() is blocked for file:// URLs and the report is usually opened from disk.
"""

import json
import os

SEARCH_SCRIPT_NAME = 'coverage-search.js'
SEARCH_DATA_NAME = 'coverage-files.js'

# Kinds in the index. A file with no line view is listed but not linked.
FILE = 1
UNLINKED_FILE = 0
DIRECTORY = 2

# Rows shown at once. The index is ranked, so the first rows are the ones wanted.
RESULT_LIMIT = 50


def search_box_html(up):
    """The search box for a page `up` levels below the report root ('' at the root, '../' ...)."""
    return ('<div class=report-search><input id=report-search type=search autocomplete=off '
            'spellcheck=false placeholder="Search files and directories (/)" '
            'aria-label="Search files and directories"><ol id=report-search-results hidden></ol>'
            '</div><script defer src="{0}{1}" data-root="{0}"></script>').format(up, SEARCH_SCRIPT_NAME)


def write_search_assets(output_directory, entries):
    """Write the search script and its index. entries are (path, lines, covered, kind).

    Paths are relative to the report root: a file's page is path + '.html' and a directory's
    is path + '/index.html'. Ordered by uncovered lines, so that among equally good matches
    the one with the most to test comes first.
    """
    ordered = sorted(entries, key=lambda entry: (-(entry[1] - entry[2]), entry[0]))
    payload = json.dumps([list(entry) for entry in ordered], separators=(',', ':'))
    with open(os.path.join(output_directory, SEARCH_DATA_NAME), 'w') as handle:
        handle.write('window.COVERAGE_FILES={};\n'.format(payload.replace('</', '<\\/')))
    with open(os.path.join(output_directory, SEARCH_SCRIPT_NAME), 'w') as handle:
        handle.write(SEARCH_SCRIPT.lstrip('\n').replace('@LIMIT@', str(RESULT_LIMIT)).replace(
            '@DATA@', SEARCH_DATA_NAME))


# Ranked, not filtered in index order: an exact file name first, then a name that starts with
# the query, then a name that contains it, then a match anywhere in the path. Within a rank the
# index order holds, which is most uncovered lines first.
SEARCH_SCRIPT = r"""
(function () {
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
    'color:var(--text-primary,#0b0b0b);background:var(--surface-1,#fcfcfb);' +
    'border:1px solid var(--gridline,#e1e0d9);border-radius:6px}' +
    '.report-search ol{position:absolute;right:0;top:100%;z-index:20;margin:4px 0 0;' +
    'width:max(100%,min(560px,94vw));box-sizing:border-box;' +
    'padding:4px 0;list-style:none;max-height:60vh;overflow:auto;' +
    'background:var(--surface-1,#fcfcfb);border:1px solid var(--gridline,#e1e0d9);' +
    'border-radius:6px;box-shadow:0 6px 24px rgba(0,0,0,.18)}' +
    '.report-search li{display:flex;gap:10px;padding:3px 10px;white-space:nowrap;cursor:pointer}' +
    '.report-search li.on{background:color-mix(in oklab,var(--link,#2a78d6) 16%,transparent)}' +
    '.report-search li a{display:flex;min-width:0;flex:1 1 auto;color:var(--text-primary,#0b0b0b);' +
    'text-decoration:none;font-family:ui-monospace,Menlo,monospace}' +
    '.report-search li .d{color:var(--text-secondary,#52514e);min-width:0;overflow:hidden;' +
    'text-overflow:ellipsis}' +
    '.report-search li .n{flex:none}' +
    '.report-search li .p{color:var(--text-secondary,#52514e);font-variant-numeric:tabular-nums}' +
    '.report-search li.note{cursor:default;color:var(--text-secondary,#52514e)}';
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

  function href(entry) {
    return root + entry[0] + (entry[3] === 2 ? '/index.html' : '.html');
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
      if (entry[3]) { link.href = href(entry); }
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
    if (entry && entry[3]) { location.href = href(entry); }
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

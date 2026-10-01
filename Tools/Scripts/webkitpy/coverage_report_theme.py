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


"""The color theme shared by every page of the HTML coverage report."""

import os

THEME_STORAGE_KEY = 'cov-theme'
THEME_SCRIPT_NAME = 'coverage-theme.js'

# (name, light, dark). Tokens that can carry text clear WCAG AA on --cov-bg and --cov-surface.
# --cov-outline edges a fill that is too close to the page to read as a shape, and clears 3:1 on both.
# Covered is blue and missed is vermilion, never red against green. The heat ramp runs low to high.
THEME_TOKENS = (
    ('--cov-bg', '#f6f5f2', '#0d0d0c'),
    ('--cov-surface', '#fcfcfb', '#1a1a19'),
    ('--cov-fg', '#14130f', '#f0efec'),
    ('--cov-muted', '#5c5a54', '#aaa89f'),
    ('--cov-border', '#d8d6cd', '#35342f'),
    ('--cov-outline', '#76746b', '#8b897f'),
    ('--cov-accent', '#1d5fb0', '#6da7ec'),
    ('--cov-covered', '#1b6ac9', '#4a96f0'),
    ('--cov-partial', '#8a6a00', '#e3b93a'),
    ('--cov-uncovered', '#a3180e', '#f0643c'),
    ('--cov-unexecuted', '#8b2f9e', '#ee8fd8'),
    ('--cov-heat-1', '#a3180e', '#f0643c'),
    ('--cov-heat-2', '#e8803f', '#a9593a'),
    ('--cov-heat-3', '#d9c978', '#3d3b34'),
    ('--cov-heat-4', '#7fb3d0', '#3f7194'),
    ('--cov-heat-5', '#1f5f9e', '#62a8ee'),
)


def _declarations(scheme, index, indent):
    lines = ['color-scheme: ' + scheme + ';']
    lines += ['{}: {};'.format(token[0], token[index]) for token in THEME_TOKENS]
    return ''.join('{}{}\n'.format(indent, line) for line in lines)


def _build_theme_css():
    light, dark = 1, 2
    return (
        ':root {\n' + _declarations('light', light, '  ') + '}\n'
        '@media (prefers-color-scheme: dark) {\n'
        '  :root:where(:not([data-theme="light"])) {\n' + _declarations('dark', dark, '    ') +
        '  }\n}\n'
        ':root[data-theme="dark"] {\n' + _declarations('dark', dark, '  ') + '}\n'
        '@media print {\n'
        '  :root, :root[data-theme] {\n' + _declarations('light', light, '    ') + '  }\n}\n'
        '.cov-theme {\n'
        '  display: none; overflow: hidden; background: var(--cov-surface);\n'
        '  border: 1px solid var(--cov-border); border-radius: 6px;\n'
        '}\n'
        '.cov-js .cov-theme { display: inline-flex; }\n'
        '.cov-theme button {\n'
        '  font: 12px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 5px 9px;\n'
        '  color: var(--cov-muted); background: transparent; border: 0; cursor: pointer;\n'
        '}\n'
        '.cov-theme button + button { border-left: 1px solid var(--cov-border); }\n'
        '.cov-theme button[aria-pressed="true"] {\n'
        '  color: var(--cov-fg); font-weight: 600; text-decoration: underline;\n'
        '  background: color-mix(in oklab, var(--cov-accent) 16%, var(--cov-surface));\n'
        '}\n'
        '.cov-theme button:focus-visible { outline: 2px solid var(--cov-accent); outline-offset: -2px; }\n'
        '@media print { .cov-js .cov-theme { display: none; } }\n')


THEME_CSS = _build_theme_css()

THEME_TOGGLE_HTML = (
    '<div class="cov-theme" role="group" aria-label="Color theme">'
    '<button type="button" data-theme-set="auto" aria-pressed="true">Auto</button>'
    '<button type="button" data-theme-set="light" aria-pressed="false">Light</button>'
    '<button type="button" data-theme-set="dark" aria-pressed="false">Dark</button></div>')

# Runs from <head> so a saved choice applies before first paint. Storage can throw on a file://
# page, and the toggle then still works until the page is closed.
THEME_TOGGLE_JS = """
(function () {
  'use strict';
  var KEY = '@KEY@';
  var root = document.documentElement;
  root.classList.add('cov-js');
  var choice = 'auto';
  try { choice = localStorage.getItem(KEY) || 'auto'; } catch (error) {}
  if (choice !== 'light' && choice !== 'dark') { choice = 'auto'; }
  if (choice !== 'auto') { root.setAttribute('data-theme', choice); }

  function apply() {
    if (choice === 'auto') { root.removeAttribute('data-theme'); }
    else { root.setAttribute('data-theme', choice); }
    var buttons = document.querySelectorAll('[data-theme-set]');
    for (var i = 0; i < buttons.length; i++) {
      var on = buttons[i].getAttribute('data-theme-set') === choice;
      buttons[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    document.dispatchEvent(new Event('covthemechange'));
  }

  function choose(next) {
    choice = next;
    try {
      if (next === 'auto') { localStorage.removeItem(KEY); }
      else { localStorage.setItem(KEY, next); }
    } catch (error) {}
    apply();
  }

  function bind() {
    var buttons = document.querySelectorAll('[data-theme-set]');
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener('click', function () { choose(button.getAttribute('data-theme-set')); });
    });
    apply();
  }

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', bind); }
  else { bind(); }
})();
""".replace('@KEY@', THEME_STORAGE_KEY).lstrip('\n')


def write_theme_script(output_directory):
    """Write THEME_SCRIPT_NAME at the report root. Every page loads it from <head>."""
    with open(os.path.join(output_directory, THEME_SCRIPT_NAME), 'w', encoding='utf-8') as handle:
        handle.write(THEME_TOGGLE_JS)

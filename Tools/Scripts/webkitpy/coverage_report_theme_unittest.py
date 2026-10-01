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


import math
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from webkitpy.coverage_report_theme import (
    THEME_CSS, THEME_SCRIPT_NAME, THEME_STORAGE_KEY, THEME_TOGGLE_HTML, THEME_TOGGLE_JS,
    write_theme_script)

CONTRACT = (
    '--cov-bg', '--cov-surface', '--cov-fg', '--cov-muted', '--cov-border', '--cov-outline',
    '--cov-accent',
    '--cov-covered', '--cov-partial', '--cov-uncovered', '--cov-unexecuted',
    '--cov-heat-1', '--cov-heat-2', '--cov-heat-3', '--cov-heat-4', '--cov-heat-5')
TEXT_TOKENS = ('--cov-fg', '--cov-muted', '--cov-accent', '--cov-covered', '--cov-partial',
               '--cov-uncovered', '--cov-unexecuted')
HEAT = tuple('--cov-heat-{}'.format(step) for step in range(1, 6))


def node_binary():
    return shutil.which('node') or ('/opt/homebrew/bin/node' if os.path.exists('/opt/homebrew/bin/node') else None)


def _declarations(block):
    return dict(re.findall(r'(--[\w-]+):\s*([^;]+);', block))


def _scopes():
    """{'light', 'dark-media', 'dark-attribute', 'print'} -> {token: value}."""
    light = re.search(r'^:root \{(.*?)^\}', THEME_CSS, re.S | re.M).group(1)
    media = re.search(r'prefers-color-scheme: dark\) \{\s*:root:where\(:not\(\[data-theme="light"\]\)\) \{(.*?)\}',
                      THEME_CSS, re.S).group(1)
    attribute = re.search(r'^:root\[data-theme="dark"\] \{(.*?)^\}', THEME_CSS, re.S | re.M).group(1)
    printing = re.search(r'@media print \{\s*:root, :root\[data-theme\] \{(.*?)\}', THEME_CSS, re.S).group(1)
    return {'light': _declarations(light), 'dark-media': _declarations(media),
            'dark-attribute': _declarations(attribute), 'print': _declarations(printing)}


def _channel(value):
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _linear(color):
    return [_channel(int(color[index:index + 2], 16) / 255.0) for index in (1, 3, 5)]


def contrast(first, second):
    def luminance(color):
        red, green, blue = _linear(color)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _oklab(color):
    r, g, b = _linear(color)
    long_cone = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * long_cone + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * long_cone - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * long_cone + 0.7827717662 * m - 0.8086757660 * s)


def delta_e(first, second):
    return 100 * math.dist(_oklab(first), _oklab(second))


def hue(color):
    _, a, b = _oklab(color)
    return math.degrees(math.atan2(b, a)) % 360


class ThemeTokensTest(unittest.TestCase):
    def test_every_scope_defines_exactly_the_contract(self):
        for name, values in _scopes().items():
            self.assertEqual(sorted(values), sorted(CONTRACT), name)

    def test_the_dark_scopes_agree_and_differ_from_light(self):
        scopes = _scopes()
        self.assertEqual(scopes['dark-media'], scopes['dark-attribute'])
        self.assertEqual(scopes['print'], scopes['light'])
        for token in CONTRACT:
            self.assertNotEqual(scopes['light'][token], scopes['dark-media'][token], token)

    def test_the_attribute_overrides_the_os_setting_both_ways(self):
        # A light stamp must beat an OS that prefers dark, so the media block steps aside for it.
        self.assertIn(':root:where(:not([data-theme="light"]))', THEME_CSS)
        self.assertIn(':root[data-theme="dark"]', THEME_CSS)
        self.assertLess(THEME_CSS.index(':root[data-theme="dark"]'), THEME_CSS.index('@media print'))

    def test_every_value_is_a_six_digit_hex_color(self):
        for name, values in _scopes().items():
            for token, value in values.items():
                self.assertRegex(value, r'^#[0-9a-f]{6}$', '{} {}'.format(name, token))

    def test_text_clears_aa_on_the_page_and_on_a_card_in_both_modes(self):
        scopes = _scopes()
        for mode in ('light', 'dark-attribute'):
            values = scopes[mode]
            for token in TEXT_TOKENS:
                for ground in ('--cov-bg', '--cov-surface'):
                    self.assertGreaterEqual(contrast(values[token], values[ground]), 4.5,
                                            '{} {} on {}'.format(mode, token, ground))

    def test_the_outline_is_visible_against_the_page_and_a_card_in_both_modes(self):
        # Non-text contrast (WCAG 1.4.11): tiles, swatches and bars are edged with it.
        scopes = _scopes()
        for mode in ('light', 'dark-attribute', 'dark-media'):
            values = scopes[mode]
            for ground in ('--cov-bg', '--cov-surface'):
                self.assertGreaterEqual(contrast(values['--cov-outline'], values[ground]), 3.0,
                                        '{} outline on {}'.format(mode, ground))

    def test_the_state_colors_are_told_apart_by_someone_with_full_color_vision(self):
        states = ('--cov-covered', '--cov-partial', '--cov-uncovered', '--cov-unexecuted')
        for mode in ('light', 'dark-attribute'):
            values = _scopes()[mode]
            for index, first in enumerate(states):
                for second in states[index + 1:]:
                    self.assertGreaterEqual(delta_e(values[first], values[second]), 15,
                                            '{} {} {}'.format(mode, first, second))

    def test_there_is_no_green_and_no_red_against_green(self):
        # OKLCH hue 110 to 170 is the green band. Covered is blue and what was missed is vermilion.
        for mode in ('light', 'dark-attribute'):
            for token, value in _scopes()[mode].items():
                self.assertFalse(110 <= hue(value) <= 170 and delta_e(value, '#808080') > 8,
                                 '{} {}'.format(mode, token))

    def test_the_heat_ramp_steps_are_visibly_different(self):
        for mode in ('light', 'dark-attribute'):
            values = _scopes()[mode]
            for first, second in zip(HEAT, HEAT[1:]):
                self.assertGreaterEqual(delta_e(values[first], values[second]), 12,
                                        '{} {} {}'.format(mode, first, second))

    def test_a_label_in_white_or_near_black_is_readable_on_every_step(self):
        for mode in ('light', 'dark-attribute'):
            for token in HEAT:
                fill = _scopes()[mode][token]
                self.assertGreaterEqual(max(contrast(fill, '#ffffff'), contrast(fill, '#111111')), 4.5,
                                        '{} {}'.format(mode, token))

    def test_the_ramp_runs_from_warm_at_the_bottom_to_cool_at_the_top(self):
        # Low coverage is warm and high is cool, in both modes.
        for mode in ('light', 'dark-attribute'):
            values = _scopes()[mode]
            low, high = _oklab(values['--cov-heat-1']), _oklab(values['--cov-heat-5'])
            self.assertGreater(low[1], 0)       # toward red
            self.assertLess(high[2], 0)         # toward blue


class ThemeToggleTest(unittest.TestCase):
    def test_the_script_is_written_to_the_report_root_under_its_name(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        write_theme_script(directory)
        self.assertEqual(os.listdir(directory), [THEME_SCRIPT_NAME])
        with open(os.path.join(directory, THEME_SCRIPT_NAME), encoding='utf-8') as handle:
            self.assertEqual(handle.read(), THEME_TOGGLE_JS)

    def test_the_markup_offers_auto_light_and_dark(self):
        self.assertEqual(re.findall(r'data-theme-set="(\w+)"', THEME_TOGGLE_HTML),
                         ['auto', 'light', 'dark'])
        self.assertEqual(THEME_TOGGLE_HTML.count('<button type="button"'), 3)
        self.assertIn('aria-label="Color theme"', THEME_TOGGLE_HTML)
        self.assertEqual(THEME_TOGGLE_HTML.count('aria-pressed="true"'), 1)

    def test_the_script_and_the_markup_share_their_hooks(self):
        self.assertIn('data-theme-set', THEME_TOGGLE_JS)
        self.assertIn(THEME_STORAGE_KEY, THEME_TOGGLE_JS)
        self.assertGreaterEqual(THEME_TOGGLE_JS.count('try {'), 2)

    def test_the_stylesheet_styles_the_toggle_and_hides_it_in_print(self):
        self.assertIn('.cov-theme button[aria-pressed="true"]', THEME_CSS)
        self.assertRegex(THEME_CSS, r'@media print \{ \.cov-js \.cov-theme \{ display: none; \} \}')

    def test_the_toggle_is_shown_only_when_the_script_that_drives_it_has_run(self):
        self.assertRegex(THEME_CSS, r'\.cov-theme \{\s*display: none;')
        self.assertIn('.cov-js .cov-theme { display: inline-flex; }', THEME_CSS)
        self.assertIn("classList.add('cov-js')", THEME_TOGGLE_JS)


@unittest.skipUnless(node_binary(), 'node is not installed')
class ThemeToggleBehaviorTest(unittest.TestCase):
    HARNESS = r"""
const vm = require('vm');
const assert = require('assert');
const source = require('fs').readFileSync(process.argv[2], 'utf8');
function button(value) {
  return {attributes: {'data-theme-set': value, 'aria-pressed': 'false'}, click: null,
          getAttribute(name) { return this.attributes[name]; },
          setAttribute(name, text) { this.attributes[name] = text; },
          addEventListener(type, handler) { this.click = handler; }};
}
function load(storage, readyState) {
  const buttons = [button('auto'), button('light'), button('dark')];
  const root = {attributes: {}, classes: [], classList: {add(name) { root.classes.push(name); }},
                setAttribute(name, text) { this.attributes[name] = text; },
                removeAttribute(name) { delete this.attributes[name]; }};
  let events = 0, deferred = null;
  const document = {documentElement: root, readyState: readyState || 'complete',
                    querySelectorAll() { return buttons; }, dispatchEvent() { events++; },
                    addEventListener(type, handler) { deferred = handler; }};
  vm.runInNewContext(source, {document, localStorage: storage, Event: function () {}});
  return {buttons, root, events() { return events; }, finish() { deferred(); }};
}
function pressed(page) { return page.buttons.filter(b => b.attributes['aria-pressed'] === 'true').map(b => b.attributes['data-theme-set']); }
function memory(initial) {
  const data = initial ? {'@KEY@': initial} : {};
  return {data, getItem: k => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = v; }, removeItem: k => { delete data[k]; }};
}

let storage = memory('dark');
let page = load(storage);
assert.strictEqual(page.root.attributes['data-theme'], 'dark');
assert.deepStrictEqual(page.root.classes, ['cov-js']);
assert.deepStrictEqual(pressed(page), ['dark']);
page.buttons[1].click();
assert.strictEqual(page.root.attributes['data-theme'], 'light');
assert.strictEqual(storage.data['@KEY@'], 'light');
page.buttons[0].click();
assert.ok(!('data-theme' in page.root.attributes));
assert.ok(!('@KEY@' in storage.data));
assert.deepStrictEqual(pressed(page), ['auto']);
assert.ok(page.events() >= 3);

storage = memory('nonsense');
page = load(storage);
assert.ok(!('data-theme' in page.root.attributes));

const broken = {getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); },
                removeItem() { throw new Error('denied'); }};
page = load(broken);
page.buttons[2].click();
assert.strictEqual(page.root.attributes['data-theme'], 'dark');
assert.deepStrictEqual(pressed(page), ['dark']);

// Run from <head>: the saved choice is applied before the buttons exist.
storage = memory('light');
page = load(storage, 'loading');
assert.strictEqual(page.root.attributes['data-theme'], 'light');
page.finish();
assert.deepStrictEqual(pressed(page), ['light']);
console.log('ok');
""".replace('@KEY@', THEME_STORAGE_KEY)

    def test_the_toggle_remembers_the_choice_and_survives_broken_storage(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        script = os.path.join(directory, 'theme.js')
        with open(script, 'w') as handle:
            handle.write(THEME_TOGGLE_JS)
        harness = os.path.join(directory, 'harness.js')
        with open(harness, 'w') as handle:
            handle.write(self.HARNESS)
        result = subprocess.run([node_binary(), harness, script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ok')


if __name__ == '__main__':
    unittest.main()

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

"""Canonicalize copied header paths in lcov coverage data.

WebKit copies headers into the build directory and other projects include them from
there, so one header is attributed to two different paths depending on which translation
unit included it: WTF's own TUs see Source/WTF/wtf/Vector.h, while WebCore's see
<build>/usr/local/include/wtf/Vector.h. The two entries are not duplicates -- different
TUs instantiate different templates, so Vector.h is 767 lines under one path and 1,272
under the other, and some headers appear only under the copy.

That means neither summing the two (double-counts the shared lines) nor dropping either
one (loses the lines unique to it) is right. The correct operation is a per-line union,
taking the highest execution count seen for each line, which is what this module does.
"""

import logging
import os
import re
from collections import defaultdict

logger = logging.getLogger(__name__)

# Installed-header locations whose original source directory is derivable from the path.
_INSTALLED_HEADER_RULES = (
    ('/usr/local/include/wtf/', 'Source/WTF/wtf/'),
    ('/usr/local/include/bmalloc/', 'Source/bmalloc/bmalloc/'),
    ('/usr/local/include/pas/', 'Source/bmalloc/libpas/src/libpas/'),
)

# Copied framework headers, where the original subdirectory is NOT derivable from the path
# (WebCore's headers come from a hundred different subdirectories), so these are resolved
# by unique basename within the framework's source tree.
_FRAMEWORK_HEADER_PATTERN = re.compile(
    r'/(?P<framework>[A-Za-z]+)\.framework/(?:Versions/[^/]+/)?(?:PrivateHeaders|Headers)/(?P<rest>.+)$')

_FRAMEWORK_SOURCE_DIRECTORY = {
    'WebCore': 'Source/WebCore',
    'JavaScriptCore': 'Source/JavaScriptCore',
    'WebKit': 'Source/WebKit',
    'WebKitLegacy': 'Source/WebKitLegacy',
}


def _build_basename_index(checkout_root, framework):
    """basename -> relative source path, for basenames that are unique in the framework."""
    directory = os.path.join(checkout_root, _FRAMEWORK_SOURCE_DIRECTORY[framework])
    if not os.path.isdir(directory):
        return {}
    seen = defaultdict(list)
    for current, directories, files in os.walk(directory):
        directories[:] = [d for d in directories if d not in ('.git', 'DerivedSources')]
        for name in files:
            if name.endswith(('.h', '.hpp')):
                seen[name].append(os.path.relpath(os.path.join(current, name), checkout_root))
    return {name: paths[0] for name, paths in seen.items() if len(paths) == 1}


class PathCanonicalizer:
    def __init__(self, checkout_root):
        self._checkout_root = checkout_root.rstrip('/')
        self._indices = {}
        self.installed_header_count = 0
        self.framework_header_count = 0
        self.unresolved_framework_headers = set()

    def _framework_index(self, framework):
        if framework not in self._indices:
            self._indices[framework] = _build_basename_index(self._checkout_root, framework)
        return self._indices[framework]

    def canonicalize(self, path):
        """Map a copied-header path back to its source path, or return it unchanged."""
        for needle, replacement in _INSTALLED_HEADER_RULES:
            index = path.find(needle)
            if index != -1:
                self.installed_header_count += 1
                return os.path.join(self._checkout_root, replacement + path[index + len(needle):])

        match = _FRAMEWORK_HEADER_PATTERN.search(path)
        if match and match.group('framework') in _FRAMEWORK_SOURCE_DIRECTORY:
            framework = match.group('framework')
            basename = os.path.basename(match.group('rest'))
            resolved = self._framework_index(framework).get(basename)
            if resolved:
                self.framework_header_count += 1
                return os.path.join(self._checkout_root, resolved)
            self.unresolved_framework_headers.add(path)
        return path

    def log_summary(self):
        logger.info('Canonicalized %d installed-header and %d copied framework-header paths '
                    'back to their source locations',
                    self.installed_header_count, self.framework_header_count)
        if self.unresolved_framework_headers:
            logger.info('%d copied framework headers had no unique source match and were left as-is',
                        len(self.unresolved_framework_headers))


class FileCoverage:
    """Per-line, per-function and per-branch hit counts for one source file."""
    __slots__ = ('lines', 'functions', 'branches')

    def __init__(self):
        self.lines = {}      # line number -> execution count
        self.functions = {}  # mangled name -> execution count
        self.branches = {}   # (line, block, branch) -> taken count

    def merge(self, other):
        # A line covered by any translation unit is covered, so take the maximum. Summing
        # would inflate the counts of every header shared between two frameworks, because
        # llvm-profdata has already merged those counters by function name.
        for line, count in other.lines.items():
            if count > self.lines.get(line, -1):
                self.lines[line] = count
        for name, count in other.functions.items():
            if count > self.functions.get(name, -1):
                self.functions[name] = count
        for key, count in other.branches.items():
            if count > self.branches.get(key, -1):
                self.branches[key] = count

    def totals(self):
        return {
            'lines': (len(self.lines), sum(1 for c in self.lines.values() if c)),
            'functions': (len(self.functions), sum(1 for c in self.functions.values() if c)),
            'branches': (len(self.branches), sum(1 for c in self.branches.values() if c)),
        }


def parse_lcov(lcov_path, canonicalizer=None):
    """Parse an lcov trace into {canonical path: FileCoverage}, unioning duplicates."""
    files = {}
    current = None
    with open(lcov_path) as handle:
        for line in handle:
            line = line.rstrip('\n')
            if line.startswith('SF:'):
                path = line[3:]
                if canonicalizer:
                    path = canonicalizer.canonicalize(path)
                current = (path, FileCoverage())
            elif current is None:
                continue
            elif line.startswith('DA:'):
                number, _, count = line[3:].partition(',')
                try:
                    current[1].lines[int(number)] = int(count)
                except ValueError:
                    pass
            elif line.startswith('FNDA:'):
                count, _, name = line[5:].partition(',')
                try:
                    current[1].functions[name] = int(count)
                except ValueError:
                    pass
            elif line.startswith('FN:'):
                _, _, name = line[3:].partition(',')
                current[1].functions.setdefault(name, 0)
            elif line.startswith('BRDA:'):
                parts = line[5:].split(',')
                if len(parts) == 4:
                    taken = 0 if parts[3] == '-' else int(parts[3] or 0)
                    current[1].branches[(parts[0], parts[1], parts[2])] = taken
            elif line == 'end_of_record':
                path, coverage = current
                if path in files:
                    files[path].merge(coverage)
                else:
                    files[path] = coverage
                current = None
    return files

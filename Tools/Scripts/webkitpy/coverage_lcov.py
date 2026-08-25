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

It also owns the question of which copied-header locations are WebKit's own and which are a
third-party project's, because that is the same knowledge seen from the other side, and it
owns how a trace is opened, because a WebKit-sized trace is written compressed: see
open_lcov().
"""

import gzip
import logging
import os
import re
from collections import defaultdict

logger = logging.getLogger(__name__)

# Installed-header locations whose original source directory is derivable from the path,
# as (location, candidate source directories in preference order).
#
# One location can have several candidates: bmalloc's copy-headers phase flattens two
# source directories into usr/local/include/bmalloc, so pas_alignment.h arrives at the same
# place as bmalloc's own headers while living in Source/bmalloc/libpas/src/libpas. The path
# in the trace cannot tell them apart, so canonicalize() tries each candidate and takes the
# one that exists on disk. 161 of the 180 bmalloc headers in a full-suite trace are libpas
# headers, so getting this wrong is not an edge case: it named a file that does not exist
# for 161 of them, which then have no source to render a line view from.
_INSTALLED_HEADER_RULES = (
    ('/usr/local/include/wtf/', ('Source/WTF/wtf/',)),
    ('/usr/local/include/bmalloc/', ('Source/bmalloc/bmalloc/',
                                     'Source/bmalloc/libpas/src/libpas/')),
    # Never seen in practice -- libpas headers are copied under bmalloc/, not pas/ -- but
    # harmless to keep, and the alternative is to notice the day the copy phase changes.
    ('/usr/local/include/pas/', ('Source/bmalloc/libpas/src/libpas/',)),
    # PAL is built as part of WebCore but installs its headers as a project of its own, so
    # every other framework's TUs see them here: 23 files and 533 lines of the trace, at
    # 82.93%, which without this rule are reported under the build directory.
    ('/usr/local/include/pal/', ('Source/WebCore/PAL/pal/',)),
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
    'WebGPU': 'Source/WebGPU',
}

# Directories under a framework's source tree that a copied macOS framework header cannot
# have come from, used to break a tie between candidates with the same basename. Without
# this, five of WebCore's copied network headers (ResourceRequest.h, ResourceResponse.h,
# ResourceError.h, CertificateInfo.h, AuthenticationChallenge.h) resolve to nothing at all,
# because each basename exists three times -- once under platform/network/cf/, which is the
# one a macOS build compiles, and once each under curl/ and soup/, which it does not.
#
# PAL is in here for a different reason: it is a project of its own that happens to live
# inside Source/WebCore, and it installs its headers to usr/local/include/pal, so a header
# arriving as WebCore.framework/PrivateHeaders/X.h is never PAL's copy of X.h. That
# disambiguates PopupMenu.h and ThreadGlobalData.h.
_NON_COCOA_SOURCE_DIRECTORIES = frozenset((
    'PAL', 'adwaita', 'curl', 'glib', 'gtk', 'haiku', 'playstation', 'soup', 'unix',
    'wayland', 'win', 'wpe', 'x11',
))


def _prefer_cocoa(paths):
    """The one candidate a macOS build could have compiled, or None if still ambiguous.

    Narrowing rather than pruning the walk: a directory excluded from the walk would take
    with it any basename that is unique only inside it, which would turn a resolved header
    into an unresolved one. Filtering candidates after the fact can only ever improve on
    the ambiguous case.
    """
    if len(paths) == 1:
        return paths[0]
    preferred = [path for path in paths
                 if not (_NON_COCOA_SOURCE_DIRECTORIES & set(path.split('/')))]
    return preferred[0] if len(preferred) == 1 else None


def _build_basename_index(checkout_root, framework):
    """basename -> relative source path, for basenames that resolve to exactly one file."""
    directory = os.path.join(checkout_root, _FRAMEWORK_SOURCE_DIRECTORY[framework])
    if not os.path.isdir(directory):
        return {}
    seen = defaultdict(list)
    for current, directories, files in os.walk(directory):
        directories[:] = [d for d in directories if d not in ('.git', 'DerivedSources')]
        for name in files:
            if name.endswith(('.h', '.hpp')):
                seen[name].append(os.path.relpath(os.path.join(current, name), checkout_root))
    index = {}
    for name, paths in seen.items():
        resolved = _prefer_cocoa(sorted(paths))
        if resolved:
            index[name] = resolved
    return index


# Copied-header directories under <build>/usr/local/include that hold WebKit's own code.
# Everything else there is a third-party project's headers, installed so that WebKit can
# include them: on the measured build that is ANGLE, api, dav1d, gtest, libwebrtc, logging,
# media, net, p2p, rtc_base, video, webm and webrtc.
#
# An allow-list rather than a deny-list because the deny-list was wrong. The report claimed
# no third-party files, and a full-suite trace had 26 of them across 724 lines at 26.24%:
# libwebrtc's headers arrive under their own top-level directory names (api/, rtc_base/,
# p2p/, logging/, video/), and the Source/ThirdParty/ and /usr/local/include/webrtc/
# patterns match none of those. A new third-party project therefore has to be *added* to
# this list to get into the report, instead of having to be noticed to be kept out.
FIRST_PARTY_COPIED_HEADER_NAMES = frozenset((
    'wtf', 'bmalloc', 'pas', 'pal', 'WebKitAdditions', 'WebCoreTestSupport', 'WGSL.h',
))


def third_party_copied_header_ignore_regexes(build_directory):
    """--ignore-filename-regex arguments for the third-party headers a build copied.

    Derived from what is actually in the build directory rather than from a list of project
    names, so a project that starts installing headers is excluded the day it does, which is
    the safe direction: the default is that the report holds no third-party code.
    """
    include_directory = os.path.join(build_directory, 'usr', 'local', 'include')
    try:
        names = sorted(os.listdir(include_directory))
    except OSError:
        return []
    return ['/usr/local/include/{}'.format(name) for name in names
            if name not in FIRST_PARTY_COPIED_HEADER_NAMES and not name.startswith('.')]


class PathCanonicalizer:
    # Why a path is still under the build directory once everything derivable has been
    # derived. Each of these is a deliberate outcome, not a failure to canonicalize: naming
    # a checkout path for one of them would be inventing one.
    WEBKIT_ADDITIONS = ('copied from the WebKitAdditions repository, which is not part of '
                        'this checkout, so the copy is the only path there is')
    COPIED_FRAMEWORK_HEADER = ('a copied framework header with no unique source match; '
                               'almost all of them are generated by the build, which is '
                               'where the copy came from')
    OTHER = 'not a copied header this tool knows how to place'

    def __init__(self, checkout_root, build_directory=None):
        self._checkout_root = checkout_root.rstrip('/')
        self._build_directory = build_directory.rstrip('/') if build_directory else None
        self._indices = {}
        self.installed_header_count = 0
        self.framework_header_count = 0
        self.unresolved_framework_headers = set()
        # reason -> {path}, for paths left under the build directory.
        self.build_directory_paths = defaultdict(set)

    def _framework_index(self, framework):
        if framework not in self._indices:
            self._indices[framework] = _build_basename_index(self._checkout_root, framework)
        return self._indices[framework]

    def _note_if_under_build_directory(self, path, reason):
        if self._build_directory and path.startswith(self._build_directory + '/'):
            self.build_directory_paths[reason].add(path)
        return path

    def canonicalize(self, path):
        """Map a copied-header path back to its source path, or return it unchanged."""
        for needle, candidates in _INSTALLED_HEADER_RULES:
            index = path.find(needle)
            if index == -1:
                continue
            self.installed_header_count += 1
            tail = path[index + len(needle):]
            for candidate in candidates:
                resolved = os.path.join(self._checkout_root, candidate + tail)
                if os.path.exists(resolved):
                    return resolved
            # Nothing matched: name the first candidate anyway, so the file still appears in
            # the report under a plausible path rather than under the build directory.
            return os.path.join(self._checkout_root, candidates[0] + tail)

        match = _FRAMEWORK_HEADER_PATTERN.search(path)
        if match and match.group('framework') in _FRAMEWORK_SOURCE_DIRECTORY:
            framework = match.group('framework')
            basename = os.path.basename(match.group('rest'))
            resolved = self._framework_index(framework).get(basename)
            if resolved:
                self.framework_header_count += 1
                return os.path.join(self._checkout_root, resolved)
            self.unresolved_framework_headers.add(path)
            return self._note_if_under_build_directory(path, self.COPIED_FRAMEWORK_HEADER)
        if '/usr/local/include/WebKitAdditions/' in path:
            return self._note_if_under_build_directory(path, self.WEBKIT_ADDITIONS)
        return self._note_if_under_build_directory(path, self.OTHER)

    def log_summary(self):
        logger.info('Canonicalized %d installed-header and %d copied framework-header paths '
                    'back to their source locations',
                    self.installed_header_count, self.framework_header_count)
        if self.unresolved_framework_headers:
            logger.info('%d copied framework headers had no unique source match and were left as-is',
                        len(self.unresolved_framework_headers))
        # Said out loud with the reasons, because "why is there a build directory in the
        # coverage report" is otherwise a question somebody has to re-measure to answer.
        total = sum(len(paths) for paths in self.build_directory_paths.values())
        if total:
            logger.info('%d of the paths in this report are still under the build directory, '
                        'because they have no path in the checkout:', total)
            for reason, paths in sorted(self.build_directory_paths.items()):
                logger.info('    %4d  %s', len(paths), reason)


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


def project_totals(coverage_by_path):
    """{metric: (count, covered)} over a whole parsed trace.

    Deliberately over the parsed, canonicalized, duplicate-unioned trace and not over
    llvm-cov's own report, because the two have different denominators: llvm-cov counts a
    copied header once per framework that includes it, which on a full-suite run is
    2,098,175 lines against this function's 1,889,061, and 72.09% function coverage against
    55.05% -- llvm-cov counts a template instantiation as a function, and lcov's records are
    keyed by mangled name. Anything gating on coverage has to gate on the number the report
    displays, or the gate and the report disagree.
    """
    # Seeded from FileCoverage rather than from a constant, so an empty trace still answers
    # for every metric and the set of metrics cannot drift from the ones it can produce.
    totals = {metric: [0, 0] for metric in FileCoverage().totals()}
    for coverage in coverage_by_path.values():
        for metric, (count, covered) in coverage.totals().items():
            totals[metric][0] += count
            totals[metric][1] += covered
    return {metric: tuple(entry) for metric, entry in totals.items()}


def open_lcov(lcov_path):
    """Open an lcov trace for reading, transparently decompressing a gzipped one.

    Sniff the gzip magic rather than trusting the extension. Traces are archived as build
    artifacts and fed back in as baselines, so they get renamed on the way through CI, and a
    trace that reads as line noise because of its name is a bad failure mode.
    """
    with open(lcov_path, 'rb') as probe:
        magic = probe.read(2)
    if magic == b'\x1f\x8b':
        return gzip.open(lcov_path, 'rt', encoding='utf-8', errors='replace')
    return open(lcov_path, 'r', encoding='utf-8', errors='replace')


def parse_lcov_source_files(lcov_path, canonicalizer=None):
    """The set of canonical paths an lcov trace has records for.

    A cheaper pass than parse_lcov for the one question that does not need the counts:
    which files the report can say anything about at all. Over the 716MB trace a
    full-suite run produces, this is a couple of seconds against tens of them.
    """
    paths = set()
    with open_lcov(lcov_path) as handle:
        for line in handle:
            if line.startswith('SF:'):
                path = line[3:].rstrip('\n')
                paths.add(canonicalizer.canonicalize(path) if canonicalizer else path)
    return paths


def parse_lcov(lcov_path, canonicalizer=None, lines_only=False):
    """Parse an lcov trace into {canonical path: FileCoverage}, unioning duplicates.

    lines_only skips the function and branch records. It exists for the per-suite traces: a
    report over several suites parses one trace per suite plus the merged one, and the branch
    map alone is 1,043,499 entries on a full-suite run while the per-suite columns show line
    coverage. It is also the only metric that is comparable across suites -- llvm-cov's set of
    function records is profile-dependent, measured at 396,692 records from one suite's
    profile against 396,696 from the merge of two, because a handful of inline template
    instantiations appear only once some profile has a record for them.
    """
    files = {}
    current = None
    with open_lcov(lcov_path) as handle:
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
            elif line == 'end_of_record':
                path, coverage = current
                if path in files:
                    files[path].merge(coverage)
                else:
                    files[path] = coverage
                current = None
            elif lines_only:
                continue
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
    return files

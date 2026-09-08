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
import tempfile
from collections import defaultdict, namedtuple

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
    # The CMake build stages the same headers under <build>/<project>/Headers/<subdir>/ and
    # compiles against those, so none of the markers above appear in its paths and every one of
    # these files was reported under the build directory instead. Measured on a CMake report:
    # 399 wtf, 184 bmalloc, 38 pal headers, and 131 of them also present under Source/WTF/wtf
    # from the translation units that reach them through -I Source/WTF -- one file, two rows,
    # counted twice in the denominator with its coverage split between them.
    ('/WTF/Headers/wtf/', ('Source/WTF/wtf/',)),
    ('/bmalloc/Headers/bmalloc/', ('Source/bmalloc/bmalloc/',
                                   'Source/bmalloc/libpas/src/libpas/')),
    ('/PAL/Headers/pal/', ('Source/WebCore/PAL/pal/',)),
    ('/WebGPU/Headers/WebGPU/', ('Source/WebGPU/WebGPU/',)),
)


def staged_equivalents_for_scope(scope, checkout_root, build_directory=None):
    """The staged header paths that canonicalize back into `scope`, for llvm-cov's --sources.

    _INSTALLED_HEADER_RULES is applied when a trace is *read*, so every derived view of a
    report shows WTF, bmalloc, PAL and WebGPU headers under Source/. llvm-cov's --sources
    filter runs long before that, against the paths actually recorded in the coverage mapping
    -- and those are the staged copies. So `--sources Source/WTF` asks llvm-cov for a set of
    paths that a framework compiled against the staged headers does not contain.

    Measured on this checkout: WebCore.framework's mapping has 13,548 files and *not one*
    Source/WTF path, because WebCore compiles against <build>/WTF/Headers/wtf. That matters far
    more than a missing scope, because llvm-cov's response to a --sources filter that matches
    nothing is to report everything: the same profile that gives 322 files and 39,107 lines for
    --products=JavaScriptCore --sources=Source/WTF gives 13,554 files and 1,453,739 lines,
    including Xcode SDK headers, for --products=WebCore, under a banner saying the report is
    scoped. Passing the staged equivalents alongside the scope is what makes the filter match.

    Returns absolute paths, and only ones that exist: a path that matches nothing is the defect
    being fixed, not a harmless extra.
    """
    checkout_root = checkout_root.rstrip('/')
    relative = os.path.relpath(os.path.normpath(scope), checkout_root)
    if relative.startswith(os.pardir):
        return []
    relative = relative.rstrip('/') + '/'

    equivalents = []
    for needle, candidates in _INSTALLED_HEADER_RULES:
        for candidate in candidates:
            if candidate.startswith(relative):
                # The scope contains the whole staged tree: Source/WTF holds Source/WTF/wtf.
                tail = ''
            elif relative.startswith(candidate):
                # The scope is inside it: Source/WTF/wtf/text -> <staged wtf>/text.
                tail = relative[len(candidate):]
            else:
                continue
            if needle.startswith('/usr/'):
                staged = needle + tail
            elif build_directory:
                staged = os.path.join(build_directory.rstrip('/'), needle.lstrip('/') + tail)
            else:
                continue
            staged = os.path.normpath(staged)
            if staged not in equivalents and os.path.isdir(staged):
                equivalents.append(staged)
    return equivalents

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

# Directory names that hold expected output for the IDL, IPC and CSS generators, or an API
# test's own headers. A copied framework header is never one of these, and pruning them is
# not the same call as _prefer_cocoa()'s below: an ambiguous candidate is a header the tool
# declines to place, while a *fixture* candidate is a header it places wrongly, in a
# directory the not-built table on the page above classifies as "Generator fixture or
# benchmark". Three of them were live in the shipped report:
#
#   StyleComputedStyleProperties+GettersInlines.h  446 DA lines onto a 176-line fixture
#   CSSPropertyNames.h                              8 DA lines onto a 268-line fixture
#   JSDOMWindow.h                                   3 DA lines onto a 102-line fixture
#
# The first said "437 of 446 instrumented lines never executed" over 176 rows, which read as
# a generator test-fixture directory being product code at 2%.
#
# coverage_build_inventory.BuildDescriptionIndex already guards against exactly this
# collision from the other side -- its docstring names bindings/scripts/test/JS/JSDOMWindow.cpp
# -- by resolving a basename against the directories of the description that names it. There
# is no description to resolve a copied header against, so this prunes instead.
#
# Measured over the five framework trees: 13,301 resolvable basenames become 13,089. Of the
# 212 that stop resolving, 209 were never asked about, and every one of them lives in
# bindings/scripts/test/JS, Scripts/webkit/tests, css/scripts/test/TestCSSPropertiesResults,
# JavaScriptCore/API/tests, wasm/debugger/tests or dav1d/tests. One basename starts resolving
# because the fixture was what made it ambiguous.
#
# 'testing' is deliberately absent, as it is in coverage_build_inventory: Source/WebCore/testing
# is real product code compiled into WebCoreTestSupport.
_FIXTURE_SOURCE_DIRECTORIES = frozenset(('test', 'tests'))


def _prefer_cocoa(paths):
    """The one candidate a macOS build could have compiled, or None if still ambiguous.

    Narrowing rather than pruning the walk: a directory excluded from the walk would take
    with it any basename that is unique only inside it, which would turn a resolved header
    into an unresolved one. Filtering candidates after the fact can only ever improve on
    the ambiguous case.

    That reasoning holds for another port's directory, which really might have been the source
    of a header, and not for a generator fixture, which never was -- see
    _FIXTURE_SOURCE_DIRECTORIES, which is pruned from the walk for that reason.
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
        directories[:] = [d for d in directories
                          if d not in ('.git', 'DerivedSources')
                          and d.lower() not in _FIXTURE_SOURCE_DIRECTORIES]
        for name in files:
            if name.endswith(('.h', '.hpp')):
                seen[name].append(os.path.relpath(os.path.join(current, name), checkout_root))
    index = {}
    for name, paths in seen.items():
        resolved = _prefer_cocoa(sorted(paths))
        if resolved:
            index[name] = resolved
    return index


def _installed_copy_rules():
    """(source marker, install prefixes) pairs, inverting _INSTALLED_HEADER_RULES.

    Derived from that table rather than written out again, so the two directions cannot drift
    apart the day a copy phase changes.
    """
    inverted = {}
    for location, candidates in _INSTALLED_HEADER_RULES:
        for candidate in candidates:
            inverted.setdefault('/' + candidate, []).append(location.lstrip('/'))
    return tuple((marker, tuple(prefixes)) for marker, prefixes in inverted.items())


_INSTALLED_COPY_RULES = _installed_copy_rules()


def compiled_copy_candidates(path, build_directory):
    """Where the build directory would hold its own copy of path's text, in preference order.

    canonicalize() maps a copied header back to the checkout, which is the right path to
    report it under and the wrong text to render it from: the copy was made once, at build
    time, and every translation unit that included it saw the copy. If the checkout's file has
    been edited since -- which is guaranteed as soon as anybody keeps working after a coverage
    run -- the copy is the only text on disk the coverage records are about.

    Measured on the shipped report: Source/WTF/wtf/Expected.h had been rewritten from 403
    lines to 31 by bug 322297, and 394 instrumented lines were rendered against the 31-line
    file, dropping 57 rows, while the 403-line text the profile describes was still sitting in
    <build>/usr/local/include/wtf/Expected.h. Eight files were in that state.

    Matched on the path suffix rather than resolved against a checkout root, because the
    caller renders from the report's own tree prefix and should not have to know where the
    checkout is.

    A candidate is only a candidate. A framework header's copy is found by basename, exactly
    as canonicalize() finds the source by basename, so the caller has to check that the text
    it finds accounts for the records it has before preferring it over the checkout's.
    """
    if not build_directory:
        return
    for marker, prefixes in _INSTALLED_COPY_RULES:
        index = path.find(marker)
        if index == -1:
            continue
        tail = path[index + len(marker):]
        for prefix in prefixes:
            yield os.path.join(build_directory, prefix + tail)
    if not path.endswith(('.h', '.hpp')):
        return
    basename = os.path.basename(path)
    for framework, source_directory in _FRAMEWORK_SOURCE_DIRECTORY.items():
        if '/{}/'.format(source_directory) in path:
            for kind in ('PrivateHeaders', 'Headers'):
                yield os.path.join(build_directory, framework + '.framework', kind, basename)


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


# The same allow-list for the CMake layout, where a project stages its headers under
# <build>/<project>/Headers rather than into a shared usr/local/include. Separate from the
# names above because these are project directories rather than the include subdirectory
# names: pal's headers arrive under PAL/Headers/pal, not pal/pal.
FIRST_PARTY_COPIED_HEADER_PROJECTS = frozenset((
    'WTF', 'bmalloc', 'PAL', 'WebKitAdditions', 'WebCore', 'JavaScriptCore', 'WebKit',
    'WebKitLegacy', 'WebGPU', 'WebCoreTestSupport', 'WGSL',
))

_COPIED_HEADER_DIRECTORY_NAMES = ('Headers', 'PrivateHeaders')


def third_party_copied_header_ignore_regexes(build_directory):
    """--ignore-filename-regex arguments for the third-party headers a build copied.

    Derived from what is actually in the build directory rather than from a list of project
    names, so a project that starts installing headers is excluded the day it does, which is
    the safe direction: the default is that the report holds no third-party code.

    Two layouts, because the two build systems stage headers differently and this returned
    nothing at all on a CMake build: there is no usr/local/include there, so ANGLE's 11 and
    libwebrtc's 13 copied headers were reported as first-party WebKit code.
    """
    regexes = []
    include_directory = os.path.join(build_directory, 'usr', 'local', 'include')
    try:
        names = sorted(os.listdir(include_directory))
    except OSError:
        names = []
    regexes += ['/usr/local/include/{}'.format(name) for name in names
                if name not in FIRST_PARTY_COPIED_HEADER_NAMES and not name.startswith('.')]

    try:
        projects = sorted(os.listdir(build_directory))
    except OSError:
        projects = []
    for project in projects:
        # A dot means a bundle -- WebCore.framework, MiniBrowser.app, a .xpc service -- whose
        # copied headers _FRAMEWORK_HEADER_PATTERN already places, and excluding those would
        # drop first-party code.
        if project.startswith('.') or '.' in project:
            continue
        if project in FIRST_PARTY_COPIED_HEADER_PROJECTS:
            continue
        for kind in _COPIED_HEADER_DIRECTORY_NAMES:
            if os.path.isdir(os.path.join(build_directory, project, kind)):
                regexes.append('/{}/{}/'.format(project, kind))
    return regexes


class PathCanonicalizer:
    # Why a path is still under the build directory once everything derivable has been
    # derived. Each of these is a deliberate outcome, not a failure to canonicalize: naming
    # a checkout path for one of them would be inventing one.
    WEBKIT_ADDITIONS = ('copied from the WebKitAdditions repository, which is not part of '
                        'this checkout, so the copy is the only path there is')
    COPIED_FRAMEWORK_HEADER = ('a copied framework header with no unique source match; '
                               'almost all of them are generated by the build, which is '
                               'where the copy came from')
    # Not a copy of anything: the build wrote the file itself, so the build directory is its
    # only location. Measured on a CMake report, all five of what used to fall into OTHER:
    # WGSL's TypeDeclarations.h and TypeOverloads.h (generated by the wgsl-types target),
    # PALSwift-Generated.h and WebGPUSwift-Generated.h (Swift-generated headers), and
    # WebKit/WebPushDaemonStubs.cpp (a file(CONFIGURE) output, Source/WebKit/PlatformCocoa.cmake).
    # Worth separating from OTHER because OTHER reads as "the tool gave up" and these are not
    # that -- there is no checkout path to name, the same as WEBKIT_ADDITIONS.
    GENERATED_BY_THE_BUILD = ('written by the build rather than copied, so it has no path in '
                              'the checkout to place it at')
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
        if ('/usr/local/include/WebKitAdditions/' in path
                or '/WebKitAdditions/Headers/WebKitAdditions/' in path):
            return self._note_if_under_build_directory(path, self.WEBKIT_ADDITIONS)
        return self._note_if_under_build_directory(path, self._unplaceable_reason(path))

    def _unplaceable_reason(self, path):
        """GENERATED_BY_THE_BUILD when nothing in the checkout corresponds, else OTHER.

        A build-directory path whose checkout-relative counterpart does not exist was written by
        the build, not copied from somewhere this tool failed to find. Deciding by asking the
        filesystem rather than by pattern, because the generators have nothing in common: two are
        Swift-generated headers, two come from a custom command, one from file(CONFIGURE).
        """
        if not self._build_directory or not path.startswith(self._build_directory + '/'):
            return self.OTHER
        relative = path[len(self._build_directory) + 1:]
        if os.path.exists(os.path.join(self._checkout_root, relative)):
            return self.OTHER
        return self.GENERATED_BY_THE_BUILD

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
    __slots__ = ('lines', 'functions', 'function_lines', 'branches')

    def __init__(self):
        self.lines = {}      # line number -> execution count
        # Mangled name -> execution count. Kept for coverage_delta, whose regressed_functions
        # compares the two sides by name because a mangled name survives a source edit and a
        # line number does not. Deliberately NOT what totals() counts; see function_lines.
        self.functions = {}
        # FN: start line -> the highest count of any function starting there, which is what
        # llvm-cov counts as one function.
        #
        # llvm-cov MERGES template instantiations and lcov does not: lcov emits one FN:/FNDA:
        # pair per instantiation and then an FNF:/FNH: pair counting the distinct starts.
        # Keying by name therefore counts a Vector<T> method once per instantiation --
        # measured over the shipped trace, 1,978,649 functions against llvm-cov's 255,297, a
        # 7.75x denominator, and 55.06% where summary.txt in the same output directory says
        # 72.09%. It measured template fan-out, not test reach, and --fail-under-functions=70
        # failed a build that llvm-cov called 72.09%.
        #
        # Start-line keying reproduces llvm-cov's own count to 0.07%: summing the distinct
        # starts per record gives 255,112 against llvm-cov's 255,297, and the residue is
        # functions that begin on the same line in different columns, which lcov cannot
        # express because FN: carries no column. Over the whole report, where duplicate
        # records for one canonical file are unioned rather than summed, it gives 239,300 at
        # 73.84% -- higher than summary.txt's 72.09% for exactly the reason the line figure is
        # lower than its 67.41%. See project_totals().
        self.function_lines = {}
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
        for line, count in other.function_lines.items():
            if count > self.function_lines.get(line, -1):
                self.function_lines[line] = count
        for key, count in other.branches.items():
            if count > self.branches.get(key, -1):
                self.branches[key] = count

    def fold_function_lines(self, starts):
        """Fill in function_lines from functions, given {mangled name: FN: start line}.

        Called once per record, when the record ends, rather than as the FN:/FNDA: lines
        arrive, so that it does not depend on llvm-cov emitting every FN: before the FNDA:
        that refers to it. It does today -- 0 of the 18,237 records in the shipped trace have
        an FNDA: whose name no FN: in the same record declared -- but nothing in the format
        says so.
        """
        for name, count in self.functions.items():
            line = starts.get(name)
            if line is not None and count > self.function_lines.get(line, -1):
                self.function_lines[line] = count

    def totals(self):
        return {
            'lines': (len(self.lines), sum(1 for c in self.lines.values() if c)),
            'functions': (len(self.function_lines),
                          sum(1 for c in self.function_lines.values() if c)),
            'branches': (len(self.branches), sum(1 for c in self.branches.values() if c)),
        }


def project_totals(coverage_by_path):
    """{metric: (count, covered)} over a whole parsed trace.

    Deliberately over the parsed, canonicalized, duplicate-unioned trace and not over
    llvm-cov's own report, because the two have different denominators, and in both directions.

    llvm-cov counts a copied header once per framework that includes it, and its per-file LF:
    is a sum of per-function line counts rather than a count of distinct lines, so a lambda
    body inside its enclosing function is counted twice -- FTLLowerDFGToB3.cpp reports LF:24,853
    over 20,863 distinct lines. On the shipped trace that is 2,098,175 lines against this
    function's 1,888,952, of which 73% is the double-counting and 27% is the copied-header
    merging.

    Functions go the other way, and by less: llvm-cov merges a template's instantiations, so
    its 255,297 is a count of distinct function starts summed per record, while this unions
    them per canonical file and gets 239,300. Which is 73.84% here against summary.txt's
    72.09%, for the same reason the line figure is 67.15% against its 67.41%.

    Anything gating on coverage has to gate on the number the report displays, or the gate and
    the report disagree.
    """
    # Seeded from FileCoverage rather than from a constant, so an empty trace still answers
    # for every metric and the set of metrics cannot drift from the ones it can produce.
    totals = {metric: [0, 0] for metric in FileCoverage().totals()}
    for coverage in coverage_by_path.values():
        for metric, (count, covered) in coverage.totals().items():
            totals[metric][0] += count
            totals[metric][1] += covered
    return {metric: tuple(entry) for metric, entry in totals.items()}


CanonicalizedTrace = namedtuple('CanonicalizedTrace',
                                ('records_in', 'records_out', 'rewritten', 'merged'))


class _MergedRecord:
    """The union of several lcov records for one canonical path, ready to re-emit.

    Only the records that actually collide are accumulated. Everything else is copied through
    byte-for-byte, so what this holds is bounded by the duplicates rather than by the trace:
    measured on a full-suite run, 649 of 16,309 records need it.
    """
    __slots__ = ('lines', 'function_starts', 'function_counts', 'branches')

    def __init__(self):
        self.lines = {}            # line -> highest count
        self.function_starts = {}  # mangled name -> FN: start line
        self.function_counts = {}  # mangled name -> highest FNDA: count
        self.branches = {}         # (line, block, branch) -> highest taken

    def add(self, line):
        # Union by maximum, exactly as FileCoverage.merge() does, so that a trace rewritten here
        # and a report rendered from parse_lcov() agree by construction and not by coincidence.
        if line.startswith('DA:'):
            number, _, count = line[3:].partition(',')
            try:
                number, count = int(number), int(count)
            except ValueError:
                return
            if count > self.lines.get(number, -1):
                self.lines[number] = count
        elif line.startswith('FNDA:'):
            count, _, name = line[5:].partition(',')
            try:
                count = int(count)
            except ValueError:
                return
            if count > self.function_counts.get(name, -1):
                self.function_counts[name] = count
        elif line.startswith('FN:'):
            number, _, name = line[3:].partition(',')
            self.function_counts.setdefault(name, 0)
            try:
                self.function_starts[name] = int(number)
            except ValueError:
                pass
        elif line.startswith('BRDA:'):
            parts = line[5:].split(',')
            if len(parts) != 4:
                return
            try:
                taken = 0 if parts[3] == '-' else int(parts[3] or 0)
            except ValueError:
                return
            key = (parts[0], parts[1], parts[2])
            if taken > self.branches.get(key, -1):
                self.branches[key] = taken

    def emit(self, path):
        """The record's lines in llvm-cov's order: SF, FN, FNDA, FNF/FNH, BRDA, BRF/BRH, DA, LF/LH."""
        out = ['SF:' + path]
        for name in sorted(self.function_starts):
            out.append('FN:{},{}'.format(self.function_starts[name], name))
        for name in sorted(self.function_counts):
            out.append('FNDA:{},{}'.format(self.function_counts[name], name))
        out.append('FNF:{}'.format(len(self.function_counts)))
        out.append('FNH:{}'.format(sum(1 for count in self.function_counts.values() if count)))
        for key in sorted(self.branches):
            out.append('BRDA:{},{},{},{}'.format(key[0], key[1], key[2],
                                                 self.branches[key] or '-'))
        out.append('BRF:{}'.format(len(self.branches)))
        out.append('BRH:{}'.format(sum(1 for taken in self.branches.values() if taken)))
        for number in sorted(self.lines):
            out.append('DA:{},{}'.format(number, self.lines[number]))
        out.append('LF:{}'.format(len(self.lines)))
        out.append('LH:{}'.format(sum(1 for count in self.lines.values() if count)))
        out.append('end_of_record')
        return out


def canonicalize_lcov(lcov_path, canonicalizer, compress=None):
    """Rewrite an lcov trace in place so its paths are the ones the report shows.

    Returns a CanonicalizedTrace.

    llvm-cov names the path each translation unit actually included, which for a staged header is
    the build directory's copy. Every derived view of a report canonicalizes that on the way in,
    so the report is right -- but the exported trace was left as llvm-cov wrote it, and the trace
    is the artifact that outlives the report, that compare-coverage-reports consumes and that a
    CI system would ingest. So the two disagreed. Measured on a full-suite run: the trace totalled
    1,900,143 lines at 66.41% against the report's 1,886,155 at 66.69%, because 649 of its 16,309
    records kept a build-directory path and 116 WTF headers appeared under both spellings, each
    holding part of the same file's coverage.

    Not done by parsing and re-emitting the whole trace. FileCoverage keeps function counts by
    mangled name and separately by start line, so it cannot reconstruct an FN: record's
    name-to-line pairing, and re-emitting all 16,309 records would rewrite bytes llvm-cov got
    right in order to fix 4% of them. Instead: one cheap SF:-only pass to find which canonical
    paths more than one record maps to, then a streaming pass that copies every uncontested
    record through unchanged -- rewriting its SF: line only when canonicalization moved it -- and
    accumulates just the colliding ones.

    compress defaults to whatever the input was, sniffed rather than taken from the extension,
    because these get renamed on the way through CI.
    """
    counts = defaultdict(int)
    with open_lcov(lcov_path) as handle:
        for line in handle:
            if line.startswith('SF:'):
                counts[canonicalizer.canonicalize(line[3:].rstrip('\n'))] += 1
    contested = {path for path, count in counts.items() if count > 1}

    if compress is None:
        with open(lcov_path, 'rb') as probe:
            compress = probe.read(2) == b'\x1f\x8b'

    merged = {}
    records_in = rewritten = records_out = 0
    directory = os.path.dirname(os.path.abspath(lcov_path))
    descriptor, temporary_path = tempfile.mkstemp(dir=directory, prefix='.canonicalizing-')
    os.close(descriptor)
    try:
        opener = gzip.open if compress else open
        with opener(temporary_path, 'wt', encoding='utf-8') as writer:
            passthrough, accumulator = None, None
            with open_lcov(lcov_path) as handle:
                for line in handle:
                    line = line.rstrip('\n')
                    if line.startswith('SF:'):
                        records_in += 1
                        original = line[3:]
                        path = canonicalizer.canonicalize(original)
                        if path != original:
                            rewritten += 1
                        if path in contested:
                            accumulator = merged.setdefault(path, _MergedRecord())
                            passthrough = None
                        else:
                            accumulator = None
                            passthrough = ['SF:' + path]
                    elif line == 'end_of_record':
                        if passthrough is not None:
                            passthrough.append(line)
                            writer.write('\n'.join(passthrough) + '\n')
                            records_out += 1
                        passthrough, accumulator = None, None
                    elif accumulator is not None:
                        accumulator.add(line)
                    elif passthrough is not None:
                        passthrough.append(line)
            for path in sorted(merged):
                writer.write('\n'.join(merged[path].emit(path)) + '\n')
                records_out += 1
        os.replace(temporary_path, lcov_path)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)

    return CanonicalizedTrace(records_in=records_in, records_out=records_out,
                              rewritten=rewritten, merged=len(merged))


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
    # {mangled name: FN: start line} within the record being read, folded into the record's
    # function_lines when it ends. See FileCoverage.fold_function_lines().
    starts = {}
    with open_lcov(lcov_path) as handle:
        for line in handle:
            line = line.rstrip('\n')
            if line.startswith('SF:'):
                path = line[3:]
                if canonicalizer:
                    path = canonicalizer.canonicalize(path)
                current = (path, FileCoverage())
                starts = {}
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
                coverage.fold_function_lines(starts)
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
                number, _, name = line[3:].partition(',')
                current[1].functions.setdefault(name, 0)
                try:
                    starts[name] = int(number)
                except ValueError:
                    pass
            elif line.startswith('BRDA:'):
                parts = line[5:].split(',')
                if len(parts) == 4:
                    taken = 0 if parts[3] == '-' else int(parts[3] or 0)
                    current[1].branches[(parts[0], parts[1], parts[2])] = taken
    return files

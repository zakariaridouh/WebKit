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

"""Work out which first-party implementation files this build configuration compiled.

A coverage report built from `llvm-cov` can only distinguish two states: a line that ran
and a line that was instrumented but did not run. A file that was never compiled has no
coverage mapping at all, so it is not 0%-covered -- it is absent, and absent code silently
shrinks the denominator instead of visibly lowering the percentage.

On the macOS Xcode configuration measured here that is 2,446 of 10,473 first-party
implementation files and 764,144 physical lines, so the headline percentage is quoted over
77% of the tree. This module supplies the missing third state and a reason for each file, so
the report can say so instead of implying the other 23% does not exist.

Everything comes from the build rather than from re-deriving what the build would have
done:

  * Whether a file was compiled comes from three records, unioned, because no one of them
    survives both build systems:

      - The per-translation-unit dependency files the compiler wrote (`*.d` under each
        target's `Objects-normal`). They name the primary source of every object, and for a
        unified build they also name every member of the bundle. Validated against the
        full-suite Xcode report: every one of the 8,027 implementation files in that report's
        lcov trace is in this set, so it has no false negatives there. On the CMake build it
        is nearly empty -- ninja pairs `depfile` with `deps = gcc`, so it folds each `.o.d`
        into the binary `.ninja_deps` and deletes it, leaving 4,718 objects against five `.d`
        files, none of which is a compiler artefact.

      - The unified-source bundles under `unified-sources/`, which contain a literal
        `#include "<path>"` per member. Reading *only* these would be wrong: WTF, bmalloc,
        WebGPU and PAL have no unified-sources directory on the Xcode build at all, so 587
        compiled files would be reported as never built.

      - The object files themselves, under `CMakeFiles/<Target>.dir/`. This is the CMake
        build's real evidence, and it is stronger than a depfile: an object proves the
        translation unit was compiled, where `compile_commands.json` would only prove it was
        configured.

  * Why an uncompiled file was not compiled comes from the build descriptions that mention
    it -- `Sources*.txt`, `CMakeLists.txt` and every `*.cmake` -- and, failing that, from a
    preprocessor conditional that encloses the whole file.

The whole thing takes 14 s over this checkout and this build directory.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# Extensions that produce a translation unit, so that a missing one is missing coverage.
# Headers are deliberately absent: a header has no coverage of its own, it is reported
# against whichever translation units included it.
IMPLEMENTATION_SUFFIXES = ('.cpp', '.cc', '.cxx', '.c', '.mm', '.m', '.swift')

# Directories under the source root that hold no first-party product code.
_SKIPPED_SOURCE_DIRECTORIES = frozenset(('DerivedSources', '.git', '.svn'))

# Build-directory trees that hold no per-translation-unit record: precompiled headers and
# modules, the TBD stubs, Xcode's own database, and LTO scratch.
_SKIPPED_BUILD_DIRECTORIES = frozenset((
    'SharedPrecompiledHeaders', 'ExplicitPrecompiledModules',
    'SwiftExplicitPrecompiledModules', 'EagerLinkingTBDs', 'XCBuildData', 'LTO',
))

# Source/ThirdParty is 12,538 implementation files of vendored code with its own upstreams
# and, in several cases, its own build system. It is not first-party, so it is not in the
# denominator and not in the absent list either. The smaller vendored trees -- dav1d under
# Source/WebCore/PAL/ThirdParty, mimalloc under Source/bmalloc -- are deliberately left in,
# so that the report shows their exclusion rather than hiding it.
_NOT_FIRST_PARTY = ('Source/ThirdParty',)

# Vendored upstream code that lives outside Source/ThirdParty. The report's own
# third-party filter is passed in by the caller; these are the trees that filter does not
# name but that are still somebody else's code.
_VENDORED_THIRD_PARTY = ('/ThirdParty/', '/mimalloc/mimalloc/')

# Path components that mean "a fixture the generators compare their output against, or a
# benchmark", not shipping code. 'testing' is deliberately absent: Source/WebCore/testing
# is real product code, compiled into WebCoreTestSupport.
_FIXTURE_COMPONENTS = frozenset(('test', 'tests', 'benchmarks', 'toys', 'chaos', 'verifier'))

# Path components that only exist for a port this configuration is not. Consulted only for
# files no build description mentions, because a description is better evidence than a
# directory name. Of the 1,116 files in the checkout these components match, exactly one is
# in the full-suite report, and this rule never runs for a file the report has a record of.
_PORT_COMPONENTS = {
    'gtk': 'GTK', 'wpe': 'WPE', 'libwpe': 'WPE', 'wpeplatform': 'WPE',
    'win': 'Windows', 'wc': 'Windows', 'playstation': 'PlayStation', 'haiku': 'Haiku',
    'glib': 'GLib', 'soup': 'libsoup', 'gstreamer': 'GStreamer', 'gcrypt': 'GnuTLS',
    'cairo': 'Cairo', 'skia': 'Skia', 'freetype': 'FreeType', 'harfbuzz': 'HarfBuzz',
    'adwaita': 'Adwaita', 'texmap': 'TextureMapper', 'coordinatedgraphics': 'Coordinated',
    'coordinated': 'Coordinated', 'atspi': 'AT-SPI', 'atk': 'ATK', 'openxr': 'OpenXR',
    'gbm': 'GBM', 'vulkan': 'Vulkan', 'wayland': 'Wayland', 'x11': 'X11', 'egl': 'EGL',
    'curl': 'curl', 'openssl': 'OpenSSL', 'unix': 'Unix', 'linux': 'Linux',
    'android': 'Android', 'fuchsia': 'Fuchsia', 'generic': 'generic fallback',
    'holepunch': 'GStreamer hole-punch',
}

# Build-description file names that this configuration's build actually consumes. Taken
# from the five Scripts/generate-unified-sources.sh, which name their lists explicitly.
_THIS_PORT_DESCRIPTIONS = frozenset((
    'Sources.txt', 'SourcesCocoa.txt', 'SourcesCocoaInternalSDK.txt', 'SourcesMac.txt',
    'SourcesCMakeCocoa.txt', 'SourcesLibWebRTC.txt', 'PlatformCocoa.cmake',
))

# Ports named by a build-description file name, e.g. SourcesGTK.txt or PlatformWin.cmake.
_DESCRIPTION_PORTS = {
    'GTK': 'GTK', 'GTKDeprecated': 'GTK', 'WPE': 'WPE', 'WPEDeprecated': 'WPE',
    'Win': 'Windows', 'PlayStation': 'PlayStation', 'Haiku': 'Haiku', 'JSCOnly': 'JSCOnly',
    'GLib': 'GLib', 'Socket': 'socket inspector', 'Soup': 'libsoup', 'GStreamer': 'GStreamer',
    'GCrypt': 'GnuTLS', 'Cairo': 'Cairo', 'Skia': 'Skia', 'Adwaita': 'Adwaita',
}

# Target names whose object files end up inside one of the binaries
# generate-coverage-report hands to llvm-cov. Anything else that compiles first-party code
# -- the JSC test tools, the libpas harness, the XPC service entry-point stubs -- produces
# coverage the report never sees, which is a different thing from being untested.
#
# The CMake names are here as well as the Xcode ones, because CMake splits each framework
# across several targets and links them together. This list is not guesswork: it is the
# transitive closure of the CMakeFiles/<T>.dir directories reachable from the five framework
# link edges in the generated build.ninja, with the Source/ThirdParty targets dropped (their
# sources are not in the denominator at all).
REPORTED_TARGETS = frozenset((
    # Xcode target names.
    'JavaScriptCore', 'libJavaScriptCore', 'WebCore', 'PAL', 'WebKit', 'WebKitPlatform',
    'WebKitSwift', 'WebKitLegacy', 'WebGPU', 'WGSL', 'WTF', 'bmalloc',
    # CMake target names, additional to the above.
    'JavaScriptCoreJIT', 'LowLevelInterpreterLib', 'PAL_SwiftInterop',
    'WebCoreAVFoundation', 'WebCoreDOMAndRendering', 'WebCoreInspector', 'WebCoreJSBindings',
    'WebCoreStyle', 'WebGPU_SwiftInterop', 'WGSLCore', 'WebKitARC', 'WebKitGPUProcess',
    'WebKitNetworkProcess', 'WebKitShared', 'WebKitUIProcess', 'WebKitWebProcess',
    'WebKit_SwiftInterop',
))

# Where a unified-source bundle's members live, by the name of the directory holding the
# bundle. A tuple because the same name resolves differently on the two build systems -- see
# BuildInventory._resolve_member -- and the first candidate that has the file wins.
_FRAMEWORK_SOURCE_DIRECTORIES = {
    'JavaScriptCore': ('Source/JavaScriptCore',),
    'WebCore': ('Source/WebCore',),
    'WebKit': ('Source/WebKit',),
    'WebKitLegacy': ('Source/WebKitLegacy',),
    'WTF': ('Source/WTF',),
    # CMake bundles these four and Xcode does not, which is why they were missing.
    'PAL': ('Source/WebCore/PAL/pal', 'Source/WebCore/PAL'),
    'WGSL': ('Source/WebGPU/WGSL',),
    'WebGPU': ('Source/WebGPU/WebGPU', 'Source/WebGPU'),
    'TestWebKit': ('Tools/TestWebKitAPI',),
    'TestWebKitAPI': ('Tools/TestWebKitAPI',),
}

_SOURCES_LIST_NAME = re.compile(r'Sources([A-Za-z]*)\.txt\Z')
_PLATFORM_CMAKE_NAME = re.compile(r'Platform([A-Za-z]*)\.cmake\Z')

# A path-shaped token ending in an implementation suffix. CMake variables are stripped
# afterwards, so ${WEBCORE_DIR}/dom/Touch.cpp resolves the same as dom/Touch.cpp.
_DESCRIPTION_TOKEN = re.compile(r'[A-Za-z0-9_${}/.+-]+\.(?:cpp|cc|cxx|c|mm|m|swift)\b')
_CMAKE_VARIABLE = re.compile(r'\$\{[^}]*\}/?')

_CONDITIONAL = re.compile(r'^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b(.*)$')
# Lines that may sit outside a whole-file guard without disproving it.
_NON_CODE = re.compile(r'^[ \t]*(#[ \t]*(include|import|pragma|error|warning)\b|//|/\*|\*|$)')

# Reasons, most to least numerous on the measured configuration. The order is the order
# they are reported in.
REASON_ORDER = (
    'other-port',
    'feature-flag-off',
    'fixture',
    'test-or-tool-target',
    'third-party',
    'no-executable-code',
    'not-in-this-configuration',
    'no-build-description',
)

REASON_LABELS = {
    'other-port': 'Another port only',
    'feature-flag-off': 'Feature or platform flag off',
    'fixture': 'Generator fixture or benchmark',
    'test-or-tool-target': 'Only in a binary the report excludes',
    'third-party': 'Vendored third party',
    'no-executable-code': 'Compiled, but emitted no coverage mapping',
    'not-in-this-configuration': 'In a build description no target here uses',
    'no-build-description': 'In no build description',
}

REASON_EXPLANATIONS = {
    'other-port': 'Belongs to GTK, WPE, Windows, PlayStation or another port, so a macOS '
                  'build cannot compile it. Nothing to fix.',
    'feature-flag-off': 'Compiled away in its entirety by one preprocessor conditional. '
                        'Turning the flag on in the coverage configuration would make it '
                        'measurable.',
    'fixture': 'Expected output for the IDL, CSS and IPC generators, or a benchmark. Not '
               'part of any product.',
    'test-or-tool-target': 'Compiled, but only into a test tool, benchmark harness or '
                           'service stub whose binary is not handed to llvm-cov. '
                           '--include-test-support covers some of these.',
    'third-party': 'Vendored upstream code, excluded from the report by design. Pass '
                   '--include-third-party to report on it.',
    'no-executable-code': 'Compiled into a reported binary but produced no coverage '
                          'mapping, and has no whole-file conditional. Usually a file that '
                          'is only data -- wtf/ASCIICType.cpp is a lookup table. Reporting '
                          'it at 0% would invent a denominator.',
    'not-in-this-configuration': 'Named by a source list or CMake file, but no target this '
                                 'build ran compiles it -- an optional component such as '
                                 'WebDriver, or a fallback implementation Cocoa replaces. '
                                 'The detail names the description that lists it.',
    'no-build-description': 'No Sources*.txt, *.cmake or CMakeLists.txt in the checkout '
                            'names it. Xcode project files are not parsed, so a file that '
                            'only an .xcodeproj knows about also lands here.',
}


class AbsentFile:
    """One first-party implementation file that the report cannot say anything about."""
    __slots__ = ('path', 'reason', 'detail', 'physical_lines')

    def __init__(self, path, reason, detail, physical_lines):
        self.path = path                    # relative to the checkout root
        self.reason = reason                # a key of REASON_ORDER
        self.detail = detail                # the flag, port or target name, or ''
        self.physical_lines = physical_lines

    def __repr__(self):
        return 'AbsentFile({!r}, {!r}, {!r}, {!r})'.format(
            self.path, self.reason, self.detail, self.physical_lines)


def physical_line_count(path):
    """Physical lines in a file. Not comparable with llvm-cov's executable-line counts,
    and reported separately for exactly that reason."""
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError:
        return 0
    if not data:
        return 0
    return data.count(b'\n') + (0 if data.endswith(b'\n') else 1)


def whole_file_conditional(path):
    """The condition of a preprocessor conditional that encloses everything in the file.

    Returns None when the file has code outside every conditional, which is the common
    case; a file that returns a condition is one whose entire body disappears when that
    condition is false, so naming it explains the file's absence from the report.

    A conditional with an #else or #elif branch is not such a guard, however true it looks:
    the file still compiles to something when the condition is false, so its absence needs
    a different explanation.

    On the measured build this identified a condition for 515 of the 650 files that were
    compiled yet produced no coverage mapping -- PLATFORM(IOS_FAMILY) for 176 of them,
    ENABLE(WEBXR) for 30, LIBPAS_ENABLED for 23.
    """
    try:
        with open(path, 'r', errors='replace') as handle:
            lines = handle.read().split('\n')
    except OSError:
        return None

    depth = 0
    regions = []
    open_region = None
    for number, line in enumerate(lines, 1):
        match = _CONDITIONAL.match(line)
        if match:
            kind, rest = match.group(1), match.group(2)
            if kind in ('if', 'ifdef', 'ifndef'):
                if not depth:
                    condition = rest.split('//')[0].split('/*')[0].strip()
                    if kind == 'ifdef':
                        condition = 'defined({})'.format(condition)
                    elif kind == 'ifndef':
                        condition = '!defined({})'.format(condition)
                    open_region = [number, number, condition]
                depth += 1
            elif kind in ('else', 'elif'):
                if depth == 1:
                    open_region = None
            elif kind == 'endif':
                depth -= 1
                if not depth and open_region:
                    open_region[1] = number
                    regions.append(open_region)
                    open_region = None
            continue
        if not depth and not _NON_CODE.match(line):
            return None

    if not regions:
        return None
    # More than one top-level conditional happens when the includes have their own guard.
    # The one spanning the most lines is the one holding the body.
    return max(regions, key=lambda region: region[1] - region[0])[2]


def _is_implementation(name):
    return name.endswith(IMPLEMENTATION_SUFFIXES)


def enumerate_source_files(checkout_root, source_directory='Source'):
    """Every first-party implementation file under Source/, relative to the checkout root."""
    files = []
    top = os.path.join(checkout_root, source_directory)
    for current, directories, names in os.walk(top):
        relative_directory = os.path.relpath(current, checkout_root)
        if relative_directory in _NOT_FIRST_PARTY:
            directories[:] = []
            continue
        directories[:] = [d for d in directories if d not in _SKIPPED_SOURCE_DIRECTORIES]
        for name in names:
            if _is_implementation(name):
                files.append(os.path.relpath(os.path.join(current, name), checkout_root))
    return files


_CMAKE_TARGET_DIRECTORY = re.compile(r'(?:\A|/)CMakeFiles/([^/]+)\.dir/')

# <binary dir>/CMakeFiles/<target>.dir/<source path, with .. written __>.o
_CMAKE_OBJECT_PATH = re.compile(r'\A(.*)/CMakeFiles/([^/]+)\.dir/(.+)\.(?:o|obj)\Z')


def _unmangle(relative_object_path):
    """CMake writes a `..` in an object's path as `__`. Put them back."""
    return '/'.join('..' if part == '__' else part
                    for part in relative_object_path.split('/'))


def _target_of(path):
    """The target that wrote a build artefact.

    Xcode names it with the nearest enclosing `<Target>.build`; CMake with
    `CMakeFiles/<Target>.dir`. Recognising only the Xcode shape attributed every CMake
    object to the empty target name, which is in no REPORTED_TARGETS, so anything found only
    that way would have been labelled as compiled into a binary the report excludes.
    """
    match = _CMAKE_TARGET_DIRECTORY.search(path)
    if match:
        return match.group(1)
    for part in reversed(path.split(os.sep)):
        if part.endswith('.build'):
            return part[:-len('.build')]
    return ''


def _iter_dependency_files(root):
    """Compiler-written dependency files under an Xcode or CMake intermediates tree."""
    for current, directories, names in os.walk(root):
        directories[:] = [d for d in directories
                          if d not in _SKIPPED_BUILD_DIRECTORIES and d != 'DerivedSources']
        for name in names:
            if name.endswith('.d'):
                yield os.path.join(current, name)


def _iter_object_files(root):
    """Object files a CMake target wrote, under `CMakeFiles/<Target>.dir/`.

    DerivedSources is *not* pruned here, unlike in the dependency-file walk: CMake puts a
    unified bundle's object at
    `CMakeFiles/<T>.dir/__/__/<Component>/DerivedSources/unified-sources/UnifiedSource-x.cpp.o`,
    so pruning it would hide exactly the objects that prove the bundles were compiled.
    """
    for current, directories, names in os.walk(root):
        directories[:] = [d for d in directories if d not in _SKIPPED_BUILD_DIRECTORIES]
        if not _CMAKE_TARGET_DIRECTORY.search(current + '/'):
            continue
        for name in names:
            if name.endswith('.o') or name.endswith('.obj'):
                yield os.path.join(current, name)


class BuildInventory:
    """Which files this build compiled, and into which targets."""

    def __init__(self, checkout_root, build_directory):
        self._checkout_root = checkout_root.rstrip('/')
        self._build_directory = build_directory.rstrip('/') if build_directory else None
        self.targets_by_file = {}       # relative source path -> set of target names
        self.dependency_file_count = 0
        self.bundle_count = 0
        self.object_file_count = 0
        self._scan()

    def _record(self, absolute_path, target):
        prefix = self._checkout_root + os.sep
        if not absolute_path.startswith(prefix):
            return
        relative = absolute_path[len(prefix):]
        # Checked-in code only. A dependency file also names the generated bundle it was
        # made from and every derived source in it, and those live under the build
        # directory, which on a default build is itself inside the checkout.
        if not relative.startswith(('Source/', 'Tools/')):
            return
        if not _is_implementation(relative):
            return
        self.targets_by_file.setdefault(relative, set()).add(target)

    def _intermediate_roots(self):
        """The directories holding the build record for *this* configuration.

        CMake keeps its intermediates inside the configuration directory. Xcode keeps them
        beside it, in `<output>/<Target>.build/<Configuration>/`: measured on the reference
        instrumented tree, every dependency file is under that shape and none is under
        `Release/` itself, so the sibling walk cannot simply be dropped.

        It used to walk `os.path.dirname(build_directory)` wholesale, which also swept in
        every *other* configuration built into the same output directory. On a CMake tree
        that is `WebKitBuild/cmake-mac`, where Debug/ and Release/ sit beside Coverage/, so a
        file that only an older configuration ever compiled could be reported as compiled
        here -- hiding exactly the absence this module exists to surface. Measured against
        the reference CMake tree, the old walk read 5,636 objects for a Release build that
        has 4,718 of its own; there the two configurations compile the same sources, so the
        file set was unchanged, but nothing about the old walk guaranteed that.

        Naming the Xcode shape keeps the sibling record without the sibling configurations.
        """
        roots = []
        if os.path.isdir(self._build_directory):
            roots.append(self._build_directory)
        parent = os.path.dirname(self._build_directory)
        configuration = os.path.basename(self._build_directory)
        if parent and configuration and os.path.isdir(parent):
            try:
                entries = sorted(os.listdir(parent))
            except OSError:
                entries = []
            for entry in entries:
                if not entry.endswith('.build'):
                    continue
                candidate = os.path.join(parent, entry, configuration)
                if os.path.isdir(candidate):
                    roots.append(candidate)
        return roots

    def _scan(self):
        if not self._build_directory:
            return
        roots = self._intermediate_roots()
        if not roots:
            return

        seen = set()
        for root in roots:
            for path in _iter_dependency_files(root):
                real = os.path.realpath(path)
                if real in seen:
                    continue
                seen.add(real)
                self.dependency_file_count += 1
                target = _target_of(path)
                try:
                    with open(path, 'rb') as handle:
                        data = handle.read()
                except OSError:
                    continue
                for token in data.split():
                    if token.startswith(b'/'):
                        self._record(token.decode('utf-8', 'replace'), target)

        self._scan_unified_source_bundles()
        self._scan_object_files(roots)

    def _scan_object_files(self, roots):
        """Every checked-in source a CMake target has an object file for.

        The dependency-file walk above is the whole record on the Xcode build and almost none
        of it on the CMake build, where ninja's rules pair `depfile` with `deps = gcc`: it
        folds each `.o.d` into the binary `.ninja_deps` and deletes the file. Measured on a
        complete CMake tree, 4,718 objects against five `.d` files, and all five are
        non-compiler artefacts this module's filters already reject. The objects themselves
        are the surviving evidence, and unlike compile_commands.json -- which only proves a
        translation unit was *configured* -- an object proves it was compiled.

        CMake derives an object's name from the source path relative to the target's source
        directory when the source is under it, and relative to its binary directory
        otherwise, with each `..` component written `__`. Both are tried and the one that
        exists wins; measured over the reference tree that resolves all 4,718 objects, 4,259
        to a checked-in source and 459 to a generated one, with none left over.
        """
        for root in roots:
            for path in _iter_object_files(root):
                match = _CMAKE_OBJECT_PATH.match(path.replace(os.sep, '/'))
                if not match:
                    continue
                binary_directory, target, inside = match.groups()
                source = _unmangle(inside)
                source_directory = os.path.join(
                    self._checkout_root, os.path.relpath(binary_directory, root))
                # Relative to the target's source directory, then to its binary directory.
                for candidate in (os.path.join(source_directory, source),
                                  os.path.join(binary_directory, source)):
                    candidate = os.path.normpath(candidate)
                    if os.path.exists(candidate):
                        self.object_file_count += 1
                        self._record(candidate, target)
                        break

    def _scan_unified_source_bundles(self):
        """The bundles name their members as `#include "<path>"`, relative to the framework.

        Redundant with the dependency files on the Xcode build -- measured to add zero files
        there -- but it is the part of the record that lives in the product directory rather
        than in the intermediates, so it survives their being pruned. On the CMake build it
        is not redundant at all: ninja's rules pair `depfile` with `deps = gcc`, so it folds
        every `.o.d` into the binary `.ninja_deps` and deletes it, leaving 4,718 objects
        against five `.d` files, none of them a compiler artefact.

        Two directory layouts, both real, both present on the same CMake tree:

          <build>/DerivedSources/<Name>/unified-sources     Xcode, and CMake's TestWebKit
          <build>/<Name>/DerivedSources/unified-sources     CMake's frameworks

        Only the first was recognised, so all 216 of the CMake tree's bundles were invisible
        and, with the depfiles gone too, both signals were zero and the report dropped its
        third state entirely.
        """
        for name, directory in sorted(self._bundle_directories()):
            source_directories = _FRAMEWORK_SOURCE_DIRECTORIES.get(name)
            if not source_directories:
                continue
            for entry in sorted(os.listdir(directory)):
                if not entry.startswith('UnifiedSource'):
                    continue
                self.bundle_count += 1
                try:
                    with open(os.path.join(directory, entry), errors='replace') as handle:
                        text = handle.read()
                except OSError:
                    continue
                for member in re.findall(r'^#include\s+"([^"]+)"', text, re.MULTILINE):
                    resolved = self._resolve_member(source_directories, member)
                    if resolved:
                        self._record(resolved, name)

    def _bundle_directories(self):
        """[(framework name, absolute unified-sources directory)] for both layouts."""
        found = []
        derived = os.path.join(self._build_directory, 'DerivedSources')
        if os.path.isdir(derived):
            for name in os.listdir(derived):
                directory = os.path.join(derived, name, 'unified-sources')
                if os.path.isdir(directory):
                    found.append((name, directory))
        try:
            entries = os.listdir(self._build_directory)
        except OSError:
            entries = []
        for name in entries:
            directory = os.path.join(self._build_directory, name, 'DerivedSources',
                                     'unified-sources')
            if os.path.isdir(directory):
                found.append((name, directory))
        return found

    def _resolve_member(self, source_directories, member):
        """A bundle member resolved against the first candidate directory that has it.

        The candidates are tried rather than fixed because the same framework name means a
        different directory on the two build systems: CMake's WebGPU bundle holds bare
        `Adapter.mm`, which is Source/WebGPU/WebGPU/Adapter.mm, and its PAL bundle holds
        `avfoundation/OutputContext.mm`, which is under Source/WebCore/PAL/pal. Requiring the
        file to exist is what keeps a wrong guess from being recorded as a compiled file.
        """
        for source_directory in source_directories:
            candidate = os.path.join(self._checkout_root, source_directory, member)
            if os.path.exists(candidate):
                return candidate
        return None

    @property
    def compiled(self):
        return frozenset(self.targets_by_file)

    def targets(self, relative_path):
        return frozenset(self.targets_by_file.get(relative_path, ()))


class BuildDescriptionIndex:
    """Which build descriptions name each source file, and which port each belongs to.

    A file's path is resolved against every ancestor directory of the description that
    names it, which is what makes basenames usable: Source/WebCore/Sources.txt listing
    `JSDOMWindow.cpp` means the *derived* one, and resolving against Source/WebCore rules
    out the unrelated bindings/scripts/test/JS/JSDOMWindow.cpp fixture that a basename
    match would have hit.
    """

    def __init__(self, checkout_root, known_files):
        self._checkout_root = checkout_root.rstrip('/')
        self._known = set(known_files)
        self.ports_by_file = {}      # relative path -> set of port names ('' for this port)
        self.tools_by_file = {}      # relative path -> set of descriptions that are tests
        self.descriptions_by_file = {}   # relative path -> set of descriptions naming it
        self.description_count = 0
        self._scan()

    @staticmethod
    def named_port(relative_description):
        """The port a description's *name* declares, '' for this port, None for neither."""
        name = os.path.basename(relative_description)
        if name in _THIS_PORT_DESCRIPTIONS:
            return ''
        match = _SOURCES_LIST_NAME.match(name) or _PLATFORM_CMAKE_NAME.match(name)
        if match and match.group(1):
            return _DESCRIPTION_PORTS.get(match.group(1), match.group(1))
        return None

    @staticmethod
    def port_from_location(relative_description):
        """The port a description's *directory* implies, as Source/WebKit/WPEPlatform does."""
        for component in relative_description.split('/')[:-1]:
            port = _PORT_COMPONENTS.get(component.lower())
            if port:
                return port
        return None

    @staticmethod
    def is_tool_description(relative_description):
        components = relative_description.split('/')
        if components[0] == 'Tools':
            return True
        return any(component.lower() in _FIXTURE_COMPONENTS for component in components[:-1])

    def _description_files(self):
        """Sources*.txt, CMakeLists.txt and every *.cmake under Source/.

        Not just Platform*.cmake: Source/WebCore/platform/ImageDecoders.cmake is the only
        thing in the checkout that names the eight non-Cocoa image decoders, and reading
        only Platform*.cmake reported them as being in no build description at all.
        """
        for current, directories, names in os.walk(os.path.join(self._checkout_root, 'Source')):
            directories[:] = [d for d in directories if d not in _SKIPPED_SOURCE_DIRECTORIES]
            for name in names:
                if name == 'CMakeLists.txt' or name.endswith('.cmake') \
                        or _SOURCES_LIST_NAME.match(name):
                    yield os.path.relpath(os.path.join(current, name), self._checkout_root)

    def _ports_by_description(self, descriptions):
        """Attribute each description to a port, using its siblings where its name is silent.

        Source/WebDriver has PlatformGTK.cmake, PlatformWPE.cmake, PlatformWin.cmake and
        PlatformPlayStation.cmake but no Cocoa list, so its unadorned CMakeLists.txt is a
        GTK/WPE/Windows/PlayStation list too, and its nine files belong to those ports
        rather than to no configuration in particular.
        """
        named = {}
        by_directory = {}
        for relative in descriptions:
            named[relative] = self.named_port(relative)
            by_directory.setdefault(os.path.dirname(relative), []).append(relative)

        ports = {}
        for relative in descriptions:
            port = named[relative]
            if port is not None:
                ports[relative] = {port}
                continue
            port = self.port_from_location(relative)
            if port:
                ports[relative] = {port}
                continue
            siblings = by_directory[os.path.dirname(relative)]
            sibling_ports = {named[s] for s in siblings if named[s]}
            if sibling_ports and not any(named[s] == '' for s in siblings):
                ports[relative] = sibling_ports
            else:
                ports[relative] = {''}
        return ports

    def _resolve(self, relative_description, token):
        """Resolve a token from a description to a known file, or None."""
        token = _CMAKE_VARIABLE.sub('', token).lstrip('/')
        if not token:
            return None
        directory = os.path.dirname(relative_description)
        while True:
            candidate = os.path.normpath(os.path.join(directory, token)) if directory else token
            if candidate in self._known:
                return candidate
            if not directory:
                return None
            directory = os.path.dirname(directory)

    def _scan(self):
        descriptions = [relative for relative in self._description_files()
                        if not relative.startswith('Source/ThirdParty/')]
        ports_by_description = self._ports_by_description(descriptions)
        for relative in descriptions:
            try:
                with open(os.path.join(self._checkout_root, relative), errors='replace') as handle:
                    text = handle.read()
            except OSError:
                continue
            self.description_count += 1
            ports = ports_by_description[relative]
            tool = self.is_tool_description(relative)
            for match in _DESCRIPTION_TOKEN.finditer(text):
                resolved = self._resolve(relative, match.group(0))
                if resolved is None:
                    continue
                self.ports_by_file.setdefault(resolved, set()).update(ports)
                self.descriptions_by_file.setdefault(resolved, set()).add(relative)
                if tool:
                    self.tools_by_file.setdefault(resolved, set()).add(relative)

    def ports(self, relative_path):
        return self.ports_by_file.get(relative_path)

    def descriptions(self, relative_path):
        return sorted(self.descriptions_by_file.get(relative_path, ()))

    def is_tool_only(self, relative_path):
        described = self.descriptions_by_file.get(relative_path)
        return bool(described) and described == self.tools_by_file.get(relative_path)


def _port_from_path(relative_path):
    for component in relative_path.split('/')[:-1]:
        port = _PORT_COMPONENTS.get(component.lower())
        if port:
            return port
    return None


def _is_fixture(relative_path):
    return any(component.lower() in _FIXTURE_COMPONENTS
               for component in relative_path.split('/')[:-1])


class AbsenceReport:
    """The third state: files the report cannot describe, with a reason for each."""

    def __init__(self):
        self.files = []                  # AbsentFile, in path order
        self.by_reason = {}              # reason -> [AbsentFile]
        self.by_directory = {}           # directory relative path -> [AbsentFile]
        self.reported_file_count = 0
        self.compiled_file_count = 0
        self.total_file_count = 0

    def add(self, absent):
        self.files.append(absent)
        self.by_reason.setdefault(absent.reason, []).append(absent)
        self.by_directory.setdefault(os.path.dirname(absent.path), []).append(absent)

    # Exposed so that a renderer can label a reason without importing this module.
    labels = REASON_LABELS
    explanations = REASON_EXPLANATIONS

    @property
    def absent_file_count(self):
        return len(self.files)

    @property
    def absent_physical_lines(self):
        return sum(absent.physical_lines for absent in self.files)

    def reasons(self):
        """[(reason, label, files, physical lines)], in REASON_ORDER, non-empty only."""
        rows = []
        for reason in REASON_ORDER:
            group = self.by_reason.get(reason)
            if group:
                rows.append((reason, REASON_LABELS[reason], len(group),
                             sum(absent.physical_lines for absent in group)))
        return rows

    def denominator_sentence(self):
        """The sentence that has to travel with any headline percentage from this report."""
        if not self.total_file_count:
            return ''
        return ('Coverage percentages cover the {:,} of {:,} first-party implementation '
                'files that this configuration compiles into a reported binary. The other '
                '{:,} ({:.1f}%, {:,} physical lines) are listed as not built rather than '
                'counted at 0%, because a file with no coverage mapping has no measurable '
                'denominator.'.format(
                    self.reported_file_count, self.total_file_count, self.absent_file_count,
                    100.0 * self.absent_file_count / self.total_file_count,
                    self.absent_physical_lines))


def find_absent_files(checkout_root, build_directory, reported_paths, third_party_regexes=()):
    """Classify every first-party implementation file the report says nothing about.

    reported_paths is the set of absolute paths the report does cover, i.e. the SF: records
    of the lcov trace after canonicalization. third_party_regexes is the report's own
    exclusion list, so that a file absent because the report filtered it is labelled as
    filtered rather than as missing.
    """
    checkout_root = checkout_root.rstrip('/')
    report = AbsenceReport()

    reported = set()
    prefix = checkout_root + os.sep
    for path in reported_paths:
        if path.startswith(prefix):
            reported.add(path[len(prefix):])

    universe = enumerate_source_files(checkout_root)
    report.total_file_count = len(universe)

    inventory = BuildInventory(checkout_root, build_directory)
    logger.info('Read %d compiler dependency files, %d unified-source bundles and %d object '
                'files from %s: %d first-party implementation files were compiled',
                inventory.dependency_file_count, inventory.bundle_count,
                inventory.object_file_count, build_directory,
                len(inventory.compiled & set(universe)))

    filters = [re.compile(pattern) for pattern in third_party_regexes]

    def filtered_out(relative_path):
        return any(pattern.search(relative_path) for pattern in filters) \
            or any(needle in '/' + relative_path for needle in _VENDORED_THIRD_PARTY)

    descriptions = BuildDescriptionIndex(checkout_root, universe)

    for relative in sorted(universe):
        if relative in reported:
            report.reported_file_count += 1
            continue
        targets = inventory.targets(relative)
        reason, detail = _classify(checkout_root, relative, targets, descriptions, filtered_out)
        report.add(AbsentFile(relative, reason, detail,
                              physical_line_count(os.path.join(checkout_root, relative))))

    report.compiled_file_count = len(inventory.compiled & set(universe))
    return report


def _classify(checkout_root, relative, targets, descriptions, filtered_out):
    """(reason, detail) for one absent file. The order of these tests is the point."""
    if filtered_out(relative):
        return 'third-party', ''

    if targets:
        # It compiled. Either its coverage went into a binary the report does not read, or
        # the preprocessor removed the whole body, or there is genuinely nothing to count.
        if not targets & REPORTED_TARGETS:
            return 'test-or-tool-target', ', '.join(sorted(targets))
        condition = whole_file_conditional(os.path.join(checkout_root, relative))
        if condition:
            return 'feature-flag-off', condition
        return 'no-executable-code', ', '.join(sorted(targets))

    # It did not compile. A build description that names it is better evidence than the
    # directory it sits in, which is why the descriptions are consulted first.
    ports = descriptions.ports(relative)
    if ports and '' not in ports:
        return 'other-port', ', '.join(sorted(ports))
    if _is_fixture(relative):
        return 'fixture', ''
    if ports and descriptions.is_tool_only(relative):
        return 'test-or-tool-target', ''
    port = _port_from_path(relative)
    if port:
        return 'other-port', port
    condition = whole_file_conditional(os.path.join(checkout_root, relative))
    if condition:
        return 'feature-flag-off', condition
    if ports:
        return 'not-in-this-configuration', ', '.join(descriptions.descriptions(relative)[:2])
    return 'no-build-description', ''

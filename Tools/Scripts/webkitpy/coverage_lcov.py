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

"""Canonicalize copied header paths in lcov coverage data, and open and parse lcov traces.

A header copied into the build directory is recorded under two paths; the highest count per line wins."""

import gzip
import json
import logging
import os
import re
import tempfile
from collections import defaultdict, namedtuple

logger = logging.getLogger(__name__)

# (location, candidate source directories in preference order). bmalloc's copy phase flattens two
# source directories into one location, so canonicalize() takes the first candidate that exists.
_INSTALLED_HEADER_RULES = (
    ('/usr/local/include/wtf/', ('Source/WTF/wtf/',)),
    ('/usr/local/include/bmalloc/', ('Source/bmalloc/bmalloc/',
                                     'Source/bmalloc/libpas/src/libpas/')),
    ('/usr/local/include/pas/', ('Source/bmalloc/libpas/src/libpas/',)),
    # PAL is built into WebCore but installs its headers as a project of its own.
    ('/usr/local/include/pal/', ('Source/WebCore/PAL/pal/',)),
    # The CMake build stages headers under <build>/<project>/Headers/<subdir>/.
    ('/WTF/Headers/wtf/', ('Source/WTF/wtf/',)),
    ('/bmalloc/Headers/bmalloc/', ('Source/bmalloc/bmalloc/',
                                   'Source/bmalloc/libpas/src/libpas/')),
    ('/PAL/Headers/pal/', ('Source/WebCore/PAL/pal/',)),
    ('/WebGPU/Headers/WebGPU/', ('Source/WebGPU/WebGPU/',)),
)


def staged_equivalents_for_scope(scope, checkout_root, build_directory=None):
    """Absolute, existing staged header paths that canonicalize back into `scope`, for --sources.
    llvm-cov filters on the staged copies, and a filter that matches nothing reports everything."""
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


# Copied framework headers: no derivable source subdirectory, so resolved by unique basename.
_FRAMEWORK_HEADER_PATTERN = re.compile(
    r'/(?P<framework>[A-Za-z]+)\.framework/(?:Versions/[^/]+/)?(?:PrivateHeaders|Headers)/(?P<rest>.+)$')

_FRAMEWORK_SOURCE_DIRECTORY = {
    'WebCore': 'Source/WebCore',
    'JavaScriptCore': 'Source/JavaScriptCore',
    'WebKit': 'Source/WebKit',
    'WebKitLegacy': 'Source/WebKitLegacy',
    'WebGPU': 'Source/WebGPU',
}

# Directories a copied macOS framework header cannot come from; breaks basename ties.
# PAL is here because it installs to usr/local/include/pal, so it is never a framework header's source.
_NON_COCOA_SOURCE_DIRECTORIES = frozenset((
    'PAL', 'adwaita', 'curl', 'glib', 'gtk', 'haiku', 'playstation', 'soup', 'unix',
    'wayland', 'win', 'wpe', 'x11',
))

# Generator-fixture and API-test directories: a copied framework header is never one of these.
# 'testing' is absent on purpose: Source/WebCore/testing is product code.
_FIXTURE_SOURCE_DIRECTORIES = frozenset(('test', 'tests'))


def _prefer_cocoa(paths):
    """The one candidate a macOS build could have compiled, or None if still ambiguous.
    Narrows candidates rather than pruning the walk, which would lose basenames unique to a directory."""
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
    """(source marker, install prefixes) pairs, derived from _INSTALLED_HEADER_RULES to avoid drift."""
    inverted = {}
    for location, candidates in _INSTALLED_HEADER_RULES:
        for candidate in candidates:
            inverted.setdefault('/' + candidate, []).append(location.lstrip('/'))
    return tuple((marker, tuple(prefixes)) for marker, prefixes in inverted.items())


_INSTALLED_COPY_RULES = _installed_copy_rules()


def compiled_copy_candidates(path, build_directory):
    """Where the build directory holds its own copy of path's text, in preference order.
    That copy is what the records describe if the checkout was edited since; callers must verify."""
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


# Copied-header directories under <build>/usr/local/include that are WebKit's own. An allow-list,
# so a new third-party project is excluded by default.
FIRST_PARTY_COPIED_HEADER_NAMES = frozenset((
    'wtf', 'bmalloc', 'pas', 'pal', 'WebKitAdditions', 'WebCoreTestSupport', 'WGSL.h',
))


# The same allow-list for the CMake layout, keyed by project directory rather than include
# subdirectory name (PAL/Headers/pal, not pal/pal).
FIRST_PARTY_COPIED_HEADER_PROJECTS = frozenset((
    'WTF', 'bmalloc', 'PAL', 'WebKitAdditions', 'WebCore', 'JavaScriptCore', 'WebKit',
    'WebKitLegacy', 'WebGPU', 'WebCoreTestSupport', 'WGSL',
))

_COPIED_HEADER_DIRECTORY_NAMES = ('Headers', 'PrivateHeaders')


def third_party_copied_header_ignore_regexes(build_directory):
    """--ignore-filename-regex arguments for the third-party headers a build copied.
    Derived from the build directory, for both the usr/local/include and CMake <project>/Headers layouts."""
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
        # A dot means a bundle (WebCore.framework); _FRAMEWORK_HEADER_PATTERN already places its headers.
        if project.startswith('.') or '.' in project:
            continue
        if project in FIRST_PARTY_COPIED_HEADER_PROJECTS:
            continue
        for kind in _COPIED_HEADER_DIRECTORY_NAMES:
            if os.path.isdir(os.path.join(build_directory, project, kind)):
                regexes.append('/{}/{}/'.format(project, kind))
    return regexes


class PathCanonicalizer:
    # Why a path is still under the build directory: deliberate outcomes, not failures.
    WEBKIT_ADDITIONS = ('copied from an additional source tree that is not part of the open-source '
                        'checkout, so the copy is the only path there is')
    COPIED_FRAMEWORK_HEADER = ('a copied framework header with no unique source match; '
                               'almost all of them are generated by the build, which is '
                               'where the copy came from')
    # Written by the build itself (generated or configured), not copied.
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
            # Nothing exists on disk: name the first candidate anyway, not the build path.
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
        Asks the filesystem rather than matching patterns; the generators have nothing in common."""
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
        # Mangled name -> count, for coverage_delta (names survive a source edit).
        # Not what totals() counts; see function_lines.
        self.functions = {}
        # FN: start line -> highest count of any function starting there. llvm-cov merges template
        # instantiations and lcov does not, so keying by name would inflate the denominator.
        self.function_lines = {}
        self.branches = {}   # (line, block, branch) -> taken count

    def merge(self, other):
        # A line covered by any translation unit is covered, so take the maximum; summing would
        # inflate headers shared between frameworks.
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
        Called when the record ends, so it does not depend on every FN: preceding its FNDA:."""
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
    """{metric: (count, covered)} over a parsed, canonicalized, duplicate-unioned trace.
    This is the number the report displays, so gates must use it; llvm-cov's own totals differ."""
    # Seeded from FileCoverage so an empty trace still answers for every metric.
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
    Only colliding records are accumulated; the rest are copied through byte-for-byte."""
    __slots__ = ('lines', 'function_starts', 'function_counts', 'branches')

    def __init__(self):
        self.lines = {}            # line -> highest count
        self.function_starts = {}  # mangled name -> FN: start line
        self.function_counts = {}  # mangled name -> highest FNDA: count
        self.branches = {}         # (line, block, branch) -> highest taken

    def add(self, line):
        # Union by maximum, as FileCoverage.merge() does, so a rewritten trace and a report agree.
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
    """Rewrite an lcov trace in place with canonical paths and return a CanonicalizedTrace.
    Only colliding records are unioned; FileCoverage cannot re-emit FN: pairings, so others pass through."""
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
    """Open an lcov trace, decompressing by gzip magic bytes, not extension (CI renames traces)."""
    with open(lcov_path, 'rb') as probe:
        magic = probe.read(2)
    if magic == b'\x1f\x8b':
        return gzip.open(lcov_path, 'rt', encoding='utf-8', errors='replace')
    return open(lcov_path, 'r', encoding='utf-8', errors='replace')


def parse_lcov_source_files(lcov_path, canonicalizer=None):
    """The set of canonical paths an lcov trace has records for; much cheaper than parse_lcov."""
    paths = set()
    with open_lcov(lcov_path) as handle:
        for line in handle:
            if line.startswith('SF:'):
                path = line[3:].rstrip('\n')
                paths.add(canonicalizer.canonicalize(path) if canonicalizer else path)
    return paths


_REGION_FILE = re.compile(r'"filename":"((?:[^"\\]|\\.)*)"')
_REGION_SEGMENT = re.compile(r'\[(\d+),(\d+),(\d+),(true|false),(true|false),(true|false)\]')


def uncovered_regions(export_path, canonicalizer=None, covered_lines=None):
    """{path: {line: [(start column, end column or None)]}} of never-run regions in llvm-cov's JSON export.
    Scanned, not json.load()ed (millions of segments); llvm::json sorts keys, so "filename" precedes "segments"."""
    with open(export_path, encoding='utf-8', errors='replace') as handle:
        text = handle.read()
    regions = {}
    position = 0
    while True:
        found = _REGION_FILE.search(text, position)
        if not found:
            break
        path = json.loads('"{}"'.format(found.group(1)))
        if canonicalizer:
            path = canonicalizer.canonicalize(path)
        start = text.find('"segments":[', found.end())
        next_file = _REGION_FILE.search(text, found.end())
        if start == -1 or (next_file and start > next_file.start()):
            position = found.end()
            continue
        end = text.find(']]', start)
        end = len(text) if end == -1 else end + 2
        position = end
        segments = [(int(line), int(column), int(count), has_count == 'true', gap == 'true')
                    for line, column, count, has_count, _, gap
                    in _REGION_SEGMENT.findall(text, start, end)]
        ranges = _uncovered_ranges(segments)
        lines = covered_lines.get(path) if covered_lines is not None else None
        if lines is not None:
            ranges = {line: spans for line, spans in ranges.items() if lines.get(line)}
        if path in regions:
            previous = regions[path]
            regions[path] = {line: [span for span in spans if span in set(previous.get(line, ()))]
                             for line, spans in ranges.items() if line in previous}
            regions[path] = {line: spans for line, spans in regions[path].items() if spans}
        elif ranges:
            regions[path] = ranges
    return {path: ranges for path, ranges in regions.items() if ranges}


def _uncovered_ranges(segments):
    """Per-line column ranges covered by zero-count, non-gap segments, each up to the next."""
    ranges = defaultdict(list)
    for index, (line, column, count, has_count, gap) in enumerate(segments):
        if not has_count or gap or count or index + 1 == len(segments):
            continue
        next_line, next_column = segments[index + 1][:2]
        if next_line == line:
            if next_column > column:
                ranges[line].append((column, next_column))
            continue
        ranges[line].append((column, None))
        for middle in range(line + 1, next_line):
            ranges[middle].append((1, None))
        if next_column > 1:
            ranges[next_line].append((1, next_column))
    return dict(ranges)


def parse_lcov(lcov_path, canonicalizer=None, lines_only=False, keep_function_names=True):
    """Parse an lcov trace into {canonical path: FileCoverage}, unioning duplicates.
    lines_only skips function and branch records; keep_function_names=False drops names to save memory."""
    files = {}
    coverage = None
    path = None
    # {mangled name: FN: start line} for the current record; pending holds an FNDA: count that
    # arrived before its FN:.
    starts = {}
    pending = {}
    with open_lcov(lcov_path) as handle:
        for line in handle:
            # Dispatch once on the first three characters, most frequent record first.
            tag = line[:3]
            if tag == 'DA:':
                if coverage is not None:
                    number, _, count = line[3:].partition(',')
                    try:
                        coverage.lines[int(number)] = int(count)
                    except ValueError:
                        pass
            elif tag == 'SF:':
                path = line[3:].rstrip('\n')
                if canonicalizer:
                    path = canonicalizer.canonicalize(path)
                coverage = FileCoverage()
                starts = {}
                pending = {}
            elif tag == 'end':
                if coverage is None or not line.startswith('end_of_record'):
                    continue
                if keep_function_names:
                    coverage.fold_function_lines(starts)
                else:
                    function_lines = coverage.function_lines
                    for name, count in pending.items():
                        start = starts.get(name)
                        if start is not None and count > function_lines.get(start, -1):
                            function_lines[start] = count
                if path in files:
                    files[path].merge(coverage)
                else:
                    files[path] = coverage
                coverage = None
            elif coverage is None or lines_only:
                continue
            elif tag == 'FND':
                count, _, name = line[5:].rstrip('\n').partition(',')
                try:
                    count = int(count)
                except ValueError:
                    continue
                if keep_function_names:
                    coverage.functions[name] = count
                    continue
                start = starts.get(name)
                if start is None:
                    if count > pending.get(name, -1):
                        pending[name] = count
                elif count > coverage.function_lines.get(start, -1):
                    coverage.function_lines[start] = count
            elif tag == 'FN:':
                number, _, name = line[3:].rstrip('\n').partition(',')
                if keep_function_names:
                    coverage.functions.setdefault(name, 0)
                try:
                    number = int(number)
                except ValueError:
                    continue
                starts[name] = number
                if not keep_function_names:
                    coverage.function_lines.setdefault(number, 0)
            elif tag == 'BRD':
                parts = line[5:].rstrip('\n').split(',')
                if len(parts) == 4:
                    taken = 0 if parts[3] == '-' else int(parts[3] or 0)
                    coverage.branches[(parts[0], parts[1], parts[2])] = taken
    return files

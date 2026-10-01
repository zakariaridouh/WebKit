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

"""Per-test coverage attribution: which tests execute a line, and which lines a test reaches.

The index is scoped: a file outside a test's --sources scope is "not measured", not "not covered".
"""

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from collections import namedtuple

from webkitpy.coverage_delta import line_ranges
from webkitpy.coverage_lcov import parse_lcov
# The state lives in an import-free module so port/driver.py can read it without importing this one.
from webkitpy.coverage_profile_environment import set_current_profile_environment
from webkitpy.llvm_profile_utils import (
    COVERAGE_PROFILE_DIRECTORY, CoverageLLVMProfDataExecutable, LLVMCovExecutable, LLVMProfileData)

logger = logging.getLogger(__name__)

# Must stay under the sandbox-writable /private/tmp/WebKitCoverage subpath, or child profile
# writes are denied and their counters silently lost.
PER_TEST_PROFILE_ROOT = os.path.join(COVERAGE_PROFILE_DIRECTORY, 'per-test')

# One file per image (%1m, not the baked-in %8m): continuous mode preallocates every file.
PER_TEST_PROFILE_PATTERN = '%1m%c.profraw'

# __XPC_-prefixed copies are what libxpc forwards to the WebContent, GPU and Networking services.
PROFILE_ENVIRONMENT_VARIABLES = ('LLVM_PROFILE_FILE', '__XPC_LLVM_PROFILE_FILE')

INDEX_FORMAT = 'webkit-per-test-coverage'
INDEX_FORMAT_VERSION = 1

SHARD_SUFFIX = '.jsonl'

# The digest keeps `fast/dom/a-b.html` and `fast/dom/a_b.html` apart once sanitized.
_UNSAFE_IN_A_FILENAME = re.compile(r'[^A-Za-z0-9._-]+')
_SLUG_NAME_LIMIT = 96


def slug_for_test_name(test_name):
    flattened = _UNSAFE_IN_A_FILENAME.sub('_', test_name).strip('_') or 'test'
    digest = hashlib.sha1(test_name.encode('utf-8')).hexdigest()[:12]
    return '{}-{}'.format(flattened[:_SLUG_NAME_LIMIT], digest)


def per_test_profile_directory(test_name, root=PER_TEST_PROFILE_ROOT):
    return os.path.join(root, slug_for_test_name(test_name))


def profile_environment(directory):
    path = os.path.join(directory, PER_TEST_PROFILE_PATTERN)
    return {name: path for name in PROFILE_ENVIRONMENT_VARIABLES}


StalePerTestProfiles = namedtuple('StalePerTestProfiles', ('count', 'total_bytes'))


def prepare_per_test_profile_root(root=PER_TEST_PROFILE_ROOT):
    """Remove per-test profile directories an interrupted run left behind.

    Call after prepare_coverage_profile_directory(), which takes the machine-global lock.
    """
    if not os.path.isdir(root):
        return StalePerTestProfiles(0, 0)
    count, total_bytes = 0, 0
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        for current, _, files in os.walk(path):
            for filename in files:
                try:
                    total_bytes += os.path.getsize(os.path.join(current, filename))
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)
        count += 1
    if count:
        logger.info('Removed %d stale per-test profile directory/ies (%d MB) from %s',
                    count, total_bytes // (1024 * 1024), root)
    return StalePerTestProfiles(count, total_bytes)


def resolve_sources(paths, source_root):
    """(absolute paths for --sources, the ones that do not exist).

    llvm-cov resolves a relative --sources against its own working directory.
    """
    resolved, missing = [], []
    for scope in paths:
        if os.path.isabs(scope):
            path = os.path.normpath(scope)
        elif os.path.exists(scope):
            path = os.path.abspath(scope)
        else:
            path = os.path.normpath(os.path.join(source_root, scope))
        if not os.path.exists(path):
            missing.append(path)
        resolved.append(path)
    return resolved, missing


def instrumented_objects(build_path, extra_objects=(), products=None):
    """The instrumented Mach-Os to reduce a per-test profile against; products limits by name."""
    from webkitpy.coverage_requirements import INSTRUMENTED_PRODUCTS, product_name

    selected = list(INSTRUMENTED_PRODUCTS) if products is None else [
        entry for entry in INSTRUMENTED_PRODUCTS if product_name(entry) in products]
    objects, missing = [], []
    for relative in selected:
        path = os.path.join(build_path, *relative.split('/'))
        if os.path.exists(path):
            objects.append(path)
        else:
            missing.append(relative)
    if missing:
        # A framework that is not passed is absent, not 0%: its lines look unreached.
        logger.warning('Not found in %s, so no test will be attributed any of their lines: %s',
                       build_path, ', '.join(missing))
    for path in extra_objects:
        if path and path not in objects:
            objects.append(path)
    return objects


def encode_lines(numbers):
    """{1, 2, 3, 7} -> '1-3,7'. The stored form of a covered-line set."""
    return ','.join('{}'.format(low) if low == high else '{}-{}'.format(low, high)
                    for low, high in line_ranges(sorted(numbers)))


def decode_lines(text):
    """'1-3,7' -> {1, 2, 3, 7}. Tolerant of an empty string, which is a file with no lines."""
    numbers = set()
    for piece in text.split(','):
        piece = piece.strip()
        if not piece:
            continue
        low, _, high = piece.partition('-')
        try:
            first = int(low)
            last = int(high) if high else first
        except ValueError:
            logger.warning('Ignoring an unreadable line range %r', piece)
            continue
        numbers.update(range(first, last + 1))
    return numbers


def relative_source_path(path, source_root):
    """A source path as the index stores it: relative to the checkout when under it."""
    if not source_root:
        return path
    root = source_root.rstrip('/')
    if path == root:
        return path
    if path.startswith(root + '/'):
        return path[len(root) + 1:]
    return path


def scope_includes(relative_path, scope_prefixes):
    """Whether a source path is inside a declared --sources scope. An empty scope includes all."""
    if not scope_prefixes:
        return True
    for prefix in scope_prefixes:
        normalized = prefix.rstrip('/')
        if relative_path == normalized or relative_path.startswith(normalized + '/'):
            return True
    return False


ReducedProfile = namedtuple('ReducedProfile',
                            ('files', 'raw_bytes', 'profdata_bytes', 'seconds'))


def _export_arguments(objects, profile_path, sources, ignore_regexes=()):
    first, *rest = objects
    arguments = ['export', first, *['-object={}'.format(path) for path in rest],
                 '-instr-profile={}'.format(profile_path), '--format=lcov',
                 # Function records are one per template instantiation and dominate the export.
                 '--skip-functions', '--skip-branches', '--skip-expansions']
    for regex in ignore_regexes:
        arguments.append('--ignore-filename-regex={}'.format(regex))
    # --sources must be last, as the flag followed by one argument per path; --sources=PATH is
    # ignored. See llvm_profile_utils.LLVMCov._sources_arguments().
    if sources:
        arguments += ['--sources', *sources]
    return arguments


def reduce_raw_profiles(profile_directory, objects, sources, source_root, canonicalizer=None,
                        ignore_regexes=(), profdata_path=None):
    """Index one test's raw profiles and reduce them to its covered lines. -> ReducedProfile

    Only executed lines are kept, so an uninstrumented line looks like one no test reached.
    """
    started = time.time()
    raw_profiles = sorted(glob.glob(os.path.join(profile_directory, '*.profraw')))
    if not raw_profiles:
        return ReducedProfile({}, 0, 0, time.time() - started)
    raw_bytes = 0
    for path in raw_profiles:
        try:
            raw_bytes += os.path.getsize(path)
        except OSError:
            pass

    with tempfile.TemporaryDirectory(prefix='per-test-coverage-') as scratch:
        indexed = profdata_path or os.path.join(scratch, 'test.profdata')
        # --failure-mode=all: the harness hard-kills drivers, so one bad profile must not lose it.
        merge = LLVMProfileData.merge(indexed, unweighted_profiles=raw_profiles,
                                      failure_mode='all', num_threads=0,
                                      executable=CoverageLLVMProfDataExecutable)
        if merge.returncode:
            logger.warning('Could not index the raw profiles in %s: %s',
                           profile_directory, (merge.stderr or '').strip())
            return ReducedProfile({}, raw_bytes, 0, time.time() - started)
        profdata_bytes = os.path.getsize(indexed) if os.path.exists(indexed) else 0

        trace = os.path.join(scratch, 'test.lcov')
        with open(trace, 'w') as handle:
            export = LLVMCovExecutable.run(
                _export_arguments(objects, indexed, sources, ignore_regexes),
                stdout=handle, stderr=subprocess.PIPE, text=True)
        if export.returncode:
            logger.warning('Could not export coverage for %s: %s', profile_directory,
                           (export.stderr or '').strip())
            return ReducedProfile({}, raw_bytes, profdata_bytes, time.time() - started)

        files = {}
        for path, coverage in parse_lcov(trace, canonicalizer, lines_only=True).items():
            covered = {number for number, count in coverage.lines.items() if count}
            if not covered:
                continue
            relative = relative_source_path(path, source_root)
            files.setdefault(relative, set()).update(covered)

    return ReducedProfile(files, raw_bytes, profdata_bytes, time.time() - started)


def accumulate_profdata(accumulated_path, addition_path):
    """Fold one test's indexed profile into the run's, in place. Returns its size in bytes."""
    if not os.path.exists(addition_path):
        return 0
    inputs = [addition_path]
    if os.path.exists(accumulated_path):
        inputs.insert(0, accumulated_path)
    directory = os.path.dirname(accumulated_path) or '.'
    os.makedirs(directory, exist_ok=True)
    partial = accumulated_path + '.partial'
    merge = LLVMProfileData.merge(partial, unweighted_profiles=inputs, failure_mode='all',
                                  num_threads=0, executable=CoverageLLVMProfDataExecutable)
    if merge.returncode:
        logger.warning('Could not accumulate %s into %s: %s', addition_path, accumulated_path,
                       (merge.stderr or '').strip())
        if os.path.exists(partial):
            os.unlink(partial)
        return os.path.getsize(accumulated_path) if os.path.exists(accumulated_path) else 0
    os.replace(partial, accumulated_path)
    return os.path.getsize(accumulated_path)


class TestCoverage:
    """One test's covered lines, and the scope they were measured over.

    The scope is per record because runs with different --sources can append to one index.
    """
    __slots__ = ('test', 'suite', 'files', 'sources', 'seconds', 'raw_bytes', 'shard')

    def __init__(self, test, files, suite=None, sources=(), seconds=None, raw_bytes=None,
                 shard=None):
        self.test = test
        self.suite = suite
        self.files = files
        self.sources = tuple(sources)
        self.seconds = seconds
        self.raw_bytes = raw_bytes
        self.shard = shard

    @property
    def line_count(self):
        return sum(len(lines) for lines in self.files.values())

    def covers(self, relative_path, line):
        return line in self.files.get(relative_path, ())

    def measured(self, relative_path):
        """Whether this test's export could have said anything about a file at all."""
        return scope_includes(relative_path, self.sources)

    def record(self):
        return {
            'record': 'test',
            'test': self.test,
            'suite': self.suite,
            'seconds': None if self.seconds is None else round(self.seconds, 3),
            'raw_bytes': self.raw_bytes,
            'files': {path: encode_lines(lines) for path, lines in sorted(self.files.items())},
        }


class PerTestIndexWriter:
    """One shard of the index: a JSON-lines file whose first line describes the run.

    One file per shard: concurrent appends longer than PIPE_BUF to one file can interleave.
    """

    def __init__(self, directory, shard, suite=None, sources=(), source_root=None, objects=()):
        self.directory = directory
        self.shard = shard
        self.suite = suite
        self.sources = tuple(relative_source_path(path, source_root) for path in sources)
        self.source_root = source_root
        self.objects = tuple(objects)
        self.count = 0
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, slug_for_test_name(shard) + SHARD_SUFFIX)
        self._handle = open(self.path, 'w')
        self._write({
            'record': 'shard',
            'format': INDEX_FORMAT,
            'version': INDEX_FORMAT_VERSION,
            'shard': shard,
            'suite': suite,
            'sources': list(self.sources),
            'source_root': source_root,
            'objects': list(self.objects),
            'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        })

    def _write(self, record):
        self._handle.write(json.dumps(record, sort_keys=True) + '\n')
        # Flushed per record so that a killed run keeps what it measured.
        self._handle.flush()

    def add(self, coverage):
        self._write(coverage.record())
        self.count += 1

    def close(self):
        if self._handle:
            self._handle.close()
            self._handle = None


class PerTestCollector:
    """The harness's side: give a test a profile directory, then reduce it and throw it away.

    Call begin() before the test's processes start and finish() after they have exited.
    """

    def __init__(self, index_directory, shard, objects, sources, source_root, suite=None,
                 profile_root=PER_TEST_PROFILE_ROOT, accumulated_profdata=None,
                 canonicalizer=None, ignore_regexes=(), staged_sources=()):
        self.objects = list(objects)
        self.sources = list(sources)
        # export_sources also names staged header copies; the index records only the declared scope.
        self.export_sources = list(sources) + [path for path in staged_sources
                                               if path not in sources]
        self.source_root = source_root
        self.profile_root = profile_root
        self.accumulated_profdata = accumulated_profdata
        self.canonicalizer = canonicalizer
        self.ignore_regexes = tuple(ignore_regexes)
        self.suite = suite
        self.reduction_seconds = 0.0
        self.raw_bytes_discarded = 0
        self.writer = PerTestIndexWriter(index_directory, shard, suite=suite, sources=sources,
                                         source_root=source_root, objects=objects)

    def begin(self, test_name):
        """Make this test's profile directory and return the environment that points at it."""
        directory = per_test_profile_directory(test_name, self.profile_root)
        shutil.rmtree(directory, ignore_errors=True)
        os.makedirs(directory, exist_ok=True)
        environment = profile_environment(directory)
        set_current_profile_environment(environment)
        return environment

    def finish(self, test_name, extra_objects=()):
        """Reduce this test's profiles, record it, delete the raws. -> TestCoverage or None

        Call after its processes have exited: a live driver still writes its mmapped counter file.
        """
        set_current_profile_environment({})
        directory = per_test_profile_directory(test_name, self.profile_root)
        objects = self.objects + [path for path in extra_objects
                                  if path and path not in self.objects]
        try:
            with tempfile.TemporaryDirectory(prefix='per-test-profdata-') as scratch:
                profdata = os.path.join(scratch, 'test.profdata')
                reduced = reduce_raw_profiles(
                    directory, objects, self.export_sources, self.source_root,
                    canonicalizer=self.canonicalizer, ignore_regexes=self.ignore_regexes,
                    profdata_path=profdata)
                if reduced.raw_bytes and self.accumulated_profdata:
                    try:
                        accumulate_profdata(self.accumulated_profdata, profdata)
                    except OSError as failure:
                        # Likely a full disk. The whole-run profile is now incomplete, but this
                        # must not end the run.
                        logger.error('Could not fold %s into %s: %s', test_name,
                                     self.accumulated_profdata, failure)
        finally:
            # Whatever happened, the raw profiles must not outlive the test.
            shutil.rmtree(directory, ignore_errors=True)

        self.reduction_seconds += reduced.seconds
        self.raw_bytes_discarded += reduced.raw_bytes
        if not reduced.raw_bytes:
            logger.debug('%s wrote no raw profile, so there is nothing to attribute to it',
                         test_name)
            return None
        coverage = TestCoverage(test_name, reduced.files, suite=self.suite, sources=self.sources,
                                seconds=reduced.seconds, raw_bytes=reduced.raw_bytes,
                                shard=self.writer.shard)
        try:
            self.writer.add(coverage)
        except OSError as failure:
            # Losing this record must not end the run.
            logger.error('Could not record %s in %s: %s', test_name, self.writer.path, failure)
        logger.debug('%s: %d covered lines in %d files, reduced %d MB in %.1f s', test_name,
                     coverage.line_count, len(coverage.files),
                     reduced.raw_bytes // (1024 * 1024), reduced.seconds)
        return coverage

    def close(self):
        self.writer.close()


PER_TEST_INDEX_DIRECTORY_NAME = 'per-test'
PER_TEST_RUN_PROFDATA_NAME = 'per-test-run.profdata'


def prepare_per_test_options(options, source_root):
    """Fill in and check the --per-test-coverage options in place. Raises ValueError."""
    if not options.coverage:
        raise ValueError(
            '--per-test-coverage needs --coverage: it collects the same profiles, one test at a '
            'time, and --coverage is what claims the machine-global profile directory and '
            'checks that --coverage-dir can be written to.')
    if not options.per_test_coverage_sources:
        raise ValueError(
            '--per-test-coverage needs at least one --per-test-coverage-sources=PATH. Every test '
            'is reduced with an llvm-cov export, and unscoped that is slow and large for every test '
            'against the five frameworks -- the scope is what makes a per-test run finish and '
            'what keeps each record to kilobytes. Name the directory the question is about, for '
            'example --per-test-coverage-sources=Source/WTF.')

    resolved, missing = resolve_sources(options.per_test_coverage_sources, source_root)
    if missing:
        raise ValueError(
            '--per-test-coverage-sources named {}, which {} exist. llvm-cov matches --sources '
            'against the paths in the coverage mapping, so a path that is not there scopes the '
            'export to nothing and every test would be recorded as covering nothing.'.format(
                ', '.join(missing), 'does not' if len(missing) == 1 else 'do not'))
    options.per_test_coverage_sources = resolved

    if not options.per_test_coverage_index:
        options.per_test_coverage_index = os.path.join(options.coverage_dir,
                                                       PER_TEST_INDEX_DIRECTORY_NAME)
    if getattr(options, 'per_test_coverage_profdata', None) is None:
        options.per_test_coverage_profdata = os.path.join(options.coverage_dir,
                                                          PER_TEST_RUN_PROFDATA_NAME)
    elif options.per_test_coverage_profdata is False:
        # --no-per-test-coverage-profdata
        options.per_test_coverage_profdata = None
    os.makedirs(options.per_test_coverage_index, exist_ok=True)
    return options


def summarize_per_test_run(index_directory, accumulated_profdata=None,
                           profile_directory=COVERAGE_PROFILE_DIRECTORY):
    """Say what a per-test run produced, at the end of the run. Never raises."""
    try:
        index = PerTestIndex.read(index_directory)
        statistics = index.statistics()
        logger.info('Per-test coverage: %d test(s) over %d file(s), %d covered (file, line) '
                    'pair(s) in %s', statistics.test_count, statistics.file_count,
                    statistics.line_count, index_directory)
        logger.info('Ask it: Tools/Scripts/coverage-attribution --index=%s --covers PATH:LINE',
                    index_directory)
    except OSError as failure:
        logger.warning('Per-test coverage wrote no index into %s (%s)', index_directory, failure)

    if accumulated_profdata and os.path.exists(accumulated_profdata):
        logger.info('The run\'s counters were also merged incrementally into %s (%d MB). Report '
                    'from it with generate-coverage-report --profdata=%s',
                    accumulated_profdata,
                    os.path.getsize(accumulated_profdata) // (1024 * 1024),
                    accumulated_profdata)
    elif accumulated_profdata:
        logger.warning('No whole-run profile was written to %s, so this run produced per-test '
                       'records and nothing generate-coverage-report can report from.',
                       accumulated_profdata)

    try:
        stray = [name for name in os.listdir(profile_directory) if name.endswith('.profraw')]
    except OSError:
        stray = []
    if stray:
        logger.warning('%d raw profile(s) were written straight into %s rather than into a '
                       'per-test directory, so whatever they recorded is attributed to no test: '
                       '%s', len(stray), profile_directory, ', '.join(sorted(stray)[:8]))


def collector_for_port(port, shard, suite, extra_objects=()):
    """A PerTestCollector for one worker process, or None when per-test coverage is off."""
    if not port.get_option('per_test_coverage'):
        return None

    from webkitpy.common.webkit_finder import WebKitFinder
    from webkitpy.coverage_lcov import PathCanonicalizer, staged_equivalents_for_scope

    source_root = WebKitFinder(port.host.filesystem).webkit_base()
    build_path = str(port._build_path())
    products = port.get_option('per_test_coverage_products')
    objects = instrumented_objects(
        build_path, extra_objects=extra_objects,
        products=None if products is None else [name for name in products.split(',') if name])
    sources = port.get_option('per_test_coverage_sources') or []
    # llvm-cov's --sources filter runs on the recorded path, which for a header is the staged
    # copy, so the staged copies must be exported too.
    staged = []
    for scope in sources:
        for path in staged_equivalents_for_scope(scope, source_root, build_path):
            if path not in staged:
                staged.append(path)
    if staged:
        logger.info('Per-test coverage: also exporting %s, the staged copies of the headers in '
                    'the scope. The mapping records the copy a translation unit included, so '
                    'without them the scope matches no header at all.', ', '.join(staged))
    return PerTestCollector(
        port.get_option('per_test_coverage_index'), shard, objects, sources, source_root,
        suite=suite, accumulated_profdata=port.get_option('per_test_coverage_profdata'),
        staged_sources=staged,
        # One canonicalizer per worker: building its basename index is slow.
        canonicalizer=PathCanonicalizer(source_root, build_path))


UNKNOWN_SHARD = '(unknown)'


class PerTestIndex:
    """The query side. Reads every shard in a directory and answers in both directions."""

    def __init__(self, tests=(), source_root=None, shards=()):
        self.tests = list(tests)
        self.source_root = source_root
        self.shards = list(shards)
        self._by_test = {}
        for coverage in self.tests:
            # Last one wins: a re-run appended to the same index replaces the earlier record.
            self._by_test[coverage.test] = coverage
        self.tests = [self._by_test[name] for name in sorted(self._by_test)]

    @classmethod
    def read(cls, directory):
        """Read an index directory. Raises OSError if it is not one."""
        paths = sorted(glob.glob(os.path.join(directory, '*' + SHARD_SUFFIX)))
        if not paths:
            raise OSError('{} holds no {} shard files, so it is not a per-test coverage '
                          'index'.format(directory, SHARD_SUFFIX))
        tests, shards, roots = [], [], []
        for path in paths:
            shard = _read_shard(path)
            shards.append(shard)
            tests.extend(shard.tests)
            if shard.source_root:
                roots.append(shard.source_root)
        if len(set(roots)) > 1:
            logger.warning('The shards in %s were collected from more than one checkout (%s), '
                           'so their line numbers describe more than one text', directory,
                           ', '.join(sorted(set(roots))))
        return cls(tests, source_root=roots[0] if roots else None, shards=shards)

    @property
    def sources(self):
        prefixes = set()
        for coverage in self.tests:
            prefixes.update(coverage.sources)
        return sorted(prefixes)

    @property
    def files(self):
        paths = set()
        for coverage in self.tests:
            paths.update(coverage.files)
        return sorted(paths)

    def normalize(self, path):
        """A caller's path in the form the index stores, whatever they typed."""
        if os.path.isabs(path):
            return relative_source_path(os.path.normpath(path), self.source_root)
        return os.path.normpath(path).replace(os.sep, '/')

    def for_test(self, test_name):
        return self._by_test.get(test_name)

    def matching_tests(self, pattern):
        """Test names containing pattern; an exact match wins outright over names it prefixes."""
        if pattern in self._by_test:
            return [pattern]
        return sorted(name for name in self._by_test if pattern in name)

    def covering(self, relative_path, line=None):
        """Which tests execute a line. -> (covering, measured count, unmeasured count)

        All tests unmeasured means the query is outside the index's scope, not that none cover it.
        """
        covering, measured, unmeasured = [], 0, 0
        for coverage in self.tests:
            if not coverage.measured(relative_path):
                unmeasured += 1
                continue
            measured += 1
            lines = coverage.files.get(relative_path)
            if not lines:
                continue
            if line is None or line in lines:
                covering.append(coverage)
        return covering, measured, unmeasured

    def line_test_counts(self, relative_path):
        """{line: number of tests executing it} for one file, over the tests that measured it."""
        counts = {}
        for coverage in self.tests:
            for line in coverage.files.get(relative_path, ()):
                counts[line] = counts.get(line, 0) + 1
        return counts

    def for_added_lines(self, added_by_path):
        """Which tests to run for a change. -> AddedLineSelection"""
        selected = {}
        covered_lines, uncovered = {}, {}
        out_of_scope, unknown_files = [], []
        for path, lines in sorted(added_by_path.items()):
            relative = self.normalize(path)
            measured = [coverage for coverage in self.tests if coverage.measured(relative)]
            if not measured:
                out_of_scope.append(relative)
                continue
            if not any(relative in coverage.files for coverage in measured):
                unknown_files.append(relative)
            for line in sorted(lines):
                hits = [coverage for coverage in measured if coverage.covers(relative, line)]
                if not hits:
                    uncovered.setdefault(relative, []).append(line)
                    continue
                covered_lines.setdefault(relative, []).append(line)
                for coverage in hits:
                    selected.setdefault(coverage.test, coverage)
        return AddedLineSelection([selected[name] for name in sorted(selected)],
                                  covered_lines, uncovered, out_of_scope, unknown_files)

    def redundant_tests(self):
        """Tests whose covered lines are all covered by the rest of the index.

        Dropping any ONE of them loses no coverage; dropping all of them may.
        """
        redundant = []
        for coverage in self.tests:
            if not coverage.line_count:
                continue
            others = [other for other in self.tests if other is not coverage]
            union = {}
            for other in others:
                for path, lines in other.files.items():
                    union.setdefault(path, set()).update(lines)
            if all(coverage.files[path] <= union.get(path, set()) for path in coverage.files):
                redundant.append(coverage)
        return redundant

    def statistics(self):
        pairs = 0
        singly = 0
        for path in self.files:
            counts = self.line_test_counts(path)
            pairs += len(counts)
            singly += sum(1 for count in counts.values() if count == 1)
        return IndexStatistics(len(self.tests), len(self.files), pairs, singly,
                               sorted({coverage.suite for coverage in self.tests
                                       if coverage.suite}),
                               self.sources)


# out_of_scope: paths no test was measured over. unknown_files: in-scope paths with no covered
# line from any test.
AddedLineSelection = namedtuple(
    'AddedLineSelection',
    ('tests', 'covered_lines', 'uncovered_lines', 'out_of_scope', 'unknown_files'))

IndexStatistics = namedtuple(
    'IndexStatistics',
    ('test_count', 'file_count', 'line_count', 'singly_covered_line_count', 'suites', 'sources'))


Shard = namedtuple('Shard', ('path', 'name', 'suite', 'sources', 'source_root', 'objects',
                             'tests'))


def _read_shard(path):
    """One shard file. A bad record is skipped and named: a killed run leaves a truncated line."""
    header = {}
    tests = []
    with open(path, 'r') as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as failure:
                logger.warning('%s:%d could not be read (%s), so one record is missing from '
                               'this index', path, number, failure)
                continue
            if record.get('record') == 'shard':
                header = record
                continue
            if record.get('record') != 'test' or not record.get('test'):
                logger.warning('%s:%d is not a test record and was skipped', path, number)
                continue
            files = {name: decode_lines(text)
                     for name, text in (record.get('files') or {}).items()}
            tests.append(TestCoverage(
                record['test'], files, suite=record.get('suite') or header.get('suite'),
                sources=header.get('sources') or (), seconds=record.get('seconds'),
                raw_bytes=record.get('raw_bytes'),
                shard=header.get('shard') or UNKNOWN_SHARD))
    version = header.get('version')
    if version is not None and version > INDEX_FORMAT_VERSION:
        logger.warning('%s was written in format version %s and this reads version %s, so some '
                       'of it may be ignored', path, version, INDEX_FORMAT_VERSION)
    return Shard(path, header.get('shard') or UNKNOWN_SHARD, header.get('suite'),
                 tuple(header.get('sources') or ()), header.get('source_root'),
                 tuple(header.get('objects') or ()), tests)


def parse_location(text):
    """'Source/WTF/wtf/Vector.h:512' -> (path, 512, 512). Also PATH:FIRST-LAST, or a bare PATH."""
    path, separator, span = text.rpartition(':')
    if not separator:
        return text, None, None
    low, _, high = span.partition('-')
    try:
        first = int(low)
        last = int(high) if high else first
    except ValueError:
        raise ValueError('{} is not PATH, PATH:LINE or PATH:FIRST-LAST'.format(text))
    if last < first:
        raise ValueError('{} names a range that ends before it starts'.format(text))
    return path, first, last

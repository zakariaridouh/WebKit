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

"""Which tests execute this line, and which lines does this test reach?

An ordinary coverage run cannot answer either question. Every process writes into the pool the
baked-in `/private/tmp/WebKitCoverage/<Product>_%8m%c.profraw` names, `%8m` merges counters into
those eight files as they are written, and the run ends with one indexed profile in which every
test's counters have already been added together. The attribution is destroyed at collection
time, not lost later, so no amount of post-processing recovers it.

The lever is that the profile runtime reads LLVM_PROFILE_FILE at process start and it overrides
the baked-in path. So: point each test's processes at a directory of their own, index that
directory on its own the moment the test finishes, reduce it to the set of covered
(file, line) pairs inside a declared source scope, and delete the raw profiles before the next
test starts. What is kept per test is kilobytes; what is thrown away is hundreds of megabytes.

Three costs decide the shape of everything here, all measured on this machine against the
CMake coverage build and its 208 MB whole-run profile:

  * One pool slot of counters is 142 MB across the five instrumented frameworks (WebCore alone
    111 MB), and continuous mode preallocates it at dyld load whether or not the test executes
    anything. So the pattern this sets is `%1m%c` and not the baked-in `%8m%c`: one file per
    image rather than eight, which is the difference between 142 MB and 1.1 GB of live raw
    profile per test. Every process in one test's process tree still shares those files -- the
    runtime keys the name on the image signature, not on the process -- so the pooling that
    makes the counters mergeable is intact.
  * Reducing one test costs one `llvm-profdata merge` and one `llvm-cov export`, and the export
    is dominated by loading the objects' coverage mappings rather than by the scope: scoped to
    Source/WTF it takes 8.3 s against the five frameworks and 0.48 s against TestWTF alone. That
    is the per-test overhead, and it is why the object list is an input rather than a constant.
  * `--skip-functions --skip-branches` on the export is not an optimization, it is the
    difference between a feasible and an infeasible artifact. lcov emits one FN:/FNDA: pair per
    template *instantiation*: `--sources Source/WTF/wtf/Vector.h` against JavaScriptCore alone
    is 52.8 MB of which 52.3 MB is mangled names for one header. The same scope over all of
    Source/WTF is 1.69 MB with function records and 488 KB without.

So this is deliberately NOT built for a full-suite run, and must not be used as if it were. A
full layout run is tens of thousands of tests; at 142 MB and 8.3 s each that is a rewrite of the
harness's scheduling and a day of machine time, and PLAN 10 already measured what the result
would buy: over 1,656 real commits, coverage-driven test selection leaves 86.1% of the suite to
run at the mean and saves nothing at all for 83.9% of commits. What per-test attribution is
good for is the questions a merged profile cannot answer at any suite size -- this line is
covered by exactly one test and that test is flaky; these forty tests were all added for one
bug and thirty-nine of them add no coverage over the fortieth; I changed these twelve lines and
want the tests that touch them -- and all three are asked of a scope, not of the tree.

The index is therefore scoped, and the scope is part of the artifact. `--covers X:N` over an
index collected with `--sources Source/WTF` must not answer "no test covers it" for a line in
Source/WebCore: the honest answer is that no test in the index was measured over that file at
all, and PerTestIndex keeps the per-shard scope so it can say so. The same reasoning as the
report's "not built here" third state: a missing denominator is not a zero.
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
from webkitpy.coverage_profile_environment import (
    current_profile_environment, set_current_profile_environment)
from webkitpy.llvm_profile_utils import (
    COVERAGE_PROFILE_DIRECTORY, LLVMCovExecutable, LLVMProfileData)

logger = logging.getLogger(__name__)

# Per-test profile directories live under the machine-global profile directory, and they have to:
# `(allow file-write* (subpath "/private/tmp/WebKitCoverage"))` in the WebContent, GPU and
# Networking sandbox profiles is the whole reason LLVM_PROFILE_FILE can be redirected at all. It
# is a subpath rule, so a directory per test inside it is permitted; anywhere else and a
# sandboxed child's profile write is denied and its counters are silently lost, which reads as
# the test executing nothing but the UI process.
PER_TEST_PROFILE_ROOT = os.path.join(COVERAGE_PROFILE_DIRECTORY, 'per-test')

# One file per image, continuous mode. See the module docstring for why not %8m.
#
# No product prefix, unlike the baked-in `<Product>_%8m%c.profraw`: the directory identifies the
# test and the runtime puts the image signature in the name, so the five frameworks still get
# five files. It does mean llvm_profile_utils's collected-but-unclaimed diagnostics cannot
# attribute these files to a product, which is what makes them harmless to attribute -- nothing
# in a per-test directory is ever collected by collect_coverage_profiles().
PER_TEST_PROFILE_PATTERN = '%1m%c.profraw'

# LLVM_PROFILE_FILE is read by the profile runtime in the process that has it. __XPC_-prefixed
# copies are what libxpc forwards to an XPC service, which is how WebKitTestRunner's
# WebContent, GPU and Networking processes are launched -- exactly the mechanism
# port/driver.py already uses for __XPC_DYLD_FRAMEWORK_PATH and __XPC_ASAN_OPTIONS. Without the
# second one a layout test's attribution is the UI process's coverage and nothing else, which is
# a plausible-looking answer that is missing most of WebCore.
PROFILE_ENVIRONMENT_VARIABLES = ('LLVM_PROFILE_FILE', '__XPC_LLVM_PROFILE_FILE')

INDEX_FORMAT = 'webkit-per-test-coverage'
INDEX_FORMAT_VERSION = 1

SHARD_SUFFIX = '.jsonl'

# Enough of the test name to read in a directory listing, and a digest so that two tests cannot
# collide. Both halves are needed: layout test names contain '/' and API test names contain '.',
# so the name cannot be a directory component as it stands, and sanitizing alone maps
# `fast/dom/a-b.html` and `fast/dom/a_b.html` onto one directory -- which would silently
# attribute one test's counters to the other.
_UNSAFE_IN_A_FILENAME = re.compile(r'[^A-Za-z0-9._-]+')
_SLUG_NAME_LIMIT = 96


def slug_for_test_name(test_name):
    """A filesystem-safe, collision-free directory name for a test. Stable across runs."""
    flattened = _UNSAFE_IN_A_FILENAME.sub('_', test_name).strip('_') or 'test'
    digest = hashlib.sha1(test_name.encode('utf-8')).hexdigest()[:12]
    return '{}-{}'.format(flattened[:_SLUG_NAME_LIMIT], digest)


def per_test_profile_directory(test_name, root=PER_TEST_PROFILE_ROOT):
    return os.path.join(root, slug_for_test_name(test_name))


def profile_environment(directory):
    """The environment a test's processes need in order to write into directory."""
    path = os.path.join(directory, PER_TEST_PROFILE_PATTERN)
    return {name: path for name in PROFILE_ENVIRONMENT_VARIABLES}


# count: per-test directories removed. total_bytes: how much they held.
StalePerTestProfiles = namedtuple('StalePerTestProfiles', ('count', 'total_bytes'))


def prepare_per_test_profile_root(root=PER_TEST_PROFILE_ROOT):
    """Remove every per-test profile directory left behind by an earlier run.

    llvm_profile_utils.prepare_coverage_profile_directory() does not do this: it unlinks
    `*.profraw` at the top level of the profile directory and does not recurse, so an
    interrupted per-test run leaves one directory per test behind, each holding up to 142 MB of
    preallocated counters. Ten abandoned layout tests is 1.4 GB that nothing else on the machine
    will ever clean up.

    Called after prepare_coverage_profile_directory(), which is what takes the machine-global
    lock; on its own this cannot tell an abandoned directory from a live one.
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

    llvm-cov matches --sources against the absolute paths in the coverage mapping and resolves a
    relative argument against its own working directory, so a relative scope silently reduces to
    nothing unless the tool happens to have been run from the checkout root. The same resolution
    generate-coverage-report does for its own --sources, for the same reason.
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
    """The instrumented Mach-Os to reduce a per-test profile against, best-effort.

    By default the same five frameworks generate-coverage-report reports from, plus whatever the
    caller adds -- for an API test that is the test binary itself, which is where the coverage
    mapping for a statically linked WTF or bmalloc function instantiated only by a test lives.

    products restricts the frameworks by name, exactly as generate-coverage-report's --products
    does, and an empty list selects none of them. It is the one knob that matters, because the
    object list IS the per-test cost: loading the five frameworks' coverage mappings is 8.3 s of
    the 8.3 s an export of the Source/WTF scope takes, against 0.48 s for TestWTF alone. A
    framework that contributes nothing to the answer is paid for once per test.
    """
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
        # Loud for the same reason generate-coverage-report is loud about it: a framework that
        # is not passed is not reported at 0%, it is absent, and the lines it holds look like
        # lines no test reaches.
        logger.warning('Not found in %s, so no test will be attributed any of their lines: %s',
                       build_path, ', '.join(missing))
    for path in extra_objects:
        if path and path not in objects:
            objects.append(path)
    return objects


# The profile environment the driver about to be started must have, set by the collector before
# each test and read by port/driver.py while it builds that environment.
# The state itself lives in coverage_profile_environment, a module with no imports, so that
# webkitpy/port/driver.py can read it without depending on this module and, through it, on
# coverage_lcov and llvm_profile_utils. driver.py is imported by every layout-test run whether it
# collects coverage or not. Re-exported here so callers have one name to import.


def encode_lines(numbers):
    """{1, 2, 3, 7} -> '1-3,7'. The stored form of a covered-line set.

    Run-length rather than a list of integers because executed lines are overwhelmingly
    contiguous -- a covered function is a covered run of lines -- so this is where the per-test
    record stops being large enough to matter. Measured by reducing the whole-run profile
    through TestWTF alone, scoped to Source/WTF, which is the largest single record this shape
    can produce: 21,509 covered lines over 268 files is 50,255 bytes of JSON as ranges and
    119,520 as lists of integers.
    """
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
    """A source path as the index stores it: relative to the checkout when it is under it.

    Relative, so that an index survives the checkout moving and can be compared with a diff's
    paths without normalizing both sides at every query. Absolute paths are kept as they are
    rather than dropped: a copied header that coverage_lcov could not place back into the
    checkout is still real coverage, and it still belongs to the test that executed it.
    """
    if not source_root:
        return path
    root = source_root.rstrip('/')
    if path == root:
        return path
    if path.startswith(root + '/'):
        return path[len(root) + 1:]
    return path


def scope_includes(relative_path, scope_prefixes):
    """Whether a source path is inside a declared --sources scope.

    An empty scope means "no scope was declared", which is treated as including everything: it
    is what an index recorded without a scope has, and answering "out of scope" for every query
    against it would be worse than answering from what it holds.
    """
    if not scope_prefixes:
        return True
    for prefix in scope_prefixes:
        normalized = prefix.rstrip('/')
        if relative_path == normalized or relative_path.startswith(normalized + '/'):
            return True
    return False


# files: {relative source path: {covered line numbers}}
# raw_bytes: how much raw profile the test produced, which is the cost being paid.
# profdata_bytes: the indexed profile it merged to, before it was discarded.
# seconds: how long the reduction took, so a run can report its own overhead.
ReducedProfile = namedtuple('ReducedProfile',
                            ('files', 'raw_bytes', 'profdata_bytes', 'seconds'))


def _export_arguments(objects, profile_path, sources, ignore_regexes=()):
    first, *rest = objects
    arguments = ['export', first, *['-object={}'.format(path) for path in rest],
                 '-instr-profile={}'.format(profile_path), '--format=lcov',
                 # Per-instantiation function records are 99% of a scoped export's bytes and
                 # answer no question this asks. See the module docstring's measurement.
                 '--skip-functions', '--skip-branches', '--skip-expansions']
    for regex in ignore_regexes:
        arguments.append('--ignore-filename-regex={}'.format(regex))
    # --sources must be last and must be the flag followed by one argument per path; the
    # --sources=PATH spelling is accepted, ignored, and produces the whole tree. See
    # llvm_profile_utils.LLVMCov._sources_arguments().
    if sources:
        arguments += ['--sources', *sources]
    return arguments


def reduce_raw_profiles(profile_directory, objects, sources, source_root, canonicalizer=None,
                        ignore_regexes=(), profdata_path=None):
    """Index one test's raw profiles and reduce them to its covered lines. -> ReducedProfile

    sources are absolute paths, because llvm-cov matches --sources against the absolute paths
    in the coverage mapping and resolves a relative one against its own working directory.

    Only executed lines are kept. An unexecuted instrumented line is not stored, and that is a
    deliberate asymmetry rather than an omission: it would nearly double the artifact -- 21,509
    covered lines against 37,891 instrumented ones in the Source/WTF scope measured above -- to
    record something the whole-run report already answers exactly, and no query here needs it.
    What it costs is that this index cannot tell an uninstrumented line from one no test
    reached, which every caller has to say out loud.

    Returns a ReducedProfile with empty files when the directory holds no raw profiles at all,
    which is what a test that never launched an instrumented process looks like -- a skipped
    test, or a driver that failed to start.
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
        # --failure-mode=all for the same reason the whole-run merge uses it: the harness
        # hard-kills drivers, so one unreadable profile out of five must not lose the test.
        merge = LLVMProfileData.merge(indexed, unweighted_profiles=raw_profiles,
                                      failure_mode='all', num_threads=0)
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
    """Fold one test's indexed profile into the run's, in place. Returns its size in bytes.

    Per-test mode overrides the baked-in profile path, so nothing is written where
    collect_coverage_profiles() looks and the run produces no whole-run profile of its own
    unless this keeps one. Merging incrementally is what makes that possible without keeping the
    raw profiles: each test contributes its counters and its 142 MB is then deleted.

    Written to a temporary file and moved into place, because llvm-profdata reads its own output
    path as an input here -- merging into the file being read would leave a half-written profile
    on top of the run's accumulated one if it failed.
    """
    if not os.path.exists(addition_path):
        return 0
    inputs = [addition_path]
    if os.path.exists(accumulated_path):
        inputs.insert(0, accumulated_path)
    directory = os.path.dirname(accumulated_path) or '.'
    os.makedirs(directory, exist_ok=True)
    partial = accumulated_path + '.partial'
    merge = LLVMProfileData.merge(partial, unweighted_profiles=inputs, failure_mode='all',
                                  num_threads=0)
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

    The scope travels with the record and is not a property of the index as a whole, because two
    runs can append to one index with different --sources. Without it, `--covers` cannot tell
    "this test does not execute that line" from "that file was never in this test's export", and
    those two answers point in opposite directions.
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

    JSON lines, appended and flushed per test, for two reasons. A run that is killed halfway --
    which for anything measured per test is the normal way a run ends -- keeps every test it had
    already reduced, and there is no rewrite of a growing document per test. One file per shard
    rather than one shared file, because the layout and API harnesses both reduce inside their
    worker processes and two processes appending JSON lines longer than PIPE_BUF to one
    descriptor can interleave a line.
    """

    def __init__(self, directory, shard, suite=None, sources=(), source_root=None, objects=()):
        self.directory = directory
        self.shard = shard
        self.suite = suite
        # Stored relative to the checkout, like every path in a record, so an index is readable
        # after the checkout moves.
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

    One of these lives in each worker process, so `shard` is the worker's name. The order the
    harness must call it in is begin() before the test's processes start, then finish() after
    they have exited -- not merely after the test has reported, because a driver that is still
    running has its counter file mmapped and will keep writing into it.
    """

    def __init__(self, index_directory, shard, objects, sources, source_root, suite=None,
                 profile_root=PER_TEST_PROFILE_ROOT, accumulated_profdata=None,
                 canonicalizer=None, ignore_regexes=(), staged_sources=()):
        self.objects = list(objects)
        self.sources = list(sources)
        # The declared scope plus the staged header directories that canonicalize back into it.
        # Two lists, not one: the export has to name the staged copies or it misses every copied
        # header (see below), and the record has to name only the scope the developer asked for,
        # because that is what a query is checked against.
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
        """Make this test's profile directory and return the environment that points at it.

        The directory is emptied rather than merely created: a test that is repeated inside one
        run -- --repeat-each, --iterations, a retry after a failure -- would otherwise be
        attributed the union of its runs, and continuous mode would happily merge the second run
        into the first run's preallocated file.

        The environment is also published for port/driver.py to pick up, since the layout-test
        driver builds its own environment out of reach of the caller. Returned as well, for the
        API-test harness, which passes an explicit environment to each test process.
        """
        directory = per_test_profile_directory(test_name, self.profile_root)
        shutil.rmtree(directory, ignore_errors=True)
        os.makedirs(directory, exist_ok=True)
        environment = profile_environment(directory)
        set_current_profile_environment(environment)
        return environment

    def finish(self, test_name, extra_objects=()):
        """Reduce this test's profiles, record it, and delete the raws. -> TestCoverage or None

        Must be called once the test's processes have EXITED, not merely once it has reported a
        result: continuous mode keeps the counter file mmapped for the life of the process, so a
        driver that is still running is still writing into the file about to be indexed.

        None when the test produced no raw profile at all, which is not a failure: a skipped
        test, or one whose driver never started, has nothing to attribute, and recording it as
        covering nothing would make it look like a test that runs and touches no code.

        extra_objects are added to the object list for this test only. That is how an API-test
        shard adds the test binary it is running -- a statically linked WTF function instantiated
        only by TestWTF has its coverage mapping there and nowhere else.
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
                        # A full disk, most likely, after a run that may already be hours old.
                        # Said loudly and once per test, because the whole-run profile is now
                        # incomplete -- but this must not be what ends the run.
                        logger.error('Could not fold %s into %s: %s', test_name,
                                     self.accumulated_profdata, failure)
        finally:
            # Before anything else can go wrong, and whatever did: this is the 142 MB that must
            # not still be there when the next test starts.
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
            # The one I/O operation left that can fail at runtime. Losing this test's record is
            # bad; losing the rest of a layout run because of it is worse, and the raw profiles
            # this record was made from are already gone either way.
            logger.error('Could not record %s in %s: %s', test_name, self.writer.path, failure)
        logger.debug('%s: %d covered lines in %d files, reduced %d MB in %.1f s', test_name,
                     coverage.line_count, len(coverage.files),
                     reduced.raw_bytes // (1024 * 1024), reduced.seconds)
        return coverage

    def close(self):
        self.writer.close()


# Where the index and the run's accumulated profile go, relative to --coverage-dir. Both
# harnesses derive them the same way so that one --coverage-dir describes one run completely:
# the index, the profile a report can be generated from, and nothing the developer has to
# remember the path of.
PER_TEST_INDEX_DIRECTORY_NAME = 'per-test'
PER_TEST_RUN_PROFDATA_NAME = 'per-test-run.profdata'


def prepare_per_test_options(options, source_root):
    """Fill in and check the --per-test-coverage options in place. Raises ValueError.

    Shared by both harnesses, which is the point: the two already have separate copies of the
    --coverage-dir check and its wording, and the failure modes here are worse than that one's.
    Every check is made before any test runs, because each of them otherwise costs the run.

    The scope is required rather than defaulted to the whole tree. An unscoped export against
    the five frameworks is 23 s and 751 MB per test, so a per-test run without a scope is not a
    slow run, it is one that will not finish; and the index it would write records the whole tree
    per test. Requiring the scope makes the cost visible at the moment it is chosen.
    """
    if not options.coverage:
        raise ValueError(
            '--per-test-coverage needs --coverage: it collects the same profiles, one test at a '
            'time, and --coverage is what claims the machine-global profile directory and '
            'checks that --coverage-dir can be written to.')
    if not options.per_test_coverage_sources:
        raise ValueError(
            '--per-test-coverage needs at least one --per-test-coverage-sources=PATH. Every test '
            'is reduced with an llvm-cov export, and unscoped that is 23 s and 751 MB per test '
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
        # --no-per-test-coverage-profdata. Stored as False rather than as '' so that the
        # difference between "not asked for" and "not decided yet" survives.
        options.per_test_coverage_profdata = None
    os.makedirs(options.per_test_coverage_index, exist_ok=True)
    return options


def summarize_per_test_run(index_directory, accumulated_profdata=None,
                           profile_directory=COVERAGE_PROFILE_DIRECTORY):
    """Say what a per-test run produced, at the end of the run. Never raises.

    It also names what did NOT go through per-test collection. A `.profraw` at the top level of
    the machine-global profile directory in a per-test run means some process wrote to its
    baked-in path instead of the one it was pointed at -- a process started before the harness
    set the environment, or an XPC service the __XPC_ forwarding did not reach -- and its
    counters are in nobody's per-test record. That is the failure mode of this whole mechanism,
    and it is otherwise completely silent.
    """
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
    """A PerTestCollector for one worker process of a harness run.

    Reads the options prepare_per_test_options() filled in, so the harnesses do not each need to
    know how the object list, the scope and the path canonicalizer are put together. Returns
    None when this run is not collecting per-test coverage, so a caller can call it
    unconditionally.
    """
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
    # Without this a scope of Source/WTF records no line of any WTF header, silently. The
    # coverage mapping of a translation unit that included <build>/WTF/Headers/wtf/Vector.h
    # records that path, not Source/WTF/wtf/Vector.h, and llvm-cov's --sources filter runs
    # against the recorded path -- long before the canonicalization that maps it back. Measured:
    # a six-test TestWTF run scoped to Source/WTF recorded 27 files, all of them .cpp, and not
    # one line of Vector.h, which is the header those tests exist to exercise.
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
        # One canonicalizer per worker, not per test: it builds a basename index per framework by
        # walking that framework's source tree, which is seconds of work and identical for every
        # test in the shard.
        canonicalizer=PathCanonicalizer(source_root, build_path))


# The name a shard file gives itself when its header line is missing or unreadable, which is
# what a run that was killed while writing the first line leaves behind.
UNKNOWN_SHARD = '(unknown)'


class PerTestIndex:
    """The query side. Reads every shard in a directory and answers in both directions."""

    def __init__(self, tests=(), source_root=None, shards=()):
        self.tests = list(tests)
        self.source_root = source_root
        self.shards = list(shards)
        self._by_test = {}
        for coverage in self.tests:
            # Last one wins, so a re-run of a test replaces its earlier record rather than
            # appearing twice. A repeat within one run is already handled by begin() emptying
            # the directory; this is the case of two runs appending to one index.
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
            # Not fatal: the records are relative, so they still line up. Said out loud because
            # two checkouts means two sets of line numbers, and nothing here can check that the
            # text at Source/X:42 was the same in both.
            logger.warning('The shards in %s were collected from more than one checkout (%s), '
                           'so their line numbers describe more than one text', directory,
                           ', '.join(sorted(set(roots))))
        return cls(tests, source_root=roots[0] if roots else None, shards=shards)

    @property
    def sources(self):
        """Every declared scope prefix in the index, deduplicated and sorted."""
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
        """Test names containing pattern, so a partial name is usable at the command line.

        An exact match wins outright rather than being offered alongside its own prefixes:
        `--test fast/dom/a.html` must not be ambiguous merely because
        `fast/dom/a.html?variant` is also in the index.
        """
        if pattern in self._by_test:
            return [pattern]
        return sorted(name for name in self._by_test if pattern in name)

    def covering(self, relative_path, line=None):
        """Which tests execute a line, and how many could have said anything about the file.

        (covering, measured, unmeasured), where measured and unmeasured are counts of tests
        whose scope did and did not include the file. An empty covering list with unmeasured
        equal to the whole index is not evidence about the line -- it is a query outside the
        index's scope -- and the caller has to report the difference.
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
        """Which tests to run for a change. -> AddedLineSelection

        added_by_path is {relative or absolute path: [line numbers]}, as
        coverage_patch.git_diff_added_lines() produces it.
        """
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
        """Tests every one of whose covered lines is also covered by the rest of the index.

        Not a minimal covering set: that is set cover, it is NP-hard, and a greedy answer would
        name a different set of "redundant" tests depending on the order it happened to walk.
        This is the exact, order-independent question -- is every line this test reaches also
        reached by the other tests in this index -- and the claim it supports is precisely that
        dropping any ONE of them cannot reduce the index's coverage by a line.

        It does NOT say they can all be dropped. Three tests covering {1,2}, {2} and {1} are all
        redundant by this test and deleting all three loses both lines. Whoever acts on the list
        drops one and asks again.

        It is a statement about the index, never about the suite. A test that looks redundant
        inside a 40-test index is redundant with respect to those 40 tests and to the scope they
        were measured over, and it may be the only test in the suite that reaches a line outside
        that scope.
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
        """Counts worth printing before any query, because they bound every answer."""
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


# tests: [TestCoverage] to run for the change.
# covered_lines / uncovered_lines: {path: [line numbers]} some test reaches, and none does.
# out_of_scope: paths no test in the index was measured over, so the index says nothing at all.
# unknown_files: paths inside the scope that no test has a single line of, which for a new file
#     usually means nothing instrumented compiled it.
AddedLineSelection = namedtuple(
    'AddedLineSelection',
    ('tests', 'covered_lines', 'uncovered_lines', 'out_of_scope', 'unknown_files'))

IndexStatistics = namedtuple(
    'IndexStatistics',
    ('test_count', 'file_count', 'line_count', 'singly_covered_line_count', 'suites', 'sources'))


# tests: [TestCoverage] read from the shard.
Shard = namedtuple('Shard', ('path', 'name', 'suite', 'sources', 'source_root', 'objects',
                             'tests'))


def _read_shard(path):
    """One shard file. A record that cannot be read is skipped and named, never fatal.

    A killed run leaves a truncated last line, and that must cost one test rather than the
    whole index -- the entire point of appending per test is that an interrupted run keeps what
    it measured.
    """
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
    """'Source/WTF/wtf/Vector.h:512' -> ('Source/WTF/wtf/Vector.h', 512, 512).

    A range is accepted -- `Vector.h:500-520` -- because the question "what covers the function
    I just changed" is about a run of lines, and asking it twenty times is not the same tool. A
    bare path means every line the index has for that file.

    Split on the last colon, so a path may contain one; a Windows drive letter is not a case
    this tool has.
    """
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

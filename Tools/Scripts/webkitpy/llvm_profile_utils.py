#!/usr/bin/env python3

import glob
import logging
import math
import os
import shlex
import shutil
import subprocess

from functools import cache

logger = logging.getLogger(__name__)


def locate_binary_xcrun(sdk, binary_name):
    completed_process = subprocess.run(['/usr/bin/xcrun', '-sdk', sdk, '--find', binary_name],
                                       check=False, text=True, capture_output=True)
    if completed_process.returncode:
        return None
    return completed_process.stdout.strip()


def simplify_profile_weights(profile_weights):
    simplified_profile_weights = []

    weight_sum = 0
    max_weight = 0
    # We need to turn percentages into weights > 1, but we don't want crazy high multipliers.
    # For example, if we have weights 0.35 and 0.65, we don't need a 7:13 ratio when 5:9 is good enough.
    max_multiplier = 15
    for group, weight in profile_weights:
        weight_sum = weight_sum + weight
        if weight > max_weight:
            max_weight = weight

    gcd = int(max_weight * max_multiplier)
    for group, weight in profile_weights:
        gcd = math.gcd(gcd, int((weight / weight_sum) * max_multiplier))

    for i in range(0, len(profile_weights)):
        group, weight = profile_weights[i]
        simplified_profile_weights.append((group, int((weight / weight_sum) * max_multiplier) // gcd))

    return simplified_profile_weights


class ExecutablesFromEnvAndXcode:
    PREFERRED_EXECUTABLE_INDEX = 0
    EXECUTABLE_NAME = None

    @classmethod
    @cache
    def detect_binaries(cls):
        binaries = []

        # Prefer the toolchain's own binary over whatever happens to be on PATH. The raw
        # profile format has no compatibility guarantees, so llvm-profdata and llvm-cov must
        # come from the same toolchain as the clang that built the instrumented binaries.
        # A stale /usr/local/bin/llvm-cov ahead of Xcode on PATH would otherwise be used in
        # preference to it, and can fail or silently misreport.
        for sdk_name in ('macosx.internal', 'iphoneos.internal', 'macosx', 'iphoneos'):
            binary_path = locate_binary_xcrun(sdk_name, cls.EXECUTABLE_NAME)
            if not binary_path:
                continue
            if binary_path in binaries:
                continue
            binaries.append(binary_path)

        # Fall back to PATH, for environments with no usable Xcode.
        binary_from_search_path = shutil.which(cls.EXECUTABLE_NAME)
        if binary_from_search_path and binary_from_search_path not in binaries:
            binaries.append(binary_from_search_path)

        logger.debug(f'Available {cls.EXECUTABLE_NAME} from {binaries}')

        return binaries

    @classmethod
    def preference_ordered_paths(cls):
        count = len(cls.detect_binaries())
        for _ in range(count):
            cls.PREFERRED_EXECUTABLE_INDEX = (cls.PREFERRED_EXECUTABLE_INDEX + 1) % count
            yield cls.detect_binaries()[cls.PREFERRED_EXECUTABLE_INDEX]

    @classmethod
    def run(cls, command, *args, check=False, stdout=None, stderr=None, capture_output=False,
            **kwargs) -> subprocess.CompletedProcess:
        kwarg_capture_output = capture_output or (stdout is None and stderr is None)

        completed_process = None
        for binary_path in cls.preference_ordered_paths():
            logger.debug(f'Running {shlex.join([binary_path, *command])}')
            completed_process = subprocess.run([binary_path, *command], *args,
                                               check=False, capture_output=kwarg_capture_output,
                                               stdout=stdout, stderr=stderr, **kwargs)
            if not completed_process.returncode:
                break

            logger.debug(f'Failed to {command} with binary {binary_path}\n'
                         f'return_code: {completed_process.returncode}\n'
                         f'stdout: {completed_process.stdout}\n'
                         f'stderr: {completed_process.stderr}\n')

        if check:
            completed_process.check_returncode()

        return completed_process


class LLVMProfDataExecutable(ExecutablesFromEnvAndXcode):
    EXECUTABLE_NAME = 'llvm-profdata'


class LLVMProfileData:
    @classmethod
    def show(cls, profile_path):
        list_functions_process = LLVMProfDataExecutable.run(['show', '--all-functions', '--value-cutoff=10',
                                                             profile_path], stdout=subprocess.PIPE, text=True)

        return subprocess.run(['/usr/bin/c++filt', '-n'], input=list_functions_process.stdout,
                              capture_output=True, text=True, check=True)

    @classmethod
    def merge(cls, output_file, unweighted_profiles=(), weighted_profiles=(), failure_mode=None, num_threads=None):
        command = ['merge', '--sparse', *unweighted_profiles]
        for profile_path, weight in weighted_profiles:
            lib_profile_path = profile_path
            command.extend(['--weighted-input', f'{weight},{lib_profile_path}'])

        # Coverage runs collect profiles from processes the test harness hard-kills, so a
        # truncated profile is expected and must not sink the whole merge.
        if failure_mode:
            command.append(f'--failure-mode={failure_mode}')
        if num_threads is not None:
            command.append(f'--num-threads={num_threads}')

        command.extend(['--output', output_file])

        return LLVMProfDataExecutable.run(command, capture_output=True, text=True)

    @classmethod
    def compress(cls, input_profile, output_file):
        return subprocess.run(['/usr/bin/compression_tool', '-encode', '-i', input_profile, '-o', output_file,
                               '-a', 'lzfse'], capture_output=True, check=True, text=True)

    @classmethod
    def decompress(cls, input_profile, output_file):
        subprocess.run(['/usr/bin/touch', output_file], check=True)
        return subprocess.run(['/usr/bin/compression_tool', '-decode', '-i', input_profile, '-o', output_file,
                               '-a', 'lzfse'], capture_output=True, check=True, text=True)


def merge_raw_profiles_in_directory_by_prefixes(prefix_list, input_directory, output_directory=None,
                                                input_suffix='.profraw', output_suffix='.profdata'):
    output_files = []
    for prefix in prefix_list:
        logger.info(f'Merging {prefix}')
        pattern = f'{prefix}*{input_suffix}'
        input_profiles = glob.glob(os.path.join(input_directory, pattern))
        output_file = os.path.join(output_directory or input_directory, f'{prefix}{output_suffix}')
        merge_process = LLVMProfileData.merge(output_file, unweighted_profiles=input_profiles)
        logger.info(f'stdout: {merge_process.stdout}')
        logger.info(f'stderr: {merge_process.stderr}')
        merge_process.check_returncode()
        output_files.append(output_file)
        logger.info(f'{prefix} is successfully merged')

    return output_files


def merge_all_raw_profiles_in_directory(input_directory, output_file, input_suffix='.profraw'):
    """Merge every raw profile in a directory into a single indexed profile.

    Unlike merge_raw_profiles_in_directory_by_prefixes(), which produces one indexed
    profile per dylib for -fprofile-use, coverage reporting needs a single index: llvm-cov
    takes exactly one --instr-profile but any number of --object, and llvm-profdata merges
    counters by function name, so a header-inline function shared between frameworks is
    counted once. Reporting per-dylib and stitching the results instead would double-count
    those functions.
    """
    input_profiles = sorted(glob.glob(os.path.join(input_directory, f'*{input_suffix}')))
    if not input_profiles:
        raise RuntimeError(f'No {input_suffix} files found in {input_directory}')

    logger.info(f'Merging {len(input_profiles)} raw profiles from {input_directory}')
    merge_process = LLVMProfileData.merge(output_file, unweighted_profiles=input_profiles,
                                          failure_mode='all', num_threads=0)
    if merge_process.stderr:
        logger.info(f'llvm-profdata stderr: {merge_process.stderr}')
    merge_process.check_returncode()

    return input_profiles


class LLVMCovExecutable(ExecutablesFromEnvAndXcode):
    EXECUTABLE_NAME = 'llvm-cov'


# Coverage-instrumented WebKit frameworks carry this directory in their baked-in
# __llvm_profile_filename (see Source/WebKit/Shared/Cocoa/WebKit2InitializeCocoa.mm).
# It is also the only path the WebContent, GPU and Networking sandbox profiles allow
# profile writes to, so it cannot be redirected with LLVM_PROFILE_FILE: a child process
# pointed anywhere else has its profile silently denied. Test harnesses therefore let
# the processes write here and collect afterwards.
COVERAGE_PROFILE_DIRECTORY = '/private/tmp/WebKitCoverage'


def prepare_coverage_profile_directory():
    """Remove any profiles left over from an earlier run, so a run's output is its own."""
    if os.path.isdir(COVERAGE_PROFILE_DIRECTORY):
        for name in os.listdir(COVERAGE_PROFILE_DIRECTORY):
            if name.endswith('.profraw'):
                os.unlink(os.path.join(COVERAGE_PROFILE_DIRECTORY, name))
    else:
        os.makedirs(COVERAGE_PROFILE_DIRECTORY, exist_ok=True)
    # The instrumented processes create the file, but not a missing directory's parents.
    os.chmod(COVERAGE_PROFILE_DIRECTORY, 0o1777)


def collect_coverage_profiles(destination_directory):
    """Move this run's raw profiles into destination_directory, and return their paths.

    Names are made unique on collision so that successive runs (for example a layout-test
    run followed by an API-test run) can accumulate into one directory for a single report.
    """
    os.makedirs(destination_directory, exist_ok=True)
    collected = []
    if not os.path.isdir(COVERAGE_PROFILE_DIRECTORY):
        logger.warning(f'No coverage profiles: {COVERAGE_PROFILE_DIRECTORY} does not exist. '
                       f'Was WebKit built with --coverage?')
        return collected

    for name in sorted(os.listdir(COVERAGE_PROFILE_DIRECTORY)):
        if not name.endswith('.profraw'):
            continue
        source = os.path.join(COVERAGE_PROFILE_DIRECTORY, name)
        target = os.path.join(destination_directory, name)
        suffix = 0
        while os.path.exists(target):
            suffix += 1
            base, extension = os.path.splitext(name)
            target = os.path.join(destination_directory, f'{base}-{suffix}{extension}')
        shutil.move(source, target)
        collected.append(target)

    if not collected:
        logger.warning(f'No .profraw files were written to {COVERAGE_PROFILE_DIRECTORY}. '
                       f'Was WebKit built with --coverage?')
    else:
        total_bytes = sum(os.path.getsize(path) for path in collected)
        logger.info(f'Collected {len(collected)} raw profiles '
                    f'({total_bytes // (1024 * 1024)} MB) into {destination_directory}')
    return collected


class LLVMCov:
    @classmethod
    def _object_arguments(cls, objects):
        # llvm-cov takes the first object as a positional argument and each additional one
        # as a repeated -object=. Omitting an object silently under-reports the files that
        # only it contains, so callers must pass every instrumented binary.
        first, *rest = objects
        return [first, *[f'-object={path}' for path in rest]]

    @classmethod
    def _common_arguments(cls, objects, profile_path, ignore_filename_regexes=(), path_equivalences=()):
        arguments = cls._object_arguments(objects)
        arguments.append(f'-instr-profile={profile_path}')
        for regex in ignore_filename_regexes:
            arguments.append(f'--ignore-filename-regex={regex}')
        for equivalence in path_equivalences:
            arguments.append(f'-path-equivalence={equivalence}')
        return arguments

    @classmethod
    def show_html(cls, objects, profile_path, output_directory, ignore_filename_regexes=(),
                  path_equivalences=(), show_instantiations=False):
        command = ['show', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences),
                   '--format=html', f'--output-dir={output_directory}']
        # Per-instantiation sub-views are on by default and are expensive on template-heavy
        # code: measured at 395 MB versus 300 MB of HTML for JavaScriptCore alone. Aggregate
        # per-file line coverage, which is what this report is for, is unaffected.
        # (There is no --skip-expansions in Apple LLVM; expansions are opt-in already.)
        if not show_instantiations:
            command.append('--show-instantiations=false')
        return LLVMCovExecutable.run(command, capture_output=True, text=True)

    @classmethod
    def export_lcov(cls, objects, profile_path, output_file, ignore_filename_regexes=(), path_equivalences=()):
        command = ['export', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences),
                   '--format=lcov']
        with open(output_file, 'w') as lcov_file:
            return LLVMCovExecutable.run(command, stdout=lcov_file, stderr=subprocess.PIPE, text=True)

    @classmethod
    def export_summary_json(cls, objects, profile_path, output_file, ignore_filename_regexes=(),
                            path_equivalences=()):
        # --summary-only keeps this to per-file totals rather than per-line data, so it is
        # cheap next to the full export and is all a directory rollup needs.
        command = ['export', *cls._common_arguments(objects, profile_path,
                                                    ignore_filename_regexes, path_equivalences),
                   '--format=text', '--summary-only']
        with open(output_file, 'w') as json_file:
            return LLVMCovExecutable.run(command, stdout=json_file, stderr=subprocess.PIPE, text=True)

    @classmethod
    def report(cls, objects, profile_path, ignore_filename_regexes=(), path_equivalences=(),
               check_binary_ids=False):
        command = ['report', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences)]
        # Makes llvm-cov emit a per-object "profile data may be out of date" warning, which is
        # the only reliable way to notice that the tree was rebuilt after the tests ran.
        if check_binary_ids:
            command.append('--check-binary-ids')
        return LLVMCovExecutable.run(command, capture_output=True, text=True)

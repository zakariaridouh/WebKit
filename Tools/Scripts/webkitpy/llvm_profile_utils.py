#!/usr/bin/env python3

import fcntl
import glob
import gzip
import logging
import math
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time

from collections import namedtuple
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
    def preferred_path(cls):
        """The single binary to use when the caller cannot retry a failure.

        run() tries each candidate until one succeeds, which a caller streaming gigabytes
        into a compressor cannot do: by the time the exit status is known the output has
        already been written. Such a caller takes the preferred candidate and reports the
        failure instead.
        """
        binaries = cls.detect_binaries()
        if not binaries:
            raise RuntimeError(f'Found no {cls.EXECUTABLE_NAME} in the toolchain or on PATH')
        return binaries[cls.PREFERRED_EXECUTABLE_INDEX % len(binaries)]

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


# How much of a run's raw profile collection may be unreadable before reporting from what is
# left is refused.
#
# llvm-profdata is run with --failure-mode=all, which fails only when *every* input fails. That
# is the right choice -- the test harness hard-kills drivers, so an unreadable profile is a
# thing that can legitimately happen -- but it also means that 99 unreadable profiles out of
# 100 merge successfully, exit 0, and produce a confidently low report. Measured against a real
# collection (/tmp/cov-webgpu, 20 profiles: 5 frameworks x 4 %4m pool slots), 0 were
# unreadable, which is what continuous mode predicts: the counter section is mmapped and
# preallocated at dyld load, so a SIGKILLed process leaves a complete file behind rather than a
# truncated one. So a double-digit percentage is systemic -- a mismatched toolchain, a rebuild
# mid-run, a full disk -- and not an incidentally killed process.
UNREADABLE_RAW_PROFILE_LIMIT = 0.1

# merged: the raw profiles that actually contributed counters to the indexed profile.
# unreadable: [(path, what llvm-profdata said)] for the ones that contributed nothing.
ProfileMerge = namedtuple('ProfileMerge', ('merged', 'unreadable'))

# 'warning: /tmp/cov/WebCore_1_0.profraw: truncated profile data'. Confirmed against the current
# Apple LLVM: one line per unreadable input, on stderr, with the input's path exactly as passed.
_PROFDATA_WARNING = re.compile(r'^warning: (?P<path>.*?): (?P<reason>.*)$')


def unreadable_profiles_from_stderr(stderr, input_profiles):
    """[(path, reason)] for the inputs llvm-profdata reported it could not read.

    Keyed on the input list rather than on the shape of the message, so an unrelated warning --
    llvm-profdata also warns about things that are not inputs at all -- cannot be counted as a
    lost profile, and so that a future wording change fails to match instead of miscounting.
    """
    reasons = {}
    for line in (stderr or '').splitlines():
        match = _PROFDATA_WARNING.match(line)
        if match and match.group('path') in set(input_profiles):
            reasons[match.group('path')] = match.group('reason')
    return [(path, reasons[path]) for path in input_profiles if path in reasons]


def merge_raw_profiles_in_directory(input_directory, output_file, input_suffix='.profraw',
                                    unreadable_limit=UNREADABLE_RAW_PROFILE_LIMIT):
    """Merge every raw profile in a directory into a single indexed profile. -> ProfileMerge.

    Unlike merge_raw_profiles_in_directory_by_prefixes(), which produces one indexed
    profile per dylib for -fprofile-use, coverage reporting needs a single index: llvm-cov
    takes exactly one --instr-profile but any number of --object, and llvm-profdata merges
    counters by function name, so a header-inline function shared between frameworks is
    counted once. Reporting per-dylib and stitching the results instead would double-count
    those functions.

    Every unreadable input is counted and named, and more than unreadable_limit of them is
    refused. The counting is the point: the coverage the report is about to display is over
    however many profiles llvm-profdata could actually read, and nothing else in the pipeline
    can tell that number from the number collected.
    """
    input_profiles = sorted(glob.glob(os.path.join(input_directory, f'*{input_suffix}')))
    if not input_profiles:
        raise RuntimeError(f'No {input_suffix} files found in {input_directory}')

    logger.info(f'Merging {len(input_profiles)} raw profiles from {input_directory}')
    merge_process = LLVMProfileData.merge(output_file, unweighted_profiles=input_profiles,
                                          failure_mode='all', num_threads=0)

    unreadable = unreadable_profiles_from_stderr(merge_process.stderr, input_profiles)
    if unreadable:
        logger.warning('%d of %d raw profiles could not be read, so nothing they recorded is in '
                       'this report:', len(unreadable), len(input_profiles))
        for path, reason in unreadable:
            logger.warning('    %s: %s', path, reason)
    elif merge_process.stderr:
        logger.info(f'llvm-profdata stderr: {merge_process.stderr}')

    if len(unreadable) > unreadable_limit * len(input_profiles):
        raise RuntimeError(
            '{} of {} raw profiles in {} could not be read ({:.0f}%, and this refuses above '
            '{:.0f}%). The report would be over the remainder and would look like a test gap '
            'rather than a broken collection. The raw profiles have not been deleted.'.format(
                len(unreadable), len(input_profiles), input_directory,
                100.0 * len(unreadable) / len(input_profiles), 100.0 * unreadable_limit))
    merge_process.check_returncode()

    lost = {path for path, _ in unreadable}
    return ProfileMerge([path for path in input_profiles if path not in lost], unreadable)


def merge_all_raw_profiles_in_directory(input_directory, output_file, input_suffix='.profraw'):
    """The raw profiles that contributed to a merge of everything in a directory.

    merge_raw_profiles_in_directory() with the unreadable ones dropped, for callers that only
    need to know which files are now accounted for in the indexed profile.
    """
    return merge_raw_profiles_in_directory(input_directory, output_file, input_suffix).merged


class LLVMCovExecutable(ExecutablesFromEnvAndXcode):
    EXECUTABLE_NAME = 'llvm-cov'


# gzip level for the lcov trace. 6 is zlib's default and the knee of the curve here: measured
# on a 750,905,698-byte full-suite trace, level 1 gives 10.25x in 2.0s, level 6 gives 13.80x
# in 5.8s, and level 9 gives 14.20x -- 2.8% smaller -- in 12.2s.
LCOV_COMPRESSION_LEVEL = 6


# Coverage-instrumented WebKit frameworks carry this directory in their baked-in
# __llvm_profile_filename (see Source/WebKit/Shared/Cocoa/WebKit2InitializeCocoa.mm).
# It is also the only path the WebContent, GPU and Networking sandbox profiles allow
# profile writes to, so it cannot be redirected with LLVM_PROFILE_FILE: a child process
# pointed anywhere else has its profile silently denied. Test harnesses therefore let
# the processes write here and collect afterwards.
COVERAGE_PROFILE_DIRECTORY = '/private/tmp/WebKitCoverage'


# Enough Mach-O to answer two questions about a binary the report is about to describe: is it
# instrumented for coverage, and where will it write its profile.
#
# Both matter because getting the second one wrong is silent and expensive. WebGPU.framework
# and WebKitLegacy.framework were instrumented, passed to llvm-cov, and reported at 0.00% and
# 0.13% over 84,332 lines, because neither project defined ENABLE_LLVM_COVERAGE, so neither
# baked a path into __llvm_profile_filename. The compiler-rt profile runtime defines that
# symbol weakly as an empty string, so an unbaked framework does not fail to write a profile
# in any visible way -- it writes default.profraw relative to whatever the process's working
# directory is, which nothing collects, and which the sandbox denies for the WebContent, GPU
# and Networking processes anyway. Every line in it is then reported as untested.
_FAT_MAGIC = b'\xca\xfe\xba\xbe'
_FAT_MAGIC_64 = b'\xca\xfe\xba\xbf'
_MACHO_MAGIC_64_LITTLE_ENDIAN = b'\xcf\xfa\xed\xfe'
_LC_SEGMENT_64 = 0x19
_LC_SYMTAB = 0x02
# nlist_64.n_type fields. N_STAB is a mask: any of those bits set means the entry is a symbolic
# debugging entry rather than a symbol. N_TYPE masks off the kind, of which only N_SECT is defined
# in a section and so has an address in n_value.
_N_STAB = 0xe0
_N_TYPE = 0x0e
_N_SECT = 0x0e

# The counters section. Coverage builds rename its segment to __MMAP_DATA so that continuous
# mode can map it, so match on the section name and ignore the segment.
COVERAGE_COUNTERS_SECTION = '__llvm_prf_cnts'
PROFILE_FILENAME_SYMBOL = b'___llvm_profile_filename'

# instrumented: carries coverage counters at all.
# profile_filename: the baked-in __llvm_profile_filename, '' when nothing baked one in, or
#     None when the symbol is not in the symbol table -- which a stripped binary also looks
#     like, so None means "cannot tell" and never "broken".
Instrumentation = namedtuple('Instrumentation', ('instrumented', 'profile_filename'))


def _macho_slice_offsets(handle):
    handle.seek(0)
    magic = handle.read(4)
    if magic not in (_FAT_MAGIC, _FAT_MAGIC_64):
        return [0]
    count = struct.unpack('>I', handle.read(4))[0]
    wide = magic == _FAT_MAGIC_64
    offsets = []
    for _ in range(count):
        entry = handle.read(32 if wide else 20)
        offsets.append(struct.unpack_from('>Q' if wide else '>I', entry, 8)[0])
    return offsets


def _macho_load_commands(handle, base):
    """([(section name, address, size, file offset)], symtab fields) for one architecture."""
    handle.seek(base)
    if handle.read(4) != _MACHO_MAGIC_64_LITTLE_ENDIAN:
        return None
    handle.seek(base + 16)
    number_of_commands, size_of_commands = struct.unpack('<II', handle.read(8))
    handle.seek(base + 32)
    commands = handle.read(size_of_commands)

    sections = []
    symtab = None
    position = 0
    for _ in range(number_of_commands):
        if position + 8 > len(commands):
            break
        command, size = struct.unpack_from('<II', commands, position)
        if size < 8:
            break
        if command == _LC_SEGMENT_64:
            number_of_sections = struct.unpack_from('<I', commands, position + 64)[0]
            offset = position + 72
            for _ in range(number_of_sections):
                if offset + 80 > len(commands):
                    break
                name = commands[offset:offset + 16].rstrip(b'\0').decode('utf-8', errors='replace')
                address, section_size = struct.unpack_from('<QQ', commands, offset + 32)
                file_offset = struct.unpack_from('<I', commands, offset + 48)[0]
                sections.append((name, address, section_size, file_offset))
                offset += 80
        elif command == _LC_SYMTAB:
            symtab = struct.unpack_from('<IIII', commands, position + 8)
        position += size
    return sections, symtab


def _symbol_address(handle, base, symtab, symbol):
    """The section-relative address a symbol is defined at, or None if nothing defines it.

    Three things have to be true of an entry before its n_value is an address, and the
    original version of this checked none of them.

    It must not be a stab. An unstripped framework carries a GSYM stab for
    ___llvm_profile_filename next to the real symbol; a stab's value field is not an address,
    and for N_GSYM it is 0. Taking the first entry whose name matched found WebGPU's stab,
    resolved 0, mapped it into no section and reported "cannot tell" for a framework whose
    path strings(1) prints. That misread WebKit, WebKitLegacy and WebGPU and not
    JavaScriptCore or WebCore, and the reason is neither random nor string-table layout: the
    stabs are a contiguous block at the tail of the local-symbol region, so what decides the
    order is the symbol's linkage. JavaScriptCore and WebCore define it in a .cpp, which
    OptionsCocoa.cmake compiles with -fvisibility=hidden, so the symbol is N_PEXT and sorts
    into the local region ahead of the stabs. WebGPU, WebKit and WebKitLegacy define it in a
    .mm, which that flag's generator expression does not cover, so it is N_EXT and sorts after
    them. Give OBJCXX -fvisibility=hidden and the three would start reading correctly on their
    own.

    It must be defined in a section. N_UNDF, N_PBUD and N_INDR are not stabs, and their
    n_value is a size, an ordinal or another string index. An undefined entry's 0 happens to
    map into no section of a dylib, so it degraded to None by luck; in an object file, whose
    sections start at 0, it would have decoded arbitrary bytes as the profile filename.

    Its name must actually be the one asked for. Matching a single string-table index is not
    the same as matching a name, because the table is not fully deduplicated -- 280 names in
    WebGPU alone occupy more than one index. Every index holding the name is collected, so a
    second copy cannot hide the definition.
    """
    symbol_offset, number_of_symbols, string_offset, string_size = symtab
    handle.seek(base + string_offset)
    strings = handle.read(string_size)
    # The string table starts with a NUL, so every name in it is NUL-preceded.
    needle = b'\0' + symbol + b'\0'
    wanted = set()
    index = strings.find(needle)
    while index != -1:
        wanted.add(index + 1)
        index = strings.find(needle, index + 1)
    if not wanted:
        return None
    handle.seek(base + symbol_offset)
    table = handle.read(number_of_symbols * 16)
    for position in range(0, len(table) - 15, 16):
        if struct.unpack_from('<I', table, position)[0] not in wanted:
            continue
        n_type = table[position + 4]
        if n_type & _N_STAB or n_type & _N_TYPE != _N_SECT:
            continue
        return struct.unpack_from('<Q', table, position + 8)[0]
    return None


def read_instrumentation(binary_path):
    """What a Mach-O says about its own coverage instrumentation. Returns an Instrumentation.

    Reads the load commands, the symbol table and one string, so it costs milliseconds even on
    a gigabyte of WebCore. A file that is not a 64-bit little-endian Mach-O reads as
    uninstrumented rather than raising: the question being asked is "will this contribute a
    profile", and something that is not a Mach-O will not.
    """
    with open(binary_path, 'rb') as handle:
        for base in _macho_slice_offsets(handle):
            parsed = _macho_load_commands(handle, base)
            if parsed is None:
                continue
            sections, symtab = parsed
            instrumented = any(name == COVERAGE_COUNTERS_SECTION for name, _, _, _ in sections)
            address = _symbol_address(handle, base, symtab, PROFILE_FILENAME_SYMBOL) if symtab else None
            if address is None:
                return Instrumentation(instrumented, None)
            for _, section_address, section_size, file_offset in sections:
                if section_address <= address < section_address + section_size:
                    handle.seek(base + file_offset + (address - section_address))
                    data = handle.read(4096)
                    end = data.find(b'\0')
                    return Instrumentation(
                        instrumented, data[:end if end != -1 else None].decode('utf-8', errors='replace'))
            return Instrumentation(instrumented, None)
    return Instrumentation(False, None)


def profile_name_prefix(profile_filename):
    """The fixed leading part of a baked-in __llvm_profile_filename's basename.

    '/private/tmp/WebKitCoverage/WebGPU_%4m%c.profraw' -> 'WebGPU_', which is what the raw
    profiles a run collects are actually named, so it is how a collected profile is matched
    back to the binary that wrote it.
    """
    return os.path.basename(profile_filename).split('%')[0]


def collected_profile_group(profile_basename):
    """The group a collected raw profile belongs to: 'WebCore_4820_0.profraw' -> 'WebCore_'.

    The inverse of profile_name_prefix(): that turns the pattern baked into a binary into the
    prefix its profiles will have, and this turns an already-written profile's name back into
    the same string, so an unclaimed profile can be named after whatever wrote it.

    Everything up to the first '_', because the baked-in patterns are '<Product>_%4m%c.profraw'
    and no product name contains an underscore. A name with no underscore is its own group,
    which is what the profile runtime's unbaked fallback, default.profraw, looks like.
    """
    head, separator, _ = profile_basename.partition('_')
    return head + separator if separator else profile_basename


def claimed_profile_name_prefixes(binary_paths):
    """{prefix: [binary paths]} for the raw-profile names the given binaries say they write."""
    claimed = {}
    for path in binary_paths:
        try:
            instrumentation = read_instrumentation(path)
        except (OSError, struct.error) as failure:
            logger.debug(f'Could not read instrumentation from {path}: {failure}')
            continue
        if not instrumentation.instrumented or not instrumentation.profile_filename:
            continue
        prefix = profile_name_prefix(instrumentation.profile_filename)
        if prefix:
            claimed.setdefault(prefix, []).append(path)
    return claimed


def collected_profiles_with_no_object(binary_paths, raw_profile_paths):
    """[(group, [profile paths])] for collected raw profiles no binary in the report claims.

    The direction objects_with_no_profile_data() does not check, and the one that catches the
    bug that actually happened. That function asks each object "did anything with your name get
    written?", which is answerable only about objects the report already knows about. This asks
    the inverse: "this run wrote Foo_*.profraw, and nothing in this report claims to be Foo" --
    so a whole product's profile data was collected, merged into the indexed profile, and then
    described by no binary, which llvm-cov reports as the product simply not existing.

    That is exactly what WebKitLegacy's absence from generate-coverage-report's
    INSTRUMENTED_PRODUCTS tuple would do, and it is still the only thing standing between that
    hardcoded tuple and a repeat: add a framework, rename one, or link a new instrumented dylib
    and the tuple is silently incomplete again. Unlike the tuple, this needs no maintenance --
    it compares what the run collected against what the binaries themselves say.
    """
    claimed = claimed_profile_name_prefixes(binary_paths)
    groups = {}
    for path in raw_profile_paths:
        name = os.path.basename(path)
        if any(name.startswith(prefix) for prefix in claimed):
            continue
        groups.setdefault(collected_profile_group(name), []).append(path)
    return sorted(groups.items())


# uninstrumented: [(path, reason)] for binaries with no coverage instrumentation at all.
# unverifiable: [(path, reason)] for instrumented binaries whose profile path cannot be read.
InstrumentationSurvey = namedtuple('InstrumentationSurvey', ('uninstrumented', 'unverifiable'))


def survey_instrumentation(binary_paths):
    """The binaries objects_with_no_profile_data() has to skip, and why. -> InstrumentationSurvey

    Both of these are silent skips otherwise, and the first one matters most on somebody's first
    run: pointing the report at a tree that was not built with --coverage is the easiest mistake
    to make, because webkit-build-directory's last-built tiebreaker will hand out the wrong tree
    and the resulting report is empty rather than wrong -- llvm-cov has no coverage mapping for
    an uninstrumented binary, so the files only it contains are absent from the report instead
    of present at 0%. read_instrumentation() knows this for free and said nothing about it.

    The second is not a defect, just a limit: __llvm_profile_filename is not in the symbol table,
    which is also exactly what a stripped binary looks like, so where it writes cannot be
    checked. Reported separately so that "cannot tell" is never read as "broken".
    """
    uninstrumented = []
    unverifiable = []
    for path in binary_paths:
        try:
            instrumentation = read_instrumentation(path)
        except (OSError, struct.error) as failure:
            logger.debug(f'Could not read instrumentation from {path}: {failure}')
            continue
        if not instrumentation.instrumented:
            uninstrumented.append((path, 'it carries no {} section, so it was not built with '
                                         '--coverage and llvm-cov has no coverage mapping for '
                                         'it at all'.format(COVERAGE_COUNTERS_SECTION)))
        elif instrumentation.profile_filename is None:
            unverifiable.append((path, 'it is instrumented, but {} is not in its symbol table, '
                                       'which is also what a stripped binary looks like, so '
                                       'where it writes its profile cannot be '
                                       'checked'.format(
                                           PROFILE_FILENAME_SYMBOL.decode().lstrip('_'))))
    return InstrumentationSurvey(uninstrumented, unverifiable)


def objects_with_no_profile_data(binary_paths, raw_profile_paths=(),
                                 profile_directory=COVERAGE_PROFILE_DIRECTORY):
    """[(path, reason)] for instrumented binaries that can contribute no profile data.

    Not "did not this time" -- that is a test-coverage question and 0% is the right answer to
    it. These are configuration errors: the binary is instrumented, so llvm-cov will describe
    every line in it, and nothing it writes can ever reach the profile.

    Two rules, both exact and both free:

    - Its __llvm_profile_filename does not name a file inside the directory a run collects
      from. Empty means no project baked one in, and the profile runtime's weak definition
      wins; anything else outside that directory is not collected and, for the WebContent, GPU
      and Networking processes, not even permitted.
    - The name it does bake in matched none of the raw profiles this run collected, so nothing
      that loaded it ever wrote one. Needs the raw profiles, so this rule is skipped when
      reporting from an already-indexed profile.

    Binaries neither rule can be applied to -- uninstrumented ones, and instrumented ones whose
    symbol table does not carry the symbol -- are skipped here and reported by
    survey_instrumentation(), because "there is nothing to check" and "the check passed" are not
    the same answer and silently returning the second for the first is how a report gets pointed
    at an uninstrumented build tree without complaint.
    """
    collected = [os.path.basename(path) for path in raw_profile_paths]
    findings = []
    for path in binary_paths:
        try:
            instrumentation = read_instrumentation(path)
        except (OSError, struct.error) as failure:
            logger.debug(f'Could not read instrumentation from {path}: {failure}')
            continue
        # profile_filename is None for a binary whose symbol table does not carry the symbol,
        # which is also what a stripped binary looks like, so that is "cannot tell";
        # survey_instrumentation() reports both of these.
        if not instrumentation.instrumented or instrumentation.profile_filename is None:
            continue
        filename = instrumentation.profile_filename
        if not filename:
            findings.append((path, 'nothing baked a path into its __llvm_profile_filename, so '
                                   'the profile runtime writes default.profraw relative to the '
                                   "process's working directory, which nothing collects. Does "
                                   'its project define ENABLE_LLVM_COVERAGE?'))
            continue
        if not filename.startswith(profile_directory + '/'):
            findings.append((path, 'its __llvm_profile_filename is {}, which is outside {}, the '
                                   'only directory a run collects from and the only one the '
                                   'sandbox lets a WebContent, GPU or Networking process write '
                                   'to'.format(filename, profile_directory)))
            continue
        prefix = profile_name_prefix(filename)
        if collected and prefix and not any(name.startswith(prefix) for name in collected):
            findings.append((path, 'it writes {}*, and this run collected no profile with that '
                                   'name, so nothing that loaded it ever wrote one'.format(prefix)))
    return findings


# The lock that makes the machine-global profile directory safe to use. It lives inside that
# directory so that the lock and the thing it protects cannot be separated, and the sandbox
# already permits writes there.
COVERAGE_PROFILE_LOCK_FILENAME = '.webkit-coverage-run.lock'

# Held for the lifetime of the process rather than for the lifetime of a call. flock is released
# when the last descriptor referring to it is closed, so keeping this handle alive is what makes
# the lock cover the run: prepare_coverage_profile_directory() is called before the tests start
# and collect_coverage_profiles() after they finish, and there is no single scope spanning both
# that this module gets to see. It also means a run that is SIGKILLed releases the lock, so there
# is no stale-lock problem to solve and no need to check whether a recorded pid is still alive.
_held_profile_directory_lock = None

# count: raw profiles removed. total_bytes: how much they were.
StaleProfiles = namedtuple('StaleProfiles', ('count', 'total_bytes'))


class CoverageProfileDirectoryInUse(RuntimeError):
    """Another coverage run holds the machine-global profile directory."""


def coverage_profile_lock_path():
    return os.path.join(COVERAGE_PROFILE_DIRECTORY, COVERAGE_PROFILE_LOCK_FILENAME)


def acquire_coverage_profile_directory_lock():
    """Claim the profile directory for this process, or raise CoverageProfileDirectoryInUse.

    Two concurrent coverage runs destroy each other, silently and in both directions. The profile
    path is baked into the instrumented frameworks, so /private/tmp/WebKitCoverage is not per-run
    or even per-checkout: it is machine-global. prepare_coverage_profile_directory() unlinks every
    .profraw in it and collect_coverage_profiles() moves every .profraw out of it, neither with a
    marker or an mtime filter, because neither can distinguish this run's files from anybody
    else's. So an API-test run started while a layout run is in progress deletes the layout run's
    live, mmapped profiles, and whichever run finishes first takes the other's files into its own
    --coverage-dir -- which is a report that silently combines two runs and makes --suite
    attribution meaningless, on top of a run that reports no coverage at all.

    Refuse instead. The alternative -- warning and continuing -- is a corrupted artifact either
    way, and the pid in the message is enough to find the other run.
    """
    global _held_profile_directory_lock
    if _held_profile_directory_lock is not None:
        return _held_profile_directory_lock

    os.makedirs(COVERAGE_PROFILE_DIRECTORY, exist_ok=True)
    path = coverage_profile_lock_path()
    try:
        handle = open(path, 'a+')
    except OSError as failure:
        # The directory is mode 1777 so that sandboxed processes of any user can write profiles
        # into it, which means the lock file can belong to somebody else. Losing the ability to
        # detect a concurrent run is worth saying out loud; it is not worth refusing to run.
        logger.warning('Cannot open %s (%s), so a concurrent coverage run cannot be detected. '
                       'Two runs at once destroy each other\'s profiles.', path, failure)
        return None

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read(4096).strip() or 'an unidentified process'
        handle.close()
        raise CoverageProfileDirectoryInUse(
            '{} is already in use by {}. Only one coverage run can be in flight on a machine: '
            'the profile path is baked into the instrumented frameworks, so the directory is '
            'shared, and each run deletes and then collects every profile in it -- including the '
            'other run\'s live, mmapped ones. Wait for that run, or kill it.'.format(
                COVERAGE_PROFILE_DIRECTORY, holder))

    handle.seek(0)
    handle.truncate()
    handle.write('pid {} ({}) since {}\n'.format(os.getpid(), os.path.basename(sys.argv[0] or '?'),
                                                 time.strftime('%Y-%m-%dT%H:%M:%S%z')))
    handle.flush()
    _held_profile_directory_lock = handle
    return handle


def release_coverage_profile_directory_lock():
    """Drop the lock early. Only tests need this; a run holds it until the process exits."""
    global _held_profile_directory_lock
    if _held_profile_directory_lock is not None:
        _held_profile_directory_lock.close()
        _held_profile_directory_lock = None


def prepare_coverage_profile_directory():
    """Claim the profile directory, then remove any profiles left over from an earlier run.

    The clearing is load-bearing and must not be removed: %Nm merges into an existing profile
    rather than replacing it, so a profile left behind by an earlier run or by a rebuild would be
    folded into this run's counters. What it needs is the lock above, because on its own it cannot
    tell an abandoned profile from one a concurrent run is still writing to.
    """
    acquire_coverage_profile_directory_lock()
    stale = StaleProfiles(0, 0)
    if os.path.isdir(COVERAGE_PROFILE_DIRECTORY):
        count, total_bytes = 0, 0
        for name in os.listdir(COVERAGE_PROFILE_DIRECTORY):
            if not name.endswith('.profraw'):
                continue
            path = os.path.join(COVERAGE_PROFILE_DIRECTORY, name)
            try:
                total_bytes += os.path.getsize(path)
            except OSError:
                pass
            os.unlink(path)
            count += 1
        stale = StaleProfiles(count, total_bytes)
    # The instrumented processes create the file, but not a missing directory's parents.
    os.chmod(COVERAGE_PROFILE_DIRECTORY, 0o1777)
    if stale.count:
        # Said out loud with a count, because this is data being thrown away: an interrupted run
        # whose profiles were never collected looks exactly like a rebuild's leftovers from here.
        logger.info('Removed %d stale raw profile(s) (%d MB) from %s', stale.count,
                    stale.total_bytes // (1024 * 1024), COVERAGE_PROFILE_DIRECTORY)
    return stale


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
    def _write_atomically(cls, output_file, write):
        """write(temporary path) -> CompletedProcess, published to output_file only on success.

        llvm-cov's output is streamed to its destination and its exit status is only known once
        the stream has ended, so writing to the final filename means a failed export leaves a
        well-formed but truncated file there. That is the worst possible shape for a coverage
        artifact: parse_lcov() reads a truncated trace as a perfectly valid smaller one, so the
        report is over whichever files llvm-cov got to before it died and nothing downstream can
        tell. os.replace() within a directory is atomic, so the final path is either absent or a
        complete file, and the caller's existing "llvm-cov failed" path is what reports it.
        """
        partial = output_file + '.partial'
        try:
            result = write(partial)
        except BaseException:
            cls._discard(partial)
            raise
        if result.returncode:
            cls._discard(partial)
        else:
            os.replace(partial, output_file)
        return result

    @classmethod
    def _discard(cls, path):
        try:
            os.unlink(path)
        except OSError:
            pass

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
    def _sources_arguments(cls, sources):
        """The trailing --sources list, which must be last and must not be spelled --sources=PATH.

        Verified against the current Apple LLVM: --sources=PATH is accepted, silently ignored, and
        produces the whole report -- 1,022,546 lines for WebCore against 35,978 for the same scope
        passed as a separate argument -- with nothing on stderr. So the only spelling that works is
        the flag followed by one argument per path, at the end of the command line.

        The paths are matched against the absolute source paths recorded in the coverage mapping,
        and a relative one is resolved against llvm-cov's own working directory, so callers pass
        absolute paths.
        """
        return ['--sources', *sources] if sources else []

    @classmethod
    def show_html(cls, objects, profile_path, output_directory, ignore_filename_regexes=(),
                  path_equivalences=(), show_instantiations=False, sources=()):
        command = ['show', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences),
                   '--format=html', f'--output-dir={output_directory}']
        # Per-instantiation sub-views are on by default and are expensive on template-heavy
        # code: measured at 395 MB versus 300 MB of HTML for JavaScriptCore alone. Aggregate
        # per-file line coverage, which is what this report is for, is unaffected.
        # (There is no --skip-expansions in Apple LLVM; expansions are opt-in already.)
        if not show_instantiations:
            command.append('--show-instantiations=false')
        return LLVMCovExecutable.run([*command, *cls._sources_arguments(sources)],
                                     capture_output=True, text=True)

    @classmethod
    def export_lcov(cls, objects, profile_path, output_file, ignore_filename_regexes=(),
                    path_equivalences=(), compress=False, header_line=None, sources=()):
        command = ['export', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences),
                   '--format=lcov', *cls._sources_arguments(sources)]
        # header_line goes in ahead of llvm-cov's output rather than being prepended afterwards,
        # which would mean rewriting a 751MB stream. Nothing that reads a trace looks at a line
        # before the first SF: record, so a '#' comment there is carried by the artifact for free.
        if not compress:
            def write_plain(path):
                with open(path, 'w') as lcov_file:
                    if header_line:
                        lcov_file.write(header_line)
                        # llvm-cov inherits the descriptor at its current offset, so this has to
                        # be out of Python's buffer before the child writes anything.
                        lcov_file.flush()
                    return LLVMCovExecutable.run(command, stdout=lcov_file,
                                                 stderr=subprocess.PIPE, text=True)

            return cls._write_atomically(output_file, write_plain)

        # Pipe llvm-cov straight into the compressor, so the uncompressed trace never lands
        # on disk. A full-suite trace is 751MB of which 709MB is mangled function names, so
        # it compresses 13.8x to 54MB at level 6; that costs 5.8s of CPU inside the 22.8s the
        # export itself takes, so it is free in wall-clock terms and it keeps the report's
        # peak disk use to the size of the report. Level 9 measured 12.2s for 2.8% less.
        argv = [LLVMCovExecutable.preferred_path(), *command]

        def write_compressed(path):
            with tempfile.TemporaryFile() as diagnostics:
                # stderr goes to a file, not a pipe: nothing reads it while the trace is being
                # copied, and llvm-cov filling a pipe buffer would deadlock the copy.
                process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=diagnostics)
                with gzip.open(path, 'wb', compresslevel=LCOV_COMPRESSION_LEVEL) as compressed:
                    if header_line:
                        compressed.write(header_line.encode('utf-8'))
                    shutil.copyfileobj(process.stdout, compressed, 1024 * 1024)
                process.stdout.close()
                returncode = process.wait()
                diagnostics.seek(0)
                stderr = diagnostics.read().decode('utf-8', errors='replace')
            return subprocess.CompletedProcess(argv, returncode, stdout='', stderr=stderr)

        return cls._write_atomically(output_file, write_compressed)

    @classmethod
    def export_summary_json(cls, objects, profile_path, output_file, ignore_filename_regexes=(),
                            path_equivalences=(), sources=()):
        # --summary-only keeps this to per-file totals rather than per-line data, so it is
        # cheap next to the full export and is all a directory rollup needs.
        command = ['export', *cls._common_arguments(objects, profile_path,
                                                    ignore_filename_regexes, path_equivalences),
                   '--format=text', '--summary-only', *cls._sources_arguments(sources)]

        def write(path):
            with open(path, 'w') as json_file:
                return LLVMCovExecutable.run(command, stdout=json_file, stderr=subprocess.PIPE,
                                             text=True)

        return cls._write_atomically(output_file, write)

    @classmethod
    def report(cls, objects, profile_path, ignore_filename_regexes=(), path_equivalences=(),
               check_binary_ids=False, sources=()):
        command = ['report', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences)]
        # Makes llvm-cov emit a per-object "profile data may be out of date" warning, which is
        # the only reliable way to notice that the tree was rebuilt after the tests ran.
        if check_binary_ids:
            command.append('--check-binary-ids')
        return LLVMCovExecutable.run([*command, *cls._sources_arguments(sources)],
                                     capture_output=True, text=True)

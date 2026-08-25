#!/usr/bin/env python3

import glob
import gzip
import logging
import math
import os
import shlex
import shutil
import struct
import subprocess
import tempfile

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
        # which is also what a stripped binary looks like, so that is "cannot tell".
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
    def export_lcov(cls, objects, profile_path, output_file, ignore_filename_regexes=(), path_equivalences=(),
                    compress=False):
        command = ['export', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences),
                   '--format=lcov']
        if not compress:
            with open(output_file, 'w') as lcov_file:
                return LLVMCovExecutable.run(command, stdout=lcov_file, stderr=subprocess.PIPE, text=True)

        # Pipe llvm-cov straight into the compressor, so the uncompressed trace never lands
        # on disk. A full-suite trace is 751MB of which 709MB is mangled function names, so
        # it compresses 13.8x to 54MB at level 6; that costs 5.8s of CPU inside the 22.8s the
        # export itself takes, so it is free in wall-clock terms and it keeps the report's
        # peak disk use to the size of the report. Level 9 measured 12.2s for 2.8% less.
        argv = [LLVMCovExecutable.preferred_path(), *command]
        with tempfile.TemporaryFile() as diagnostics:
            # stderr goes to a file, not a pipe: nothing reads it while the trace is being
            # copied, and llvm-cov filling a pipe buffer would deadlock the copy.
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=diagnostics)
            with gzip.open(output_file, 'wb', compresslevel=LCOV_COMPRESSION_LEVEL) as compressed:
                shutil.copyfileobj(process.stdout, compressed, 1024 * 1024)
            process.stdout.close()
            returncode = process.wait()
            diagnostics.seek(0)
            stderr = diagnostics.read().decode('utf-8', errors='replace')
        return subprocess.CompletedProcess(argv, returncode, stdout='', stderr=stderr)

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

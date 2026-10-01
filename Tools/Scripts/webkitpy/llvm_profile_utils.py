#!/usr/bin/env python3

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
import threading
import time

from collections import namedtuple
from dataclasses import dataclass
from functools import cache

logger = logging.getLogger(__name__)

REQUIRED_FRAMEWORK_NAMES = ('JavaScriptCore', 'WebCore', 'WebKit')
FRAMEWORK_NAME_PATTERN = re.compile(r'[A-Za-z][A-Za-z0-9+-]*')

_LLVM_VERSION = re.compile(r'\bLLVM version (\d+(?:\.\d+)*)')


def read_llvm_version(path):
    """((major, minor, ...), banner line) of an LLVM tool, or (None, None).
    Does not require a zero exit: the old llvm-cov prints its banner and exits 1."""
    try:
        completed = subprocess.run([path, '--version'], check=False, text=True,
                                   capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as failure:
        logger.debug(f'Could not ask {path} for its version: {failure}')
        return (None, None)
    for line in (completed.stdout or '').splitlines():
        match = _LLVM_VERSION.search(line)
        if match:
            return (tuple(int(component) for component in match.group(1).split('.')), line.strip())
    return (None, None)


@dataclass(frozen=True)
class ProfiledFramework:
    name: str
    optional: bool = False


def resolve_profiled_frameworks(directories, suffix='.profdata'):
    if isinstance(directories, str):
        raise TypeError('Directories must be passed as a sequence of paths, not a single path, '
                        'which would be globbed one character at a time')
    directories = list(directories)

    discovered = set()
    for directory in directories:
        for path in sorted(glob.glob(os.path.join(directory, f'*{suffix}'))):
            if not os.path.isfile(path):
                continue
            basename = os.path.basename(path)[:-len(suffix)]
            name = basename
            if suffix == '.profraw':
                name = basename.partition('_')[0]
            if not FRAMEWORK_NAME_PATTERN.fullmatch(name):
                logger.warning(f'Ignoring {path}: {name!r} is not a valid framework name')
                continue
            if suffix == '.profraw' and name == basename:
                logger.warning(f'Ignoring {path}: expected {name}_%m_pid%p%c.profraw')
                continue
            discovered.add(name)

    optional_names = sorted(discovered.difference(REQUIRED_FRAMEWORK_NAMES))
    logger.info(f'Processing {", ".join(REQUIRED_FRAMEWORK_NAMES)} as required; '
                f'discovered {", ".join(optional_names) if optional_names else "no other framework"} '
                f'from *{suffix} in {", ".join(directories)}')

    return tuple([ProfiledFramework(name) for name in REQUIRED_FRAMEWORK_NAMES]
                 + [ProfiledFramework(name, optional=True) for name in optional_names])


def weighted_profiles_for_framework(framework, directory_weight_pairs):
    if not directory_weight_pairs:
        raise ValueError('weighted_profiles_for_framework needs at least one (directory, weight) pair')

    profile_weight_pairs = [(os.path.join(directory, f'{framework.name}.profdata'), weight)
                            for directory, weight in directory_weight_pairs]

    if framework.optional:
        available = []
        missing = []
        for path, weight in profile_weight_pairs:
            if os.path.isfile(path):
                available.append((path, weight))
            else:
                missing.append(path)
        if missing:
            logger.debug(f'Missing optional {framework.name} profiles: {missing}')
        profile_weight_pairs = available

    return simplify_profile_weights(profile_weight_pairs)


def locate_binary_xcrun(sdk, binary_name):
    try:
        completed_process = subprocess.run(['/usr/bin/xcrun', '-sdk', sdk, '--find', binary_name],
                                           check=False, text=True, capture_output=True)
    except OSError as e:
        logger.debug(f'Failed to run xcrun: {e}')
        return None
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
    EXECUTABLE_NAME = None
    SEARCH_PATH_FIRST = False
    preferred_binary_path = None

    @classmethod
    def order_binaries(cls, binary_paths):
        return binary_paths

    # Shared by every subclass, so llvm-cov's discovery is serialized against llvm-profdata's too.
    _usable_binaries_lock = threading.Lock()

    @classmethod
    @cache
    def detect_binaries(cls):
        # Coverage prefers the toolchain's own binary over PATH: the raw profile format has no compatibility
        # guarantees, so the tools must come from the toolchain that built the instrumented binaries.
        binaries = []
        for sdk_name in ('macosx.internal', 'iphoneos.internal', 'macosx', 'iphoneos'):
            binary_path = locate_binary_xcrun(sdk_name, cls.EXECUTABLE_NAME)
            if binary_path and binary_path not in binaries:
                binaries.append(binary_path)

        binary_from_search_path = shutil.which(cls.EXECUTABLE_NAME)
        if binary_from_search_path:
            if cls.SEARCH_PATH_FIRST:
                binaries = [binary_from_search_path] + [path for path in binaries if path != binary_from_search_path]
            elif binary_from_search_path not in binaries:
                binaries.append(binary_from_search_path)

        binaries = cls.order_binaries(binaries)
        logger.debug(f'Available {cls.EXECUTABLE_NAME} from {binaries}')

        return binaries

    @classmethod
    @cache
    def version_of(cls, path):
        """(major, banner line) for a binary, or (None, None)."""
        version, line = read_llvm_version(path)
        return (version[0], line) if version else (None, None)

    @classmethod
    def usable_binaries(cls):
        """detect_binaries() with anything that is not the profile's own toolchain removed.
        Serialized because @cache does not make the computation atomic: concurrent first calls would each rediscover."""
        with cls._usable_binaries_lock:
            return cls._usable_binaries()

    @classmethod
    @cache
    def _usable_binaries(cls):
        """The list itself. Call usable_binaries(), which holds the lock.
        The toolchain's copy is the reference; any other LLVM major is rejected, but an unreadable version is kept."""
        binaries = cls.detect_binaries()
        if not binaries:
            return []
        reference_major, reference_version = cls.version_of(binaries[0])
        usable, rejected = [binaries[0]], []
        for path in binaries[1:]:
            major, version = cls.version_of(path)
            if reference_major is not None and major is not None and major != reference_major:
                rejected.append((path, major, version))
                continue
            usable.append(path)
        for path, major, version in rejected:
            logger.warning('Ignoring %s (%s): it reports LLVM major version %s, not the %s of '
                           'the toolchain that built these binaries, %s (%s). The raw profile '
                           'format has no compatibility guarantees between toolchains, in '
                           'either direction.', path, version or 'no readable version', major,
                           reference_major, binaries[0],
                           reference_version or 'no readable version')
        logger.info('Using %s (%s)%s', usable[0],
                    reference_version or 'no readable version',
                    '' if len(usable) == 1 else ', with {} fallback(s)'.format(len(usable) - 1))
        return usable

    @classmethod
    def preference_ordered_paths(cls):
        """The binaries run() will try, best first, starting at the last one that worked.
        preferred_path() reads the same order, so concurrent report and export use the same binary."""
        binary_paths = cls.usable_binaries()
        start = binary_paths.index(cls.preferred_binary_path) if cls.preferred_binary_path in binary_paths else 0
        return binary_paths[start:] + binary_paths[:start]

    @classmethod
    def preferred_path(cls):
        """The single binary to use when the caller cannot retry a failure, such as one streaming into a compressor.
        It is the same binary run() tries first."""
        binaries = cls.preference_ordered_paths()
        if not binaries:
            raise RuntimeError(f'Found no {cls.EXECUTABLE_NAME} in the toolchain or on PATH')
        return binaries[0]

    @staticmethod
    def _output_position(stdout):
        try:
            return stdout.tell() if stdout.seekable() else None
        except (AttributeError, OSError, ValueError):
            return None

    @classmethod
    def run(cls, command, *args, check=False, stdout=None, stderr=None, capture_output=False,
            **kwargs) -> subprocess.CompletedProcess:
        kwarg_capture_output = capture_output or (stdout is None and stderr is None)
        output_start = cls._output_position(stdout)

        completed_process = None
        for binary_path in cls.preference_ordered_paths():
            if completed_process is not None and output_start is not None:
                stdout.seek(output_start)
                stdout.truncate()
            logger.debug(f'Running {shlex.join([binary_path, *command])}')
            completed_process = subprocess.run([binary_path, *command], *args,
                                               check=False, capture_output=kwarg_capture_output,
                                               stdout=stdout, stderr=stderr, **kwargs)
            if not completed_process.returncode:
                cls.preferred_binary_path = binary_path
                break

            logger.debug(f'Failed to {command} with binary {binary_path}\n'
                         f'return_code: {completed_process.returncode}\n'
                         f'stdout: {completed_process.stdout}\n'
                         f'stderr: {completed_process.stderr}\n')

        if completed_process is None:
            raise RuntimeError(f'Found no {cls.EXECUTABLE_NAME} in the toolchain or on PATH')

        if check:
            completed_process.check_returncode()

        return completed_process


class LLVMProfDataExecutable(ExecutablesFromEnvAndXcode):
    EXECUTABLE_NAME = 'llvm-profdata'
    SEARCH_PATH_FIRST = True

    @classmethod
    @cache
    def binary_version(cls, binary_path):
        return read_llvm_version(binary_path)[0]

    @classmethod
    def order_binaries(cls, binary_paths):
        versions = {binary_path: cls.binary_version(binary_path) for binary_path in binary_paths}
        logger.debug(f'{cls.EXECUTABLE_NAME} versions: {versions}')
        return sorted(binary_paths, key=lambda binary_path: (versions[binary_path] is None, versions[binary_path] or ()))

    @classmethod
    @cache
    def _usable_binaries(cls):
        # No major-version filter: a merged PGO profile takes the format of the binary that wrote it, so
        # order_binaries() puts the oldest first. Coverage uses CoverageLLVMProfDataExecutable instead.
        return list(cls.detect_binaries())


class CoverageLLVMProfDataExecutable(ExecutablesFromEnvAndXcode):
    # Chosen like llvm-cov: the toolchain's copy first, any other LLVM major refused.
    EXECUTABLE_NAME = 'llvm-profdata'


class LLVMProfileData:
    @classmethod
    def show(cls, profile_path):
        list_functions_process = LLVMProfDataExecutable.run(['show', '--all-functions', '--value-cutoff=10',
                                                             profile_path], stdout=subprocess.PIPE, text=True)

        return subprocess.run(['/usr/bin/c++filt', '-n'], input=list_functions_process.stdout,
                              capture_output=True, text=True, check=True)

    @classmethod
    def merge(cls, output_file, unweighted_profiles=(), weighted_profiles=(), failure_mode=None, num_threads=None,
              executable=LLVMProfDataExecutable):
        command = ['merge', '--sparse', *unweighted_profiles]
        for profile_path, weight in weighted_profiles:
            lib_profile_path = profile_path
            command.extend(['--weighted-input', f'{weight},{lib_profile_path}'])

        # The harness hard-kills coverage-run processes, so a truncated profile must not sink the merge.
        if failure_mode:
            command.append(f'--failure-mode={failure_mode}')
        if num_threads is not None:
            command.append(f'--num-threads={num_threads}')

        command.extend(['--output', output_file])

        return executable.run(command, capture_output=True, text=True)

    @classmethod
    def compress(cls, input_profile, output_file):
        return subprocess.run(['/usr/bin/compression_tool', '-encode', '-i', input_profile, '-o', output_file,
                               '-a', 'lzfse'], capture_output=True, check=True, text=True)

    @classmethod
    def decompress(cls, input_profile, output_file):
        subprocess.run(['/usr/bin/touch', output_file], check=True)
        return subprocess.run(['/usr/bin/compression_tool', '-decode', '-i', input_profile, '-o', output_file,
                               '-a', 'lzfse'], capture_output=True, check=True, text=True)


def merge_raw_profiles_in_directory(frameworks, input_directory, output_directory=None,
                                    input_suffix='.profraw', output_suffix='.profdata'):
    output_files = []
    for framework in frameworks:
        pattern = f'{framework.name}_*{input_suffix}'
        input_profiles = glob.glob(os.path.join(input_directory, pattern))

        logger.info(f'Merging {framework.name}')
        output_file = os.path.join(output_directory or input_directory, f'{framework.name}{output_suffix}')
        merge_process = LLVMProfileData.merge(output_file, unweighted_profiles=input_profiles)
        logger.info(f'stdout: {merge_process.stdout}')
        logger.info(f'stderr: {merge_process.stderr}')
        merge_process.check_returncode()
        output_files.append(output_file)
        logger.info(f'{framework.name} is successfully merged')

    return output_files


# Fraction of raw profiles that may be unreadable before reporting is refused. --failure-mode=all only fails when every
# input does, and a double-digit loss is systemic (mismatched toolchain, rebuild mid-run, full disk).
UNREADABLE_RAW_PROFILE_LIMIT = 0.1

# unreadable: [(path, what llvm-profdata said)] for the inputs that contributed nothing.
ProfileMerge = namedtuple('ProfileMerge', ('merged', 'unreadable'))

# One line per unreadable input on stderr, with the path as passed: 'warning: <path>: truncated profile data'.
_PROFDATA_WARNING = re.compile(r'^warning: (?P<path>.*?): (?P<reason>.*)$')


def unreadable_profiles_from_stderr(stderr, input_profiles):
    """[(path, reason)] for the inputs llvm-profdata reported it could not read.
    Keyed on the input list, so an unrelated warning is never counted as a lost profile."""
    reasons = {}
    for line in (stderr or '').splitlines():
        match = _PROFDATA_WARNING.match(line)
        if match and match.group('path') in set(input_profiles):
            reasons[match.group('path')] = match.group('reason')
    return [(path, reasons[path]) for path in input_profiles if path in reasons]


def merge_coverage_profiles(input_directory, output_file, input_suffix='.profraw',
                            unreadable_limit=UNREADABLE_RAW_PROFILE_LIMIT):
    """Merge every raw profile in a directory into a single indexed profile. -> ProfileMerge.
    llvm-cov takes one --instr-profile, so this makes one index; more than unreadable_limit unreadable inputs raise."""
    input_profiles = sorted(glob.glob(os.path.join(input_directory, f'*{input_suffix}')))
    if not input_profiles:
        raise RuntimeError(f'No {input_suffix} files found in {input_directory}')

    logger.info(f'Merging {len(input_profiles)} raw profiles from {input_directory}')
    merge_process = LLVMProfileData.merge(output_file, unweighted_profiles=input_profiles,
                                          failure_mode='all', num_threads=0,
                                          executable=CoverageLLVMProfDataExecutable)

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


def merge_all_coverage_profiles(input_directory, output_file, input_suffix='.profraw'):
    """The raw profiles that contributed to a merge of everything in a directory (unreadable ones dropped)."""
    return merge_coverage_profiles(input_directory, output_file, input_suffix).merged


class LLVMCovExecutable(ExecutablesFromEnvAndXcode):
    EXECUTABLE_NAME = 'llvm-cov'


# gzip level for the lcov trace: 6 is zlib's default, and higher levels cost far more time for little size.
LCOV_COMPRESSION_LEVEL = 6


# The directory coverage-instrumented frameworks bake into __llvm_profile_filename (WebKit2InitializeCocoa.mm). It is
# the only path the WebContent, GPU and Networking sandboxes allow profile writes to, so harnesses collect from it.
COVERAGE_PROFILE_DIRECTORY = '/private/tmp/WebKitCoverage'


# Enough Mach-O to tell whether a binary is instrumented for coverage and where it will write its profile. An unbaked
# __llvm_profile_filename (a weak empty string) writes default.profraw to the working directory, uncollected.
_FAT_MAGIC = b'\xca\xfe\xba\xbe'
_FAT_MAGIC_64 = b'\xca\xfe\xba\xbf'
_MACHO_MAGIC_64_LITTLE_ENDIAN = b'\xcf\xfa\xed\xfe'
_LC_SEGMENT_64 = 0x19
_LC_SYMTAB = 0x02
# nlist_64.n_type: N_STAB is a mask (any bit set means a debugging entry); only N_SECT has an address in n_value.
_N_STAB = 0xe0
_N_TYPE = 0x0e
_N_SECT = 0x0e

# Coverage builds rename the counters segment to __MMAP_DATA, so match on the section name only.
COVERAGE_COUNTERS_SECTION = '__llvm_prf_cnts'
PROFILE_FILENAME_SYMBOL = b'___llvm_profile_filename'

# profile_filename: the baked-in __llvm_profile_filename, '' when none is baked in, None when the symbol is not in the
# symbol table (also how a stripped binary looks), so None means 'cannot tell', never 'broken'.
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
    Skips stabs and non-section entries, whose n_value is no address; a name may sit at several string-table indexes."""
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
    A file that is not a 64-bit little-endian Mach-O reads as uninstrumented rather than raising."""
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
    '/private/tmp/WebKitCoverage/WebGPU_%8m%c.profraw' -> 'WebGPU_', which matches collected profiles to binaries."""
    return os.path.basename(profile_filename).split('%')[0]


def collected_profile_group(profile_basename):
    """The group a collected raw profile belongs to: 'WebCore_4820_0.profraw' -> 'WebCore_'.
    The inverse of profile_name_prefix(); a name with no underscore (default.profraw) is its own group."""
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
    Catches a product whose profiles were merged but which no binary describes, so llvm-cov reports it as absent."""
    claimed = claimed_profile_name_prefixes(binary_paths)
    groups = {}
    for path in raw_profile_paths:
        name = os.path.basename(path)
        if any(name.startswith(prefix) for prefix in claimed):
            continue
        groups.setdefault(collected_profile_group(name), []).append(path)
    return sorted(groups.items())


def partition_unclaimed_profiles(orphans, unreported_writer_names, left_out_prefixes=()):
    """Split collected_profiles_with_no_object()'s result into (unexplained, expected).
    Expected groups are known non-report writers plus left_out_prefixes, the binaries the report omits on purpose."""
    expected_groups = {name + '_' for name in unreported_writer_names} | set(left_out_prefixes)
    unexplained = [entry for entry in orphans if entry[0] not in expected_groups]
    expected = [entry for entry in orphans if entry[0] in expected_groups]
    return unexplained, expected


# Both are [(path, reason)]; unverifiable means instrumented but with an unreadable profile path.
InstrumentationSurvey = namedtuple('InstrumentationSurvey', ('uninstrumented', 'unverifiable'))


def survey_instrumentation(binary_paths):
    """The binaries objects_with_no_profile_data() has to skip, and why. -> InstrumentationSurvey
    An uninstrumented tree yields an empty report, not a wrong one; unverifiable means "cannot tell", never "broken"."""
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
    Flags a baked path outside the collection directory or, given raw profiles, one that matched none of them."""
    collected = [os.path.basename(path) for path in raw_profile_paths]
    findings = []
    for path in binary_paths:
        try:
            instrumentation = read_instrumentation(path)
        except (OSError, struct.error) as failure:
            logger.debug(f'Could not read instrumentation from {path}: {failure}')
            continue
        # None means 'cannot tell'; survey_instrumentation() reports it.
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


# Lives inside the profile directory so the lock and what it protects cannot be separated.
COVERAGE_PROFILE_LOCK_FILENAME = '.webkit-coverage-run.lock'

# Held for the process lifetime: flock is released when the last descriptor closes, and prepare/collect run at different
# times. A SIGKILLed run releases it, so there is no stale-lock case.
_held_profile_directory_lock = None

StaleProfiles = namedtuple('StaleProfiles', ('count', 'total_bytes'))


class CoverageProfileDirectoryInUse(RuntimeError):
    """Another coverage run holds the machine-global profile directory."""


def coverage_profile_lock_path():
    return os.path.join(COVERAGE_PROFILE_DIRECTORY, COVERAGE_PROFILE_LOCK_FILENAME)


def acquire_coverage_profile_directory_lock():
    """Claim the profile directory for this process, or raise CoverageProfileDirectoryInUse.
    The directory is machine-global and each run deletes and collects every profile in it, so a second is refused."""
    global _held_profile_directory_lock
    if _held_profile_directory_lock is not None:
        return _held_profile_directory_lock

    os.makedirs(COVERAGE_PROFILE_DIRECTORY, exist_ok=True)
    path = coverage_profile_lock_path()
    try:
        handle = open(path, 'a+')
    except OSError as failure:
        # The directory is mode 1777, so the lock file can belong to somebody else: warn rather than refuse to run.
        logger.warning('Cannot open %s (%s), so a concurrent coverage run cannot be detected. '
                       'Two runs at once destroy each other\'s profiles.', path, failure)
        return None

    import fcntl
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


def coverage_profile_directory_holder():
    """The run holding the profile directory, as a string, or None, without taking the lock.
    Asks the kernel with a shared flock, since the lock file's text outlives the run that wrote it."""
    if _held_profile_directory_lock is not None:
        # This process is the holder; flock would conflict with our own descriptor.
        _held_profile_directory_lock.seek(0)
        return _held_profile_directory_lock.read(4096).strip() or 'this process'

    try:
        handle = open(coverage_profile_lock_path(), 'r')
    except OSError:
        return None
    import fcntl
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return handle.read(4096).strip() or 'an unidentified process'
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        handle.close()


def remove_raw_profiles(directory):
    count, total_bytes = 0, 0
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            if not name.endswith('.profraw'):
                continue
            path = os.path.join(directory, name)
            try:
                total_bytes += os.path.getsize(path)
            except OSError:
                pass
            os.unlink(path)
            count += 1
    return StaleProfiles(count, total_bytes)


def prepare_coverage_profile_directory():
    """Claim the profile directory, then remove any profiles left over from an earlier run.
    The clearing is load-bearing: %Nm merges into an existing profile rather than replacing it."""
    acquire_coverage_profile_directory_lock()
    stale = remove_raw_profiles(COVERAGE_PROFILE_DIRECTORY)
    # The instrumented processes create the file, but not a missing directory's parents.
    os.chmod(COVERAGE_PROFILE_DIRECTORY, 0o1777)
    if stale.count:
        # Logged with a count: this is data being thrown away.
        logger.info('Removed %d stale raw profile(s) (%d MB) from %s', stale.count,
                    stale.total_bytes // (1024 * 1024), COVERAGE_PROFILE_DIRECTORY)
    return stale


def collect_coverage_profiles(destination_directory):
    """Move this run's raw profiles into destination_directory, and return their paths.
    Names are made unique on collision so that successive runs can accumulate into one directory."""
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
        # llvm-cov takes the first object positionally and the rest as -object=. Pass every instrumented binary:
        # an omitted one silently under-reports the files only it contains.
        first, *rest = objects
        return [first, *[f'-object={path}' for path in rest]]

    @classmethod
    def _write_atomically(cls, output_file, write):
        """write(temporary path) -> CompletedProcess, published to output_file only on success.
        A failed export would otherwise leave a truncated file that parse_lcov() reads as a valid, smaller trace."""
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
        """The trailing --sources list: last, one argument per path (--sources=PATH is silently ignored).
        Paths must be absolute: llvm-cov resolves relative ones against its own working directory."""
        return ['--sources', *sources] if sources else []

    @classmethod
    def show_html(cls, objects, profile_path, output_directory, ignore_filename_regexes=(),
                  path_equivalences=(), show_instantiations=False, sources=()):
        command = ['show', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences),
                   '--format=html', f'--output-dir={output_directory}']
        # Per-instantiation sub-views are costly on template-heavy code and do not change per-file line coverage.
        if not show_instantiations:
            command.append('--show-instantiations=false')
        return LLVMCovExecutable.run([*command, *cls._sources_arguments(sources)],
                                     capture_output=True, text=True)

    @classmethod
    def export_lcov(cls, objects, profile_path, output_file, ignore_filename_regexes=(),
                    path_equivalences=(), compress=False, header_line=None, sources=()):
        command = ['export', *cls._common_arguments(objects, profile_path, ignore_filename_regexes, path_equivalences),
                   '--format=lcov', *cls._sources_arguments(sources)]
        # header_line goes ahead of llvm-cov's output rather than being prepended afterwards, which rewrites the stream.
        # Readers skip everything before the first SF: record, so a '#' comment there is free.
        if not compress:
            def write_plain(path):
                with open(path, 'w') as lcov_file:
                    if header_line:
                        lcov_file.write(header_line)
                        # llvm-cov inherits the descriptor at its current offset, so flush before the child writes.
                        lcov_file.flush()
                    return LLVMCovExecutable.run(command, stdout=lcov_file,
                                                 stderr=subprocess.PIPE, text=True)

            return cls._write_atomically(output_file, write_plain)

        # Pipe llvm-cov straight into the compressor so the uncompressed trace never lands on disk.
        argv = [LLVMCovExecutable.preferred_path(), *command]

        def write_compressed(path):
            with tempfile.TemporaryFile() as diagnostics:
                # stderr goes to a file, not a pipe: nothing drains it during the copy, so a full pipe would deadlock.
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
    def export_regions_json(cls, objects, profile_path, output_file, ignore_filename_regexes=(),
                            path_equivalences=(), sources=()):
        """The per-file region segments, for highlighting what did not run inside a line.
        Functions, expansions and branches are skipped: the line views take branches from the lcov trace."""
        command = ['export', *cls._common_arguments(objects, profile_path,
                                                    ignore_filename_regexes, path_equivalences),
                   '--format=text', '--skip-functions', '--skip-expansions', '--skip-branches',
                   *cls._sources_arguments(sources)]

        def write(path):
            with open(path, 'w') as json_file:
                return LLVMCovExecutable.run(command, stdout=json_file, stderr=subprocess.PIPE,
                                             text=True)

        return cls._write_atomically(output_file, write)

    @classmethod
    def export_summary_json(cls, objects, profile_path, output_file, ignore_filename_regexes=(),
                            path_equivalences=(), sources=()):
        # --summary-only keeps this to per-file totals, which is all a directory rollup needs.
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
        # Warns per object when profile data may be out of date: the only way to notice a rebuild after the tests ran.
        if check_binary_ids:
            command.append('--check-binary-ids')
        return LLVMCovExecutable.run([*command, *cls._sources_arguments(sources)],
                                     capture_output=True, text=True)

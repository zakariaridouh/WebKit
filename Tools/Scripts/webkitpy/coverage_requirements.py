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

"""What a coverage run requires of a build, in one place, and whether a tree meets it.

A working coverage run needs three things of a build: --coverage,
ENABLE_USER_SCRIPT_SANDBOXING=NO and a WEBKIT_OUTPUTDIR of its own. Getting any of them wrong
is silent, and one of them is silent for hours:

  * Forget --coverage and a `run-webkit-tests --coverage` run completes, exits 0 and collects
    nothing. The only signal is one warning from generate-coverage-report, which for the
    layout suite arrives after the run.
  * Forget ENABLE_USER_SCRIPT_SANDBOXING=NO inside a sandbox and every Xcode script phase
    fails with `sandbox_apply: Operation not permitted`, because sandbox-exec cannot nest.
  * Reuse the ordinary build directory and the instrumented frameworks -- WebCore alone is
    8.7x its uninstrumented size -- replace the ones every other build uses.

There were four. --lto-mode=none was the fourth until it was measured and found to be a
no-op: Debug, Release and Profiling already default to no LTO (DebugRelease.xcconfig sets
WK_DEFAULT_LTO_MODE = $(WK_USER_LTO_MODE_none)), and a real --coverage --lto-mode=thin build
of everything up to JavaScriptCore succeeds with zero duplicate symbols. Duplicate
__llvm_profile_filename reproduces only with two *strong* definitions in one link, and it
fails identically with and without LTO. Deleting its tuple entry removed it from the build
command, from every explanation and from the pre-flight's remedy at once, which is why these
live here rather than in the front door.

The product lists live here too, for the same reason. "Which Mach-Os must exist and be
instrumented" is the same question as "what does a coverage build have to have produced",
and the pre-flight below and generate-coverage-report's llvm-cov invocation must agree about
it: a product missing from either list is silently absent from the report rather than
reported at 0%, which is the 84,332-line bug this tooling shipped for a week.
"""

import logging
import os
import struct
import subprocess
import time

from collections import namedtuple

logger = logging.getLogger(__name__)

# argument: exactly what to add to a build-webkit command line.
# why: one sentence, printed whenever it is added, because a tool that silently corrects an
#     invocation teaches nobody what the invocation should have been.
BuildSetting = namedtuple('BuildSetting', ('argument', 'why'))

MANDATORY_BUILD_SETTINGS = (
    BuildSetting('--coverage',
                 'instruments the build (CLANG_COVERAGE_MAPPING) and bakes '
                 '/private/tmp/WebKitCoverage/<Framework>_%4m%c.profraw into each framework, '
                 'which is what makes a test run collect anything at all'),
    BuildSetting('ENABLE_USER_SCRIPT_SANDBOXING=NO',
                 'Xcode wraps every script phase in sandbox-exec, which cannot nest, so inside '
                 'an agent or CI sandbox each phase fails with "sandbox_apply: Operation not '
                 'permitted". It does not affect the output binaries'),
)

# The other undocumented requirement. An instrumented WebCore is many times the size of an
# uninstrumented one and is not what anybody wants their ordinary build to be, so a coverage
# build belongs in a tree of its own -- and both the tests and the report have to be pointed at
# that same tree, which webkit-build-directory's last-built tiebreaker will not do reliably.
BUILD_OUTPUT_ENVIRONMENT_VARIABLE = 'WEBKIT_OUTPUTDIR'

DEFAULT_BUILD_DIRECTORY_NAME = 'WebKitBuild-Coverage'

# Every instrumented binary the report must be given. Omitting one does not fail or warn:
# llvm-cov simply reports nothing for the files only that binary contains, so the code looks
# absent rather than uncovered. WebKitLegacy was missing here once, which silently discarded all
# 302 of its files -- 41 MB of instrumented product code with eight __llvm_prf sections.
#
# WTF and bmalloc need no entry: they are static libraries, so their coverage mapping lives
# inside the binaries that link them.
INSTRUMENTED_PRODUCTS = (
    'JavaScriptCore.framework/Versions/A/JavaScriptCore',
    'WebCore.framework/Versions/A/WebCore',
    'WebKit.framework/Versions/A/WebKit',
    'WebKitLegacy.framework/Versions/A/WebKitLegacy',
    'WebGPU.framework/Versions/A/WebGPU',
)

# Instrumented too, and loaded during a test run, but they are test scaffolding or tools
# rather than shipping code, so including them would put their line counts in the product
# denominator. Skipped by default -- but reported as skipped, because silently dropping
# instrumented data is exactly the bug above.
TEST_SUPPORT_PRODUCTS = (
    'libWebCoreTestSupport.dylib',
    'jsc',
    'webpushd',
    'adattributiond',
)

# Everything --products may name. Both lists, because --products restricts both.
KNOWN_PRODUCTS = INSTRUMENTED_PRODUCTS + TEST_SUPPORT_PRODUCTS

FATAL, WARNING, NOTE = 'fatal', 'warning', 'note'

# summary: one line naming what is wrong.
# detail: the evidence, printed under the summary.
# remedy: the command or edit that fixes it, printed verbatim so it can be copied.
Finding = namedtuple('Finding', ('severity', 'summary', 'detail', 'remedy'))


def product_name(relative):
    """The name --products matches on: JavaScriptCore, WebCore, jsc, ...

    The basename, which is the framework's binary for a framework and the tool itself for a
    tool, so one spelling covers both kinds of entry.
    """
    return os.path.basename(relative)


def coverage_build_command(configuration, build_style='--xcode', extra_arguments=(),
                           script='Tools/Scripts/build-webkit'):
    """The build-webkit command line a coverage build needs, mandatory settings included."""
    return [script, build_style, '--' + configuration.lower(),
            *[setting.argument for setting in MANDATORY_BUILD_SETTINGS],
            *extra_arguments]


def missing_build_settings(arguments):
    """The mandatory settings an argument list does not already carry.

    Matched on the whole argument, and for a `--flag=value` setting also on `--flag` alone,
    so that a developer who passed --lto-mode=thin is told that it is being replaced rather
    than ending up with both spellings on one command line.
    """
    given = list(arguments)
    missing = []
    for setting in MANDATORY_BUILD_SETTINGS:
        flag = setting.argument.split('=')[0]
        if any(argument == setting.argument or argument.split('=')[0] == flag
               for argument in given):
            continue
        missing.append(setting)
    return missing


def conflicting_build_settings(arguments):
    """[(what was given, the mandatory setting it contradicts)] for an argument list."""
    conflicts = []
    for setting in MANDATORY_BUILD_SETTINGS:
        if '=' not in setting.argument:
            continue
        flag = setting.argument.split('=')[0]
        for argument in arguments:
            if argument != setting.argument and argument.split('=')[0] == flag:
                conflicts.append((argument, setting))
    return conflicts


# root: what to pass as --build-directory or WEBKIT_OUTPUTDIR, which is the directory the
#     configuration directory lives *in*.
# note: what was adjusted and why, or None when the path was already the right shape.
BuildRoot = namedtuple('BuildRoot', ('root', 'given', 'note'))

# The configuration directory names a build can produce. The port appends the last component of
# webkit-build-directory's answer to --build-directory, which for an embedded port is
# Release-iphoneos and friends, so a prefix test rather than an equality test.
_CONFIGURATIONS = ('Release', 'Debug', 'Production')


def _looks_like_a_configuration_directory(name):
    return any(name == configuration or name.startswith(configuration + '-')
               for configuration in _CONFIGURATIONS)


def normalize_build_root(path, configuration=None):
    """Accept either the build tree or its configuration directory. -> BuildRoot

    webkitpy's Port._build_path() joins --build-directory with the configuration name, so
    `--build-directory=.../WebKitBuild-Coverage/Release` resolves to
    `.../WebKitBuild-Coverage/Release/Release` -- a directory that does not exist, which makes
    every product "not found in the build directory" and the report empty. That is the shape
    every documented path is written in, so it is the first thing a reader of them types, and
    the error it produces names neither the doubling nor the flag.

    Both spellings are accepted here instead, and the adjustment is reported rather than made
    quietly: the developer's mental model of where their build is should survive using the tool.
    """
    given = path.rstrip('/') or path
    name = os.path.basename(given)
    if not _looks_like_a_configuration_directory(name):
        return BuildRoot(given, given, None)
    root = os.path.dirname(given)
    note = ('{} names the {} configuration directory itself. The build directory is the '
            'directory it is in, because the configuration is appended to it -- passing this '
            'through would look for {}. Using {} instead.'.format(
                given, name, os.path.join(given, name), root))
    if configuration and not (name == configuration or name.startswith(configuration + '-')):
        note += (' Note it is a {} directory and this run is configured for {}.'.format(
            name, configuration))
    return BuildRoot(root, given, note)


def build_directory_from_environment(environment=None):
    """WEBKIT_OUTPUTDIR, or None. Read here so that one module knows the variable's name."""
    environment = os.environ if environment is None else environment
    return environment.get(BUILD_OUTPUT_ENVIRONMENT_VARIABLE) or None


# products: [(relative path, absolute path)] for everything that was looked for.
# instrumented / uninstrumented / missing: [(relative, absolute)] partitioned by what was found.
# unbaked: [(relative, absolute)] instrumented, but with no profile path baked in, so whatever
#     they record is written to default.profraw in the process's working directory and collected
#     by nothing.
# unverifiable: [(relative, absolute)] instrumented, but with no __llvm_profile_filename symbol
#     to read, which is also what a stripped binary looks like.
# seconds: how long the whole survey took, because "it is instant" is the reason to do it before
#     a run rather than after one.
InstrumentationSurvey = namedtuple(
    'InstrumentationSurvey',
    ('products', 'instrumented', 'uninstrumented', 'missing', 'unbaked', 'unverifiable',
     'seconds'))


def survey_products(build_path, relative_products=None):
    """Read every product's Mach-O and say what it is. -> InstrumentationSurvey

    This is the pre-flight that matters, and it costs milliseconds: read_instrumentation()
    reads load commands, one symbol and one string, so the whole set of seven binaries -- five
    frameworks, libWebCoreTestSupport and jsc, over 1.5 GB of Mach-O -- is measured at 0.36 s.
    Against that, the failure it detects costs a whole layout run.
    """
    from webkitpy.llvm_profile_utils import read_instrumentation

    relative_products = (list(INSTRUMENTED_PRODUCTS) + list(TEST_SUPPORT_PRODUCTS)
                         if relative_products is None else list(relative_products))
    started = time.time()
    products, instrumented, uninstrumented, missing = [], [], [], []
    unbaked, unverifiable = [], []
    for relative in relative_products:
        absolute = os.path.join(build_path, *relative.split('/'))
        entry = (relative, absolute)
        products.append(entry)
        if not os.path.exists(absolute):
            missing.append(entry)
            continue
        try:
            instrumentation = read_instrumentation(absolute)
        except (OSError, struct.error) as failure:
            logger.debug('Could not read instrumentation from %s: %s', absolute, failure)
            unverifiable.append(entry)
            continue
        if not instrumentation.instrumented:
            uninstrumented.append(entry)
            continue
        instrumented.append(entry)
        if instrumentation.profile_filename is None:
            unverifiable.append(entry)
        elif not instrumentation.profile_filename:
            unbaked.append(entry)
    return InstrumentationSurvey(products, instrumented, uninstrumented, missing, unbaked,
                                 unverifiable, time.time() - started)


def instrumentation_findings(survey, build_path, build_command):
    """[Finding] for what a product survey says about a tree's fitness for a coverage run.

    Fatal when nothing that must be instrumented is, because a run against that tree collects
    nothing and says so only afterwards. Fatal too when one required framework is
    uninstrumented while others are: that is a partial or interrupted build, and the report it
    produces is missing whatever only that framework contains -- silently, since llvm-cov has
    no mapping for it and the files read as absent rather than as untested.
    """
    findings = []
    required = set(INSTRUMENTED_PRODUCTS)
    uninstrumented_required = [entry for entry in survey.uninstrumented if entry[0] in required]
    missing_required = [entry for entry in survey.missing if entry[0] in required]
    instrumented_required = [entry for entry in survey.instrumented if entry[0] in required]

    if not instrumented_required:
        findings.append(Finding(
            FATAL,
            'nothing under {} is instrumented for coverage'.format(build_path),
            'None of {} carries a {} section, so llvm-cov has no coverage mapping for any of '
            'them. A test run against this tree would complete, exit 0 and collect nothing; '
            'the only signal today arrives from generate-coverage-report after the run, which '
            'for the layout suite is hours later. Checked in {:.2f} s.'.format(
                ', '.join(product_name(relative) for relative, _ in survey.products),
                _counters_section_name(), survey.seconds),
            ' '.join(build_command)))
        return findings

    for relative, absolute in uninstrumented_required:
        findings.append(Finding(
            FATAL,
            '{} is not instrumented, but other frameworks in the same tree are'.format(
                product_name(relative)),
            '{} carries no {} section while {} of {} required frameworks do. That is a partial '
            'build, and a report from it is missing every file only this framework contains -- '
            'silently, because llvm-cov has no mapping for it, so those files read as absent '
            'rather than as untested.'.format(absolute, _counters_section_name(),
                                              len(instrumented_required), len(required)),
            ' '.join(build_command)))
    for relative, absolute in missing_required:
        findings.append(Finding(
            FATAL,
            '{} has not been built'.format(product_name(relative)),
            '{} does not exist, so every file only it contains would be absent from the report '
            'rather than reported at 0%.'.format(absolute),
            ' '.join(build_command)))

    for relative, absolute in survey.unbaked:
        if relative not in required:
            continue
        findings.append(Finding(
            WARNING,
            '{} is instrumented but has no profile path baked in'.format(product_name(relative)),
            '{} is instrumented and its __llvm_profile_filename is empty, so the profile '
            'runtime writes default.profraw relative to whatever the process\'s working '
            'directory is. Nothing collects that, and for a sandboxed WebContent, GPU or '
            'Networking process nothing is allowed to write it -- so every line in it is '
            'reported as untested while it executes. This is the 84,332-line bug: WebGPU and '
            'WebKitLegacy were both in exactly this state.'.format(absolute),
            None))
    unbaked_extras = [relative for relative, _ in survey.unbaked if relative not in required]
    if unbaked_extras:
        # One finding rather than one per binary. These are excluded from the report by default,
        # so nothing they do is misreported -- the fact worth knowing is that they cannot
        # contribute even with --include-test-support, which is a different statement from "they
        # were not exercised".
        findings.append(Finding(
            NOTE,
            '{} test-support binaries are instrumented with no profile path baked in'.format(
                len(unbaked_extras)),
            '{} write default.profraw into whatever their working directory happens to be, '
            'which nothing collects. They are excluded from the report by default, so this '
            'misreports nothing -- but --include-test-support would report them at 0% rather '
            'than reporting what they ran. jsc is a live instance of this.'.format(
                ', '.join(product_name(relative) for relative in unbaked_extras)),
            None))
    for relative, absolute in survey.unverifiable:
        findings.append(Finding(
            NOTE,
            'cannot tell where {} writes its profile'.format(product_name(relative)),
            '{} is instrumented, but __llvm_profile_filename is not in its symbol table, which '
            'is also what a stripped binary looks like. "Cannot tell" and "broken" are '
            'different answers.'.format(absolute),
            None))
    return findings


def _counters_section_name():
    from webkitpy.llvm_profile_utils import COVERAGE_COUNTERS_SECTION

    return COVERAGE_COUNTERS_SECTION


def _output(command, timeout=10, accept_failure=False):
    """command's stdout, or None when it could not be run. Never raises.

    accept_failure keeps the output of a command that exited non-zero, which is what a version
    probe wants: /usr/local/bin/llvm-cov on this machine prints `LLVM version 3.2svn Apple Build
    #3425-36` and then exits 1, and treating that as "no version" would hide the exact
    thirteen-major-version mismatch this is looking for.
    """
    try:
        completed = subprocess.run(command, check=False, text=True, capture_output=True,
                                   timeout=timeout)
    except (OSError, subprocess.SubprocessError) as failure:
        logger.debug('Could not run %s: %s', command[0], failure)
        return None
    if completed.returncode and not accept_failure:
        return None
    return completed.stdout


# The orphaned-server trap: a killed layout run leaves httpd, pywebsocket3 and a UDP
# listener behind, and the next run dies about 80 seconds in with `Address already in use`. The
# harness now detects both the webtransport-h3 mapping and the DNS server on 8053 and names the
# holder, so this survey is no longer the only thing that would notice -- but it runs before the
# build rather than at server startup, which is hours earlier.
_ORPHANED_SERVER_PATTERNS = (
    ('httpd', 'layout-test-results/httpd.conf'),
    ('pywebsocket3', 'pywebsocket3'),
)

_ORPHANED_SERVER_REMEDY = (
    "pkill -9 -f 'layout-test-results/httpd.conf'; pkill -9 -f pywebsocket3; "
    'lsof -nP -iUDP:8053')


def orphaned_server_findings():
    """[Finding] for servers a killed layout run left behind.

    pgrep rather than lsof for the processes, because it needs no privilege and cannot block;
    lsof only for the UDP port, whose holder has no distinctive command line.
    """
    findings = []
    for name, pattern in _ORPHANED_SERVER_PATTERNS:
        output = _output(['/usr/bin/pgrep', '-f', pattern])
        pids = [line.strip() for line in (output or '').splitlines() if line.strip()]
        if pids:
            findings.append(Finding(
                WARNING,
                '{} is still running from an earlier layout-test run'.format(name),
                'pgrep -f {} matched pid(s) {}. A layout run started now dies about 80 s in '
                'with "OSError: [Errno 48] Address already in use", after the build and the '
                'pre-flight have already succeeded.'.format(pattern, ', '.join(pids)),
                _ORPHANED_SERVER_REMEDY))
    output = _output(['/usr/sbin/lsof', '-nP', '-iUDP:8053'])
    if output and len(output.splitlines()) > 1:
        findings.append(Finding(
            WARNING,
            'something is holding UDP port 8053',
            'lsof -nP -iUDP:8053 reported {} line(s) of output. That port belongs to the '
            'layout tests\' DNS server, which binds it before the harness\'s port check '
            'runs, so the run fails at server startup rather than here.'.format(
                len(output.splitlines()) - 1),
            _ORPHANED_SERVER_REMEDY))
    return findings


def toolchain_findings():
    """[Finding] about the two binaries that will produce the numbers.

    /usr/local/bin/llvm-cov on the machine this was developed on is LLVM 3.2svn, thirteen major
    versions behind the clang that builds WebKit, and the raw profile format has no
    compatibility guarantees between toolchains. llvm_profile_utils now orders the candidates
    without mutating shared state and refuses one older than the toolchain, so which binary
    produced a given number is determined by the preference order -- but the machine still has
    four of them, so naming the candidates before the run is the cheapest way to make an
    ambiguity visible at the time it can still be acted on.
    """
    from webkitpy.llvm_profile_utils import LLVMCovExecutable, LLVMProfDataExecutable

    findings = []
    versions = {}
    for executable in (LLVMCovExecutable, LLVMProfDataExecutable):
        candidates = list(executable.detect_binaries())
        if not candidates:
            findings.append(Finding(
                FATAL, 'no {} in the toolchain or on PATH'.format(executable.EXECUTABLE_NAME),
                'Neither xcrun nor PATH could produce one, so nothing can index a profile or '
                'read a coverage mapping.', 'xcode-select --install'))
            continue
        versions[executable.EXECUTABLE_NAME] = (candidates[0], _version_of(candidates[0]))
        preferred_version = versions[executable.EXECUTABLE_NAME][1]
        others = [(path, _version_of(path)) for path in candidates[1:]]
        divergent = [(path, version) for path, version in others
                     if version != preferred_version]
        if divergent:
            findings.append(Finding(
                WARNING,
                '{} is ambiguous on this machine, and the candidates are not the same '
                'version'.format(executable.EXECUTABLE_NAME),
                'Using {} ({}). Also found {}. The runner prefers the toolchain\'s and refuses '
                'one older than it, so this is not a wrong answer -- but the raw profile format '
                'has no compatibility guarantees between toolchains, so an ambiguity is worth '
                'seeing. Every candidate is recorded in '
                'coverage-provenance.json.'.format(
                    candidates[0], preferred_version or 'no readable version',
                    '; '.join('{} ({})'.format(path, version or 'no readable version')
                              for path, version in divergent)),
                None))
        elif others:
            findings.append(Finding(
                NOTE, '{} is ambiguous on this machine'.format(executable.EXECUTABLE_NAME),
                'Using {}. {} other copies were found and all of them report {}, so which one '
                'runs does not change the numbers.'.format(candidates[0], len(others),
                                                           preferred_version),
                None))
    cov = versions.get('llvm-cov')
    profdata = versions.get('llvm-profdata')
    if cov and profdata and cov[1] and profdata[1] and cov[1] != profdata[1]:
        findings.append(Finding(
            WARNING, 'llvm-cov and llvm-profdata are from different toolchains',
            'llvm-cov is {} and llvm-profdata is {}. The raw profile format has no '
            'compatibility guarantees between toolchains, so a mismatched pair can fail or '
            'silently misreport.'.format(cov[1], profdata[1]),
            None))
    return findings


def _version_of(path):
    output = _output([path, '--version'], accept_failure=True)
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        # The first line of Apple LLVM's output is the vendor banner; the version is on it for
        # a modern build (`Apple LLVM version N.N.N`) and on the next one for LLVM 3.2svn
        # (`LLVM (http://llvm.org/):` then `  LLVM version 3.2svn ...`).
        if 'version' in stripped:
            return stripped
    return None

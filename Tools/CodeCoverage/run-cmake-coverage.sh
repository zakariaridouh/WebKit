#!/bin/bash
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
#
# One unattended CMake coverage run: configure, build, test, report.
#
# CMake-only by construction, which is why it is in the name: there is no build system to
# choose and so no --cmake to pass.
#
# Tools/Scripts/webkit-coverage --cmake answers a different question about the same tree --
# how well tested are the lines this patch added -- and scopes its report to the change. This
# produces a whole-tree report, which is what you want when the question is "what does the
# suite reach", and it is a straight-line script so it can be left alone.

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: Tools/CodeCoverage/run-cmake-coverage.sh [options] [layout-test-path ...]

Drives the CMake build, via the mac-coverage preset. There is no build-system option and no
--cmake to pass; for the Xcode build, or to measure a change rather than the whole tree, use
Tools/Scripts/webkit-coverage, which takes --cmake for this tree too.

  --output DIR      Where to write the report. Default WebKitBuild/coverage-report.
  --api-tests ARGS  Which API tests to run. Default all of them; "" to skip.
  --jobs N          Compile parallelism. Default: as many as there are cores.
  --full-suite      Every layout test. Hours. Implied when no path is given and
                    --no-layout-tests is absent.
  --no-layout-tests Skip the layout suite entirely.
  --open            Open the report when it is written.
  --sources PATH    Restrict the report to PATH. Repeatable. A whole-tree report is a few
                    hundred MB; one scoped to a directory is a few.

With no layout-test path this runs the whole suite, so give it a path -- fast/dom, or a
file -- when you want an answer in minutes rather than hours.

Read Tools/CodeCoverage/README.md first if this is the first run on this machine. The one
trap that matters here: some terminals and launchers send SIGTERM to descendants that
register as ordinary Dock-visible applications, and WebKitTestRunner does, so every layout
test looks like a crash. Run this from a plain login shell, over ssh, or under launchctl --
not from an IDE terminal or a multiplexer that does this. It affects instrumented and
uninstrumented trees alike, so it is not a coverage failure.

Only one coverage run can be in flight per machine: the profile directory is global and each
run clears it on startup. The tooling checks an advisory lock and will tell you.
USAGE
}

output_dir=""
api_tests="__all__"
jobs="$(sysctl -n hw.ncpu 2>/dev/null || echo 8)"
layout_tests=()
sources=()
run_layout=1
open_report=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output) output_dir="$2"; shift 2 ;;
        --api-tests) api_tests="$2"; shift 2 ;;
        --jobs|-j) jobs="$2"; shift 2 ;;
        --sources) sources+=("--sources=$2"); shift 2 ;;
        --full-suite) shift ;;
        --no-layout-tests) run_layout=0; shift ;;
        --open) open_report=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --cmake)
            echo "--cmake is not an option here: this script only drives the CMake build." >&2
            echo "Tools/Scripts/webkit-coverage is the one that takes --cmake." >&2
            exit 2 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) layout_tests+=("$1"); shift ;;
    esac
done

invocation_dir="${PWD}"
cd "$(dirname "$0")/../.."
build_dir="WebKitBuild/cmake-mac/Coverage"
coverage_dir="${build_dir}/coverage-profiles"

# Absolute from here on. A relative --output is resolved against where you ran this, not
# against the checkout, because that is what you meant when you typed it -- and the report
# path is printed for you to open, so it has to be one that works from anywhere.
if [[ -z "${output_dir}" ]]; then
    output_dir="${PWD}/WebKitBuild/coverage-report"
elif [[ "${output_dir}" != /* ]]; then
    output_dir="${invocation_dir}/${output_dir}"
fi

step() { printf '\n=== %s\n' "$1"; }

step "Configuring ${build_dir}"
cmake --preset mac-coverage

step "Building with ${jobs} jobs (a cold instrumented build is tens of minutes)"
cmake --build --preset mac-coverage -- -j "${jobs}"

step "Clearing ${coverage_dir}"
rm -rf "${coverage_dir}"
mkdir -p "${coverage_dir}"

if [[ "${api_tests}" != "" ]]; then
    step "API tests"
    api_arguments=()
    [[ "${api_tests}" != "__all__" ]] && api_arguments=("${api_tests}")
    # ${a[@]+"${a[@]}"}: /bin/bash on macOS is 3.2, where "${a[@]}" on an empty array is an
    # unbound-variable error under set -u.
    Tools/Scripts/run-api-tests --release --cmake --coverage \
        --coverage-dir="${coverage_dir}" ${api_arguments[@]+"${api_arguments[@]}"}
fi

if [[ "${run_layout}" == 1 ]]; then
    step "Layout tests${layout_tests[*]+: ${layout_tests[*]}}"
    Tools/Scripts/run-webkit-tests --release --cmake --coverage \
        --coverage-dir="${coverage_dir}" ${layout_tests[@]+"${layout_tests[@]}"}
fi

step "Report"
Tools/Scripts/generate-coverage-report --release --cmake \
    --coverage-dir="${coverage_dir}" --output-dir="${output_dir}" ${sources[@]+"${sources[@]}"}

index="${output_dir}/index.html"
if [[ ! -f "${index}" ]]; then
    echo "generate-coverage-report exited 0 but wrote no ${index}." >&2
    exit 1
fi

# A file:// URL rather than a path: a bare path pasted into a browser or clicked in a terminal
# gets read as a hostname, and "WebKitBuild/coverage-report/index.html" becomes a request to a
# server called webkitbuild.
printf '\nReport: file://%s\n' "${index}"
[[ "${open_report}" == 1 ]] && open "${index}"
exit 0

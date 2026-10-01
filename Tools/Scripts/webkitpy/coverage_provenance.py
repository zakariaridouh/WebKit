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

"""What a coverage report was produced from, recorded beside it.
A flat JSON record, also embedded as a one-line '#' comment at the head of the lcov trace.
"""

import datetime
import hashlib
import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Bumped when a field changes meaning; adding a field needs no bump.
SCHEMA = 'webkit-coverage-provenance-1'

PROVENANCE_FILENAME = 'coverage-provenance.json'

# lcov has no comment syntax; the readers here skip everything before the first SF: record,
# and llvm-cov never emits such a line.
TRACE_COMMENT_PREFIX = '#webkit-coverage-provenance: '

# The digest is the load-bearing part; the per-file list is capped.
MAX_RECORDED_DIRTY_FILES = 200

# Above this, record size and mtime instead of hashing.
MAX_DIGESTED_FILE_BYTES = 32 * 1024 * 1024

# Left out of the trace's comment line: its own measurements and the large per-file list.
_TRACE_COMMENT_OMITS = ('source_dirty_files', 'source_dirty_files_truncated',
                        'trace_record_count', 'trace_size_bytes')


def _git(checkout_root, *arguments):
    """git output as text, or None if git could not answer.
    core.fsmonitor is off: a watched status can report a clean tree falsely."""
    try:
        completed = subprocess.run(['git', '-c', 'core.fsmonitor=false', *arguments],
                                   cwd=checkout_root, check=False, text=True,
                                   capture_output=True)
    except OSError as failure:
        logger.debug('Could not run git %s: %s', arguments[0], failure)
        return None
    if completed.returncode:
        logger.debug('git %s failed: %s', arguments[0], completed.stderr.strip())
        return None
    return completed.stdout


def _file_digest(path):
    """'sha256:<hex>' for a file's contents, or None when it is not a readable file."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_DIGESTED_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError:
        return None
    return 'sha256:' + digest.hexdigest()


def _modified_at(path):
    try:
        return _timestamp(os.path.getmtime(path))
    except OSError:
        return None


def _timestamp(seconds):
    return datetime.datetime.fromtimestamp(
        seconds, datetime.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def dirty_files(checkout_root):
    """[{path, status, digest, size_bytes, modified_at}] for everything the tree differs from HEAD by.
    Includes untracked files; directory entries (submodules) get no digest rather than being walked."""
    output = _git(checkout_root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    if output is None:
        return None
    files = []
    for entry in output.split('\0'):
        if len(entry) < 4:
            continue
        status, relative = entry[:2].strip(), entry[3:]
        # 'R  old -> new' is reported as two NUL-separated fields; -z gives the new name first.
        absolute = os.path.join(checkout_root, relative)
        size = None
        try:
            if os.path.isfile(absolute):
                size = os.path.getsize(absolute)
        except OSError:
            pass
        files.append({'path': relative, 'status': status, 'digest': _file_digest(absolute),
                      'size_bytes': size, 'modified_at': _modified_at(absolute)})
    return sorted(files, key=lambda entry: entry['path'])


def dirty_digest(files):
    """One digest over every dirty file, so two runs can be compared without the list.
    A file too large to hash contributes its size and mtime instead."""
    digest = hashlib.sha256()
    for entry in files or ():
        fingerprint = entry['digest'] or 'size:{} mtime:{}'.format(entry['size_bytes'],
                                                                   entry['modified_at'])
        line = '{}\t{}\t{}\n'.format(entry['status'], entry['path'], fingerprint)
        digest.update(line.encode('utf-8'))
    return 'sha256:' + digest.hexdigest()


def checkout_state(checkout_root):
    """The revision the report is about, and how far the tree has drifted from it.
    source_revision is HEAD now, not necessarily what was built; the dirty digest and mtimes expose drift."""
    revision = _git(checkout_root, 'rev-parse', 'HEAD')
    branch = _git(checkout_root, 'rev-parse', '--abbrev-ref', 'HEAD')
    files = dirty_files(checkout_root)
    recorded = (files or [])[:MAX_RECORDED_DIRTY_FILES]
    return {
        'source_root': checkout_root,
        'source_revision': revision.strip() if revision else None,
        'source_branch': branch.strip() if branch else None,
        # None, not 0: "git could not tell us" and "nothing is dirty" are different facts.
        'source_dirty_file_count': None if files is None else len(files),
        'source_dirty_digest': None if files is None else dirty_digest(files),
        'source_dirty_files': recorded,
        'source_dirty_files_truncated': bool(files) and len(files) > len(recorded),
    }


def object_states(paths):
    """[{path, size_bytes, modified_at, instrumented, profile_filename}] for the reported binaries.
    The mtimes let a consumer detect source newer than the binaries, which nothing else catches."""
    from webkitpy.llvm_profile_utils import read_instrumentation

    states = []
    for path in paths:
        state = {'path': path, 'size_bytes': None, 'modified_at': _modified_at(path),
                 'instrumented': None, 'profile_filename': None}
        try:
            state['size_bytes'] = os.path.getsize(path)
            instrumentation = read_instrumentation(path)
            state['instrumented'] = instrumentation.instrumented
            state['profile_filename'] = instrumentation.profile_filename
        except OSError:
            pass
        states.append(state)
    return states


def tool_versions():
    """{tool name: {path, version, candidates}} for the two binaries that produced the numbers.
    candidates lists every binary found, in try order; the runner rotates through them on failure."""
    from webkitpy.llvm_profile_utils import CoverageLLVMProfDataExecutable, LLVMCovExecutable, read_llvm_version

    versions = {}
    for executable in (LLVMCovExecutable, CoverageLLVMProfDataExecutable):
        candidates = list(executable.detect_binaries())
        tried = executable.preference_ordered_paths()
        preferred = tried[0] if tried else None
        version = read_llvm_version(preferred)[1] if preferred else None
        versions[executable.EXECUTABLE_NAME] = {'path': preferred, 'version': version,
                                                'candidates': candidates}
    return versions


def count_lcov_records(lcov_path):
    """The number of SF: records in a trace, gzipped or not.
    Records, not distinct files: the trace has one per (file, framework) pair."""
    from webkitpy.coverage_lcov import open_lcov

    count = 0
    with open_lcov(lcov_path) as handle:
        for line in handle:
            if line.startswith('SF:'):
                count += 1
    return count


def provenance_record(checkout_root, build_directory, port_name, configuration, objects,
                      profile_path, command_line=(), products=None, suites=(),
                      raw_profile_count=None, unreadable_raw_profile_count=None,
                      ignore_filename_regexes=(), sources_scope=(), include_third_party=False,
                      include_test_support=False, generator=None, scope=None):
    """The record, complete except for the trace's own measurements. See add_trace_measurements().
    scope (a CoverageScope) defaults to FULL_SUITE; a selective run's percentage is only a lower bound."""
    from webkitpy.coverage_scope import CoverageScope

    record = {'schema': SCHEMA,
              'generated_at': _timestamp(datetime.datetime.now(datetime.timezone.utc).timestamp()),
              'generator': generator,
              'command_line': list(command_line)}
    record.update(checkout_state(checkout_root))
    record.update({
        'port': port_name,
        'configuration': configuration,
        'build_directory': build_directory,
        # None means "every known product", which is not the same as naming them all.
        'products': products,
        'objects': object_states(objects),
        'suites': [{'name': name, 'source': source} for name, source in suites],
        'profile_path': profile_path,
        'profile_size_bytes': (os.path.getsize(profile_path)
                               if os.path.exists(profile_path) else None),
        'raw_profile_count': raw_profile_count,
        'unreadable_raw_profile_count': unreadable_raw_profile_count,
        'ignore_filename_regexes': list(ignore_filename_regexes),
        # Empty means the whole tree; otherwise the report is a subset, not the project total.
        'sources_scope': list(sources_scope),
        # Which tests ran; sources_scope only restricts what is described.
        'test_scope': (scope or CoverageScope.full_suite()).to_json(),
        'include_third_party': include_third_party,
        'include_test_support': include_test_support,
        'tools': tool_versions(),
        'trace_path': None,
        'trace_size_bytes': None,
        'trace_record_count': None,
    })
    return record


def add_trace_measurements(record, lcov_path):
    """Fill in what can only be measured once the trace exists. Returns the record."""
    record['trace_path'] = os.path.basename(lcov_path)
    try:
        record['trace_size_bytes'] = os.path.getsize(lcov_path)
        record['trace_record_count'] = count_lcov_records(lcov_path)
    except OSError as failure:
        logger.debug('Could not measure %s: %s', lcov_path, failure)
    return record


def trace_comment(record):
    """The single '#' line that makes a bare .lcov.gz self-describing."""
    summary = {key: value for key, value in record.items() if key not in _TRACE_COMMENT_OMITS}
    return TRACE_COMMENT_PREFIX + json.dumps(summary, sort_keys=True,
                                             separators=(',', ':')) + '\n'


def read_trace_comment(lcov_path):
    """The provenance a trace carries, or None if it carries none."""
    from webkitpy.coverage_lcov import open_lcov

    try:
        with open_lcov(lcov_path) as handle:
            for line in handle:
                if line.startswith(TRACE_COMMENT_PREFIX):
                    return json.loads(line[len(TRACE_COMMENT_PREFIX):])
                if line.startswith('SF:'):
                    return None
    except (OSError, ValueError) as failure:
        logger.debug('Could not read provenance from %s: %s', lcov_path, failure)
    return None


def write_provenance(record, output_directory, filename=PROVENANCE_FILENAME):
    """Write the record beside the report and return its path."""
    path = os.path.join(output_directory, filename)
    with open(path, 'w') as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write('\n')
    return path


def summary_lines(record):
    """The two or three lines worth putting in a run's log, so the terminal is a record too."""
    from webkitpy.coverage_scope import scope_from_provenance

    lines = ['{} at {}{}'.format(
        record.get('source_branch') or 'detached', (record.get('source_revision') or '?')[:12],
        '' if record.get('source_dirty_file_count') == 0 else
        ' + {} dirty file(s), {}'.format(record.get('source_dirty_file_count'),
                                         (record.get('source_dirty_digest') or '?')[:19]))]
    tools = record.get('tools') or {}
    lines.append('{} {}, {} objects, {} raw profiles'.format(
        'llvm-cov', (tools.get('llvm-cov') or {}).get('version') or '?',
        len(record.get('objects') or ()), record.get('raw_profile_count')))
    if record.get('sources_scope'):
        lines.append('scoped to {}, so these totals are NOT the project total'.format(
            ', '.join(record['sources_scope'])))
    lines.extend(scope_from_provenance(record).banner_lines())
    return lines

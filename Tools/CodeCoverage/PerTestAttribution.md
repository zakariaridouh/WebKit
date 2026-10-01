# Per-test coverage attribution

An ordinary coverage run says that `Source/WTF/wtf/text/StringImpl.cpp:1516` is covered. It cannot
say *by what*. Every test's counters merge into one indexed profile as they are written. The
baked-in `/private/tmp/WebKitCoverage/<Product>_%8m%c.profraw` pools them into eight files per
framework. So collection destroys the attribution, and no later step recovers it.

This mechanism keeps it for a **named set of tests or one directory**. A second tool asks the
resulting index three questions:

```sh
Tools/Scripts/run-api-tests --release --cmake --coverage --coverage-dir=/tmp/cov \
    --per-test-coverage --per-test-coverage-sources=Source/WTF \
    WTF_Vector.Reverse WTF_Vector.StringComparison

Tools/Scripts/coverage-attribution --index=/tmp/cov/per-test \
    --covers Source/WTF/wtf/text/StringImpl.cpp:1516
```

```
Source/WTF/wtf/text/StringImpl.cpp:1516 is executed by 1 test -- so if that test is flaky or is
deleted, this line stops being covered
    api      TestWTF.WTF_Vector.StringComparison
```

| Question | Command |
|---|---|
| Which tests execute this line? | `--covers PATH:LINE`, or `PATH:FIRST-LAST`, or a bare `PATH` |
| What does this test reach? | `--test NAME` (a substring matches) |
| Which tests should I run for this change? | `--tests-for-diff REF` |
| Which of these tests earn their runtime? | `--redundant` |

`--tests-for-diff` finds the tests that touch your change. It does **not** let you skip the suite.
Over 1,656 real commits, coverage-driven selection still leaves 86.1% of the layout suite to run at
the mean, and saves nothing for 83.9% of commits. One word of scope from a human beats it by an
order of magnitude. That is why `coverage_test_scope.py` exists.

A per-test map of the *whole* suite is deliberately not built (see `Followups.md`): it is an
overnight run with about 45 TB of writes. This is the scoped version.

## The mechanism

The profile runtime reads `LLVM_PROFILE_FILE` at process start, and that overrides the path baked
into the frameworks. Everything else follows from this.

**Before each test starts,** the harness makes a directory for it and points the test there with
`LLVM_PROFILE_FILE` *and* `__XPC_LLVM_PROFILE_FILE`. libxpc forwards the second one to the
WebContent, GPU and Networking services, as `port/driver.py` already does for
`__XPC_DYLD_FRAMEWORK_PATH`. Without it, a layout test's record holds the UI process only. That
looks plausible and misses most of WebCore.

**The directory is under `/private/tmp/WebKitCoverage`, and must be.** The WebContent, GPU and
Networking sandbox profiles contain `(allow file-write* (subpath "/private/tmp/WebKitCoverage"))`.
That rule is the only reason `LLVM_PROFILE_FILE` can be redirected. It is a *subpath* rule, so a
directory per test inside it works. Anywhere else, a sandboxed child's profile write is denied with
no diagnostic.

**The pattern is `%1m%c`, not the baked-in `%8m%c`.** One pool slot of counters is 142 MB across
the five frameworks (WebCore alone 111 MB). Continuous mode preallocates it at dyld load, whether
the test runs anything or not. Eight slots would be 1.1 GB of live raw profile per test. `%1m` gives
one file per *image*. The runtime keys the name on the image signature, not the process, so every
process in the test's tree shares one file per image.

**After the test's processes exit,** the harness merges the directory into a per-test indexed
profile. It exports that through `llvm-cov`, restricted to the declared scope, and keeps the set of
covered `(file, line)` pairs. It folds the profile into the run's accumulating one and deletes the
raw profiles. Peak disk is one test's worth, not the run's.

Two ordering rules matter. Unit tests assert both against the harness source.

- **Restart the driver for every test.** The profile path is read once, at process start. A driver
  that is already running writes into the previous test's file.
- **Reduce only after the processes have exited,** not merely after the test reports. Continuous mode
  keeps the counter file mapped for the life of the process. Indexing it while any process in the
  tree is alive reads a file that is still changing.

## What it costs

Measured on the CMake coverage build with the default object list (the five frameworks, plus the
test binary for an API test). API: six `TestWTF.WTF_Vector.*` tests, scope `Source/WTF`. Layout: one
test, `fast/dom/Node/initial-values.html`, scope `Source/WebCore/dom`, `--child-processes=1`.

| | API test | Layout test |
|---|---|---|
| Raw profile per test, discarded | 5.5 MB | **141 MB** |
| Index record per test | about 2.4 KB | 28.7 KB |

- **Disk.** The 141 MB is one pool slot of each framework, which is what one layout test's process
  tree maps. It is transient, so peak disk stays at one test however long the run is. It is also why
  a per-test run of the whole suite does not fit on a disk.
- **Time.** Loading the objects' coverage mappings dominates the reduction, not the scope. One
  `llvm-cov export --format=lcov` scoped to `Source/WTF` is about 17 times slower with the five
  frameworks than with `TestWTF` alone. The object list is the cost knob. `--per-test-coverage-products=LIST`
  sets it. An empty list plus the API test binary is the fast path for statically linked code.
- **Scale.** A full layout run is tens of thousands of tests. Each test costs seconds of reduction plus a
  driver restart, so that is at least a day of machine time. Do the multiplication before you start.
- **Export flags.** The export uses `--skip-functions --skip-branches`. That is the difference
  between a feasible artifact and an infeasible one. lcov emits one `FN:`/`FNDA:` pair per template
  *instantiation*. `--sources Source/WTF/wtf/Vector.h` against JavaScriptCore alone gives 52.8 MB,
  and 52.3 MB of it is mangled names for one header.
- **Encoding.** The index stores line sets as ranges: 50 KB of JSON against 120 KB as integer lists
  for a whole-run profile reduced through TestWTF.

## The index

The index is a directory, `per-test/` under `--coverage-dir` by default. It holds one JSON-lines
file per worker process (for example `/tmp/cov/per-test/worker_0-db01c3678f03.jsonl`). The first
line describes the run. Every later line is one test.

```json
{"record": "shard", "format": "webkit-per-test-coverage", "version": 1, "shard": "worker/0",
 "suite": "api", "sources": ["Source/WTF"], "source_root": "/path/to/checkout",
 "objects": ["…/WebCore.framework/Versions/A/WebCore", "…"], "created": "2026-09-05T16:34:02-0700"}
{"record": "test", "test": "TestWTF.WTF_Vector.StringComparison", "suite": "api",
 "seconds": 6.948, "raw_bytes": 5718016,
 "files": {"Source/WTF/wtf/text/StringImpl.cpp": "119-120,122,124,126,130,136-138,…"}}
```

Four properties of this shape are deliberate:

- **Appended and flushed per test.** A per-test run is usually killed, not finished. Everything
  already reduced survives. A truncated last line costs one test.
- **One file per worker.** Both harnesses reduce inside their worker processes. Two processes that
  append lines longer than `PIPE_BUF` to one descriptor can interleave a line.
- **Paths relative to the checkout.** An index survives a move of the checkout, and a diff's paths
  compare without normalising both sides at every query.
- **The scope belongs to the shard, not the index.** Two runs can append to one index with
  different `--per-test-coverage-sources`. A query must tell "this test does not execute that line"
  from "that file was never in this test's export".

Successive runs append shards. Re-running a test replaces its record, and the newest wins.
`--per-test-coverage-index=DIR` moves the index.

Per-test mode overrides the baked-in profile path, so nothing lands in `--coverage-dir` for
`generate-coverage-report` to read. The run therefore also merges each test's profile into
`per-test-run.profdata` as it goes. That keeps a whole-run report possible with no raw profiles kept:

```sh
Tools/Scripts/generate-coverage-report --release --cmake --output-dir=/tmp/report \
    --profdata=/tmp/cov/per-test-run.profdata
```

`--no-per-test-coverage-profdata` turns the merge off and saves one merge per test.

## What is verified

- **The layout path reaches the sandboxed child processes.** A layout test's record holds covered
  lines in `Document.cpp`, `Element.cpp` and `ContainerNode.cpp`. The UI process never parses HTML,
  so those counters come from the WebContent process: `__XPC_LLVM_PROFILE_FILE` reached it, and the
  sandbox let it write. The run ended with no stray-profile warning.
- **The API path tells tests apart.** `--covers` on lines that distinguish tests names exactly one.
- **The raw profiles are discarded.** `/private/tmp/WebKitCoverage/per-test` is empty after a run.
  The accumulated profile is what remains to report from.

Not exercised: more than one worker writing shards at once, a run of more than six tests,
`--tests-for-diff` against a real diff that touches a file inside the scope (unit tests cover the
selection logic), and any port other than mac-cmake.

## Limits

Read these before you quote an answer. The tool prints the relevant one with each answer, because
most are invisible in the output alone.

### The index holds executed lines and nothing else

"No test in this index executes `Foo.cpp:42`" does not separate three cases. The line may be one no
test reaches. It may have no code (a comment, a brace, a declaration). Or its file may be one this
configuration never compiled. Storing the unexecuted lines too would nearly double the artifact, to
record something the whole-run report already answers exactly. Ask the report for the denominator.
Ask this tool for the attribution.

### A query outside the scope is unanswerable, not negative

```
Source/WebCore/dom/Document.cpp: Of the 6 tests in this index, 0 were measured over that file.
So this index cannot say anything about it.
```

The exit code is 3, not 0. This is the same "third state" that the report uses for a file the
configuration never built: a missing denominator is not a zero. Every query prints how many of the
index's tests were measured over the file. That number makes an empty answer readable. "0 of 40"
means the question was outside the scope. "40 of 40" is a real finding.

### The scope must name the *compiled* path, and the harness does that for you

`--sources Source/WTF` alone records **no line of any WTF header**. The coverage mapping stores the
path the translation unit included, which is `<build>/WTF/Headers/wtf/Vector.h` because the CMake
build stages headers. `llvm-cov`'s `--sources` filter runs against that path, long before the
canonicalisation maps it back to `Source/WTF/wtf/`. A scope of `Source/WTF` alone matches none of it.

So the harness passes `coverage_lcov.staged_equivalents_for_scope()`'s answer alongside the scope
and says so at the start of the run. The *record* names only the scope you asked for, because a
query is checked against that.

### The object list decides what can be attributed

A per-test profile holds the counters of the images that test loaded. `llvm-cov` can report them
only against a mapping in one of the objects it got.

The six `WTF_Vector` tests get **no line of `wtf/Vector.h`**, and that is correct. `TestWTF`'s own
mapping has only a few lines for it. The big mapping belongs to JavaScriptCore and WebCore, whose
*instantiations* are different functions, with different names, from the ones the test used. What
the test really executed of `Vector` is attributed to `Tools/TestWebKitAPI/Tests/WTF/Vector.cpp`,
which a `Source/` scope excludes.

So choose the scope and the objects to match the binary that carries the mapping. For a layout test
the frameworks carry everything, and the default is right. For an API test about header-only code,
add the test's own directory to the scope.

### An API test's record is mostly process startup

Of the 479 covered `(file, line)` pairs in the six-test index, **466 are common to all six tests**
(`WTFConfig`, `Threading`, `CryptographicallyRandomNumber`, `RunLoop`, `FastMalloc`). Only 13
distinguish them. Everything a short API test runs before `main` is in its record, so the
*difference* between two records holds the information. `--redundant` reports five of the six as
redundant for exactly this reason, and it is right.

### `--redundant` means "any one", not "all"

A test is listed when the rest of the index covers every line it covers. So dropping **any one**
listed test cannot lose a line. Three tests that cover `{1,2}`, `{2}` and `{1}` are all listed, and
deleting all three loses both lines. Drop one and ask again. The statement is about the index and its
scope, never about the suite. A test that looks redundant in a 40-test index may be the only test in
the suite that reaches a line outside the scope.

### One record is one execution

A test's record is what it ran on the run that measured it. Another machine, another order or a
retry can take a different branch and give a different record. Nothing records that, and a flaky
test's record is whichever execution got reduced. A line that the index says a test covers really did
run at least once.

### Nothing records which revision was measured

Neither an lcov trace nor this index carries the revision it came from. Read against an edited
checkout, it gives the right line numbers of the wrong text (see C3 in `Followups.md`). Re-run
instead of reinterpreting.

### Test discovery is attributed to nothing

A per-test run ends by naming what did not go through per-test collection:

```
15 raw profile(s) were written straight into /private/tmp/WebKitCoverage rather than into a
per-test directory, so whatever they recorded is attributed to no test: …
```

For `run-api-tests`, those are the harness's test-discovery step, which runs every API test binary
with `--list-tests` before any test starts and so writes to the baked-in path. A non-empty list is
expected there. A layout run should write none. The same warning is the only signal if a *test's*
process escapes its directory, which is this mechanism's one silent failure mode. Read it every time.

## Operational notes

- **One coverage run per machine.** `/private/tmp/WebKitCoverage` is machine-global, and
  `prepare_coverage_profile_directory()` claims it with an advisory lock. Per-test mode adds a
  subdirectory tree under it and clears that tree when a run starts. An interrupted run leaves one
  directory per test, up to 142 MB each. Nothing else on the machine cleans them up.
- **Every option is checked before the tests start.** A per-test run deletes each test's raw
  profiles as it goes. A scope that names nothing would give a full-length run with nothing left to
  re-reduce. `--per-test-coverage` without `--coverage`, without a scope, or with a scope that does
  not exist fails at once and says why.
- **API tests run one process per test.** A batched shard runs several tests in one process, and the
  profile path is read once at process start. Every test in such a shard would get the whole shard's
  counters under the first test's name.
- **Use `--child-processes=1` or `2` for a layout run.** Each worker reduces its own tests, so eight
  workers means eight concurrent `llvm-cov` runs that each load the five frameworks' coverage
  mappings. A coverage run is already capped at 8 workers because instrumented frameworks exhaust
  memory at full parallelism.
- **`--world-leaks` reports nothing under `--per-test-coverage`.** The leak check runs at the end of
  a shard against the driver that ran it. Per-test collection kills the driver after every test, so
  none is left to ask. Nothing fails. The check is absent.

## Where the code is

| File | Role |
|---|---|
| `Tools/Scripts/webkitpy/coverage_attribution.py` | the index format, the reduction, the queries |
| `Tools/Scripts/coverage-attribution` | the query tool |
| `Tools/Scripts/webkitpy/coverage_attribution_unittest.py` | tests |
| `run-api-tests`, `webkitpy/api_tests/runner.py` | the API-test collection mode |
| `webkitpy/layout_tests/run_webkit_tests.py`, `controllers/layout_test_runner.py` | the layout-test collection mode |
| `webkitpy/port/driver.py` | where the driver picks up the per-test profile path |

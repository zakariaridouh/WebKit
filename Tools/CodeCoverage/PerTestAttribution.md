# Per-test coverage attribution

An ordinary coverage run can tell you that `Source/WTF/wtf/text/StringImpl.cpp:1516` is covered.
It cannot tell you *by what*. Every test's counters are merged into one indexed profile as they
are written — `%8m` in the baked-in
`/private/tmp/WebKitCoverage/<Product>_%8m%c.profraw` pools them into eight files per framework
— so the attribution is destroyed at collection time, not lost afterwards. No post-processing
recovers it.

This is the mechanism that keeps it, for a **named set of tests or one directory**, and the tool
that asks the resulting index three questions:

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

The questions it answers, and the one that is not what it looks like:

| Question | Command |
|---|---|
| Which tests execute this line? | `--covers PATH:LINE`, or `PATH:FIRST-LAST`, or a bare `PATH` |
| What does this test reach? | `--test NAME` (a substring matches) |
| Which tests should I run for this change? | `--tests-for-diff REF` |
| Which of these tests earn their runtime? | `--redundant` |

`--tests-for-diff` is a way to find the tests that touch your change. It is **not** a way to
avoid running the suite: coverage-driven selection was measured over 1,656 real commits (PLAN 10)
and still leaves 86.1% of the layout suite to run at the mean, with no saving at all for 83.9% of
commits. One word of scope from a human beats it by an order of magnitude, which is why
`coverage_test_scope.py` exists and why this does not try to replace it.

`Followups.md` records "a per-test coverage map" as deliberately not done, on the measurement that
a whole-suite one is an overnight run and ~45 TB of writes. That still stands and this does not
contradict it: what is built here is the **scoped** version, for a named set of tests or one
directory, with the raw profiles indexed and deleted as the run goes. The whole-suite map is still
not worth having.

---

## The mechanism

The profile runtime reads `LLVM_PROFILE_FILE` at process start, and it overrides the path baked
into the frameworks. Everything else follows from that.

**Per test, before its processes start**, the harness makes a directory of its own and points the
test at it, via `LLVM_PROFILE_FILE` *and* `__XPC_LLVM_PROFILE_FILE` — the second is what libxpc
forwards to the WebContent, GPU and Networking services, exactly as `port/driver.py` already
forwards `__XPC_DYLD_FRAMEWORK_PATH`. Without it a layout test's record is the UI process's
coverage and nothing else, which looks plausible and is missing most of WebCore.

**The directory is under `/private/tmp/WebKitCoverage`, and it has to be.**
`(allow file-write* (subpath "/private/tmp/WebKitCoverage"))` in the WebContent, GPU and
Networking sandbox profiles is the only reason `LLVM_PROFILE_FILE` can be redirected at all. It
is a *subpath* rule, so a directory per test inside it is permitted; anywhere else and a
sandboxed child's profile write is denied with no diagnostic of any kind.

**The pattern is `%1m%c`, not the baked-in `%8m%c`.** One pool slot of counters is 142 MB across
the five frameworks (WebCore alone 111 MB) and continuous mode preallocates it at dyld load
whether the test executes anything or not, so eight slots would be 1.1 GB of live raw profile per
test. `%1m` is one file per *image* — the runtime keys the name on the image signature, not on the
process, which is what a real run shows: a TestWTF test writes
`18413697643720372875_0.profraw` and `270522276108579632_0.profraw`, two images, one file each,
shared by every process in the test's tree.

**Per test, after its processes have exited**, the harness merges that directory into a per-test
indexed profile, exports it through `llvm-cov` restricted to the declared scope, keeps the set of
covered `(file, line)` pairs, folds the profile into the run's accumulating one, and deletes the
raw profiles. Peak disk is therefore one test's worth, not the run's.

Two ordering rules are load-bearing and both are asserted by unit tests against the harness
source:

- The driver must be **restarted for every test**. The profile path is read once, at process
  start, so a driver that is already running is already writing into the previous test's file.
- The reduction must happen **after the processes have exited**, not merely after the test has
  reported. Continuous mode keeps the counter file mmapped for the life of the process, so
  indexing it while anything in the tree is alive reads a file that is still being written.

---

## What is kept, and what it costs

Measured on this machine, CMake coverage build at `WebKitBuild/cmake-mac/Coverage`, with the
default object list (the five frameworks, plus the test binary for an API test):

- **API**: six `TestWTF.WTF_Vector.*` tests, `run-api-tests --per-test-coverage
  --per-test-coverage-sources=Source/WTF`.
- **Layout**: one test, `run-webkit-tests --per-test-coverage
  --per-test-coverage-sources=Source/WebCore/dom --child-processes=1
  fast/dom/Node/initial-values.html`.

| | API test | Layout test |
|---|---|---|
| Raw profile per test, discarded | 5.5 MB (5,718,016 B) | **141 MB** |
| Reduction per test | 6.8–7.0 s | 9.5 s |
| Whole run | 44.8 s for 6 tests | 19.3 s for 1 test |
| Index record per test | 2,368–2,415 B | 28,691 B |
| Covered (file, line) pairs recorded | 479 over 31 files | 7,037 over 176 files |
| Run's accumulated profile | 324,360 B | 20,143,544 B |

The 141 MB is the predicted number: one pool slot is 142 MB across the five frameworks, and one
layout test's process tree maps exactly one slot of each. It is transient — the directory is
deleted before the next test starts — so peak disk is one test's worth however long the run is,
and it is also the reason a per-test-for-the-whole-suite run is not a thing that fits on a disk.

The reduction is dominated by **loading the objects' coverage mappings**, not by the scope.
Against the 208 MB whole-run profile, one `llvm-cov export --format=lcov` scoped to `Source/WTF`
takes **8.3 s** with the five frameworks and **0.48 s** with `TestWTF` alone. So the object list
is the cost knob, and `--per-test-coverage-products=LIST` is how to turn it: an empty list plus
the API test binary is the fast path for a question about statically linked code.

Multiply the layout row out before starting anything large. A full layout run is tens of thousands
of tests; at 9.5 s of reduction and one driver restart each, that is a day of machine time for an
answer PLAN 10 already priced.

`--skip-functions --skip-branches` on the export is not an optimization but the difference
between a feasible and an infeasible artifact. lcov emits one `FN:`/`FNDA:` pair per template
*instantiation*: `--sources Source/WTF/wtf/Vector.h` against JavaScriptCore alone is 52.8 MB, of
which 52.3 MB is mangled names for one header. All of `Source/WTF` is 1.69 MB with function
records and 488 KB without.

Run-length encoding the line sets is where the record stops being large enough to matter:
reducing the whole-run profile through TestWTF gives 21,509 covered lines over 268 files, which is
50,255 bytes of JSON as ranges and 119,520 as lists of integers.

---

## The index

A directory — `per-test/` under `--coverage-dir` by default — holding one JSON-lines file per
worker process:

```
/tmp/cov/per-test/worker_0-db01c3678f03.jsonl
```

The first line describes the run; every line after it is one test.

```json
{"record": "shard", "format": "webkit-per-test-coverage", "version": 1, "shard": "worker/0",
 "suite": "api", "sources": ["Source/WTF"], "source_root": "/Users/…/OpenSource",
 "objects": ["…/WebCore.framework/Versions/A/WebCore", "…"], "created": "2026-09-05T16:34:02-0700"}
{"record": "test", "test": "TestWTF.WTF_Vector.StringComparison", "suite": "api",
 "seconds": 6.948, "raw_bytes": 5718016,
 "files": {"Source/WTF/wtf/text/StringImpl.cpp": "119-120,122,124,126,130,136-138,…"}}
```

Four properties of that shape are deliberate:

- **Appended and flushed per test.** A run measured per test is normally killed rather than
  finished, and everything it had already reduced survives. A truncated last line costs one test.
- **One file per worker.** Both harnesses reduce inside their worker processes, and two processes
  appending JSON lines longer than `PIPE_BUF` to one descriptor can interleave a line.
- **Paths relative to the checkout.** So an index survives the checkout moving, and so a diff's
  paths can be compared without normalizing both sides at every query.
- **The scope is per shard, not per index.** Two runs can append to one index with different
  `--per-test-coverage-sources`, and a query has to be able to tell "this test does not execute
  that line" from "that file was never in this test's export".

Successive runs append shards. Re-running a test replaces its record; the newest wins.

Per-test mode overrides the baked-in profile path, so nothing lands in `--coverage-dir` for
`generate-coverage-report` to read. The run therefore also merges each test's profile into
`per-test-run.profdata` incrementally, which is what keeps a whole-run report possible without
keeping any raw profiles:

```sh
Tools/Scripts/generate-coverage-report --release --cmake --output-dir=/tmp/report \
    --profdata=/tmp/cov/per-test-run.profdata
```

`--no-per-test-coverage-profdata` turns that off and saves one merge per test.

---

## What has been verified, and how

Everything in the cost table above is from a real run; so is every limit below. Specifically:

- **The layout path reaches the sandboxed child processes.** One layout test's record holds 2,032
  covered lines of `Source/WebCore/dom/Document.cpp`, 587 of `Element.cpp` and 406 of
  `ContainerNode.cpp`. The UI process never parses HTML, so those counters are the WebContent
  process's: `__XPC_LLVM_PROFILE_FILE` reached it and the sandbox let it write into the per-test
  directory. That run also ended with **no** stray-profile warning, meaning every instrumented
  process in the tree wrote where it was pointed.
- **The API path discriminates between tests.** Of the six-test index's 479 covered pairs, 13
  distinguish one test from another; `--covers` on those names exactly one test.
- **The raws really are discarded.** `/private/tmp/WebKitCoverage/per-test` is empty after both
  runs, and the accumulated profile is what is left to report from.

What has *not* been exercised: more than one worker process writing shards concurrently (both
runs used one), a run of more than six tests, `--tests-for-diff` against a diff that touches a
file inside a scope (the unit tests cover the selection logic; the live run had no such diff),
and any port other than mac-cmake.

---

## Limits

Read these before quoting an answer. Every one of them was measured, and most of them are
invisible from the output alone, which is why the tool prints the relevant one with each answer.

### The index holds executed lines and nothing else

"No test in this index executes `Foo.cpp:42`" does not distinguish a line no test reaches from a
line the compiler emitted no code for — a comment, a brace, a declaration — or from a line in a
file this configuration never compiled. Storing the unexecuted lines too would nearly double the
artifact (21,509 covered against 37,891 instrumented, in the measured `Source/WTF` scope) to
record something the whole-run report already answers exactly. Ask the report for the
denominator; ask this for the attribution.

### A query outside the scope is unanswerable, not negative

```
Source/WebCore/dom/Document.cpp: Of the 6 tests in this index, 0 were measured over that file.
So this index cannot say anything about it.
```

Exit code 3, not 0. This is the same "third state" the coverage report uses for files a
configuration never built: a missing denominator is not a zero. Every query prints how many of
the index's tests were measured over the file it was asked about, because that number is what
makes an empty answer readable — 0 of 40 is a question outside the scope, 40 of 40 is a real
finding.

### The scope has to name the *compiled* path, and does so for you

`--sources Source/WTF` on its own records **not one line of any WTF header**. The coverage
mapping records the path the translation unit actually included —
`<build>/WTF/Headers/wtf/Vector.h`, since the CMake build stages headers — and llvm-cov's
`--sources` filter runs against that, long before the canonicalization that maps it back to
`Source/WTF/wtf/`. Measured: with the frameworks as objects, that staged path has 1,159
instrumented lines of which 1,085 are covered, and a scope of `Source/WTF` alone matches none of
them.

The harness therefore passes `coverage_lcov.staged_equivalents_for_scope()`'s answer alongside
the scope and says so at the start of the run. The *record* still names only the scope you asked
for, since that is what a query is checked against. This was found by running the real thing: the
first validation run recorded 27 files, all `.cpp`, for six tests of `wtf::Vector`.

### The object list decides what can be attributed at all

A per-test profile holds the counters of the images that test loaded, and llvm-cov can only
report them against a mapping in one of the objects it was given.

The six `WTF_Vector` tests are attributed **no line of `wtf/Vector.h`**, and that is correct.
`TestWTF`'s own mapping has 4 lines for it; the 1,159-line mapping belongs to JavaScriptCore and
WebCore, whose *instantiations* are different functions with different names from the ones the
test instantiated. What the test really executed of `Vector` is attributed to
`Tools/TestWebKitAPI/Tests/WTF/Vector.cpp` — 2,127 instrumented lines, 2,117 covered — which a
`Source/` scope excludes.

So: choose the scope and the objects to match the binary that carries the mapping. For a layout
test the frameworks carry everything and the default is right. For an API test asking about
header-only code, include the test's own directory in the scope.

### An API test's record is mostly process startup

Of the 479 covered `(file, line)` pairs the six-test index holds, **466 are common to all six
tests** — `WTFConfig`, `Threading`, `CryptographicallyRandomNumber`, `RunLoop`,
`FastMalloc`. 13 pairs distinguish them. Everything a short API test executes before `main` is in
its record, and the *difference* between two records is where the information is. `--redundant`
reports five of those six tests as redundant for exactly this reason, and it is right to.

### `--redundant` means "any one", not "all"

A test is listed when every line it covers is also covered by the rest of the index, so dropping
**any one** of them cannot lose a line. Three tests covering `{1,2}`, `{2}` and `{1}` are all
listed, and deleting all three loses both lines. Drop one and ask again. It is also a statement
about the index and its scope, never about the suite: a test that looks redundant in a 40-test
index may be the only test in the suite that reaches a line outside the declared scope.

### One record is one execution

A test's record is what it executed on the run that measured it. A test that takes a different
branch on a different machine, or in a different order, or on a retry, has a different record;
nothing here records that, and a flaky test's record is whichever execution was reduced. This is
the same limit the whole-run profile has, and it cuts the same way: a line the index says a test
covers really was executed by it at least once.

### Nothing records which revision was measured

Neither an lcov trace nor this index carries the revision it was built from, so an index read
against an edited checkout reads the right line numbers of the wrong text. Same gap as the
report's line views. Re-run rather than reinterpret.

### Test discovery is not attributed to anything

A per-test run ends by naming what did not go through per-test collection:

```
15 raw profile(s) were written straight into /private/tmp/WebKitCoverage rather than into a
per-test directory, so whatever they recorded is attributed to no test: …
```

On the measured API run those 15 are the harness's own test-discovery step, which runs every API
test binary with `--list-tests` before any test starts and so writes to the baked-in path. The
layout run wrote none at all. So a non-empty list is expected for `run-api-tests` and unexpected
for `run-webkit-tests` — and the same warning is the only signal there would be if a *test's*
process escaped its directory, which is this mechanism's one silent failure mode. Read it every
time.

---

## Operational notes

- **One coverage run per machine, still.** `/private/tmp/WebKitCoverage` is machine-global and
  `prepare_coverage_profile_directory()` claims it with an advisory lock. Per-test mode adds a
  subdirectory tree under it and clears that tree at the start of a run: an interrupted run
  leaves one directory per test behind, up to 142 MB each, and nothing else on the machine will
  ever clean them up.
- **Every option is checked before the tests start.** A per-test run deletes each test's raw
  profiles as it goes, so a scope that names nothing would produce a full-length run with nothing
  left to re-reduce. `--per-test-coverage` without `--coverage`, without a scope, or with a scope
  that does not exist, fails immediately and says why.
- **API tests are forced to one process per test.** A batched shard runs several tests in one
  process and the profile path is read once at process start, so every test in such a shard would
  be attributed the whole shard's counters under the first test's name.
- **Consider `--child-processes=1` or `2` for a layout run.** Each worker reduces its own tests, so
  eight workers means eight concurrent `llvm-cov` invocations each loading the five frameworks'
  coverage mappings, on top of a coverage run that is already capped at 8 workers because
  instrumented frameworks exhaust memory at full parallelism.
- **`--world-leaks` reports nothing under `--per-test-coverage`.** The leak check runs at the end
  of a shard against the driver that ran it, and per-test collection kills the driver after every
  test, so there is none left to ask. Nothing fails; the check is simply absent.

## Where the code is

| | |
|---|---|
| `Tools/Scripts/webkitpy/coverage_attribution.py` | the index format, the reduction, the queries |
| `Tools/Scripts/coverage-attribution` | the three questions |
| `Tools/Scripts/webkitpy/coverage_attribution_unittest.py` | 90 tests |
| `run-api-tests`, `webkitpy/api_tests/runner.py` | the API-test collection mode |
| `webkitpy/layout_tests/run_webkit_tests.py`, `controllers/layout_test_runner.py` | the layout-test collection mode |
| `webkitpy/port/driver.py` | where the driver picks the per-test profile path up |

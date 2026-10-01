# Code coverage for WebKit

This directory documents how to measure which lines of WebKit's native code (C, C++, Objective-C
and Swift) a test run executed. The method is LLVM source-based coverage: you build with
instrumentation, then run tests that write profiles. The tools in `Tools/Scripts` turn the profiles
into an HTML report, a text summary and files for other tools.

## Quick start (macOS, CMake)

```sh
cmake --preset mac-coverage
cmake --build --preset mac-coverage             # first build: about 12 minutes on a 32-core Mac, about 19 GB of disk
Tools/Scripts/webkit-coverage --cmake fast/dom  # runs the layout tests you name; reports how well they cover the lines YOU changed
```

1. `cmake --preset mac-coverage` configures an instrumented build in `WebKitBuild/cmake-mac/Coverage`.
2. `cmake --build --preset mac-coverage` builds it. It has its own directory, so your normal build is untouched.
3. `webkit-coverage --cmake fast/dom` runs the layout tests under `LayoutTests/fast/dom`. Then it reports
   how many of the lines you changed those tests executed.

Open the `index.html` path it prints.

How `webkit-coverage` works:

- **The change.** It measures the diff since `main` or `origin/main`, whichever is nearer. The diff
  includes your working tree and untracked files. Use `--since=REF` to pick another base. If your branch
  sits on top of the coverage branch itself, use `--since=<that branch>`. `--since=main...HEAD` counts
  only commits.
- **The tests.** You choose them. With no test path it refuses to guess: it prints a suggestion when it
  has one and exits 3. `--full-suite` runs every layout test. That takes hours.
- **The plan.** `--dry-run` prints the build tree, the pre-flight checks, the detected change, the test
  scope and every command. It runs nothing. Try it once.
- **The build.** It builds only when the tree is not instrumented.

To measure the whole tree in one unattended command, use the script. It configures, builds, runs all the
API tests plus the layout tests you name, writes the report and opens it:

```sh
Tools/CodeCoverage/run-cmake-coverage.sh --open fast/dom
```

With no path it runs the whole layout suite. That takes hours. A failing test does not stop it: the
report is written first, then the script exits 1.

### Xcode

This path needs a recent Xcode. Use a build directory of its own. An instrumented build is many times the
size of a normal one.

```sh
export WEBKIT_OUTPUTDIR="$PWD/WebKitBuild-Coverage"
Tools/Scripts/build-webkit --xcode --release --coverage
Tools/Scripts/run-webkit-tests --release --coverage --coverage-dir=/tmp/cov fast/forms
Tools/Scripts/generate-coverage-report --release --coverage-dir=/tmp/cov --output-dir=/tmp/report
```

`Tools/Scripts/webkit-coverage --release fast/forms` does all of this and builds only when needed.
`build-webkit --coverage` adds `ENABLE_USER_SCRIPT_SANDBOXING=NO`. Xcode sandboxes its script phases, and
that fails when the build already runs inside a sandbox.

## The report

Each tool prints the path of `index.html` when it finishes. `webkit-coverage` writes to `coverage-report`
under the build directory, or `coverage-report-selective` for a scoped run. The script writes to
`WebKitBuild/coverage-report`. `--output-dir=DIR` picks another place. All pages work when opened from disk
(`file://`) and need no network.

- **`index.html`** is a dashboard of line, function and branch coverage. It has a treemap of the current
  directory and a table you can sort and filter. Each row has a coverage bar. You can hide fully covered
  rows, set a minimum line count, and switch between light, dark and auto themes.
- **Per-file pages** show a hit-count gutter shaded by count. A sticky bar jumps to the next and previous
  uncovered code with the `n` and `p` keys. `?` lists all keys, and `/` searches the report. You can fold
  covered code. A minimap on the right edge marks uncovered lines. A function outline lists the functions.
  Permalinks look like `#L10-L20`.
- **`summary.txt`** is llvm-cov's own table. See [Reading the numbers](#reading-the-numbers).
- **`coverage.lcov.gz`** is the lcov trace. Its paths and totals match the report. After a subset run the
  name is `coverage-selective.lcov.gz`.
- **`coverage-provenance.json`** records which binaries, tools and test scope produced the report.

## Exporting for other tools

`coverage.lcov.gz` is already lcov. `genhtml`, Coverage Gutters, Codecov and Coveralls read it. For other
formats, add `--export=FORMAT[,FORMAT...]` to `generate-coverage-report`. Use `all` for every format. The
files go to `<output-dir>/export/`. Every number is the report's, and every path is relative to the
checkout root.

| Format | File | Read by |
|---|---|---|
| `cobertura` | `coverage.cobertura.xml` | GitLab, Jenkins, Azure DevOps, Codecov, ReportGenerator |
| `istanbul` | `coverage-final.json`, `coverage-summary.json` | nyc and istanbul tools, many editor extensions |
| `sonarqube` | `sonar-generic-coverage.xml` | SonarQube generic coverage |
| `csv` | `coverage.csv` | spreadsheets |
| `json` | `coverage.json` | a small schema of our own, documented at the top of `Tools/Scripts/webkitpy/coverage_export.py` |

```sh
Tools/Scripts/generate-coverage-report --release --cmake --coverage-dir=/tmp/cov \
    --output-dir=/tmp/report --sources Source/WTF --export=cobertura,istanbul
```

- Scope the export with `--sources`. Whole-tree istanbul JSON is about 450 MB.
- Cobertura hit counts saturate at 2^31-1.
- `run-cmake-coverage.sh --export FORMATS` forwards the option.

## Choosing what to run and measure

- **Layout tests.** Give a directory or a test file: `webkit-coverage --cmake fast/dom`.
- **API tests.** `webkit-coverage --api-tests=ARGS` passes ARGS to `run-api-tests`. An empty string runs
  all of them. Without this option, a line that only TestWebKitAPI reaches shows as uncovered. The script
  takes `--api-tests ARGS` and `--no-layout-tests`.
- **Part of the tree.** `generate-coverage-report --sources PATH` limits the report to a directory or a
  file. Repeat it for more. `webkit-coverage` already limits the report to the files you changed;
  `--all-sources` widens it. The script takes `--sources` too.
- **One column per suite.** `--suite=NAME:PATH` gives each test suite its own column. Give each run its own
  `--coverage-dir` when you run it. Profiles carry no run name, so you cannot split them afterwards.
  Runs that share one `--coverage-dir` add up to one report.
- **Some frameworks.** `--products=LIST` picks from JavaScriptCore, WebCore, WebKit, WebKitLegacy and
  WebGPU. The default is all of them.
- **Which tests ran a line.** A merged profile cannot say. `--per-test-coverage` keeps that data for a named
  set of tests. It costs seconds per test, so it is not for a whole suite. See
  [PerTestAttribution.md](PerTestAttribution.md) for the limits.

Two examples, one for suite columns and one for per-test data:

```sh
Tools/Scripts/generate-coverage-report --release --cmake --output-dir=/tmp/report \
    --suite=layout:/tmp/cov-layout --suite=api:/tmp/cov-api

Tools/Scripts/run-webkit-tests --release --cmake --coverage --coverage-dir=/tmp/cov \
    --per-test-coverage --per-test-coverage-sources=Source/WebCore/dom fast/dom/Node
Tools/Scripts/coverage-attribution --index=/tmp/cov/per-test --covers Source/WebCore/dom/Document.cpp:1000
```

Naming note: `--coverage` means "collect profiles" in `run-webkit-tests` and `run-api-tests`.
`--coverage-build` selects the coverage build tree in `generate-coverage-report` and `webkit-coverage`.

## What the numbers do not include

- **Non-native code.** JavaScript builtins and WebAssembly have no coverage mapping. IDL bindings are
  generated code, which the report excludes. Each has its own tool and doc. See
  [JavaScriptCoverage.md](JavaScriptCoverage.md) and [BindingsCoverage.md](BindingsCoverage.md).
- **Code only a test binary contains.** TestIPC and the other test binaries run code that is credited
  through the framework when the same object files are in both. llvm-cov matches counters by function
  name and hash. Code that exists only in a test binary is not credited.
- **Third-party code.** `Source/ThirdParty` is excluded. `--include-third-party` includes it.
- **Test-support binaries.** `--include-test-support` includes libWebCoreTestSupport, jsc and similar
  tools.
- **iOS devices.** They are not supported. The iOS simulator works. See [iOSCoverage.md](iOSCoverage.md).

## Reading the numbers

### Quote the report, not summary.txt

The HTML report and `coverage.lcov.gz` agree. `summary.txt` is llvm-cov's own table, and its totals are
larger. llvm-cov counts a header copied into several frameworks once per framework. It counts a lambda
body twice, once in the lambda and once in its enclosing function. It also counts each template
instantiation as a function. The `--fail-under-*` gates use the report's numbers.

### Files that were never built

A file that this configuration never compiled has no coverage data. The report does not count it as 0%.
It lists these files as "not built here" with a reason, such as another port or a feature flag that is
off. `not-built.tsv` has the full list. `webkit-coverage` skips this step; `--build-inventory` turns it on.

### A subset run gives a lower bound

More tests can only turn an uncovered line into a covered one. So in a run of part of the suite, a covered
line is exact and an uncovered line is unknown. The tools reflect this:

- Percentages read `>= 41.3%`.
- The output directory and the trace get a `-selective` infix.
- `--fail-under-lines` fails, because no exact number exists to gate on.
- `compare-coverage-reports` refuses to compare a subset trace with a full baseline.

### Patch coverage is usually the number you want

| Question | Command | Cost |
|---|---|---|
| Are the added lines tested? | `compare-coverage-reports --current=TRACE --git-diff=REF` | one test run |
| Did the project total move? | `compare-coverage-reports --baseline=TRACE --current=TRACE` | two test runs |

`webkit-coverage` already runs the first command for you. It lists the added lines no test executed:

```
Patch coverage: 61.54% (8 of 13 added lines with coverage data covered)
 61.54%      8/13      21   80.68%  Source/WebKit/UIProcess/WebPageProxy.cpp
        uncovered added lines 16576, 16585, 16605, 16621-16622
```

Lines with no coverage record (comments, blank lines, braces, declarations) stay out of the denominator.

### Gates

| Option | Tool | Fails when |
|---|---|---|
| `--fail-under-lines`, `-functions`, `-branches` | `generate-coverage-report` | the total is below PERCENT, or the run is a subset |
| `--fail-under-patch` | `webkit-coverage`, `compare-coverage-reports` | too few added lines were executed |
| `--fail-under-delta` | `compare-coverage-reports` | coverage fell by more than PERCENT points |

- A gate that fails exits 2. The report is still written.
- A tooling failure exits 1. `webkit-coverage` exits 3 when you name no tests.
- `--fail-under-patch` is safe on a subset: it can raise a false alarm but never a false pass.
- Do not use `--fail-under-delta` as a patch check. A large untested addition to a well-covered file barely
  moves the total.

### Profiles that no binary claims

`generate-coverage-report` warns when some profiles come from a binary that the report does not list. The
code in that binary then shows as absent. Pass the binary with `--object=PATH`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The run exits 0 and there is no coverage data. | The tree has no instrumentation. `webkit-coverage` checks first and prints the build command. The harnesses do not check. |
| Every test looks like a crash: exit 143, no crash log. | Some terminals and launchers send `SIGTERM` to descendants that register as Dock-visible apps. WebKitTestRunner is one. Run from a plain login shell or over ssh. Coverage is not the cause. |
| The next run dies with `Address already in use`. | A killed run leaves servers behind. Run `pkill -9 -f 'layout-test-results/httpd.conf'` and `pkill -9 -f pywebsocket3`. |
| Two coverage runs interfere, or one reports no profiles. | All runs share `/private/tmp/WebKitCoverage`, and each run clears it at start. Run one at a time. An advisory lock names the holder. |
| `generate-coverage-report` refuses and names binaries. | Something was rebuilt after the profile was written. Run the tests again. `--allow-profile-mismatch` overrides this if you know why they differ. |
| A line view shows the wrong source, or warns that it might. | The page reads the working tree, which may have changed since the build. The report recovers or withholds what it can detect and warns about the rest. |

## Where the other docs are

- [Followups.md](Followups.md): open work.
- [iOSCoverage.md](iOSCoverage.md): the iOS simulator, and what a device would need.
- [PerTestAttribution.md](PerTestAttribution.md): which tests executed a line.
- [BindingsCoverage.md](BindingsCoverage.md): which web-facing IDL attributes and operations no test calls.
- [JavaScriptCoverage.md](JavaScriptCoverage.md): JavaScriptCore builtins, the Web Inspector front end and
  WebAssembly.
- [run-cmake-coverage.sh](run-cmake-coverage.sh): the one-command CMake run. `--help` lists its options.

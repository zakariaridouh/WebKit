# Code coverage for WebKit

WebKit can measure which lines of its own C, C++, Objective-C and Swift source a test run
executed, using LLVM source-based coverage. This directory holds the documentation;
the tools live in `Tools/Scripts`.

`Followups.md` in this directory lists what is still missing.

---

## The short version

```sh
Tools/Scripts/webkit-coverage --release fast/forms
```

That builds the tree with instrumentation if it needs to, runs the layout tests you named,
and reports how much of *the code you changed* those tests executed. It ends with a count of
uncovered added lines and a path to the report.

Add `--dry-run` to see the whole plan — which tree, the pre-flight, the build command, the
change it detected, the test scope — without running anything. It is worth doing once.

Two things to know before the first run:

- **The scope is yours to choose.** `webkit-coverage` will suggest layout tests when it can
  recognise the area you touched, and decline when it cannot, which is most of the time. It
  never narrows a run on your behalf. With no scope and no `--full-suite` it exits 3 and tells
  you so, rather than starting something that takes hours.
- **A coverage build wants a build directory of its own.** An instrumented WebCore is many
  times the size of a normal one, so sharing a tree replaces the binaries every other build
  uses. `webkit-coverage` already defaults to a sibling `WebKitBuild-Coverage/`; pass
  `--build-directory=<path>` only if you want a different one. On the direct
  `build-webkit --coverage` path it is yours to set, via `WEBKIT_OUTPUTDIR`.

---

## The manual path

`webkit-coverage` is a wrapper. The four steps underneath it are worth knowing, because a real
investigation usually means running them separately.

### Xcode

```sh
export WEBKIT_OUTPUTDIR="$PWD/WebKitBuild-Coverage"

Tools/Scripts/build-webkit --xcode --release --coverage

Tools/Scripts/run-api-tests    --release --coverage --coverage-dir=/tmp/cov WTF
Tools/Scripts/run-webkit-tests --release --coverage --coverage-dir=/tmp/cov fast/forms

Tools/Scripts/generate-coverage-report --release --coverage-dir=/tmp/cov --output-dir=/tmp/report
```

### macOS CMake

`run-cmake-coverage.sh` in this directory does all four steps in one unattended command, which is
what you want for a long run. It drives this build and only this one, so it takes no `--cmake`:

```sh
Tools/CodeCoverage/run-cmake-coverage.sh --open fast/dom          # build, test, report, open it
Tools/CodeCoverage/run-cmake-coverage.sh --api-tests WTF --no-layout-tests --sources Source/WTF
Tools/CodeCoverage/run-cmake-coverage.sh                          # the whole suite; hours
```

It exits non-zero on any failure and prints the path to the HTML report. Note that
`Tools/Scripts/webkit-coverage` is the Xcode-only equivalent and answers a different question
-- how well tested are the lines this patch added.

The steps underneath it:

```sh
cmake --preset mac-coverage
cmake --build --preset mac-coverage

Tools/Scripts/run-api-tests    --release --cmake --coverage --coverage-dir=/tmp/cov WTF_Vector
Tools/Scripts/run-webkit-tests --release --cmake --coverage --coverage-dir=/tmp/cov fast/dom

Tools/Scripts/generate-coverage-report --release --cmake --coverage-dir=/tmp/cov --output-dir=/tmp/report
```

The CMake build puts its products in `WebKitBuild/cmake-mac/Coverage`, and
`generate-coverage-report --cmake` finds that on its own; `--no-coverage-build` opts out. Note
that this one *is* nested under `WebKitBuild/`, unlike the Xcode path's sibling directory, so
`rm -rf WebKitBuild` takes it with everything else — which for a tree whose profiles are paired
with those binaries means re-running the tests, not just the build.

Successive runs accumulate into one `--coverage-dir`, so an API run and a layout run share a
report. Give each run its own directory instead and name them, and each gets a column:

```sh
Tools/Scripts/generate-coverage-report --release --output-dir=/tmp/report \
    --suite=layout:/tmp/cov-layout --suite=api:/tmp/cov-api
```

> **If you are copying an older command line, drop `--lto-mode=none`.** It used to be described
> as mandatory. It was measured to be a no-op on both build systems — Debug, Release and
> Profiling already default to no LTO — and coverage links cleanly under ThinLTO. Duplicate
> `__llvm_profile_filename` needs two *strong* definitions and fails identically with or without
> LTO.

One naming wrinkle: the flag that says "use the coverage build tree" is spelled
`--coverage-build` on `generate-coverage-report` and `webkit-coverage`, but `--coverage` on
`run-webkit-tests` and `run-api-tests`, where it also means "collect profiles". The help text
for each says which.

---

## Reading the numbers

### Two files will disagree, and the report is the one to quote

`generate-coverage-report` writes both its own HTML index and `llvm-cov`'s `summary.txt`. Their
totals differ, and the difference is fully accounted for: `llvm-cov`'s per-file line count is a
*sum of per-function line counts*, so a lambda body inside its enclosing function is counted
twice. On a full-suite run that is 2,098,175 lines against the report's 1,888,952. The report's
number is a count of source lines; `llvm-cov`'s is not.

Quote the report.

### The percentage has a denominator you must state

Coverage percentages cover only the files this configuration compiled into a binary the report
reads — on a full macOS run, **8,027 of 10,473** first-party implementation files. The other
2,446 are not counted at 0%. A file with no coverage mapping has no measurable denominator, and
some of them legitimately contain no executable lines at all, so reporting them as 0% would
invent a number.

Instead they appear as a **third state**, "not built here", broken down by reason: another port
only, a feature or platform flag off (naming the flag), a generator fixture, vendored third
party, and so on. `not-built.tsv` in the report directory has the full list. Generated sources
under `DerivedSources` are excluded and counted separately again.

So the honest form is *"67.15% of the 8,027 of 10,473 files this configuration compiles"*, and
the report writes that sentence for you.

### Patch coverage is usually the number you want

Two different questions get conflated:

| Question | Command | Cost |
|---|---|---|
| Are the lines I added tested? | `compare-coverage-reports --current=… --git-diff=…` | **one** test run |
| Did the project total move? | `compare-coverage-reports --baseline=… --current=…` | **two** test runs |

Patch coverage needs no baseline, so it needs no second build and no second run, and it is
immune to line-number drift by construction. It reports the added lines that were never
executed, by number:

```
Patch coverage: 61.54% (8 of 13 added lines with coverage data covered)
 61.54%      8/13      21   80.68%  Source/WebKit/UIProcess/WebPageProxy.cpp
        uncovered added lines 16576, 16585, 16605, 16621-16622
```

Note the two percentages in that row: 61.54% of the added lines, in a file that is 80.68%
covered overall. Whole-file coverage cannot tell you that.

Added lines that carry no coverage record — comments, blank lines, braces, declarations — are
excluded from the denominator rather than counted against you.

**Delta coverage cannot be gated on.** A twenty-line, entirely untested addition to a large
well-covered file moves the project total by about 0.2 percentage points, which passes any
plausible `--fail-under-delta`. Use `--fail-under-patch`.

### A subset run gives a lower bound, and says so

Adding tests can only turn a line from uncovered to covered. So in any run that is not the
whole suite, **a covered line is exact and an uncovered line is unknown**.

The tooling encodes that rather than trusting you to remember it. A scoped report prints
`≥ 41.3%`, never `41.3%`; its output directory and trace carry a `-selective` infix so the file
cannot later be mistaken for a full-suite artifact; the banner states the shortfall in test
counts rather than percentages; `--fail-under-lines` **refuses** to gate on such a trace,
because the gate is not evaluable; and `compare-coverage-reports` **refuses** to compare a
selective trace against a full baseline, because every line the subset did not reach would read
as a regression.

`--fail-under-patch` still works on a subset, and is sound but not complete: it can raise a
false alarm, never grant a false pass. For a gate, that is the right direction.

---

## Troubleshooting

Ordered by what you will see.

**The run finishes, exits 0, and there is no coverage data.** The tree was not built with
instrumentation. `webkit-coverage` catches this in well under a second, before any test runs,
and prints the build command to fix it. If you drive the harnesses directly there is no such
check, and the only signal is one warning from `generate-coverage-report` afterwards — which
for a full layout run arrives hours later. This is the single most common mistake.

**Every Xcode script phase fails with `sandbox-exec: sandbox_apply: Operation not permitted`.**
Xcode wraps script phases in `sandbox-exec`, and a process that is already sandboxed cannot
apply another one. Pass `ENABLE_USER_SCRIPT_SANDBOXING=NO`; it does not affect the output
binaries. `webkit-coverage` and `build-webkit --coverage` add it and explain it.

**Swift fails with about ten `cannot convert value of type 'Span<T>' to expected argument type
'UnsafePointer<T>'` errors.** Same cause, one step removed: `swift-frontend` could not launch
the `_SwiftifyImport` macro plugin, Swift reported that as a *warning*, silently dropped the
safe overloads the macro synthesizes, and the call sites then failed. **This is not a coverage
problem** — see rdar://185533403. `SWIFTC_DISABLE_SANDBOX=YES` is the escape hatch, and it is
deliberately not applied for you, because it disables a security sandbox.

**The next run dies about 80 seconds in with `Address already in use`.** A killed run leaves its
servers behind:

```sh
pkill -9 -f 'layout-test-results/httpd.conf'
pkill -9 -f pywebsocket3
```

and check for a stale holder of the WPT DNS port. The harness now names the holding process and
prints the command.

**Two coverage runs interfere, or one reports no profiles.** The profile directory
`/private/tmp/WebKitCoverage` is machine-global and each run clears it on startup, including
another run's live memory-mapped files. Only one coverage run can be in flight at a time. There
is an advisory lock file naming the holder; it is checked, but nothing enforces it.

**`generate-coverage-report` refuses and names some binaries.** A profile is only valid against
the binaries that produced it, and something has been rebuilt since. Re-run the tests, or pass
`--allow-profile-mismatch` if you understand why they differ. Note the trace itself needs no
binaries — it is self-describing and keepable indefinitely, so generate it before you rebuild.

**A line view shows the wrong source, or warns that it might.** The report renders line views
from the working tree, which can have moved since the build. It detects what it can — records
past the end of a file, and files newer than the newest binary — and either recovers the text
the build actually compiled, withholds the view with a reason, or warns. The warning says the
text "may not be the text that was compiled", which is the honest limit: nothing on disk records
the revision that was built.

**Every test looks like a crash: exit 143, no crash log, "No crash log found".** Some terminal
applications and launchers send `SIGTERM` to descendants that register as ordinary
Dock-visible macOS applications, and `WebKitTestRunner` does. It affects instrumented and
uninstrumented trees alike, so it is not a coverage failure. Run the suite from a plain shell
session, or force an accessory activation policy into the driver via `run-webkit-tests
--wrapper`. Rule this out before attributing a mass crash to your change.

---

## What gets excluded, and why

- **Third-party code**, at compile time on both build systems and again at report time. The
  report-time filter is still needed, because a third-party header copied into the build
  directory is attributed to the copy and would otherwise slip past a path pattern.
- **Test and tool binaries** by default; `--include-test-support` opts in.
- **Generated sources** under `DerivedSources`, counted and named separately.
- **Per-instantiation template detail.** Functions are counted once per function, not once per
  instantiation — a method instantiated four hundred times counts once. Counting them separately
  measures template fan-out rather than test reach.

Report size is controlled with `--sources` (scope it to a directory or a file list) and
`--no-source-views`. A whole-tree report is a few hundred megabytes; a scoped one is a few.

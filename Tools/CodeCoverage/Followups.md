# Code coverage — followups

What is left, in the order I would do it. Everything here is scoped so one person or one agent can
pick it up without reading the whole history. `README.md` in this directory is the user-facing guide.

Each item says what it is, why it matters, and how big it is. "Measured" means a number in it came
from this tree; "estimated" means it did not.

---

## The one thing that should happen before anything else

**V1. Validate the whole thing end to end, on a machine nobody is agent-driving.** *(a day of
machine time, mostly waiting)*

The tooling has been through a lot of change, and **the last full-suite validation predates most of
it**. Every headline number quoted anywhere in this project comes from a single full run on a single
machine, and since then the report's own line and function totals changed, five silent-wrongness
defects were fixed, the profile path was baked into 46 more images, and both build systems changed.
Nothing is known to be broken; nothing is known to be right either.

Do a clean instrumented build, an API run and a full layout run, and then:

- Compare the headline against the numbers in `README.md`. They will differ, and the *reasons* should
  be attributable to specific changes.
- Confirm the regression count. The last full run reported 96, of which 0 were caused by
  instrumentation. Two changes since then move this: the 10× timeout multiplier is gone, and a
  driver-visible-app SIGTERM issue was found that made whole runs look like mass crashes. **Do not run
  the suite from a terminal that kills Dock-visible descendants** — see `README.md`.
- Drop `--no-retry-failures`. The 96 included 64 single-observation flakes that a retry pass would
  have reclassified.
- Confirm the 46 newly-pathed images actually deposit profiles, and that profile volume is still
  around 1.7 GB rather than many times that. This is the one measurement nobody has: 46 more images
  writing `%4m` pools could exceed it, and nobody has checked.

Until this is done, treat every number as provisional. It is also the cheapest way to find whatever
the last hundred commits broke.

---

## Correctness

**C1. Retire the second headline number, or explain it in the report.** *(small)*

`llvm-cov`'s `summary.txt` and the report disagree by design, fully reconciled to zero residual:
`llvm-cov` reports 2,098,175 lines / 67.41% where the report says 1,888,952 / 67.15%, because
`llvm-cov`'s per-file `LF:` sums per-function line counts and double-counts a lambda inside its
enclosing function. The report is right. But both files sit in the same output directory with no
explanation, and the discrepancy is invisible from inside the report. Add a two-line footnote on the
index page and a link to `summary.txt`, or stop writing `summary.txt` by default.

**C2. Toolchain ambiguity is visible but not resolved.** *(small)*

Selection order was fixed and a version older than the toolchain is now refused, and every candidate
is recorded in `coverage-provenance.json`. What remains is that a machine can still have four
`llvm-cov` binaries and nothing removes or deprioritises the wrong ones. Consider a hard failure when
two candidates of *different* versions could both have run, rather than a warning.

**C3. Provenance records the revision at report time, not the revision that was built.** *(medium)*

Nothing on disk records what was built. The dirty-file digest and per-object mtimes make drift
*detectable* — that is how the report knows to warn that a line view "may not be the text that was
compiled" — but they cannot establish the built revision. The fix is for the build to stamp it, which
means touching the build systems, which is why it has not been done. Until then the warning is
correctly phrased as uncertainty, and should stay that way.

**C4. `_WebKit_SwiftUI.framework` cannot be given a profile path.** *(blocked, upstream)*

It is 16/16 Swift, and `swiftc` has no `-fprofile-instr-generate=<path>` equivalent:
`-profile-generate` emits counters but no `__llvm_profile_filename`, and the `-ir-profile-generate` /
`-cs-profile-generate` flags are IR-level PGO, not source-based coverage. So it writes
`default.profraw` into its working directory and nothing collects it. Every other Swift-linked image
is fixed because at least one of its sources is Objective-C++. This needs a Swift change or a
one-line C++ shim in that target.

---

## Ergonomics

**E1. Make the coverage output directory automatic.** *(small)*

Of the three things a developer must currently know, two are now applied and explained
automatically. The third — that a coverage build needs a build directory of its own, because an
instrumented WebCore is many times its normal size and would replace the binaries every other build
uses — is still manual on the direct `build-webkit --coverage` path, though `webkit-coverage` defaults
it.

Note two traps if you do this. `WebKitBuild/Coverage` **cannot** be the name: `set-webkit-configuration`
writes marker files into `WebKitBuild/` and `Coverage` is one of them, so a directory of that name
collides with a file, and macOS is case-insensitive by default so a lowercase variant collides too.
And on the Xcode path `WEBKIT_OUTPUTDIR` becomes `SYMROOT`, to which `xcodebuild` always appends
`$(CONFIGURATION)` — so the leaf is necessarily `Release` or `Debug` and the coverage marker has to be
a level *above* it. A sibling `WebKitBuild-Coverage/` is safest, because `rm -rf WebKitBuild` is how
people clean and a nested tree would be destroyed by a routine clean along with the profile it is
paired with.

**E2. A baseline cache keyed by merge-base.** *(medium, ~200 lines)*

A trace needs no binaries — it is 54 MB gzipped and self-describing now that provenance is inside it,
so it can be kept indefinitely while the 45 GB tree it came from is deleted. Caching one per
merge-base would make the common comparison cost **zero** extra test runs. Roughly 5 GB per hundred
baselines. `compare-coverage-reports --baseline-source-root` already exists so a trace from another
checkout can be rebased onto this one.

**E3. Documentation beyond this directory.** *(small)*

There is no coverage documentation on webkit.org or docs.webkit.org, and there has never been a
webkit-dev thread about `llvm-cov`. Bug 83103 ("Consider using a code coverage tool", 2012, no
discussion in 13 years) is the natural venue if this is ever socialised.

---

## Coverage of things not currently covered

**N1. Debug-configuration coverage.** *(small, untried)*

Nobody has run one, and it would reach assertion paths that a Release run cannot. The historical
blocker (bug 231929, Debug+Coverage failing at `GenerateTAPI`) is **stale**: TAPI is gated behind
`WK_ENABLE_SLOW_BUILD_VERIFICATION`, which is `YES` only for `Production`, and `Production` is
unreachable from a developer build. So this should just work. `webkit-coverage` accepts `--debug` and
has never been run with it.

**N2. `run-web-platform-tests` has no coverage support.** *(small, low value)*

The WPT tests that matter are already measured: `imported/w3c/web-platform-tests` is 52.4% of the
layout suite and runs under `run-webkit-tests` like any other imported test. But
`Tools/Scripts/run-web-platform-tests`, which drives WPT's own runner via `webkitpy/w3c/wpt_runner`,
has no `--coverage`. Only worth doing for someone who uses that path.

**N3. iOS and simulator.** *(large)*

No sandbox carve-out exists in the nine iOS `.sb.in` profiles, and `InitializeThreading.cpp` carries
a deliberate `#error` for iOS-family coverage builds. Orthogonal to a local Mac workflow.

**N4. GTK/WPE.** *(large, needs a Linux machine)*

Out of scope and blocked off-Darwin: `ENABLE_LLVM_COVERAGE` is gated on `WEBKIT_SDK_IS_MACOS`, three
of the five path-baking translation units are Cocoa-only, `llvm_profile_utils` is Mach-O-only, and
`run-gtk-tests`/`run-wpe-tests` have no `--coverage`. CMake is the only route, and it now works on
macOS, which is the prerequisite.

---

## Deliberately not doing these

Recorded so they are not reopened without new evidence. Each was investigated and rejected **on
measurement**, not on taste.

| | Why not |
|---|---|
| **Coverage-driven test selection** | Measured: **14–23%** saving (two independent methods), because 84% of real commits touch code every page load executes. Mean fraction of the suite that must still run is 86.1%, and p25 through p90 are all 100%. Meanwhile a developer typing `svg/` runs 2.7% of the suite in about three minutes. Granularity is not the binding term — per-suite, per-shard and per-test all give the same 14–23%. |
| **A per-test coverage map** | Obtainable, and not worth it: an overnight run, **~45 TB of writes**, and a custom counter-scan reducer, because 72% of every raw profile is the static names table and `llvm-cov export --summary-only` is 3.83 s for JavaScriptCore alone. |
| **CI and trend dashboards** | The goal is a local per-patch loop. Gate CI only after V1 has confirmed the numbers; otherwise today's are baked into a dashboard. |
| **Tuning `%Nm`** | Volume is already flat at ~1.7 GB regardless of how few tests run, and a 1.28 GB merge takes 7 s. There is no problem. |
| **Parallelising `llvm-cov show`** | Moot — `show` is no longer run by default, and the measured win from dropping it was 1.2 s. |
| **Further report shrinking** | Moot. `--sources` makes a scoped report about 8.6 MB against 366 MB for the whole tree. |
| **WebKitLegacy via DumpRenderTree** | Already answered: it reached 13.76% from a single page load once its profile path was fixed. It was never a DumpRenderTree problem — `WebKit.framework` reexports WebKitLegacy, so it loads in every WebContent process. A DRT suite run is hours for a component nobody writes new code in. |
| **`-mllvm -limited-coverage-experimental=true`** | Actively harmful. It made a never-called inline function *disappear from the report*, inflating one file from 66.67% to 100% — it buys size by hiding the denominator. |
| **Synthesizing 0% rows for files with no coverage mapping** | Fabricates a denominator. Some files legitimately have zero executable lines; they are reported as a third state instead. |
| **`--lto-mode=none`** | Was always a no-op — Debug, Release and Profiling already default to no LTO — and coverage links fine under ThinLTO, measured on both build systems. Duplicate `__llvm_profile_filename` needs two *strong* definitions and fails identically with or without LTO. |

---

## Bugs to file

None of this has been filed. Two bugs exist and are cited in the commits: **264202** (build fails
while generating coverage data) and **259562** (`build-jsc`/`build-webgpu --coverage` silently
ignored). Everything else in this branch has no bug, and several commits deserve one — in particular
the `productDir()` fix, which affects anyone using `WEBKIT_OUTPUTDIR` on an Xcode build whether or
not they care about coverage.

Two radars are worth a comment rather than a new report: **rdar://185533403** (Swift silently drops
safe overloads when a macro plugin cannot load, which is the real cause of the ten misleading `Span`
errors) and **rdar://124640196** (a clang hang that no longer reproduces on a current clang).

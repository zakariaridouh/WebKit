# Code coverage follow-ups

Open work, in the order to do it. Each item says what it is, why it matters, and how big it is.
`README.md` is the user guide.

## Do this first

**V1. Validate the whole pipeline on a machine nobody else is driving.** *(a day, mostly waiting)*

Every headline number in these docs comes from one full run, taken before several tooling changes.
Nothing is known to be broken. Nothing is known to be right.

1. Build clean: `cmake --preset mac-coverage`, then `cmake --build --preset mac-coverage`. (Or run
   `Tools/CodeCoverage/run-cmake-coverage.sh`: build, tests and report, unattended.) A cold build
   takes a long time and about 19 GB of disk. ccache does not help, because it refuses the
   coverage flags. Read C6 first.
2. Run the API tests and the full layout suite. Drop `--no-retry-failures`. Do not run from a
   terminal that kills Dock-visible descendants (see `README.md`).
3. Check:
   - **The headline.** Compare it with `README.md`. Tie each difference to a specific change.
   - **The regression count.** Failures in an earlier full run did not come from instrumentation,
     and most were single-observation flakes that a retry reclassifies. The 10x timeout multiplier
     is gone since then, so expect a different count.
   - **The profile volume.** Confirm that the 46 images with a newly baked profile path deposit
     profiles, and that the volume stays near 1.7 GB. One pool slot costs 142 MB across the five
     frameworks (WebCore alone 111 MB), so volume is 142 MB x slots plus the test binaries. Four
     slots wrote 633 MB. That predicts about 1.27 GB at 8 slots (the current setting) and 2.3 GB at
     16. Nobody has measured 8. Raise the pool size only after a measurement. Change every site that
     `rg '%8m'` lists: each framework bakes the pattern on its own, and they must agree.

Until V1 is done, treat every number as provisional.

## Settled -- do not reopen without new evidence

**S1. The latent PCH link failures have one known cause, and it is not coverage.** *(closed)*

With `USE_PCH_CODEGEN`, a PCH object holds an out-of-line copy of every inline function that the
prefix header defines. If the prefix only declares a callee, that copy has an unresolved reference.
`-dead_strip` deletes the copy when nothing calls it. When something does, `ld` can bind the call
to an incomplete copy in another PCH object. Upstream fixed the two real cases:
`RenderElement::createsGroup()` (bug 326912) and the JSC `*Inlines.h` includes in `WebCorePrefix.h`.
See `Source/cmake/WebKitCompilerFlags.cmake` (`USE_PCH_CODEGEN`) and `Source/cmake/OptionsCocoa.cmake`
(`-dead_strip`). Do not chase the rest one symbol at a time: adding the callees' headers to the
prefix raises the number of unresolved references.

**S2. JavaScriptCore's PCH object emits no coverage records.** *(explained; one question open)*

With `USE_PCH_CODEGEN=ON`, `JavaScriptCore_pch_obj.cpp.o` has no `__llvm_*` sections, and WebCore's
has them. The functions are instrumentable (`-mllvm -system-headers-coverage` yields records). In
JavaScriptCore's compile the `wtf/` headers carry the system-header flag. In WebCore's they do not.
Ruled out as the cause: the coverage flags, `-fprofile-list`, the WTF include paths, the generated
`*_pch_obj.cpp`, and the extra `-include <prefix>` that only WebCore passes. **Open:** what makes
the same header a system header in one target and not the other.

Effect: with `-fpch-codegen`, a PCH consumer does not re-emit the prefix header's inlines, so
nothing instruments them. JavaScriptCore objects with no `-include-pch` (including the two ObjC++
unified sources) do re-emit them, so leave those out when you size the hole. Do not fix this with
`-system-headers-coverage`: it also instruments the SDK's `__inline` functions.

**S3. `_WEBKIT_TARGET_LINK_FRAMEWORK_INTO` changes no macOS build.** *(fixed, bug 328139)*

On macOS its public-framework loop runs once per configure, so `build.ninja` is byte-identical with
and without the fix. The fix still matters. A loop that runs would lose every append but the last,
drop what the recursion computes below depth 1, and put a leading `;` on the link line (a bare
`WebKit::`). Both lists are built locally and written back once. Still missing: a cycle guard.

## Correctness

**C1. Explain the second headline number in the report, or stop writing it.** *(small)*

`summary.txt` and the report disagree by design: `llvm-cov` sums per-function line counts, so it
counts a lambda body again inside its enclosing function, and a copied header once per framework.
The report is right. `README.md` explains this. The report does not. Add a footnote and a link to
`summary.txt` on the index page, or stop writing `summary.txt` by default.

**C2. Toolchain ambiguity is reported, not prevented.** *(small)*

Coverage takes `llvm-cov` and `llvm-profdata` from the toolchain first
(`CoverageLLVMProfDataExecutable`) and ignores any other LLVM major, with a warning. PGO keeps
upstream's oldest-first policy (`LLVMProfDataExecutable`, c7eb356e8810).
`coverage-provenance.json` records every candidate. Open: a machine can hold several `llvm-cov`
binaries of one major, and the pre-flight only warns when their versions differ. Consider a hard
failure when two candidates of different versions could both run.

**C3. Provenance records the revision at report time, not the revision that was built.** *(medium)*

The dirty-file digest and per-object mtimes make drift detectable, so the report warns that a line
view "may not be the text that was compiled". They cannot prove the built revision. The build must
stamp it, which means changing the build systems. Until then, keep the warning as uncertainty.

**C4. `_WebKit_SwiftUI.framework` cannot get a profile path.** *(blocked, upstream)*

`swiftc` has no `-fprofile-instr-generate=<path>` equivalent. `-profile-generate` emits no
`__llvm_profile_filename`, and `-ir-profile-generate` and `-cs-profile-generate` are IR-level PGO.
So this all-Swift framework writes `default.profraw` into its working directory, where nothing
collects it. Every other Swift-linked image has an Objective-C++ source. Fixing it needs a Swift
change or a one-line C++ shim in that target.

**C5. The collected-but-unclaimed profile guard.** *(done for CMake; Xcode names missing)*

`UNREPORTED_PROFILE_WRITERS` in `coverage_requirements.py` names 28 profile groups written by
binaries that no report describes. `partition_unclaimed_profiles()` turns those into one INFO line,
and the warning keeps only profiles that nothing accounts for. Profiles from a known binary that the
report leaves out on purpose (test-support binaries without `--include-test-support`, products
excluded by `--products`) do not warn. The tool derives that from the binaries.

- The list comes from the `<target>_CoverageProfilePath.cpp` files of a full CMake coverage build.
  It fails toward warning: a spare name costs nothing, and a missing writer still warns. Matching
  is on the whole group, so `WebKit` cannot silence `WebKitLegacy_`. Add a new target to the list
  when it joins the build (`DumpRenderTree` and `webpushtool` did).
- **Open:** the Xcode build names its XPC services after `PRODUCT_NAME`
  (`com.apple.WebKit.WebContent` and siblings). Add those from an Xcode coverage build.
- **Rejected:** warn only for known product names (the bug is a product *missing* from
  `INSTRUMENTED_PRODUCTS`); warn only for groups that no instrumented Mach-O claims
  (`WebKitTestRunner` claims its group and would still warn); warn only for dylibs (needs the
  writer's Mach-O filetype, and `WebProcess_` is an XPC service under a non-matching name).

**C6. A WebKit object can compile before WebKitAdditions is staged, and the error names neither.**
*(small)*

`WKAppKitGestureController.mm` (`Source/WebKit/UIProcess/mac/AppKitGestures/`) calls
`-supportsMomentumScroll:` and `-gestureCentroidInWindowForGesture:`. Their definitions come from
`WebKitAdditions/WKAppKitGestureControllerAdditionsImpl.mm`, imported under `__has_include`. A custom
target stages that file, and every target that needs it should depend on
`WebKitAdditions_CopyHeaders`. In the generated ninja file, bmalloc, WTF, JavaScriptCore and
WebCore have that dependency. WebKit and WebKit_SwiftInterop have none.

`Source/WebKit/CMakeLists.txt` does `list(APPEND WebKit_FRAMEWORKS WebKitAdditions)`, so the
declaration exists. `_WEBKIT_FRAMEWORK_LINK_FRAMEWORK` loses it. `WebKitAdditions_LINKED_INTO` is
already `JavaScriptCore` when WebKit is configured, so the macro swaps the entry for
`JavaScriptCore`. `WebKit::WebKitAdditions`, which carries `WebKitAdditions_CopyHeaders` as an
interface dependency, never reaches the target. The swap is right for the link and wrong for the
build order. A framework that arrives through the private-framework recursion keeps its entry. That
is why WebCore has the dependency.

The result is a silent race. When it is lost, the method bodies are missing, and
`-Werror,-Wobjc-method-access` fires at the uses with nothing naming the file. A build that passes
proves nothing: the race resolves that way whenever staging wins.

Not fixed. The fix belongs in the `if (_linked_into)` branch of `_WEBKIT_FRAMEWORK_LINK_FRAMEWORK`:
carry the substituted framework's interface dependencies. It affects every Cocoa target, and only macOS and iOS configure on a Mac.

## Ergonomics

**E1. Make the coverage output directory automatic.** *(small)*

A coverage build needs its own build directory: an instrumented WebCore is many times normal size
and would replace the binaries that every other build uses. `webkit-coverage` sets one. The direct
`build-webkit --coverage` path on Xcode still needs `WEBKIT_OUTPUTDIR` by hand. Two traps if you
automate it:

- `WebKitBuild/Coverage` cannot be the name: `set-webkit-configuration` writes a marker file with
  that name into `WebKitBuild/`. macOS is case-insensitive, so a lowercase variant collides too.
- On Xcode, `WEBKIT_OUTPUTDIR` becomes `SYMROOT`, and `xcodebuild` always appends
  `$(CONFIGURATION)`. The coverage marker must sit a level above that leaf. A sibling
  `WebKitBuild-Coverage/` is safest: `rm -rf WebKitBuild` would delete a nested tree together with
  its profile.

**E2. A baseline cache keyed by merge-base.** *(medium, about 200 lines)*

A trace needs no binaries. It is 54 MB gzipped and carries its own provenance, so it can outlive the
45 GB tree it came from. One trace per merge-base (about 5 GB per hundred) would make the common
comparison cost no extra test run. `compare-coverage-reports --baseline-source-root` already rebases
a trace from another checkout onto this one.

**E3. Documentation beyond this directory.** *(small)*

Coverage has no page on webkit.org or docs.webkit.org, and no webkit-dev thread. Bug 83103 (2012,
"Consider using a code coverage tool") is the natural venue.

## Coverage of things not currently covered

**N1. Debug-configuration coverage.** *(small, untested since 670ab4595438)*

Nobody has run one. It would reach assertion paths that Release cannot. Both known blockers are
gone. Bug 231929 (Debug plus Coverage fails at `GenerateTAPI`) is stale: TAPI runs only for
`Production`, which a developer build cannot reach. The CMake Debug link needed `-dead_strip` with
PCH codegen, and upstream 670ab4595438 (bug 327611) passes it. Link-check first, before a long
run. `webkit-coverage` accepts `--debug` and has never run with it.

**N2. `run-web-platform-tests` has no coverage support.** *(small, low value)*

The WPT tests that matter are already measured: `imported/w3c/web-platform-tests` is 52.4% of the
layout suite and runs under `run-webkit-tests`. `run-web-platform-tests` drives WPT's own runner and
has no `--coverage`. Do it only for someone who uses that path.

**N3. iOS device.** *(large; the simulator works)*

`iOSCoverage.md` has the full account. A device needs two things that the simulator does not: the
`__llvm_profile_runtime` ordering trick, and a way to copy profiles off the device. Until then
`webkitpy/port/embedded_device.py` refuses `--coverage`. macCatalyst is refused too. It would very
likely work, but nobody has measured it.

**N4. GTK and WPE.** *(large, needs a Linux machine)*

The `gtk-coverage` and `wpe-coverage` presets set `ENABLE_COVERAGE=ON`, and nothing more exists.
`ENABLE_LLVM_COVERAGE` (which bakes the profile paths) covers only macOS and simulator SDKs. Three of
the five path-baking translation units are Cocoa-only. `llvm_profile_utils` reads Mach-O only.
`run-gtk-tests` and `run-wpe-tests` have no `--coverage`.

**N5. Per-CSS-property coverage and per-overload attribution.** *(large)*

`generate-bindings-coverage` cannot attribute the CSS property IDL attributes (its largest
unattributed group). `[DelegateToSharedSyntheticAttribute]` points all of them at four generated
accessors that dispatch on the property name at run time. No per-property counter exists, and the
shared one would call every property covered once any is. Measuring them needs JS-side observation.
Separately, the numbered body per operation overload is measurable but not attributed to an IDL
line. That needs the generator's flattening order across an interface, its partials and its mixins.

**N6. The JavaScript side.** *(four small, one medium)*

- **Read `//@` directives in `generate-javascript-coverage`.** A JSTests file asks for jsc options
  in a `//@ runDefault(...)` or `//@ requireOptions(...)` comment. The tool ignores them, so code
  behind a flag reads as untested. `run-jsc-stress-tests` already parses them. No builtin sits
  behind an off-by-default feature flag, so the effect is small.
- **Dump the control-flow profiler at VM teardown, behind `useControlFlowProfiler`.** A small share of
  stress-test runs call `quit()` before the driver can dump, and are lost. A call from `~VM` (a
  JavaScriptCore change) would make the measurement independent of the test's control flow.
- **Emit lcov from both JS tools.** One `SF:` per `.js` file and one `DA:` per body line would make
  `compare-coverage-reports --git-diff` work on a builtins or front-end change, with no new code on
  that side.
- **Check the WebCore builtins over the inspector protocol.** `Debugger::attach` names builtin
  sources when the control flow profiler is on (`skipBuiltins` in `Debugger.cpp`), and the driver
  enables the profiler before `Debugger.enable`. Nobody has checked that the report places them.
- **Drive the Web Inspector front end.** The front-end number is one boot and no interaction.
  Scripted interaction over the same channel would raise it.

**N7. Code that exists only in TestIPC is not credited.** *(known limit, no action planned)*

On CMake, TestIPC compiles the WebKitPlatform objects itself and does not link `WebKit.framework`
(74d770aecd15). Its IPC code is still credited: `llvm-cov` matches counters by function name and
structural hash, and the same `.o` files are in `WebKit.framework`. Only code that exists in TestIPC
alone gets no credit.

**C-trace. The index total is 280 lines below `project_totals()`.** *(small, re-check first)*

In one full-suite run, `parse_lcov` over the trace gave 1,886,435 lines and the HTML index
showed 1,886,155. Ruled out: the trace itself, duplicate `DA:` records, and paths outside the two
roots that the index tree builds. Something between `parse_lcov` and `write_directory_index` drops
280 lines. It is the last place where the report and its own trace disagree, and the gate reads one
of them. The index page was redesigned since, so re-check first.

## Deliberately not doing

Each was rejected on measurement, not on taste. Do not reopen without new evidence.

| | Why not |
|---|---|
| **Coverage-driven test selection** | Saves 14-23% (two independent methods). 84% of real commits touch code that every page load runs, so p25 through p90 of the fraction to re-run are all 100%. A developer who types `svg/` runs 2.7% of the suite in about three minutes. Per-suite, per-shard and per-test granularity give the same result. |
| **A per-test coverage map of the whole suite** | Possible, not worth it: an overnight run, about 45 TB of writes, and a custom counter-scan reducer. 72% of every raw profile is the static names table. The scoped version (one directory or a named set of tests) exists: see `PerTestAttribution.md`. |
| **CI and trend dashboards** | The goal is a local per-patch loop. Gate CI only after V1 confirms the numbers. |
| **Tuning `%Nm`** | Profile volume is flat however few tests run, and merging even 1.28 GB is quick. Change the pool size only after V1 measures the volume. |
| **Parallelising `llvm-cov show`** | Moot. `show` does not run by default. |
| **Further report shrinking** | Moot. `--sources` makes a scoped report about 8.6 MB against 366 MB for the whole tree. |
| **WebKitLegacy via DumpRenderTree** | Answered. WebKitLegacy reached 13.76% from one page load once its profile path was fixed. `WebKit.framework` reexports it, so it loads in every WebContent process. DumpRenderTree builds on macOS CMake (8b5eae0ac0c3), which changes nothing here. A DumpRenderTree suite run takes hours for a component that gets no new code. |
| **`-mllvm -limited-coverage-experimental=true`** | Harmful. It made a never-called inline function disappear from the report and lifted one file from 66.67% to 100%. It saves size by hiding the denominator. |
| **Synthesising 0% rows for files with no coverage mapping** | That fabricates a denominator. Some files have zero executable lines. The report shows them as a third state. |
| **`--lto-mode=none`** | Always a no-op: Debug, Release and Profiling default to no LTO. Coverage links under ThinLTO on both build systems. A duplicate `__llvm_profile_filename` needs two strong definitions and fails the same way with or without LTO. |
| **Requiring `inline` on in-class declarations that an `*Inlines.h` defines** | See S1. It turns a link-time non-event into a compile error with no local fix: the callees are JSC inlines that WebCore cannot make visible. Also, `-Werror=undefined-inline` is already requested, but `OptionsCocoa.cmake` adds `-Wno-undefined-inline` after it, so it is off for every non-Swift file. Remove that line first if you revisit this. |
| **`-mllvm -system-headers-coverage`** | See S2. It closes JavaScriptCore's PCH-object hole, but it puts the SDK's `__inline` functions in a WebKit denominator. |

## Bugs

Filed:

| Bug | Subject |
|---|---|
| 264202 | The coverage build and tooling. Every coverage commit cites it. |
| 259562 | `build-jsc` and `build-webgpu` ignore `--coverage` silently |
| 328136 | `productDir()` when `WEBKIT_OUTPUTDIR` is set |
| 328137 | A layout-test run aborts when `tailspin` cannot run |
| 328138 | Layout-test servers mistake an orphaned server for their own |
| 328139 | `_WEBKIT_TARGET_LINK_FRAMEWORK_INTO` dependency propagation |

Not filed: **WebKit has no order-only dependency on `WebKitAdditions_CopyHeaders`** (C6). An object
can compile before the additions are staged and silently take the no-additions path. It is a
CMake-port bug, independent of coverage.

Known toolchain problems with existing reports. Do not file new ones:

- Swift silently drops safe overloads when a macro plugin cannot load. That is the real cause of
  the ten misleading `Span` errors.
- A clang hang that no longer reproduces on a current clang.

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
  writing `%8m` pools could exceed it, and nobody has checked. The pool size is now 8, not 4, which
  doubles it: measured from the `__llvm_prf_cnts`, `__llvm_prf_data` and `__llvm_prf_names` section
  sizes of a Coverage build, one pool slot costs 142 MB across the five frameworks (WebCore alone is
  111 MB of that), so the volume is `142 MB x N` from the frameworks plus the test binaries. The
  observed 633 MB at 4 slots predicts about 1.27 GB at 8 and 2.3 GB at 16, which is why the bump
  stopped at 8. Raise it only with a real volume measurement, and change it at every site listed by
  `rg '%8m'` -- each framework bakes the pattern separately and they must agree.

Until this is done, treat every number as provisional. It is also the cheapest way to find whatever
the last hundred commits broke. A full `cmake --build --preset mac-coverage` reached exit 0 on
2026-08-25, so nothing stands in the way of the CMake half — but see C6 before trusting a clean
build from cold, because one of its edges is ordered by luck.

---

## Settled by measurement — do not reopen without new evidence

These were open questions on the macOS CMake port. Each is answered, and the answer is not the
one the question expected, so the measurement is here rather than only in a commit message.

**S1. The remaining latent link failures are not instances of the `createsGroup()` defect.**
*(closed; two arguments in an earlier version of this section were wrong and are corrected here)*

`RenderElement::createsGroup()` was a real WebCore defect: defined in the class body while its
callee was defined in an `*Inlines.h` the prefix header does not include. Relinking WebCore
without `-Wl,-dead_strip` surfaces **19** more unresolved references from the PCH objects — 18 JSC
inlines plus `_gCRAnnotations` from `CGCrashReporterAutoLog`, which is a CoreGraphics header inline
and not a JSC one. They are not confined to one object each: `_gCRAnnotations` is undefined in five
of WebCore's six `*_pch_obj.cpp.o` and the JSC symbols in four of six.

Every referencing function is itself a JSC inline whose definition the prefix header does supply
(`JSC::JSValue::isCallable()`, `JSC::JSObject::getDirect()`, `JSC::WriteBarrierStructureID`'s
constructors, …) while the callee's definition lives in a JSC `*Inlines.h` it does not. Adding those
headers to `WebCorePrefix.h` does not resolve them and adds many more:

| Added to `WebCorePrefix.h` | Undefined symbols on a relink without `-dead_strip` | Of the original 19, still undefined |
|---|---|---|
| nothing | 19 | 19 |
| `DeferGCInlines.h`, `GCSegmentedArrayInlines.h`, `StructureInlinesLight.h`, `StructureRareDataInlines.h`, `WriteBarrierInlines.h` | 44 | 8 |
| `JSCellInlines.h`, `StructureInlines.h`, `GCSegmentedArrayInlines.h` | 216 | 7 |

The third column is the load-bearing one: `JSC::JSCell::isCallable`, `setStructure`, `isConstructor`,
the `CreatingWellDefinedBuiltinCellTag` constructor, both `Structure::get` overloads and
`_gCRAnnotations` are still undefined *after* their defining header is in the prefix. Why the
definition does not satisfy the reference is **not established**, and the earlier explanation given
here — that they are `ALWAYS_INLINE` and so never have an out-of-line definition — is wrong twice
over: `Source/WTF/wtf/Compiler.h:196-202` makes `ALWAYS_INLINE` plain `inline` unless `NDEBUG`, and
with the definition visible in the same translation unit clang inlines the call and the reference
disappears.

The other wrong argument was the systemic proposal's premise. `-Werror=undefined-inline` is **not**
in effect on this port: `WebKitCompilerFlags.cmake:323` prepends it, then
`OptionsCocoa.cmake:322` adds `-Wno-undefined-inline` for every non-Swift language, and it lands
later on the command line. Measured on the coverage build's `compile_commands.json`: 4,798 entries,
4,558 carry `-Werror=undefined-inline`, 4,723 carry `-Wno-undefined-inline`, and in all 4,558 the
`-Wno-` comes second. So the diagnostic is enabled on **zero** translation units, and anyone
revisiting the proposal has to delete `OptionsCocoa.cmake:322` first.

What stands without either argument: `-Wl,-dead_strip` is on WebCore's link line, the 19 references
are real and reference-only, and adding the callees' headers raises the total rather than lowering
it. That is enough to say this is not a bug to fix one symbol at a time, and not enough to say what
the right change is.

**S2. JavaScriptCore's PCH object emits no coverage records because its contents are system
headers.** *(explained; the obvious fix is wrong)*

With `USE_PCH_CODEGEN=ON`:

| | size | symbols | `__llvm_*` sections | `__profc_` |
|---|---|---|---|---|
| `WebCore_pch_obj.cpp.o` | 42,866,872 | 106,240 | 5 | 22,193 |
| `JavaScriptCore_pch_obj.cpp.o` | 26,228,672 | 33,387 | **0** | **0** |

The functions are there — 8,838 `JSC::` and 8,314 `WTF::` definitions, a 1.46 MB `__text` — and they
are instrumentable. Adding `-mllvm -system-headers-coverage` to that exact compile takes it from 0
to **25,208** `__profc_` with all five sections; WebCore's goes 22,193 → 28,413, so WebCore is
partially suppressed too. Preprocessing both translation units, the same file
`<build>/WTF/Headers/wtf/MathExtras.h` carries the system linemarker flag in JavaScriptCore's and
not in WebCore's: 1,472 of 1,472 `wtf/` linemarkers system-flagged against 22 of 1,538.

Ruled out as the discriminator, with the measurement: the coverage flags (identical), `-fprofile-list`
(its patterns are `*ThirdParty/*` and two libwebrtc copied-header paths, none of which match a JSC
header), the `-I`/`-isystem`/`-iframework` search paths for WTF (a
plain `-I` in both), the generated `*_pch_obj.cpp` sources (identical one-line comments), and the
extra `-include <prefix>` that WebCore alone passes from `Source/WebCore/PlatformCocoa.cmake` —
added to JavaScriptCore's PCH-object compile, and separately to its PCH generation, it produced a
byte-identical object with 0 records both times. **What has not been established is what does make
the same header system in one target's compile and not the other's.**

This is a denominator hole, but a smaller one than an earlier version of this section claimed.
`-fpch-codegen` means a consumer that uses the PCH does not re-emit the prefix header's inlines, so
for those consumers the functions are instrumented nowhere. It is not "nowhere" full stop: 11
JavaScriptCore objects carry no `-include-pch` at all, including the two ObjC++ unified sources
(`UnifiedSource-API-1-nonARC.mm.o`, `UnifiedSource-inspector-1-nonARC.mm.o`), and those do re-emit
and instrument them — 1,367 symbols defined weak-private in the PCH object have `__profc_` counters
there, among them `JSC::jsUndefined()`, `JSC::MarkedBlock::isMarked` and
`JSC::Identifier::equal`. So the claim that its PCH object's functions "do not overlap" what its
consumers instrument is **false**, and anyone sizing the hole must exclude those.

`-mllvm -system-headers-coverage` is not the fix either: JavaScriptCore's 25,208 include the SDK's
`__inline` functions, so it trades a hole for system code in the denominator.

**S3. `_WEBKIT_TARGET_LINK_FRAMEWORK_INTO`'s public-framework loop is inert even once fixed.**
*(fixed anyway)*

The double dereference is real and is fixed. On macOS the loop body executes exactly once for the
whole configure — WebCore absorbing PAL, propagating JavaScriptCore, which WebCore already lists —
so two configures of the `mac-coverage` preset with and without the fix produce a byte-identical
`build.ninja`.

Fixing the `foreach` was not enough, and landing it alone was a mistake: the body it made reachable
loses every append but the last (`PARENT_SCOPE` does not update this scope's copy, so the next
iteration reads a stale value), discards everything the recursion computes below depth 1, and on an
empty accumulator produces a leading `;` — an empty list element that survives
`list(REMOVE_DUPLICATES)` and reaches the link line as a bare `WebKit::`. All three reproduce with
`cmake -P`, and the empty-accumulator path is entered today by JavaScriptCore absorbing WTF. Both
lists are now accumulated locally and written back once; `build.ninja` is byte-identical on
`mac-coverage` and `ios-release` either way. Still missing: a cycle guard on the recursion.

**S4. The PAL removal is safe on iOS.** *(verified)*

`WebKitLegacy.framework` for the `ios-release` preset builds and links at exit 0 without the three
lines. It carries no `libPAL.a`, links `WebCore.framework`, leaves 12 PAL symbols undefined for
WebCore to satisfy — WebCore defines 4,302 — and defines three itself, all local `t` symbols from
header inlines (`PAL::TextEncoding::~TextEncoding`, `encodeForURLParsing`). No `WebPanel`, no
duplicated `__DATA` state. Against the macOS pre-fix state of 859 duplicated symbols, 282 in
`__DATA`, that is the intended outcome and no narrower restoration is needed.

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

**C5. ~~The collected-but-unclaimed guard is noisy on a healthy CMake run.~~** *(fixed)*

`UNREPORTED_PROFILE_WRITERS` in `coverage_requirements.py` now names the 26 profile-name groups
written by binaries no report describes, and `partition_unclaimed_profiles()` splits the orphans so
those become one INFO line and the warning keeps only what nothing accounts for. The list came from
the `<target>_CoverageProfilePath.cpp` files a CMake coverage build generates, on a full build at
exit 0, so it is the build's own record rather than a guess. It fails toward warning: a name nothing
writes costs nothing, a writer missing from it still warns, and matching is on the whole group so
`WebKit` cannot silence `WebKitLegacy_`.

Still incomplete on the Xcode build, which names its XPC services after `PRODUCT_NAME`
(`com.apple.WebKit.WebContent` and siblings). Someone with an Xcode coverage build should add those.

Three mechanical alternatives were rejected, recorded so they are not tried again:

- *"Warn only for groups matching a known product name."* Defeats the guard. The bug it exists to
  catch is a product **missing** from `INSTRUMENTED_PRODUCTS`, which by definition does not match.
- *"Warn only for groups no instrumented Mach-O in the build tree claims."* `WebKitTestRunner` is an
  instrumented Mach-O in the build tree that claims its group, so this still warns about it.
- *"Warn only for dylibs, note executables and bundles."* Closest to right, since the concerning case
  is product code — but it needs the writer's Mach-O filetype, and the mapping from a profile group
  back to a path is not mechanical (`WebProcess_` is an XPC service several directories down under a
  name that does not match).

**C6. A WebKit object can compile before WebKitAdditions is staged, and the failure names
neither.** *(small, and it was misdiagnosed twice)*

`WKAppKitGestureController.mm` calls `-supportsMomentumScroll:` and
`-gestureCentroidInWindowForGesture:`, and gets their definitions from
`WebKitAdditions/WKAppKitGestureControllerAdditionsImpl.mm`, `#import`ed under a
`__has_include`. A custom target stages that file into the build directory, and every target
that needs it is supposed to depend on it:

    cmake_object_order_depends_target_bmalloc          WebKitAdditions/WebKitAdditions_CopyHeaders
    cmake_object_order_depends_target_WTF              WebKitAdditions/WebKitAdditions_CopyHeaders
    cmake_object_order_depends_target_JavaScriptCore   WebKitAdditions/WebKitAdditions_CopyHeaders
    cmake_object_order_depends_target_WebCore          WebKitAdditions/WebKitAdditions_CopyHeaders
    cmake_object_order_depends_target_WebKit           (none)
    cmake_object_order_depends_target_WebKit_SwiftInterop  (none)

`Source/WebKit/CMakeLists.txt:779` does `list(APPEND WebKit_FRAMEWORKS WebKitAdditions)`, so the
declaration is there. It is lost in `_WEBKIT_FRAMEWORK_LINK_FRAMEWORK`: by the time WebKit is
configured, `WebKitAdditions_LINKED_INTO` is already `JavaScriptCore`, so the entry is replaced
by `JavaScriptCore` and `WebKit::WebKitAdditions` — carrying
`WebKitAdditions_INTERFACE_DEPENDENCIES`, which is `WebKitAdditions_CopyHeaders` — never reaches
the target. Substituting the absorbing framework is right for the *link* and wrong for the build
order, because WebKit still compiles against those headers. A framework reached through the
private-framework recursion instead keeps its entry, which is why WebCore has the dependency and
WebKit does not.

So it is a race, and `__has_include` makes it silent: lose it and the method bodies simply are
not there, and `-Werror,-Wobjc-method-access` fires on the uses 1,200 lines below with nothing
naming the missing file. It is a CMake-port defect, in scope for this work, and not fixed here —
the fix belongs in the `if (_linked_into)` branch of `_WEBKIT_FRAMEWORK_LINK_FRAMEWORK`, which
should carry the substituted framework's interface dependencies, and that affects every Cocoa
target on a host where only macOS and iOS can be configured.

Two wrong diagnoses are recorded because each cost time. The first was that the methods were
"declared nowhere in this checkout or in Internal" and the bug was upstream: they are at
`WKAppKitGestureControllerAdditionsImpl.mm:64` and `:74`. The second was that a stale Internal
explained it: pulling Internal did make the build reach exit 0, which looked like confirmation,
but a race resolves that way whenever the staging happens to win.

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

**N3. iOS device.** *(large; the simulator is done)*

The simulator part is finished and documented in `iOSCoverage.md`. The premise recorded here was
wrong twice. No sandbox carve-out was needed, because no sandbox profile is applied to a simulator
process at all -- `sandbox_check(getpid(), NULL, 0)` returns 0 in a process launched into a booted
simulator, and the only iOS-family path that would apply one is behind `ENABLE(SIMULATOR_SANDBOX)`,
which is defined nowhere in OpenSource or Internal. And no path translation was needed, because
`/private/tmp` inside a CoreSimulator runtime *is* the host's `/private/tmp`: a binary built for
`iphonesimulator` with `-fprofile-instr-generate` and a baked
`__llvm_profile_filename` of `/private/tmp/<dir>/probe_%8m%c.profraw` wrote a 49,152-byte profile
that the host read at that same path, and host `llvm-cov` reported from it -- 100% for the function
that ran, 0% for the one that did not. `collect_coverage_profiles()` works unchanged.

The `#error` is now narrowed to a non-simulator iOS-family target at all five sites,
`ENABLE_LLVM_COVERAGE` auto-enables for a simulator SDK, and six of the nine iOS `.sb.in` profiles
gained the carve-out -- defensively, since nothing evaluates it today. The end-to-end run is coded
but unexercised: no instrumented iOS-simulator build has been made.

What is left is the device, and the blocker is not the sandbox either. Auxiliary processes on iOS
already get container-temp write access dynamically: `WebProcessPool.cpp` issues a read-write
sandbox extension for `<UIProcess container>/tmp/<serviceName>` and `WebProcessCocoa.mm` consumes it
before calling `initializeLLVMProfiling()`. Two things a device needs that the simulator did not:

- **The `__llvm_profile_runtime` ordering trick.** `wtf/LLVMProfilingUtils.h` defines
  `extern "C" int __llvm_profile_runtime = 0;`, which suppresses compiler-rt's automatic
  registration constructor so the profile file is not opened at dyld load, then calls
  `__llvm_profile_initialize_file()` by hand after the extension is consumed. Continuous mode maps
  its file early, so a coverage build without this has the write denied. Coverage does none of it.
- **Retrieval.** Nothing copies a profile off a device. `webkitpy/port/embedded_device.py` refuses
  `--coverage` rather than let a run produce an empty report hours later.

No device was testable: `xcrun devicectl list devices` reported both attached physical devices
`unavailable`. macCatalyst is also still refused, conservatively -- it would very likely work, since
it applies the macOS sandbox profile and uses the host's `/private/tmp`, but it was not measured.

**N4. GTK/WPE.** *(large, needs a Linux machine)*

Out of scope and blocked off-Darwin: `ENABLE_LLVM_COVERAGE` is gated on `WEBKIT_SDK_IS_MACOS`, three
of the five path-baking translation units are Cocoa-only, `llvm_profile_utils` is Mach-O-only, and
`run-gtk-tests`/`run-wpe-tests` have no `--coverage`. CMake is the only route, and it now works on
macOS, which is the prerequisite.

**N5. Per-CSS-property coverage, and per-overload attribution.** *(large)*

`generate-bindings-coverage` reports 1,566 CSS property IDL attributes as unattributable, because
`[DelegateToSharedSyntheticAttribute]` points them all at four generated accessors that dispatch on
the property name at run time. There is no per-property counter to read, and reading the shared one
would call every CSS property covered the moment any one of them was. Measuring those needs JS-side
observation, not a C++ profile. Separately, the generator emits a numbered body per operation
overload and those bodies *are* measurable -- 273 here, 37 never executed -- but are not attributed
to an IDL line, because that needs the generator's flattening order across an interface, its
partials and its mixins reproduced.

---

**N6. The JavaScript side, four separate pieces.** *(three small, one large)*

- **Read `//@ runDefault()` directives in `generate-javascript-coverage`.** A JSTests file asks for
  its own jsc options in a `//@` comment and the tool ignores them, so a feature behind a flag reads
  as untested JavaScript. Measured: 4 of the builtins the whole stress suite never executes are
  `ShadowRealmPrototype.js`, and `--jsc-option=--useShadowRealm=1` covers 2 of them immediately.
  `run-jsc-stress-tests` already parses the directives.
- **Dump the control-flow profiler at VM teardown, behind `useControlFlowProfiler`.** 82 of 5,926
  stress-test runs call `quit()` before the tool's driver can dump, and are lost. Nothing calls
  `ControlFlowProfiler::dumpData()` except `$vm`; a call from `~VM` under the same option would make
  the measurement independent of the test's control flow. A JavaScriptCore change, not a tooling one.
- **Emit lcov from `generate-javascript-coverage`.** One `SF:` per `.js` file and one `DA:` per body
  line would make `compare-coverage-reports --git-diff` work on a builtins change, with no new code
  on that side.
- **An Inspector-protocol coverage driver.** `Runtime.enableControlFlowProfiler` +
  `Runtime.getBasicBlocks` + `Debugger.scriptParsed` is the same mechanism with the sourceID-to-URL
  mapping already solved, and it is the only route to the 181 WebCore builtins, the 93 WebCore JS
  resources and the 199,850 lines of Web Inspector front end.

**C-trace. The index total is 280 lines below `project_totals()`, and nobody knows why.** *(small)*

Measured after the trace canonicalization landed: `parse_lcov` over the shipped trace gives
1,886,435 lines, and the HTML index displays 1,886,155 — a gap of 280 lines, 0.015%. Ruled out: it
is not the trace (a canonicalized trace parses identically with and without a canonicalizer, and
the rewrite is byte-equal in meaning to `parse_lcov`'s view of the original — 16,014 paths,
1,886,435 lines, 1,257,907 covered, zero differences), it is not duplicate `DA:` records inside one
record, and it is not paths falling outside the two roots the index tree builds (there are none).
So something between `parse_lcov` and `write_directory_index` drops 280 lines. Small, but it is the
last place the report and its own trace disagree, and the gate reads one of them.

## Deliberately not doing these

Recorded so they are not reopened without new evidence. Each was investigated and rejected **on
measurement**, not on taste.

| | Why not |
|---|---|
| **Coverage-driven test selection** | Measured: **14–23%** saving (two independent methods), because 84% of real commits touch code every page load executes. Mean fraction of the suite that must still run is 86.1%, and p25 through p90 are all 100%. Meanwhile a developer typing `svg/` runs 2.7% of the suite in about three minutes. Granularity is not the binding term — per-suite, per-shard and per-test all give the same 14–23%. |
| **A per-test coverage map of the whole suite** | Obtainable, and not worth it: an overnight run, **~45 TB of writes**, and a custom counter-scan reducer, because 72% of every raw profile is the static names table and `llvm-cov export --summary-only` is 3.83 s for JavaScriptCore alone. Re-measured since, per layout test: 141 MB of transient raw profile and 9.5 s of reduction. The **scoped** version -- one directory or a named set of tests, indexed and discarded as the run goes -- is built, and documented in `PerTestAttribution.md`. The whole-suite map is still not worth having. |
| **CI and trend dashboards** | The goal is a local per-patch loop. Gate CI only after V1 has confirmed the numbers; otherwise today's are baked into a dashboard. |
| **Tuning `%Nm`** | Volume is already flat at ~1.7 GB regardless of how few tests run, and a 1.28 GB merge takes 7 s. There is no problem. |
| **Parallelising `llvm-cov show`** | Moot — `show` is no longer run by default, and the measured win from dropping it was 1.2 s. |
| **Further report shrinking** | Moot. `--sources` makes a scoped report about 8.6 MB against 366 MB for the whole tree. |
| **WebKitLegacy via DumpRenderTree** | Already answered: it reached 13.76% from a single page load once its profile path was fixed. It was never a DumpRenderTree problem — `WebKit.framework` reexports WebKitLegacy, so it loads in every WebContent process. A DRT suite run is hours for a component nobody writes new code in. |
| **`-mllvm -limited-coverage-experimental=true`** | Actively harmful. It made a never-called inline function *disappear from the report*, inflating one file from 66.67% to 100% — it buys size by hiding the denominator. |
| **Synthesizing 0% rows for files with no coverage mapping** | Fabricates a denominator. Some files legitimately have zero executable lines; they are reported as a third state instead. |
| **`--lto-mode=none`** | Was always a no-op — Debug, Release and Profiling already default to no LTO — and coverage links fine under ThinLTO, measured on both build systems. Duplicate `__llvm_profile_filename` needs two *strong* definitions and fails identically with or without LTO. |
| **Requiring `inline` on in-class declarations defined in a separate `*Inlines.h`** | See S1. It would convert a link-time non-event into a compile error with no local fix, because the callees are JSC inlines whose definitions WebCore cannot make visible. |
| **`-mllvm -system-headers-coverage` for coverage builds** | See S2. It would close JavaScriptCore's PCH-object denominator hole (0 → 25,208 counters) at the price of instrumenting the SDK's own `__inline` functions, which is system code in a WebKit denominator. |

---

## Bugs to file

None of this has been filed. Two bugs exist and are cited in the commits: **264202** (build fails
while generating coverage data) and **259562** (`build-jsc`/`build-webgpu --coverage` silently
ignored). Everything else in this branch has no bug, and several commits deserve one — in particular
the `productDir()` fix, which affects anyone using `WEBKIT_OUTPUTDIR` on an Xcode build whether or
not they care about coverage.

Two more that reproduce without coverage and are worth their own reports:

- **`RenderElement::createsGroup()` and the shape behind it.** Not the eighteen remaining symbols —
  S1 explains why those are not bugs — but the one that was fixed, and the general observation that
  `-fpch-codegen` over a prefix header of declaration-only headers depends on `-Wl,-dead_strip` to
  link at all.
- **A WebKit target with no order-only dependency on `WebKitAdditions_CopyHeaders`**, so an object
  can compile before the additions are staged and silently take the no-additions path. See C6. Ours
  to fix rather than to file, but it is a CMake-port bug independent of coverage.

Two radars are worth a comment rather than a new report: **rdar://185533403** (Swift silently drops
safe overloads when a macro plugin cannot load, which is the real cause of the ten misleading `Span`
errors) and **rdar://124640196** (a clang hang that no longer reproduces on a current clang).

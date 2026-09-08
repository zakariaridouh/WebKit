# Coverage on iOS and the iOS simulator

`Tools/CodeCoverage/README.md` describes the pipeline. This document is about the one platform
question it did not answer: whether any of it works when the tests run somewhere other than the
host.

**The short version.** The simulator works, and it needed less than anyone expected: no path
translation, no `simctl get_app_container`, and no sandbox change to make it work. A device does
not work, and the reason is not the one that was written down.

---

## What the `#error` said, and what is actually true

Until now five translation units carried this:

```
#if PLATFORM(IOS_FAMILY)
#error "LLVM_COVERAGE is macOS-only: the iOS sandbox profiles have no file-write allowance for a
        coverage directory, so profiles would be silently discarded."
#endif
```

Both halves of that turn out to be wrong for the simulator, and the second half is misleading
even for a device.

**No sandbox profile is applied to a simulator process at all.** Measured: a signed app installed
with `simctl install` and launched with `simctl launch` on an iPhone 17 Pro simulator running iOS
27.5 reported `sandbox_check(getpid(), NULL, 0) == 0`. WebKit applies none of its own on the iOS
family either — `Source/WebKit/Shared/mac/AuxiliaryProcessMac.mm`'s sandbox code is inside
`#if PLATFORM(MAC) || PLATFORM(MACCATALYST)`, and `GPUProcess::initializeSandbox` in
`GPUProcess/ios/GPUProcessIOS.mm` (and the Networking and Model equivalents) has an empty body.
The one iOS-family path that *would* apply a compiled profile is
`WebProcess::initializeSandbox`'s `#elif ENABLE(SIMULATOR_SANDBOX)` branch in
`WebProcess/cocoa/WebProcessCocoa.mm`, which calls `sandbox_apply_bytecode` on a
`com.apple.WebKit.WebContent.simulator` blob. `ENABLE_SIMULATOR_SANDBOX` is defined nowhere in
either checkout, so that branch is dead. `Source/WebKit/Scripts/compile-sandbox.sh` nevertheless
*produces* that blob whenever `sbutil` exists for the SDK, so the artifact is built and never
loaded.

**And the iOS profiles were never short of a place to write.** They have no allowance *named* a
coverage directory, which is what the `#error` literally said, but the shape was already there
twice over:

| Where | Rule |
|---|---|
| `SandboxProfiles/ios/com.apple.WebKit.WebContent.sb.in:63`, under `USE(EXTENSIONKIT)` | `(allow file-read* file-write* (container-subpath "tmp"))` |
| `Shared/Sandbox/iOS/gpu-defines.sb:139`, under `(system-attribute apple-internal)` | `(allow file-read* file-write* (subpath "/private/tmp/AudioCapture") (subpath "/tmp/AudioCapture"))` |

The second one is a `/private/tmp` write allowance in an iOS sandbox profile, which is exactly
the thing the `#error` said did not and could not exist. It is now joined by a coverage one; see
"What changed" below.

---

## The simulator: what makes it work

### `/private/tmp` inside a CoreSimulator runtime *is* the host's `/private/tmp`

This is the whole result. It was measured two ways, both with binaries built against
`iphonesimulator27.5` for `arm64-apple-ios18.0-simulator` with
`-fprofile-instr-generate -fcoverage-mapping`:

```sh
# 1. A directory created on the host, written from inside the simulator.
mkdir -p /private/tmp/probe
xcrun simctl spawn <udid> env LLVM_PROFILE_FILE='/private/tmp/probe/p_%8m%c.profraw' ./probe-sim
ls /private/tmp/probe                       # -> p_7357641815619938117_1.profraw, 49152 bytes

# 2. The real thing: a *baked* __llvm_profile_filename in an installed, launched app.
#    char __llvm_profile_filename[] = "/private/tmp/probe2/app_%8m%c.profraw";
codesign --force --sign - ZCovProbe.app
xcrun simctl install <udid> ZCovProbe.app
xcrun simctl launch --console-pty <udid> org.webkit.zcovprobe
ls /private/tmp/probe2                      # -> app_15781169085728180357_7.profraw, 49152 bytes
```

The second is the configuration that matters, because it goes through LaunchServices, gets an app
container, and reads the path from the same symbol WebKit's frameworks define rather than from
`LLVM_PROFILE_FILE`. Both files were read on the host at the path they were written to.

So `llvm_profile_utils.prepare_coverage_profile_directory()` and `collect_coverage_profiles()`
work on a simulator run **unchanged**. The path translation this was expected to need does not
exist, because there is nothing to translate.

`TMPDIR` is the trap. In the simulator it is inside the simulator:

| Launch path | `TMPDIR` |
|---|---|
| `simctl spawn` | `~/Library/Developer/CoreSimulator/Devices/<UDID>/data/tmp/` |
| installed app | `~/Library/Developer/CoreSimulator/Devices/<UDID>/data/Containers/Data/Application/<UUID>/tmp` |

Both are host-visible, and neither is a place anything collects from. That matters because the
iOS **PGO** branches next door in `InitializeThreading.cpp`, `ScriptController.cpp` and
`WebKit2InitializeCocoa.mm` do use `%t/WebKitPGO/...`, so copying the neighbouring line is the
easy way to break this. `webkitpy/coverage_simulator.py` exists to name that failure if it
happens; see "Harness" below.

### The host toolchain reads a simulator profile directly

```sh
xcrun llvm-profdata merge -sparse app_15781169085728180357_7.profraw -o app.profdata
xcrun llvm-cov report ZCovProbe -instr-profile=app.profdata
#   43 regions, 81.40%; 3 of 3 functions executed; 36 lines, 97.22%
```

No architecture or format difference to accommodate: the simulator slice is `arm64` like the
host, and `llvm-profdata` from the same toolchain that compiled it reads the raw profile.

### Continuous mode survives SIGKILL in the simulator

`%c` is in the baked path because the layout-test harness stops web processes with `SIGKILL`. It
had to be confirmed for the simulator separately, and was:

```sh
# kill.c calls a() and b() in a loop, never calls never(), then sleeps.
xcrun simctl spawn <udid> ./kill-sim &     # baked path /private/tmp/.../kill_%8m%c.profraw
sleep 6 && pkill -9 -f kill-sim
xcrun llvm-cov report ./kill-sim -instr-profile=<merged> -show-functions kill.c
#   a       100.00%
#   b       100.00%
#   never     0.00%
#   main    100.00%
```

The counters were recovered from a process that was killed without running an `atexit` handler,
and the function that was never called still reads 0%. That is the property the whole
harness-facing design rests on.

---

## Running it

The Xcode build is the only one that can run tests: `CMakePresets.json`'s `cocoa-embedded`, which
every `ios-*` preset inherits, sets `ENABLE_TOOLS=OFF`, so the CMake iOS presets build no
`WebKitTestRunner` and no API tests.

```sh
export WEBKIT_OUTPUTDIR="$PWD/WebKitBuild-Coverage-iOS"

Tools/Scripts/build-webkit --ios-simulator --release --coverage

Tools/Scripts/run-api-tests    --ios-simulator --release --coverage --coverage-dir=/tmp/cov WTF
Tools/Scripts/run-webkit-tests --ios-simulator --release --coverage --coverage-dir=/tmp/cov fast/forms

Tools/Scripts/generate-coverage-report --ios-simulator --release \
    --coverage-dir=/tmp/cov --output-dir=/tmp/report
```

`--ios-simulator` on the report step is not optional: `generate-coverage-report` takes the same
`platform_options()` every harness does and reads the products from `port._build_path()`, so
without it the report looks in `$WEBKIT_OUTPUTDIR/Release` and finds a macOS build or nothing.
With it, verified, it resolves `$WEBKIT_OUTPUTDIR/Release-iphonesimulator` — which is where
`xcodebuild` puts the simulator products, so the tool needed no change of its own.

Everything the README says about the macOS path applies: the profile directory is machine-global
and holds one run at a time, `--coverage-dir` accumulates across runs, and a subset run reports a
lower bound. The build directory has to be its own, for the same reason and more so — the
simulator products are a second full instrumented tree.

**This command sequence has not been run end to end.** A full instrumented iOS-simulator build is
tens of minutes to hours and was out of budget for the work that established the result above.
Every piece of it was verified separately — see "What is verified, and how" — but the sequence
itself is unexercised, so treat the first run as a shakedown.

The CMake side configures, for the parts of the tree that do build:

```sh
cmake --preset ios-sim-release -B WebKitBuild/cmake-iphonesimulator/Coverage -DENABLE_COVERAGE=ON
```

`ENABLE_LLVM_COVERAGE` is turned on automatically for a simulator SDK exactly as it is for macOS,
and `WEBKIT_BAKE_COVERAGE_PROFILE_PATH` generates the same one-line definition per target.

---

## The device: what is missing, and it is not the sandbox

No device was available to test — `xcrun devicectl list devices` reported both attached physical
devices as `unavailable` — so everything here is read from the source rather than measured, and
labelled accordingly. `build-webkit --coverage` for a device SDK fails at the narrowed `#error`.

Three things a device needs that a simulator does not.

**1. A profile path that exists on the device.** `/private/tmp` is not writable there and is not
the host's. The only plausible path is `%t/WebKitCoverage/<Product>_%8m%c.profraw`, matching what
the iOS PGO branches already use — but see (2), because `%t` on iOS is not simply writable either.

**2. The `__llvm_profile_runtime` ordering trick, which coverage does not currently do.** This is
the interesting one. On iOS the auxiliary processes get write access to a temp directory
*dynamically*, not from a rule in the `.sb`:

- `UIProcess/WebProcessPool.cpp` creates a read-write sandbox extension for
  `<UIProcess container>/tmp/<webContentServiceName>` under
  `#if PLATFORM(IOS_FAMILY) && !USE(EXTENSIONKIT)`.
- `WebProcess/cocoa/WebProcessCocoa.mm`'s `platformSetWebsiteDataStoreParameters` consumes it
  permanently, and *then* calls `initializeLLVMProfiling()`.
- `wtf/LLVMProfilingUtils.h` defines `extern "C" int __llvm_profile_runtime = 0;`. That
  definition is what suppresses compiler-rt's automatic registration constructor, so the profile
  file is **not** opened at dyld load. `initializeLLVMProfiling()` then calls
  `__llvm_profile_initialize_file()` by hand and logs whether the file appeared.

The ordering is the point. Continuous mode maps its profile file early; without the
`__llvm_profile_runtime` definition it would map it before the sandbox extension exists, and the
write would be denied. A device coverage build has to reproduce that whole arrangement — the
suppressed constructor, the deferred `__llvm_profile_initialize_file()`, and a call site after the
extension is consumed in each of the WebContent, GPU and Networking processes. Under
`USE(EXTENSIONKIT)` the static `(container-subpath "tmp")` rule replaces the extension and the
ordering problem goes away, which is worth checking before doing any of the above.

**3. Retrieval.** Nothing copies a profile off a device. `webkitpy/port/embedded_device.py` refuses
`--coverage` for this reason rather than letting a run produce an empty report hours later. The
pieces that would be needed: `devicectl device copy from` (or the `apple_additions()` device
manager, which already moves crash logs), a per-process container to enumerate, and a
`collect_coverage_profiles()` equivalent that reads from there. `coverage_simulator.py`'s
`collect_container_profiles()` is the shape of it, one filesystem removed.

A fourth thing, smaller: the six iOS sandbox profiles now allow `/private/tmp/WebKitCoverage`,
which is the simulator's path. A device would want the container form instead —
`(allow file-write* (container-subpath "tmp/WebKitCoverage"))` — which was **not** added, because
`container-subpath` is meaningless for the simulator's shared `/private/tmp` and adding an
unexercised rule for a configuration that cannot build would be adding a claim nobody checked.

**macCatalyst is left refused too.** It very likely works — it applies the *macOS* sandbox
profile, which has had the coverage allowance all along, and runs against the host's own
`/private/tmp` — but it was not measured, it did not build with coverage before this either, and
`PLATFORM(IOS_FAMILY_SIMULATOR)` is false for it. Turning it on is a one-line change to the same
five `#if`s plus a `maccatalyst` row in `CommonBase.xcconfig`, for whoever wants to measure it.

---

## What changed

| File | Change |
|---|---|
| `Source/WebKit/Shared/Cocoa/WebKit2InitializeCocoa.mm` | `#error` narrowed to `PLATFORM(IOS_FAMILY) && !PLATFORM(IOS_FAMILY_SIMULATOR)`; carries the measurements for all five sites |
| `Source/JavaScriptCore/runtime/InitializeThreading.cpp` | same narrowing |
| `Source/WebCore/bindings/js/ScriptController.cpp` | same narrowing |
| `Source/WebGPU/WebGPU/Instance.mm` | same narrowing |
| `Source/WebKitLegacy/mac/WebView/WebView.mm` | same narrowing |
| `Source/cmake/WebKitFeatures.cmake` | `ENABLE_LLVM_COVERAGE` auto-enables for a simulator SDK as well as macOS, and a device SDK with it forced on is now a `FATAL_ERROR` rather than a wrong baked path |
| `Source/cmake/WebKitMacros.cmake` | comment only: says why one path serves macOS and every simulator |
| `Configurations/CommonBase.xcconfig` | `WK_COVERAGE_PROFILE_DIRECTORY`, enumerated per platform; a device gets no `-fprofile-instr-generate=<path>` at all rather than a macOS path |
| six `Source/WebKit/Resources/SandboxProfiles/ios/*.sb.in` | `(allow file-write* (subpath "/private/tmp/WebKitCoverage"))` under `ENABLE(LLVM_COVERAGE)`, matching the three macOS profiles |
| `Tools/Scripts/webkitpy/coverage_simulator.py` | new: finds and collects raw profiles that landed *inside* a simulator, which is what a `%t`-baked path produces |
| `Tools/Scripts/webkitpy/port/base.py` | `coverage_unsupported_reason()` and `collect_stray_coverage_profiles()`, both no-ops |
| `Tools/Scripts/webkitpy/port/embedded_device.py` | refuses `--coverage`, naming the simulator as the alternative |
| `Tools/Scripts/webkitpy/port/embedded_simulator.py` | implements the stray-profile fallback for the devices a run used |
| `Tools/Scripts/webkitpy/layout_tests/run_webkit_tests.py` | asks the port before the run; falls back when nothing was collected |
| `Tools/Scripts/run-api-tests` | the same fallback |

The sandbox rule deserves a note, because it is a rule in a security-sensitive file that nothing
currently evaluates. It is not what makes the simulator work — no profile is applied there today.
It is there so that turning `ENABLE_SIMULATOR_SANDBOX` on cannot silently break coverage, and so
that the iOS profiles say the same thing about coverage that the macOS ones do. It is behind
`ENABLE(LLVM_COVERAGE)`, which is off in every build that is not a coverage build.

### Harness

There is no path translation, and that is deliberate. What there is instead is a diagnostic for
the one way this can go wrong.

`webkitpy/coverage_simulator.py` looks in the two places a `%t`-relative profile path lands inside
a simulator — `<data>/tmp` and `<data>/Containers/Data/*/*/tmp` — and moves anything it finds into
the run's `--coverage-dir`, with a warning naming the cause. It runs only when
`collect_coverage_profiles()` found nothing at all, so a correct run never touches it. It uses
`simctl list devices -j` to enumerate devices and their `dataPath`, falls back to the on-disk
`~/Library/Developer/CoreSimulator/Devices` layout if `simctl` cannot be run, and reads the
directories directly, because they are ordinary host directories.

`--per-test-coverage` should work on the simulator without any change of its own:
`webkitpy/xcode/simulated_device.py`'s `launch_app()` prefixes every environment variable with
`SIMCTL_CHILD_`, so the `LLVM_PROFILE_FILE` / `__XPC_LLVM_PROFILE_FILE` pair
`webkitpy/coverage_profile_environment.py` sets per test reaches the app and its XPC services the
same way `__XPC_DYLD_FRAMEWORK_PATH` does. That is a reading of the code, not a measurement.

---

## What is verified, and how

Everything in the table below was run on this machine against Xcode 27.4 (27E97), SDKs
`iphonesimulator27.5` and `iphoneos27.5`, on an iPhone 17 Pro simulator running iOS 27.5.

| Claim | How |
|---|---|
| A simulator process can write `/private/tmp/...` and the host reads it back | `simctl spawn` and `simctl install` + `simctl launch`, both with a baked `%8m%c` path |
| No sandbox profile is applied in the simulator | `sandbox_check(getpid(), NULL, 0) == 0` in the launched app |
| The host toolchain reads the profile | `llvm-profdata merge -sparse` then `llvm-cov report`, 97.22% of lines |
| `%c` survives SIGKILL in the simulator | killed mid-`sleep`; the uncalled function still reads 0% |
| The narrowed `#if` does what it says | a TU including `wtf/Platform.h`, compiled for `arm64-apple-macos15`, `arm64-apple-ios18.0-simulator` and `arm64-apple-ios18.0`: the first two emit `___llvm_profile_filename` holding `/private/tmp/WebKitCoverage/...`, the third stops at the `#error`, and with coverage off no symbol is emitted |
| The xcconfig resolves per platform | `xcodebuild -project Source/WTF/WTF.xcodeproj -showBuildSettings ENABLE_LLVM_COVERAGE=YES` for three SDKs: `OTHER_CFLAGS` gains `-fprofile-instr-generate=/private/tmp/WebKitCoverage/WTF_%8m%c.profraw` for `macosx` and `iphonesimulator`, and nothing for `iphoneos` |
| The six sandbox profiles still compile | the `DerivedSources.make` preprocessing by hand, then `xcrun --sdk iphonesimulator sbutil compile`, with `ENABLE_LLVM_COVERAGE` 0 and 1: all twelve exit 0, and the coverage variant is exactly 48 bytes of bytecode larger in every one of the six. Two of the six were compiled for `--sdk iphoneos` as well, both settings, also exit 0 and also +48 |
| `coverage_simulator.py` finds a real stray profile | run against the live booted simulator, where it located the `%t`-written `.../data/tmp/ZCovProbe/probe_7357641815619938117_7.profraw` |
| CMake configures for a simulator SDK | `cmake --preset ios-sim-release -B WebKitBuild/cmake-iphonesimulator/Coverage -DENABLE_COVERAGE=ON` exits 0, prints `Enabling ENABLE_LLVM_COVERAGE since ENABLE_COVERAGE is enabled`, and generates six `*_CoverageProfilePath.cpp` holding `/private/tmp/WebKitCoverage/<target>_%8m%c.profraw` |
| CMake refuses a device SDK, and only when it matters | `--preset ios-release -DENABLE_COVERAGE=ON -DENABLE_LLVM_COVERAGE=ON` is a `FATAL_ERROR`; `-DENABLE_LLVM_COVERAGE=ON` alone still just logs `Disabling ENABLE_LLVM_COVERAGE since ENABLE_COVERAGE is disabled` and exits 0; and `-DENABLE_COVERAGE=ON` alone still configures with `ENABLE_LLVM_COVERAGE:BOOL=OFF`, which is the instrument-and-set-`LLVM_PROFILE_FILE` mode |
| The report tool needs no change | `--ios-simulator --release` resolves `$WEBKIT_OUTPUTDIR/Release-iphonesimulator` |
| The new code is tested | `webkitpy/coverage_simulator_unittest.py`, 17 tests, plus one test each in `webkitpy/port/ios_device_unittest.py` and `ios_simulator_unittest.py` |

Not verified: a full instrumented iOS-simulator WebKit build, and therefore not the end-to-end
run. Anything about a device.

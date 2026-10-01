# Coverage on iOS and the iOS simulator

`README.md` describes the pipeline. This file covers what changes when the tests run somewhere
other than the host.

| Target | Status |
|---|---|
| iOS simulator, Xcode build | Works by design. It needs no path translation and no sandbox change. The full command sequence has not been run end to end. |
| iOS simulator, CMake build | Configures with coverage on. A CMake build can build the test runner and run tests (436e26eebf6a removed `ENABLE_TOOLS=OFF` from `cocoa-embedded`). The end-to-end coverage run is not yet tried. |
| iOS device | Not supported. `build-webkit --coverage` stops at an `#error`. |
| macCatalyst | Refused. It would very likely work. Nobody has measured it. |

## Why the simulator needs no special handling

**No sandbox profile applies to a simulator process.** A signed app, installed with `simctl install`
and started with `simctl launch`, gets `sandbox_check(getpid(), NULL, 0) == 0`. WebKit applies none
of its own on the iOS family:

- `AuxiliaryProcessMac.mm` has its sandbox code inside `#if PLATFORM(MAC) || PLATFORM(MACCATALYST)`.
- `GPUProcess::initializeSandbox` (`GPUProcess/ios/GPUProcessIOS.mm`) and the Networking and Model
  equivalents have empty bodies.
- The one iOS-family path that would apply a compiled profile is the
  `#elif ENABLE(SIMULATOR_SANDBOX)` branch of `WebProcess::initializeSandbox`
  (`WebProcess/cocoa/WebProcessCocoa.mm`). `ENABLE_SIMULATOR_SANDBOX` is defined nowhere, so the
  branch is dead. `Source/WebKit/Scripts/compile-sandbox.sh` still builds its blob when `sbutil`
  exists.

**The iOS profiles already allow writes to a temp directory.**

| Where | Rule |
|---|---|
| `SandboxProfiles/ios/com.apple.WebKit.WebContent.sb.in`, under `USE(EXTENSIONKIT)` | `(allow file-read* file-write* (container-subpath "tmp"))` |
| `Shared/Sandbox/iOS/gpu-defines.sb`, under `(system-attribute apple-internal)` | `(allow file-read* file-write* (subpath "/private/tmp/AudioCapture") (subpath "/tmp/AudioCapture"))` |

**`/private/tmp` inside a CoreSimulator runtime is the host's `/private/tmp`.** This is the key
fact. A profile that a simulator process writes there is readable on the host at the same path. So
`prepare_coverage_profile_directory()` and `collect_coverage_profiles()` in `llvm_profile_utils`
work on a simulator run unchanged. There is nothing to translate.

Two probes show it. Both use binaries built for `arm64-apple-ios18.0-simulator` with
`-fprofile-instr-generate -fcoverage-mapping`:

```sh
# 1. A directory made on the host, written from inside the simulator.
mkdir -p /private/tmp/probe
xcrun simctl spawn <udid> env LLVM_PROFILE_FILE='/private/tmp/probe/p_%8m%c.profraw' ./probe-sim
ls /private/tmp/probe                 # a .profraw file appears on the host

# 2. A baked __llvm_profile_filename in an installed, launched app.
#    char __llvm_profile_filename[] = "/private/tmp/probe2/app_%8m%c.profraw";
codesign --force --sign - CovProbe.app
xcrun simctl install <udid> CovProbe.app
xcrun simctl launch --console-pty <udid> org.webkit.covprobe
ls /private/tmp/probe2                # a .profraw file appears on the host
```

Probe 2 is the one that matters. It goes through LaunchServices, gets an app container, and takes
its path from the symbol that WebKit's frameworks define.

**The host toolchain reads a simulator profile directly.** The simulator slice is `arm64`, like the
host.

```sh
xcrun llvm-profdata merge -sparse app_*.profraw -o app.profdata
xcrun llvm-cov report CovProbe -instr-profile=app.profdata
```

**Continuous mode (`%c`) survives `SIGKILL` in the simulator.** The layout-test harness stops web
processes with `SIGKILL`. Kill a looping probe with `pkill -9`, then run
`xcrun llvm-cov report ./kill-sim -instr-profile=<merged> -show-functions kill.c`. The functions that
ran read 100%, and the function that never ran reads 0%.

### The `TMPDIR` trap

In the simulator `TMPDIR` is inside the simulator. Both locations are visible on the host, and
nothing collects from either.

| Launch path | `TMPDIR` |
|---|---|
| `simctl spawn` | `~/Library/Developer/CoreSimulator/Devices/<UDID>/data/tmp/` |
| installed app | `~/Library/Developer/CoreSimulator/Devices/<UDID>/data/Containers/Data/Application/<UUID>/tmp` |

The iOS PGO branches in `InitializeThreading.cpp`, `ScriptController.cpp` and
`WebKit2InitializeCocoa.mm` use `%t/WebKitPGO/...`. Copying that neighbouring line into a coverage
path breaks this. `webkitpy/coverage_simulator.py` exists to name that failure; see "Harness".

## Running it

### Xcode build

```sh
export WEBKIT_OUTPUTDIR="$PWD/WebKitBuild-Coverage-iOS"

Tools/Scripts/build-webkit --ios-simulator --release --coverage

Tools/Scripts/run-api-tests    --ios-simulator --release --coverage --coverage-dir=/tmp/cov WTF
Tools/Scripts/run-webkit-tests --ios-simulator --release --coverage --coverage-dir=/tmp/cov fast/forms

Tools/Scripts/generate-coverage-report --ios-simulator --release \
    --coverage-dir=/tmp/cov --output-dir=/tmp/report
```

`generate-coverage-report` needs `--ios-simulator` too. It takes the same `platform_options()` as
the harnesses and reads the products from `port._build_path()`. Without the flag it looks in
`$WEBKIT_OUTPUTDIR/Release` and finds a macOS build or nothing. With it, it resolves
`$WEBKIT_OUTPUTDIR/Release-iphonesimulator`, where `xcodebuild` puts the simulator products.

Everything in `README.md` about the macOS path applies here. The profile directory is machine-global
and holds one run at a time. `--coverage-dir` accumulates across runs. A subset run reports a lower
bound. Use a build directory of its own: the simulator products are a second full instrumented
tree.

The sequence has not been run end to end. Every piece was checked on its own (see "What is
verified"). Treat the first run as a shakedown.

### CMake build

```sh
cmake --preset ios-sim-release -B WebKitBuild/cmake-iphonesimulator/Coverage -DENABLE_COVERAGE=ON
```

- `ENABLE_LLVM_COVERAGE` turns on by itself for a simulator SDK, as it does for macOS.
  `WEBKIT_BAKE_COVERAGE_PROFILE_PATH` generates the same one-line `<target>_CoverageProfilePath.cpp`
  for each target.
- `WebCoreTestSupport` is a shared library on CMake (436e26eebf6a). It writes its own
  `WebCoreTestSupport_*.profraw` group. `generate-coverage-report` reports that binary only with
  `--include-test-support`.
- Not yet tried: build the tree, run tests with the same harness commands (add `--cmake`), and
  report.

## The device

Nobody could test a device, so this section comes from reading the source. `build-webkit
--coverage` for a device SDK stops at the narrowed `#error`:

```
LLVM_COVERAGE has no iOS-device support; see Tools/CodeCoverage/iOSCoverage.md.
The simulator is supported.
```

A device needs three things that a simulator does not.

**1. A profile path that exists on the device.** `/private/tmp` is not writable there, and it is
not the host's. The likely path is `%t/WebKitCoverage/<Product>_%8m%c.profraw`, like the iOS PGO
branches. But `%t` on iOS is not simply writable either (see 2).

**2. The `__llvm_profile_runtime` ordering trick.** Coverage does not do this today. On iOS the
auxiliary processes get write access to a temp directory dynamically, not from a rule in the `.sb`:

- `UIProcess/WebProcessPool.cpp` creates a read-write sandbox extension for
  `<UIProcess container>/tmp/<webContentServiceName>` under
  `#if PLATFORM(IOS_FAMILY) && !USE(EXTENSIONKIT)`.
- `WebProcess/cocoa/WebProcessCocoa.mm` consumes it, and then calls `initializeLLVMProfiling()`.
- `wtf/LLVMProfilingUtils.h` defines `extern "C" int __llvm_profile_runtime = 0;`. That definition
  suppresses compiler-rt's automatic registration constructor, so dyld load does not open the
  profile file. `initializeLLVMProfiling()` then calls `__llvm_profile_initialize_file()` by hand.

The order matters. Continuous mode maps its file early. Without the definition it would map the
file before the extension exists, and the write would be denied. A device coverage build must
repeat the whole arrangement: the suppressed constructor, the deferred
`__llvm_profile_initialize_file()`, and a call after the extension is consumed in each of the
WebContent, GPU and Networking processes. Under `USE(EXTENSIONKIT)` the static
`(container-subpath "tmp")` rule replaces the extension and the ordering problem goes away. Check
that first.

**3. Retrieval.** Nothing copies a profile off a device. `webkitpy/port/embedded_device.py` refuses
`--coverage` so that a run does not produce an empty report hours later. Needed: `devicectl device
copy from` (or the existing `DEVICE_MANAGER` in `embedded_device.py`), a
per-process container to enumerate, and a `collect_coverage_profiles()` equivalent that reads from
there. `collect_container_profiles()` in `coverage_simulator.py` has the same shape, one filesystem
removed.

The six iOS sandbox profiles allow `/private/tmp/WebKitCoverage`, which is the simulator's path. A
device would want `(allow file-write* (container-subpath "tmp/WebKitCoverage"))`. Nobody added it:
`container-subpath` means nothing for the simulator's shared `/private/tmp`, and an unexercised rule
for a configuration that cannot build claims something nobody checked.

**macCatalyst.** It applies the macOS sandbox profile, which has the coverage allowance, and it
uses the host's `/private/tmp`. So it very likely works. Nobody measured it, and
`PLATFORM(IOS_FAMILY_SIMULATOR)` is false for it. To try it, change the same five `#if`s and add a
`maccatalyst` row to `CommonBase.xcconfig`.

## Where the support lives

| File | Role |
|---|---|
| `WebKit2InitializeCocoa.mm`, `InitializeThreading.cpp`, `ScriptController.cpp`, `WebGPU/Instance.mm`, `WebView.mm` (WebKitLegacy) | The five sites that bake the profile path. The `#error` fires only for `PLATFORM(IOS_FAMILY) && !PLATFORM(IOS_FAMILY_SIMULATOR)`. |
| `Source/cmake/WebKitFeatures.cmake` | `ENABLE_LLVM_COVERAGE` turns on for a simulator SDK as well as macOS. A device SDK with it forced on is a `FATAL_ERROR`. |
| `Configurations/CommonBase.xcconfig` | `WK_COVERAGE_PROFILE_DIRECTORY`, set for each platform. A device gets no `-fprofile-instr-generate=<path>` at all. |
| Six `Source/WebKit/Resources/SandboxProfiles/ios/*.sb.in` | `(allow file-write* (subpath "/private/tmp/WebKitCoverage"))` under `ENABLE(LLVM_COVERAGE)`. |
| `webkitpy/coverage_simulator.py` | Finds and collects profiles that landed inside a simulator. |
| `webkitpy/port/base.py`, `embedded_device.py`, `embedded_simulator.py` | `coverage_unsupported_reason()` and `collect_stray_coverage_profiles()`. The device refuses `--coverage`. The simulator implements the fallback. |
| `webkitpy/layout_tests/run_webkit_tests.py`, `run-api-tests` | Ask the port before the run. Fall back when nothing was collected. |

The sandbox rule is not what makes the simulator work, because no profile is applied there today.
It stops a future `ENABLE_SIMULATOR_SANDBOX` from breaking coverage silently, and it makes the iOS
profiles say what the macOS ones say. It sits behind `ENABLE(LLVM_COVERAGE)`, which is off in every
build that is not a coverage build.

### Harness

There is no path translation, on purpose. There is a diagnostic for the one way this can go wrong.

`webkitpy/coverage_simulator.py` looks in the two places where a `%t`-relative profile lands inside
a simulator: `<data>/tmp` and `<data>/Containers/Data/*/*/tmp`. It moves what it finds into the
run's `--coverage-dir` and warns with the cause. It runs only when `collect_coverage_profiles()`
found nothing, so a correct run never touches it. It lists devices and their `dataPath` with
`simctl list devices -j`. If `simctl` cannot run, it falls back to the
`~/Library/Developer/CoreSimulator/Devices` layout on disk.

`--per-test-coverage` should work on the simulator unchanged. `launch_app()` in
`webkitpy/xcode/simulated_device.py` prefixes every environment variable with `SIMCTL_CHILD_`. So
the `LLVM_PROFILE_FILE` and `__XPC_LLVM_PROFILE_FILE` pair that
`webkitpy/coverage_profile_environment.py` sets for each test reaches the app and its XPC services,
the same way `__XPC_DYLD_FRAMEWORK_PATH` does. This comes from reading the code. Nobody has
measured it.

## What is verified

| Claim | How |
|---|---|
| A simulator process can write `/private/tmp/...` and the host reads it | `simctl spawn`, and `simctl install` plus `simctl launch`, both with a baked `%8m%c` path |
| No sandbox profile applies in the simulator | `sandbox_check(getpid(), NULL, 0) == 0` in the launched app |
| The host toolchain reads the profile | `llvm-profdata merge -sparse`, then `llvm-cov report` |
| `%c` survives `SIGKILL` in the simulator | Killed a looping probe. The uncalled function still reads 0%. |
| The narrowed `#if` does what it says | One TU that includes `wtf/Platform.h`, compiled for `arm64-apple-macos15`, `arm64-apple-ios18.0-simulator` and `arm64-apple-ios18.0`. The first two emit `___llvm_profile_filename` with `/private/tmp/WebKitCoverage/...`. The third stops at the `#error`. With coverage off, no symbol is emitted. |
| The xcconfig resolves per platform | `xcodebuild -project Source/WTF/WTF.xcodeproj -showBuildSettings ENABLE_LLVM_COVERAGE=YES` for three SDKs. `OTHER_CFLAGS` gains `-fprofile-instr-generate=/private/tmp/WebKitCoverage/WTF_%8m%c.profraw` for `macosx` and `iphonesimulator`, and nothing for `iphoneos`. |
| The six sandbox profiles still compile | Preprocess as `DerivedSources.make` does, then `xcrun --sdk iphonesimulator sbutil compile`, with `ENABLE_LLVM_COVERAGE` 0 and 1. All twelve exit 0. The coverage variant is 48 bytes larger in each. Two profiles also compile for `--sdk iphoneos`. |
| `coverage_simulator.py` finds a real stray profile | Run against a booted simulator. It located a `%t`-written profile under `.../data/tmp/`. |
| CMake configures for a simulator SDK | `cmake --preset ios-sim-release -B WebKitBuild/cmake-iphonesimulator/Coverage -DENABLE_COVERAGE=ON` exits 0, prints `Enabling ENABLE_LLVM_COVERAGE since ENABLE_COVERAGE is enabled`, and writes the `*_CoverageProfilePath.cpp` files. |
| CMake refuses a device SDK, and only when it matters | `--preset ios-release -DENABLE_COVERAGE=ON -DENABLE_LLVM_COVERAGE=ON` is a `FATAL_ERROR`. `-DENABLE_LLVM_COVERAGE=ON` alone logs `Disabling ENABLE_LLVM_COVERAGE since ENABLE_COVERAGE is disabled` and exits 0. `-DENABLE_COVERAGE=ON` alone configures with `ENABLE_LLVM_COVERAGE:BOOL=OFF`. |
| The report tool needs no change | `--ios-simulator --release` resolves `$WEBKIT_OUTPUTDIR/Release-iphonesimulator`. |
| The new code is tested | `webkitpy/coverage_simulator_unittest.py`, plus one test each in `webkitpy/port/ios_device_unittest.py` and `ios_simulator_unittest.py`. |

Not verified: a full instrumented iOS-simulator WebKit build (Xcode or CMake), the end-to-end run
on either, and anything about a device.

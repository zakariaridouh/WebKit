# JavaScript and WebAssembly coverage for WebKit

`README.md` covers WebKit's C, C++, Objective-C and Swift. This file covers what that pipeline
cannot see: **the JavaScript that ships inside WebKit**, and **WebAssembly**.

LLVM source-based coverage instruments compiled code. JSC compiles shipped JavaScript at run time,
not clang at build time. So that JavaScript has no coverage mapping and appears in no `.profraw`.
It is not even "not built here". It is absent.

| Code | What | Measurable today |
|---|---|---|
| JSC builtins | JavaScript compiled into JavaScriptCore | **yes**: `generate-javascript-coverage` |
| Web Inspector front end | `Source/WebInspectorUI/UserInterface` | **yes**: `generate-inspector-js-coverage --target frontend` |
| WebCore JS resources | for example the 87 files of `modern-media-controls` | **measured, not attributed**: they arrive minified |
| Page and layout-test JavaScript | whatever a page loads | **yes**: `generate-inspector-js-coverage --page URL` |
| WebCore builtins | streams, compression, `JSDOMBindingInternals` | **not yet checked**: see "Builtins over the protocol" |
| Guest WebAssembly modules | whatever a test compiles | **no**, and this build says so |
| JSC's WASM implementation | `Source/JavaScriptCore/wasm/` | yes, by the *existing* native pipeline |

## The short version

```sh
Tools/Scripts/generate-javascript-coverage JSTests/stress             # JSC builtins, via jsc
Tools/Scripts/generate-inspector-js-coverage --target frontend        # the Web Inspector front end
Tools/Scripts/generate-inspector-js-coverage --page file:///some/page.html
```

A run over `JSTests/stress` (thousands of files) takes minutes at `--jobs 6`. The
report has this shape:

```
  Functions   ≥ NN.NN%  X of Y builtins executed
  Lines       ≥ NN.NN%  X of Y body lines covered

N builtins were never executed:

  Source/JavaScriptCore/builtins/IteratorHelpers.js
         57  wrappedIterator                              4 lines
         65  builtinSetIterable                           6 lines
```

Both numbers are lower bounds and print with `≥` (see "What the two numbers mean").

### Some builtins are unreachable from jsc

`Source/JavaScriptCore/inspector/InjectedScriptSource.js` is compiled into JavaScriptCore like any
builtin. Nothing runs it unless an inspector is attached. Three functions in
`builtins/IteratorHelpers.js` (`wrappedIterator`, `builtinSetIterable`, `builtinMapIterable`) have
only injected scripts as callers:

```sh
rg --color never -n '@(builtinSetIterable|builtinMapIterable|wrappedIterator)' Source/
```

Together this is roughly a third of the builtin lines. `InjectedScriptSource.js` is mostly one
function of about 1,000 lines, `createInspectorInjectedScript`. The JSC test suite cannot reach it by
construction. A low percentage here does not mean the suite is weak.

So three denominators are all true and all different. State which one you mean. The tool prints the
first and gives a per-file table, so you can derive the others.

| Denominator | Excludes |
|---|---|
| every builtin line | nothing |
| minus `InjectedScriptSource.js` | the injected script |
| minus all inspector-only builtins | the injected script and the three `IteratorHelpers.js` functions |

## JSC builtins: how it works

### The instrument already exists

`Options::useControlFlowProfiler` makes `BytecodeGenerator` emit `op_profile_control_flow` at every
basic-block boundary and register a `BasicBlockLocation` with `VM::controlFlowProfiler()`.
`$vm.dumpBasicBlockExecutionRanges()` prints every block:

```
$ jsc --useControlFlowProfiler=true --useDollarVM=true test.js dump.js
SourceID: 5
	BasicBlock: [86019, 86056] hasExecuted: true, executionCount:3
	BasicBlock: [85868, 85938] hasExecuted: false, executionCount:0
```

That is per-basic-block coverage of JavaScript with execution counts, and it applies to builtins
because builtins are JavaScript. `Tools/Scripts/run-jsc-stress-tests` already uses the option for its
`ftl-no-cjit-type-profiler` configuration. `JSTests/controlFlowProfiler/` already asserts against it
through `$vm.hasBasicBlockExecuted`. Nothing here adds instrumentation. It adds the harness and the
report.

### The offsets are into one combined string

`builtins_generate_combined_implementation.py` joins every builtin's source into `s_JSCCombinedCode`.
`BuiltinExecutables` hands the whole string to one `StringSourceProvider`
(`Source/JavaScriptCore/builtins/BuiltinExecutables.cpp`). So all builtins share one `SourceID`, and
the dump has no file name and no line number.

The build's own generated file gives both halves of the mapping:

- `<build>/JavaScriptCore/DerivedSources/JSCBuiltins.cpp` declares, per builtin,
  `s_<codeName>Code = s_JSCCombinedCode + <offset>` and `s_<codeName>CodeLength = <length>`. These
  split the offset space into one window per builtin.
- `codeName` is `BuiltinsGenerator.mangledNameForFunction()`'s output: `lcfirst(file base name +
  ucfirst(function name))`, plus a `Constructor` suffix for an `@constructor`. The tool walks the
  `.js` files, computes the same name, and places every window. A name that two builtins claim, or
  a window that no builtin claims, stays unplaced. The tool logs it and lists it in the report.

### Line numbers are exact, and the tool checks them

The generator rewrites the text it embeds. `_parse_functions` turns every `//…` into `//` and every
`/*…*/` into `/**/` before it finds function bounds. The wrapper turns `function forEach(callback)`
into `(function (callback)`. The first change keeps the line count. The second collapses it.
`coverage_javascript.py` repeats both changes and carries an offset map back to the original text.

The tool then verifies each builtin. The head line must still name the function. The file line that
the last embedded line should land on must be the closing brace. A builtin that fails reports its
function-level coverage and no line detail. So an edited `.js` file with a stale `JSCBuiltins.cpp`
gives a missing number, not the right line numbers of the wrong text.

### Finding the builtins source needs a nonce

`SourceID`s are assigned in order of source-provider creation, and JSC creates the builtins provider
lazily. Its ID is not a constant. The shape of its offsets does not identify it either: a small test
file's blocks fall inside the *first* builtin's window.

So the driver takes **two dumps in one process**. It dumps, calls `Array.prototype.forEach` over an
array of 104,729 elements, and dumps again. Between the dumps the only JavaScript that runs is that
anchor. Therefore:

- A test file's blocks have a delta of **zero**, whatever their offsets are.
- The builtins source has a block inside `arrayPrototypeForEach`'s window with a delta of exactly
  104,729.

The anchor runs after the measurement dump, so it does not appear in the numbers. The `forEach`
reference comes from a fresh `$vm.createGlobalObject()`, so a test that assigns to
`Array.prototype.forEach` cannot move the anchor.

Two rules follow from this design:

- **Compare a delta, not an absolute count.** A test that calls `forEach` itself shifts the count,
  and the count then matches nothing.
- **Pad the driver.** The driver adds a comment past the end of the combined source. None of the
  driver's own offsets can then fall in a builtin's window. Without the padding, the driver's own
  loop matches the anchor.

If no source carries the nonce, or more than one does, the tool reports the run as unattributable
and names it. It never guesses.

### What the two numbers mean

**Functions** has a complete denominator. Every builtin that the build embedded is in the index,
whether it ran or not, because the index comes from the generated table and not from the dump. JSC
generates a builtin's bytecode on first call, and the profiler registers blocks during bytecode
generation. So a builtin that nothing called has **no basic blocks**. Zero blocks is the strongest
statement that it never ran.

**Lines** is per basic block over each builtin's body. It counts non-blank, non-comment lines that
are not punctuation-only, between the `function` keyword and the closing brace. Punctuation-only
lines are excluded, as the native pipeline excludes braces and other lines without code (see
`README.md`). A function that ends in an explicit `return` never executes the bytecode at its
closing brace, so counting `}` would show an uncovered line in every fully exercised builtin.

A block claims a line only when it overlaps a **non-whitespace** character of that line. JSC's blocks
are contiguous over a function body. Each starts at the newline that ended the previous one, so a
block's range often reaches into the indentation of the next statement. Without this rule, a
never-taken `@throwTypeError(...)` on that next line reads as covered.

Both numbers are **lower bounds**. They print with `≥`, using the same `coverage_scope.CoverageScope`
as the native pipeline. jsc is not the only thing that runs these builtins. The layout tests
exercise Promise, the module loader and the iterator protocol far harder than anything here.

### When a run is not counted

jsc evaluates its files in order into one global object, and the driver is last. A test that calls
`quit()` ends the process before the driver and contributes nothing. That is a small share of the
runs over `JSTests/stress`. The report names each one with its reason. A test that *throws* does not
have this problem: jsc reports the exception and goes on to the next file.

`--batch-size N` spreads jsc's startup cost over N files. One `quit()` then loses the whole
batch. The default is 1.

### Cost

The profiler, the padded driver and the anchor add a few milliseconds to the startup cost of each
jsc process.

This does **not** need an instrumented build. `useControlFlowProfiler` is a `Normal` option in
`OptionsList.h`, present in every configuration, so an ordinary Release build works and is faster.
That comes from reading `OptionsList.h`. Nobody has tried it on a non-coverage `jsc`.

### It writes no profiles into the shared directory

Every binary in a coverage build has `/private/tmp/WebKitCoverage/<Product>_%8m%c.profraw` baked
into `__llvm_profile_filename`. That directory is machine-global (see the "two coverage runs
interfere" entry in `README.md`). Thousands of jsc runs would drop thousands of raw profiles into
whatever native coverage run is in flight. So every jsc that the tool starts has `LLVM_PROFILE_FILE`
pointed at a directory of its own, or at `/dev/null`. No code path leaves it unset.

The tool also sets `DYLD_FRAMEWORK_PATH`. The CMake build's `jsc` links
`/System/Library/Frameworks/JavaScriptCore.framework`, and from a plain shell it dies with
`Symbol not found: __ZN3JSC17DeferredWorkTimer24scheduleWorkSoonIfActiveE…`.

## The Web Inspector front end and page JavaScript, over the protocol

`InspectorRuntimeAgent` wraps the **same** `ControlFlowProfiler`:

| Protocol command | Where |
|---|---|
| `Runtime.enableControlFlowProfiler` | `Source/JavaScriptCore/inspector/protocol/Runtime.json`, `InspectorRuntimeAgent.cpp` |
| `Runtime.disableControlFlowProfiler` | `Runtime.json` |
| `Runtime.getBasicBlocks(sourceID)` returns `[{startOffset, endOffset, hasExecuted, executionCount}]` | `Runtime.json`, `InspectorRuntimeAgent.cpp` |
| `Debugger.scriptParsed` returns `{scriptId, url, startLine, startColumn, endLine, endColumn}` | `Debugger.json` |
| `Debugger.getScriptSource(scriptId)` returns the exact text the VM compiled | `Debugger.json` |

The last two commands make this *easier* than the jsc path. `scriptParsed` supplies the
`sourceID`-to-URL mapping that the combined builtins source lacks. `getScriptSource` supplies the
text. So none of the offset recovery in `coverage_javascript.py` applies, and no line number is
inferred from a build artefact.

```sh
Tools/Scripts/generate-inspector-js-coverage --target frontend
```

A front-end run gives the same result each time. The report has the same
shape as the jsc report: functions, lines, and a never-executed list by file. It places the files
that `Main.html` loads. It does not place the rest (`Debug/`, `Test/`, workers). This is one boot of
the front end and no interaction, so the number is a floor and not a verdict.

### The driver: WebKitTestRunner, and the inspector inspecting the inspector

Remote inspection on macOS is an XPC service, not a WebSocket, so there is no cheap out-of-process
client to write. The tree already has one. `Internals::openDummyInspectorFrontend(url)` attaches an
`InspectorStubFrontend` to the calling page and opens `url` as its front end. `WebInspectorUI`
ships `TestStub.html` and `InspectorProtocol.awaitCommand` for this, and `LayoutTests/inspector/*`
uses them. So the driver is a generated HTML page that runs under `WebKitTestRunner`. It adds no
client, no build product and no layout test.

`--target frontend` is a double attach:

1. The driver page opens the real `Main.html` as **its own** front end. That gives the front end an
   `InspectorFrontendHost`, so it boots. It complains about missing localized strings and carries
   on.
2. From inside that front end, `TestStub.html` opens as **the front end's** front end.
   `WebKitTestRunner` injects `window.internals` into the front end too, so this is possible.

Now a protocol channel exists whose *inspected page* is the Web Inspector. `openDummyInspectorFrontend`
uses `window.open`, so all three pages share one WebContent process, one `VM` and one
`ControlFlowProfiler`.

### Reload the target

`Runtime.enableControlFlowProfiler` calls `VM::enableControlFlowProfiler` inside `vm.whenIdle()` and
then `deleteAllCode`. Everything the target compiled before that loses its blocks. So the driver
enables the profiler, reloads the target, and waits for `scriptParsed` events to stop. For
`--target page` the workload `<iframe>` reloads, not the driver page: the driver page's `Internals`
owns the stub frontend, and its document must survive.

### `getBasicBlocks` returns two kinds of record

`InspectorRuntimeAgent::getBasicBlocks` joins two things (`ControlFlowProfiler.cpp`). The protocol
does not document this.

- The basic blocks that `BytecodeGenerator` registered, with real execution counts. They exist only
  for code that was **compiled**, which for a function means called.
- Every range in `VM::functionHasExecutedCache()`. It holds the whole *text* of every function that
  a compiled `CodeBlock` declares, called or not. A function's range flips to executed when that
  function's own `CodeBlock` is created.

The second kind is why a function that nothing called is visible at all. It is also a trap. A
function's whole-text range reads `hasExecuted` once the function has run **once**, and it spans
every line, including lines that never ran. Crediting it would report every entered function as fully
covered. So the tool discards every range that **strictly contains another range** of the same
source before it attributes any line.

Two more shapes in real data need handling:

- **An inverted, zero-width range** such as `[1356, 1355]`, at the offset where a top-level function
  declaration begins. It is always `hasExecuted`, because the program ran. It must not count as "a
  range inside the extent that executed".
- **The same range reported twice.** A single-expression arrow's extent and its one basic block have
  identical offsets. The tool merges them: executed if either is, and the higher count. This is how
  `coverage_lcov` merges.

### Line numbers, and how they are checked

Every number comes from the text that the VM compiled. The tool either fetches it with
`Debugger.getScriptSource`, or reads the checkout file **after** it verifies the file's identity. A
`file:` URL under a build's `WebInspectorUI.framework/Resources` maps back to
`Source/WebInspectorUI/UserInterface/...` and is then checked. When the text was fetched, the check
compares against it exactly. Otherwise the check uses `endLine` and `endColumn`, which `Debugger.cpp`
derives from the source's line count and its last line's length. A path that fails is reported as a
URL, not a file. Naming a checkout file whose text is not what ran would point at the right line of
the wrong text.

A function assigned to a name takes the name from the assignment: `WI.roleSelectorForNode =
function(node)` reports as `WI.roleSelectorForNode`. Without this, many of the never-executed
functions would show as `(anonymous)`.

### Page JavaScript

```sh
Tools/Scripts/generate-inspector-js-coverage --page file:///path/to/page.html
```

The report lists each source that has blocks, with its line and function coverage. With no
`--page`, a generated workload exercises the WebCore stream builtins and a `<video controls>`. That
makes WebCore inject its media controls, which then appear under their `sourceURL`
(`__InjectedScript_ModernMediaControls.js`), with function names.

## What this cannot reach

### Builtins over the protocol

WebCore compiles each builtin function as its own `SourceProvider`, with no URL.
`ReadableStreamDefaultReaderBuiltins.cpp` declares `s_readableStreamDefaultReaderCancelCode` and
its length, not an offset into a combined string. JSC's builtins share one provider
(`s_JSCCombinedCode`).

`Debugger::attach` skips builtin functions unless the control flow profiler is on (`skipBuiltins` in
`Source/JavaScriptCore/debugger/Debugger.cpp`). Without that exception no `scriptParsed` event
names them, and `Debugger.getScriptSource` cannot be asked about a `sourceID` that the debugger never
registered. Their blocks are in the profiler either way.

The driver enables the profiler before `Debugger.enable`, so `scriptParsed` should name builtin
sources. **Not yet checked:** that the report places them. Until someone checks, treat WebCore's
builtins as unmeasured over the protocol. `generate-javascript-coverage` stays the tool for JSC's
builtins.

**Scanning `sourceID`s by hand does not work.** A scan of 1 to 4000 returned block data for almost
every ID. Most of the records were a pair of identical `[0, N]` ranges with N converging on about
224. The cause: each protocol *response* reaches the front end as a fresh script.
`InspectorFrontendAPIDispatcher::dispatchMessageAsync` evaluates
`InspectorFrontendAPI.dispatchMessageAsync(<message>)`, which makes a new `SourceProvider` and a new
`sourceID`. Each probe creates the next source the scan will find, so the scan never ends. N
converges because the response that describes a 225-byte source is itself 225 bytes long. The tool
never probes a `sourceID` that the backend did not name.

If the `Debugger.cpp` route falls short, one new command would list every URL-less source in one
round trip, with no scanning:

```
Runtime.getAllBasicBlocks -> [{ sourceID, url, sourceLength, basicBlocks }]
```

`ControlFlowProfiler` already holds `m_sourceIDBuckets`. The command is a loop over its keys plus
`VM::functionHasExecutedCache()`. It needs a `Source/JavaScriptCore` change. Nobody has attempted it.

### Injected media controls: measured, but not attributable to files

The injected script arrives as one source with no URL and the directive
`//# sourceURL=__InjectedScript_ModernMediaControls.js`. The build joins the 87 authored files into
`ModernMediaControls.js` (9,892 lines). Then `make-js-file-arrays.py` runs `jsmin` over it, which
leaves 2,042 lines and no source map. A line in the report cannot be traced to
`Modules/modern-media-controls/controls/slider.js`. A coverage build passes `--no-minify` instead,
from both `WebCoreMacros.cmake` and `DerivedSources.make`. The script then keeps the concatenation's
9,892 lines, but the tool does not yet split it back into files.

The tool still reports it, with names, under its `sourceURL`. That answers "how much of the media
controls ran" and not "which file". Unminified, the concatenation offsets are recoverable the same
way `s_JSCCombinedCode`'s are.

## WebAssembly

Two questions are called "WASM coverage". Only one has an answer here.

### Which WASM tiers a run reaches: yes, with the existing pipeline

`Source/JavaScriptCore/wasm/` is ordinary C++, so the native pipeline already covers it. Each tier
has its own plan class, so that class's coverage tells you whether a run compiled through the tier:

| File | Tier |
|---|---|
| `WasmIPIntPlan.cpp` | the in-place interpreter (IPInt) |
| `WasmBBQPlan.cpp` | BBQ |
| `WasmOMGPlan.cpp` | OMG |
| `WasmOSREntryPlan.cpp` | OSR entry into a WASM loop |

There is no `WasmLLIntPlan`. IPInt replaced the WASM LLInt, so the tiers are IPInt, BBQ, OMG.

Example: a small module whose loop is in JavaScript, called two million times, tiers up through BBQ
and OMG. Its `WasmOSREntryPlan.cpp` stays at 0%, because nothing OSR-entered a WASM loop. That kind
of finding is cheap to get.

To reproduce it, with a private profile directory:

```sh
LLVM_PROFILE_FILE=/tmp/wasm-profiles/jsc_%p_%m.profraw \
DYLD_FRAMEWORK_PATH=WebKitBuild/cmake-mac/Coverage \
    WebKitBuild/cmake-mac/Coverage/jsc your-wasm-test.js

xcrun llvm-profdata merge -sparse -o /tmp/wasm.profdata /tmp/wasm-profiles/*.profraw
xcrun llvm-cov report WebKitBuild/cmake-mac/Coverage/JavaScriptCore.framework/JavaScriptCore \
    -instr-profile=/tmp/wasm.profdata Source/JavaScriptCore/wasm
```

Use `xcrun llvm-cov`. An old `llvm-cov` earlier on `PATH` cannot read current profiles.

### Coverage of the guest WASM modules a suite runs: no

**Which lines or functions of a `.wasm` module a test executed is not measurable with this build.
The reason is structural, not a missing harness.**

- **JSC's control-flow profiler cannot see WASM.** `BytecodeGenerator` creates `BasicBlockLocation`s,
  and WASM has no `BytecodeGenerator`. This search matches nothing:
  `rg --color never -c 'BasicBlockLocation|profile_control_flow|controlFlowProfiler'
  Source/JavaScriptCore/wasm/`. The mechanism this file rests on stops at the WASM boundary.
- **`--dumpWasmSourceFileName` needs a debug build.** It would at least list which modules a suite
  ran, by writing each validated module to `<name>N.wasm`. It sits under
  `#if ENABLE(WEBASSEMBLY) && ASSERT_ENABLED` in `WasmStreamingParser.cpp`. A release build says so:

  ```
  $ jsc --dumpWasmSourceFileName=/tmp/wsrc wasm-test.js
  Wasm streaming parser created, but we can only dump source in debug builds.
  ```

  So even the inventory question needs a build this configuration is not.
- **`--dumpWasmOpcodeStatistics` prints nothing under jsc.** It registers, and says so
  (`Use notifyutil -v -p com.apple.WebKit.wasm.op.stat to dump statistics.`). But it delivers the dump
  on the **main dispatch queue** (`notify_register_dispatch` in `WasmOpcodeCounter.cpp`), and jsc's
  main thread runs the script to the end and exits without servicing that queue. Posting
  `notifyutil -p com.apple.WebKit.wasm.op.stat` during a long run produced nothing. It should work
  in a WebContent process, which does service its main queue, so it is usable under
  `WebKitTestRunner` and not under jsc. Even there it counts opcodes **decoded**, once per tier that
  parses the function. The `increment()` calls sit in `WasmFunctionParser`'s decode loop. It does
  not count opcodes executed.

Real guest-WASM coverage needs one of two things: instrumenting the module (a `.wasm`-to-`.wasm`
rewrite, outside WebKit), or basic-block counters in IPInt and BBQ (a JSC change). Nobody has
attempted either.

## Known limits

### `generate-javascript-coverage` (jsc)

- **`//@` directives in JSTests files are not read.** A test that asks for options of its own (for
  example `//@ runDefault("--useConcurrentJIT=false")` or `//@ requireOptions(...)`) does not get them.
  Code behind a flag then reads as untested. `--jsc-option=--FLAG=VALUE` passes a flag to every jsc
  process, and you can repeat it. Parsing the directives the way `run-jsc-stress-tests` does is the
  real fix. No builtin sits behind an off-by-default feature flag, so the effect is small.
- **A `quit()` before the driver loses the run.** The report names each lost run. A
  `dumpBasicBlockExecutionRanges` at VM teardown, behind the same option, would fix it.
- **This measures jsc only.** The layout tests use the builtins far more. Read every number as a
  lower bound from the JSC suite alone, which is what `≥` says.

### `generate-inspector-js-coverage` (the protocol)

- **One page load, no interaction.** Driving the front end (opening tabs, selecting resources) would
  raise the number. Sending front-end actions with `Runtime.evaluate` into the front-end page needs
  no protocol change.
- **No merge across runs.** Each run reports itself. The jsc side accumulates over thousands of
  processes by taking the maximum count per range. The equivalent here is a merge per `(path, line)`,
  because `sourceID`s are not stable across processes. Until then, a directory of pages means N
  reports.
- **No lcov output, so no patch coverage.** One `SF:` record per resolved file and one `DA:` per
  code line would make `compare-coverage-reports` and patch coverage work on front-end changes, with
  no new code on that side. It applies to both tools. It is the highest-value follow-up here.
- **The function total is a lower bound on both halves.** A function whose enclosing function was
  never compiled has no range. A `function` expression whose extent is not itself a range (the range
  starts at the parameter list) is not counted.
- **A source with no basic blocks is not a source at 0%.** The reload did not reach it. The tool
  counts it separately and keeps it out of the denominator.
- **It needs `WebKitTestRunner`, so macOS and a build.** `--save-log=PATH` keeps a run's stderr.
  `--log=PATH` re-reports it with no build, in a few seconds.

### Both

- **The reports are text and JSON, not HTML.** `--json=PATH` carries per-file `uncovered_lines` and
  `never_executed_functions`, which a line view could render.

## Files

| File | Role |
|---|---|
| `Tools/Scripts/generate-javascript-coverage` | the JSC builtins tool |
| `Tools/Scripts/webkitpy/coverage_javascript.py` | offset recovery, attribution, report model, and the shared line rules |
| `Tools/Scripts/webkitpy/coverage_javascript_unittest.py` | tests, no build required |
| `Tools/Scripts/generate-inspector-js-coverage` | the Inspector-protocol tool |
| `Tools/Scripts/webkitpy/coverage_inspector.py` | driver page, payload reader, path verification, report model |
| `Tools/Scripts/webkitpy/coverage_inspector_unittest.py` | tests over a captured payload, no build required |

`TextLineIndex` and `is_code_line` live in `coverage_javascript.py`. `coverage_inspector.py` imports
them and does not copy them. Two rules were costly to get right, and they must mean the same thing
in both reports: a range claims a line only where it overlaps non-whitespace, and punctuation-only
lines are not in the denominator.

```sh
cd Tools/Scripts && python3 -m pytest webkitpy/coverage_javascript_unittest.py \
    webkitpy/coverage_inspector_unittest.py -q
```

### Two environment traps

**`DYLD_FRAMEWORK_PATH` is not enough for `WebKitTestRunner`.** The Network, GPU and WebContent
processes are XPC services, and they read `__XPC_DYLD_FRAMEWORK_PATH` and
`__XPC_DYLD_LIBRARY_PATH`. With only the unprefixed names set, the run dies at once with
`WebKit framework version mismatch` followed by `com.apple.WebKit.Networking.Development
terminated`. `webkitpy.port.base.setup_environ_for_server` copies those four names for this reason.

**`LLVM_PROFILE_FILE` is not enough either, and the failure is silent.** Every binary in a coverage
build has `/private/tmp/WebKitCoverage/<Product>_%8m%c.profraw` baked in, and that directory is
machine-global. One `WebKitTestRunner` run with `LLVM_PROFILE_FILE` set and `__XPC_LLVM_PROFILE_FILE`
unset leaves `.profraw` files in the shared directory, possibly in the middle of another coverage
run, because the child processes never see the override. Both tools set both names on every
process they start. No code path leaves either unset.

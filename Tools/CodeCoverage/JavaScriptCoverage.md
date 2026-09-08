# JavaScript and WebAssembly coverage for WebKit

`README.md` in this directory describes coverage of WebKit's C, C++, Objective-C and Swift. This
file is about the code that pipeline structurally cannot see: **the JavaScript that ships inside
WebKit**, and **WebAssembly**.

LLVM source-based coverage instruments compiled code. JavaScript shipped in the product is
compiled by JSC at runtime, not by clang at build time, so it has no coverage mapping, appears in
no `.profraw`, and is in none of the three states the main report distinguishes. It is not even
"not built here" — it is simply absent.

There is a lot of it:

| | files | what | measurable today |
|---|---|---|---|
| JSC builtins | 24 | 102 functions, 2,737 statement lines | **yes** — `generate-javascript-coverage` |
| Web Inspector front end | 711 | 199,850 lines | **yes** — `generate-inspector-js-coverage --target frontend`, 659 files placed |
| WebCore JS resources | 93 | 12,358 lines, 87 of the files `modern-media-controls` | **measured, not attributed** — they arrive minified |
| Page and layout-test JavaScript | — | whatever a page loads | **yes** — `generate-inspector-js-coverage --page URL` |
| WebCore builtins | 16 | 181 functions (streams, compression, `JSDOMBindingInternals`) | **no** — no protocol call enumerates them |
| Guest WebAssembly modules | — | whatever a test compiles | **no, and this build says so** |
| JSC's WASM implementation | — | `Source/JavaScriptCore/wasm/`, 43,914 lines | yes, by the *existing* native pipeline |

The rest of this file says what "measurable" means in each row, with numbers from this checkout.

---

## The short version

```sh
Tools/Scripts/generate-javascript-coverage JSTests/stress             # JSC builtins, via jsc
Tools/Scripts/generate-inspector-js-coverage --target frontend        # the Web Inspector front end
Tools/Scripts/generate-inspector-js-coverage --page file:///some/page.html
```

Measured on this checkout, over all 5,926 files of `JSTests/stress`, five minutes at `--jobs 6`:

```
  test files          5,926 run, 5,844 dumps attributed to the builtins source

  Functions   ≥ 90.20%  92 of 102 builtins executed
  Lines       ≥ 55.32%  1514 of 2737 body lines covered
```

Four of those ten never-executed builtins are an artefact of the harness rather than a gap in the
tests: `ShadowRealm` is behind `--useShadowRealm=1`, and a JSTests file asks for its own options
with a `//@ runDefault(...)` directive that `run-jsc-stress-tests` honours and this tool does not.
With the flag passed for the whole suite:

```sh
Tools/Scripts/generate-javascript-coverage --jsc-option=--useShadowRealm=1 JSTests/stress
```

```
  Functions   ≥ 92.16%  94 of 102 builtins executed
  Lines       ≥ 55.75%  1526 of 2737 body lines covered

8 builtins were never executed:

  Source/JavaScriptCore/builtins/IteratorHelpers.js
         57  wrappedIterator                              4 lines
         65  builtinSetIterable                           6 lines
         81  builtinMapIterable                           6 lines
  Source/JavaScriptCore/builtins/ShadowRealmPrototype.js
         57  crossRealmThrow                              3 lines
         65  importValue                                 13 lines
  Source/JavaScriptCore/inspector/InjectedScriptSource.js
         31  createObjectWithoutPrototype                 7 lines
         44  createArrayWithoutPrototype                  6 lines
         57  createInspectorInjectedScript              1020 lines
```

### What that list actually says

**Six of the eight are Web Inspector code, and jsc cannot reach any of it.**
`InjectedScriptSource.js` is compiled into JavaScriptCore like any other builtin, but nothing
instantiates it without an inspector attached — and the three `IteratorHelpers.js` functions turn
out to have exactly two callers in the whole tree, both of them injected scripts:

```
$ rg --color never -n 'builtinSetIterable|builtinMapIterable|wrappedIterator' Source/
Source/WebCore/inspector/CommandLineAPIModuleSource.js:127  for (let type of @builtinSetIterable(…
Source/JavaScriptCore/inspector/InjectedScriptSource.js:124  return @wrappedIterator(iteratorFunction…
Source/JavaScriptCore/inspector/InjectedScriptSource.js:928  for (let value of @builtinSetIterable(object))
Source/JavaScriptCore/inspector/InjectedScriptSource.js:947  for (let [key, value] of @builtinMapIterable(object))
```

That is 1,049 of the 2,737 measurable lines — **38% of the JavaScript built into JavaScriptCore is
unreachable from the JSC test suite by construction**, not undertested. Which is itself the most
useful thing this measurement produced, and it is invisible in the 55.75%.

Attaching an inspector is what runs that code, and `generate-inspector-js-coverage` does attach one —
but it still cannot see these particular 1,049 lines, because they are *builtins* and the protocol
has no way to name a builtin's source. That is the one thing neither tool reaches; see
"What this cannot reach" below.

**Two are a genuine test gap**: `ShadowRealmPrototype.crossRealmThrow` and
`ShadowRealmPrototype.importValue`, 16 lines.

So the three denominators, all true and all different:

| denominator | covered | |
|---|---|---|
| every builtin line | 1,526 / 2,737 | **55.75%** |
| excluding `InjectedScriptSource.js` | 1,526 / 1,704 | **89.55%** |
| excluding all Inspector-only builtins | 1,526 / 1,688 | **90.40%** |

State which one you mean. The tool reports the first and gives you the per-file table to derive
the others.

---

## JSC builtins: how it works

### The instrument already exists

`Options::useControlFlowProfiler` makes `BytecodeGenerator` emit `op_profile_control_flow` at every
basic-block boundary and register a `BasicBlockLocation` with `VM::controlFlowProfiler()`.
`$vm.dumpBasicBlockExecutionRanges()` prints every one of them:

```
$ jsc --useControlFlowProfiler=true --useDollarVM=true test.js dump.js
SourceID: 5
	BasicBlock: [86019, 86056] hasExecuted: true, executionCount:3
	BasicBlock: [85868, 85938] hasExecuted: false, executionCount:0
```

That is per-basic-block coverage of JavaScript source, with execution counts, and it applies to
builtins because builtins *are* JavaScript. `Tools/Scripts/run-jsc-stress-tests` already uses the
same option for its `ftl-no-cjit-type-profiler` configuration, and `JSTests/controlFlowProfiler/`
already asserts against it through `$vm.hasBasicBlockExecuted`. Nothing here adds instrumentation;
it adds the harness and the report.

### The offsets are into one 140,940-byte string

`builtins_generate_combined_implementation.py` concatenates every builtin's source into
`s_JSCCombinedCode` and `BuiltinExecutables` hands the whole thing to one `StringSourceProvider`
(`BuiltinExecutables.cpp:45`). So all 102 builtins share one `SourceID`, and the dump contains no
file name and no line number anywhere.

Both halves of the mapping are recovered from the build's own generated file:

- `<build>/JavaScriptCore/DerivedSources/JSCBuiltins.cpp` declares, per builtin,
  `s_<codeName>Code = s_JSCCombinedCode + <offset>` and `s_<codeName>CodeLength = <length>`. That
  partitions the offset space into 102 windows whose lengths sum exactly to 140,940.
- The `codeName` is `BuiltinsGenerator.mangledNameForFunction()`'s output — `lcfirst(file base name
  + ucfirst(function name))`, plus a `Constructor` suffix for an `@constructor`. Walking the `.js`
  files forward and computing the same name places every window. **102 of 102, no collisions,
  nothing left over.**

### Line numbers are exact, and that is checked rather than assumed

The generator rewrites what it embeds. `_parse_functions` replaces every `//…` with `//` and every
`/*…*/` with `/**/` before finding function bounds, and the wrapper turns `function
forEach(callback)` into `(function (callback)`. The first substitution preserves line count; the
second collapses it. `coverage_javascript.py` reproduces both while carrying an offset map back to
the original text, because without it every line number is wrong by the height of the licence
block: `arrayPrototypeForEach` came out at `ArrayPrototype.js:91` instead of 115, and 93 of the
102 builtins were misplaced.

Placement is then *verified* per builtin — the head line must still name the function, and the
file line the last embedded line is predicted to land on must be the closing brace. **102 of 102
verify on this checkout**, and a builtin that fails reports its function-level coverage and no
line detail. That is what makes an edited `.js` file with a stale `JSCBuiltins.cpp` produce a
missing number instead of the right line numbers of the wrong text.

### Identifying the builtins source needs a nonce

`SourceID`s are assigned in order of source-provider creation and the builtins provider is created
lazily, so its ID is not a constant: measured as 3 in a run over one test file and 5 in a run over
two. Nor can it be recognised by the shape of its offsets — a program source's blocks are numbered
from 0, so a 219-byte test file's blocks all fall inside the *first* builtin's window and satisfy
any such test.

So the driver takes **two dumps in one process**: dump, then call `Array.prototype.forEach` over
an array of 104,729 elements, then dump again. Between the two dumps the only JavaScript that runs
is that anchor, so:

- a test file's blocks have a delta of **zero**, whatever its offsets look like;
- the builtins source has a block inside `arrayPrototypeForEach`'s window with a delta of exactly
  104,729.

Because the anchor runs *after* the measurement dump, it does not appear in the numbers. The
`forEach` reference is taken from a fresh `$vm.createGlobalObject()`, so a test that has already
assigned to `Array.prototype.forEach` — and jsc evaluates every file into one global object —
cannot silently move the anchor. Verified against a file that does exactly that: the nonce still
lands at offset 86,019.

Two mistakes are worth recording, because both produced plausible-looking wrong numbers:

- **An absolute count does not work.** A test that itself calls `forEach` six times leaves the
  block at 6; the anchor takes it to 104,735; `count == 104729` then matches nothing in the
  builtins source. Measured on `JSTests/stress/array-species-functions.js`, which reported *0 of
  102 builtins executed* while its own dump plainly showed `forEach` at count 6.
- **The driver's own source matched instead.** Its anchor loop really does run 104,729 times, and
  unpadded its blocks sit at offsets ~400, inside the first builtin's window. Over the whole
  stress suite this made `Array.prototype.forEach` and `Array.prototype.reduceRight` read as never
  executed. The driver is now padded with a comment past the end of the combined source, so none
  of its offsets can fall in a builtin's window at all.

If no source carries the nonce, or more than one does, the run is reported as unattributable and
named. It is never guessed.

### What the two numbers mean

**Functions** has a complete denominator. Every builtin the build embedded is in the index whether
it ran or not, because the index comes from the generated table and not from the dump. A builtin
nothing called has **no basic blocks at all** — JSC generates a builtin's bytecode on first call
and the profiler registers its blocks during bytecode generation — so zero blocks is the strongest
possible statement that it never ran, not missing data.

**Lines** is per basic block over each builtin's body: non-blank, non-comment, non-punctuation-only
lines between the `function` keyword and the closing brace. Punctuation-only lines are excluded for
the reason `README.md` already gives for the native pipeline ("comments, blank lines, braces,
declarations … are excluded from the denominator rather than counted against you"): a function that
ends in an explicit `return` never executes the bytecode at its closing brace, so counting `}`
would report an uncovered line in every fully exercised builtin.

A block claims a line only when it overlaps a **non-whitespace** character of it. JSC's blocks are
contiguous over a function body and each begins at the newline that ended the previous one, so a
block's range routinely reaches into the *indentation* of the next statement. Crediting that line
to the block was how a never-taken throw came out covered: in `flatIntoArray`, block
`[82860, 82953]` executed 12 times and ends inside line 288's leading whitespace, while the block
that is `@throwTypeError("flatten array exceeds 2**53 - 1");` on that same line never ran. The tool
reported the closing brace as the only uncovered line — the wrong line and the wrong conclusion.
With the rule in place it names 285 and 288, which are the recursive nested-array path and the
overflow throw.

Both numbers are **lower bounds** and printed with `≥`, using the same `coverage_scope.CoverageScope`
the native pipeline uses. jsc is not the only thing that runs these builtins: Promise, the module
loader and the iterator protocol are exercised far harder by the layout tests than by anything here.

### When a run is not counted

jsc evaluates its files in order into one global object and the driver is last, so a test that
calls `quit()` ends the process before the driver and contributes nothing. Over `JSTests/stress`
that is **82 of 5,926 runs** (1.4%), each named in the report with its reason —
`atomics-strong-cas.js:4` is `if (i >= 300000) quit();`. A test that *throws* does not have this
problem: jsc reports the exception and carries on to the next file, which is verified.

`--batch-size N` amortises jsc's 40 ms of startup across N files at the cost of losing a whole
batch to one `quit()`. The default is 1.

### Cost

44 ms per jsc process. Bare `jsc empty.js` on this build is 40.5 ms, so the profiler, the
141,634-byte padded driver and the 104,729-iteration anchor together cost **3.4 ms** — the price
is jsc's startup, not the measurement. The whole of `JSTests/stress` — 5,926 processes — is 5
minutes wall clock at `--jobs 6`.

This does **not** need an instrumented build. The control-flow profiler is a `Normal` option
present in every configuration, so an ordinary Release build works and is faster. That follows from
`OptionsList.h` rather than from measurement: this checkout has no non-coverage `jsc` to try it on.

### It writes no profiles into the shared directory

Every binary in a coverage build has `/private/tmp/WebKitCoverage/<Product>_%8m%c.profraw` baked
into `__llvm_profile_filename`, and that directory is machine-global — `README.md`'s "two coverage
runs interfere" entry is about exactly this. Running jsc 5,926 times would land 5,926 raw profiles
in the middle of whatever native coverage run is in flight. Every jsc this tool starts has
`LLVM_PROFILE_FILE` pointed at a directory of its own, or at `/dev/null`; there is no code path
that leaves it unset.

It also sets `DYLD_FRAMEWORK_PATH`, because the CMake build's `jsc` links against
`/System/Library/Frameworks/JavaScriptCore.framework` and run from a plain shell dies with
`Symbol not found: __ZN3JSC17DeferredWorkTimer24scheduleWorkSoonIfActiveE…`.

---

## The Web Inspector front end, and page JavaScript, over the protocol

`InspectorRuntimeAgent` wraps the **identical** `ControlFlowProfiler`:

| protocol command | file |
|---|---|
| `Runtime.enableControlFlowProfiler` | `Source/JavaScriptCore/inspector/protocol/Runtime.json:386`, `InspectorRuntimeAgent.cpp:487` |
| `Runtime.disableControlFlowProfiler` | `Runtime.json:390` |
| `Runtime.getBasicBlocks(sourceID)` → `[{startOffset, endOffset, hasExecuted, executionCount}]` | `Runtime.json:394`, `InspectorRuntimeAgent.cpp:530` |
| `Debugger.scriptParsed` → `{scriptId, url, startLine, startColumn, endLine, endColumn}` | `Debugger.json:376` |
| `Debugger.getScriptSource(scriptId)` → the exact text the VM compiled | `Debugger.json:262` |

The last two rows are what makes this **easier** than the jsc path rather than harder. `scriptParsed`
supplies the `sourceID`-to-URL mapping the combined builtins source does not have, and
`getScriptSource` supplies the text, so none of the offset recovery in `coverage_javascript.py` is
needed and no line number is ever inferred from a build artefact.

```sh
Tools/Scripts/generate-inspector-js-coverage --target frontend
```

Measured on this checkout, **10.2 seconds wall clock**, and bit-for-bit reproducible across runs:

```
  sources             665 reported by Debugger.scriptParsed, 665 with basic blocks,
                      659 placed in a checkout file

  Functions   ≥ 11.72%  1622 of 13838 functions executed
  Lines       ≥ 17.49%  22830 of 130520 code lines covered

12216 functions were never executed. The first 10, by file:

  Source/WebInspectorUI/UserInterface/Base/BlobUtilities.js
          27  blobForContent
          34  decodeBase64ToBlob
          58  textToBlob
          63  blobAsText
  Source/WebInspectorUI/UserInterface/Base/DOMUtilities.js
          32  WI.roleSelectorForNode
          43  WI.linkifyAccessibilityNodeReference
          60  WI.linkifyStyleable
          69  WI.linkifyNodeReference
          80  WI.linkifyNodeReferenceElement
          94  WI.bindInteractionsForNodeToElement
```

That is one boot of the front end and no interaction, so it is a floor and not a verdict — but it
is the first number of any kind for this code. Split by who wrote it:

| | functions | lines |
|---|---|---|
| WebKit's own front end (636 files) | 1,117 / 10,172 — **10.98%** | 12,931 / 94,648 — **13.66%** |
| `External/` (CodeMirror, three.js, Esprima; 23 files) | 505 / 3,666 — **13.78%** | 9,799 / 35,683 — **27.46%** |
| both | 1,622 / 13,838 — **11.72%** | 22,830 / 130,520 — **17.49%** |

659 of the 711 `.js` files under `Source/WebInspectorUI/UserInterface` are placed in the report;
the other 52 are the ones `Main.html` does not load (`Debug/`, `Test/`, workers). Exactly **one**
file has zero covered lines, which is the useful shape of the result: nearly everything is loaded
and parsed, and almost none of it runs.

### The driver: WebKitTestRunner, and the inspector inspecting the inspector

Remote inspection on macOS is an XPC service, not a WebSocket, so there is no cheap out-of-process
protocol client to write — but the tree already has one. `Internals::openDummyInspectorFrontend(url)`
attaches an `InspectorStubFrontend` to the calling page and opens `url` as its front end, and
`WebInspectorUI` ships `TestStub.html` — eight small files and `InspectorProtocol.awaitCommand` —
for exactly this. `LayoutTests/inspector/*` is driven this way. So the driver is a generated HTML
page run under `WebKitTestRunner`: no new client, no new build product, no layout test added.

`--target frontend` is a double attach, and it is the whole trick:

1. the driver page opens the real `Main.html` as **its own** front end, which is how the front end
   gets an `InspectorFrontendHost` and boots at all (it complains about missing localized strings
   and carries on);
2. from inside that front end — reachable because `WebKitTestRunner` injects `window.internals`
   into it too — `TestStub.html` is opened as **the front end's** front end.

Now there is a protocol channel whose *inspected page* is the Web Inspector. `openDummyInspectorFrontend`
uses `window.open`, so all three pages are in one WebContent process and therefore one `VM` and one
`ControlFlowProfiler`.

### Both targets have to be reloaded, and that is not a detail

`Runtime.enableControlFlowProfiler` calls `VM::enableControlFlowProfiler` inside `vm.whenIdle()` and
then `deleteAllCode` (`InspectorRuntimeAgent.cpp:519`). Everything the target compiled before that
point loses its blocks. Measured: enabling the profiler after the front end had booted left
**1 source with block data out of 653**. The driver therefore enables the profiler, reloads the
target, and waits for `scriptParsed` to go quiet. With the reload: **662 of 662**.

For `--target page` it is the workload `<iframe>` that reloads, not the driver page: the driver
page's `Internals` owns the stub frontend and its document has to survive.

### `getBasicBlocks` returns two different kinds of record

`InspectorRuntimeAgent::getBasicBlocks` concatenates two things (`ControlFlowProfiler.cpp:72-102`),
and this is not documented anywhere in the protocol:

- the basic blocks `BytecodeGenerator` registered, with real execution counts — but only for code
  that was **compiled**, which for a function means called;
- every range in `VM::functionHasExecutedCache()`, which is filled in with the whole *text* of
  every function a compiled `CodeBlock` declares whether or not it was ever called
  (`CodeBlock.cpp:447` and `:458`, both guarded by `wasCompiledWithControlFlowProfilerOpcodes`) and
  flipped to executed when that function's own `CodeBlock` is created (`CodeBlock.cpp:421`).

The second record is why a function nothing called is visible at all, and it is what the headline
list is made of. It is also the one trap. A function's whole-text range reads `hasExecuted` as soon
as the function has run **once**, and it spans every line of the function including the ones that
never ran, so crediting it reports any function that was entered as fully covered. Every range that
**strictly contains another range** of the same source is therefore discarded before a line is
attributed. Verified against a page whose ranges were printed against their own text: the arrow
function at `[699, 1022]` tiles exactly over its own blocks `[699, 977]`, `[978, 1014]`,
`[1014, 1014]`, `[1015, 1016]` and `[1017, 1022]`, and dropping the tile is what makes
`[1014, 1014]` — a `)` that never ran — visible.

Two smaller shapes in real data, both of which produced wrong numbers before they were handled:

- **`[1356, 1355]`** — an inverted, zero-width range at the offset a top-level function declaration
  begins. It is always `hasExecuted`, because the *program* ran. Counted as "a range inside the
  extent that executed" it reported `createCodeMirrorTextMarkers` as executed in a file the same run
  correctly reported as 0 of 117 lines covered.
- **the same range reported twice** — a single-expression arrow's extent and its one basic block
  have identical offsets. Folded by taking executed from either and the higher count, the way
  `coverage_lcov` merges.

### Line numbers, and the check that they are right

Every number is computed from the text the VM compiled: either fetched with
`Debugger.getScriptSource`, or read from the checkout file **after** verifying its identity. A
`file:` URL under a build's `WebInspectorUI.framework/Resources` is mapped back to
`Source/WebInspectorUI/UserInterface/...` and then checked — against the fetched text exactly when
it was fetched, otherwise against `endLine` and `endColumn`, which `Debugger.cpp:411-419` derives
from the source's line count and its last line's length. A path that fails is reported as a URL, not
as a file, because naming a checkout file whose text is not what ran is how a report ends up
pointing at the right line of the wrong text.

The result was checked in bulk. Of the front-end run's function records that carry a name,
**13,341 of 13,341 land on a file line that contains that function's own name**, zero mismatches;
401 more are genuinely anonymous and reported as `(anonymous)`. Anonymous functions assigned to a
name get it from the assignment — `WI.roleSelectorForNode = function(node)` reports as
`WI.roleSelectorForNode` — because without that, 8 of the first 12 never-executed functions in the
report were `(anonymous)`.

### Page JavaScript

```sh
Tools/Scripts/generate-inspector-js-coverage --page file:///path/to/page.html
```

Measured on a page that loads `LayoutTests/resources/js-test.js` and calls four of its functions:

```
  sources             9 reported by Debugger.scriptParsed, 5 with basic blocks, 1 placed in a checkout file

     lines          lines    functions  file
  ≥ 17.53%        119/679        19/70  LayoutTests/resources/js-test.js
```

With no `--page`, a generated workload exercises the WebCore stream builtins and a
`<video controls>`, which is what makes WebCore inject its media controls:

```
  Functions   ≥ 34.63%  285 of 823 functions executed
  Lines       ≥ 49.51%  1111 of 2244 code lines covered

  ≥ 47.83%       957/2001      285/727  (injected) __InjectedScript_ModernMediaControls.js
```

2,568 basic blocks for the media controls, with function names, from one page load.

---

## What this cannot reach, with the evidence

### WebCore's 181 builtins: no protocol call enumerates them

They are compiled one `SourceProvider` per builtin function — `ReadableStreamDefaultReaderBuiltins.cpp`
declares `s_readableStreamDefaultReaderCancelCode` and its length, not an offset into a combined
string — with no URL. And `Debugger::attach` deliberately skips them:

```cpp
if (function->scope()->realm() == globalObject && function->executable()->isFunctionExecutable()
    && !function->isHostOrBuiltinFunction())        // Debugger.cpp:196
```

So no `scriptParsed` event ever names them, and `Debugger.getScriptSource` cannot be asked about a
`sourceID` the debugger has not registered. Their blocks *are* in the profiler:
`Runtime.getBasicBlocks("N")` for an arbitrary N returns them.

**Scanning `sourceID`s by hand does not work, and the reason is worth recording.** A scan of
1..4000 returned block data for **3,997** of them. 3,893 of those were a pair of identical
`[0, N]` records with N converging on 223 and 224 — because each protocol *response* is delivered to
the front end by evaluating `InspectorFrontendAPI.dispatchMessageAsync(<message>)` as a fresh script
(`InspectorFrontendAPIDispatcher.cpp:146`), which creates a new `SourceProvider` with a new
`sourceID`. Each probe manufactures the next source the scan will find, so the scan never
terminates; and the length converges because the response that describes a 225-byte source is itself
225 bytes long. The tool therefore never probes a `sourceID` the backend did not name.

The fix is a JSC change, not a tooling one, and it is one command: see "Proposed protocol addition"
below.

### WebCore's injected media controls: measured, but not attributable to files

The injected script arrives as one 160,899-byte source, 2,042 lines, with no URL and the directive
`//# sourceURL=__InjectedScript_ModernMediaControls.js`. The build concatenates the 87 authored
files into `ModernMediaControls.js` (335,694 bytes, **9,892 lines**) and then
`make-js-file-arrays.py` runs `jsmin` over it, because neither `WebCoreMacros.cmake` nor
`DerivedSources.make` passes the `--no-minify` the script already has. 9,892 lines become 2,042 and
there is no source map, so a line in the report cannot be traced to
`Modules/modern-media-controls/controls/slider.js`.

It is still reported — 2,001 code lines, 727 functions, with names, under its `sourceURL` directive —
which answers "how much of the media controls ran" but not "which file". Building with
`--no-minify` would make the concatenation offsets recoverable exactly the way
`s_JSCCombinedCode`'s are.

### The JSC builtins are not visible here either

They share one `SourceProvider` (`s_JSCCombinedCode`) which is also a builtin source, so the same
`Debugger::attach` filter hides it. `generate-javascript-coverage` remains the tool for those, and
its blind spot remains: with an inspector attached `InjectedScriptSource.js` certainly runs, and
that tool reports its 1,020 lines as 0%. Closing that needs the same protocol addition.

### Proposed protocol addition

One command would make WebCore's builtins, the JSC builtins and any other URL-less source
enumerable in a single round trip, with no scanning and no `sourceID` inflation:

```
Runtime.getAllBasicBlocks -> [{ sourceID, url, sourceLength, basicBlocks }]
```

`ControlFlowProfiler` already holds `m_sourceIDBuckets`, so the implementation is a loop over its
keys plus `VM::functionHasExecutedCache()`. It needs a `Source/JavaScriptCore` change and is not
attempted here.

---

## WebAssembly

Two different questions get called "WASM coverage", and only one of them is answerable here. This
section says plainly which.

### Which of JSC's WASM tiers a run reaches — yes, with the existing pipeline

`Source/JavaScriptCore/wasm/` is ordinary C++ and the native pipeline already covers it. What was
not obvious is that this is enough to answer a real question: *which tiers did the suite compile
through?* Each tier has its own plan class, so its coverage is the answer.

Measured on this checkout, from one 40-line JavaScript file that builds a two-instruction WASM
module and calls it two million times, profiled into a private directory and reported with
`llvm-cov` over `--sources Source/JavaScriptCore/wasm`:

| file | functions | tier reached? |
|---|---|---|
| `WasmIPIntPlan.cpp` | 63.64% | yes — the in-place interpreter ran |
| `WasmBBQPlan.cpp` | 85.71% | yes — tiered up to BBQ |
| `WasmOMGPlan.cpp` | 83.33% | yes — and on to OMG |
| `WasmOSREntryPlan.cpp` | **0.00%** | no — nothing OSR-entered a WASM loop |
| `WasmStreamingParser.cpp` | 86.67% | — |
| `WasmSectionParser.cpp` | 12.20% | — |
| `WasmFunctionParser.h` | 27.37% | — |

Whole-directory total for that one module: **7.61% of regions, 11.72% of 43,914 lines**.

`WasmOSREntryPlan.cpp` at 0% is the shape of finding this gives you cheaply: the module's own loop
was in JavaScript, not in WASM, so nothing exercised WASM loop OSR entry. There is no
`WasmLLIntPlan` in this tree at all — the WASM LLInt has been replaced by IPInt, so the tiers are
IPInt → BBQ → OMG.

Reproducing it needs nothing new:

```sh
LLVM_PROFILE_FILE=/tmp/mine/jsc_%p_%m.profraw \
DYLD_FRAMEWORK_PATH=WebKitBuild/cmake-mac/Coverage \
    WebKitBuild/cmake-mac/Coverage/jsc your-wasm-test.js

xcrun llvm-profdata merge -sparse -o /tmp/mine.profdata /tmp/mine/*.profraw
xcrun llvm-cov report WebKitBuild/cmake-mac/Coverage/JavaScriptCore.framework/JavaScriptCore \
    -instr-profile=/tmp/mine.profdata Source/JavaScriptCore/wasm
```

Use `xcrun llvm-cov`, not `/usr/local/bin/llvm-cov`, which on this machine is LLVM 3.2svn.

### Coverage of the guest WASM modules a suite runs — no

**Which lines or functions of a `.wasm` module a test executed is not measurable with this build,
and the reason is structural rather than a missing harness.**

- **JSC's control-flow profiler cannot see WASM.** `BasicBlockLocation`s are created by
  `BytecodeGenerator`, and WASM has no `BytecodeGenerator`.
  `rg --color never -c 'BasicBlockLocation|profile_control_flow|controlFlowProfiler'
  Source/JavaScriptCore/wasm/` matches **zero** lines. The mechanism the rest of this document is
  built on stops at the WASM boundary.
- **`--dumpWasmSourceFileName` needs a debug build.** It would at least tell you *which* modules a
  suite ran, by writing each validated module's bytes to `<name>N.wasm`. It is guarded by
  `#if ENABLE(WEBASSEMBLY) && ASSERT_ENABLED` (`WasmStreamingParser.cpp:83`), and this build says
  so out loud rather than failing quietly:

  ```
  $ jsc --dumpWasmSourceFileName=/tmp/wsrc wasm-test.js
  Wasm streaming parser created, but we can only dump source in debug builds.
  ```

  So even the inventory question — what WASM does the suite execute — needs a build this
  configuration is not.
- **`--dumpWasmOpcodeStatistics` produces no output under jsc.** It registers, and says so:

  ```
  $ jsc --dumpWasmOpcodeStatistics=true wasm-test.js
  <WASM.OP.STAT><16631> Registering callback for wasm opcode statistics.
  <WASM.OP.STAT><16631> Use `notifyutil -v -p com.apple.WebKit.wasm.op.stat` to dump statistics.
  ```

  but the dump is delivered on the **main dispatch queue**
  (`WasmOpcodeCounter.cpp:113`, `notify_register_dispatch(key, &token, mainDispatchQueueSingleton(), …)`),
  and jsc's main thread runs the script to completion and exits without ever servicing that queue.
  Verified: posting `notifyutil -p com.apple.WebKit.wasm.op.stat` three seconds into a
  40-million-iteration run produced nothing beyond the two registration lines. It would work in a
  WebContent process, which does service its main queue — so this is usable under
  `WebKitTestRunner` and not under jsc.

  It is also worth knowing what it counts even where it works: the `increment()` calls are in
  `WasmFunctionParser`'s decode loop (`WasmFunctionParser.h:560`), so it counts opcodes
  **decoded**, once per tier that parses the function — not opcodes executed.

Real guest-WASM coverage would mean either instrumenting the module (a `.wasm`-to-`.wasm` rewrite,
outside WebKit) or adding basic-block counters to IPInt and BBQ, which is a JSC change and not a
tooling one. It is not attempted here and this file does not claim it.

---

## Known limits

### `generate-javascript-coverage` (jsc)

- **`//@` directives in JSTests files are not read.** A test that asks for `--useShadowRealm=1`,
  `--useTemporal=1` or a JIT configuration does not get it, so a feature behind a flag reads as
  untested code. Measured: without the flag, 4 of the 10 never-executed builtins over the whole
  stress suite are `ShadowRealmPrototype.js`; with it, 2 of those 4 are covered and the function
  total moves from 92 to 94 of 102. `--jsc-option` is the workaround; parsing the directives the
  way `run-jsc-stress-tests` does is the fix.
- **A `quit()` before the driver loses the run.** 82 of 5,926 on the stress suite. They are named,
  not hidden. A `dumpBasicBlockExecutionRanges` at VM teardown, behind the same option, would fix
  this for good.
- **This measures jsc only.** The builtins are used far more heavily by the layout tests, and this
  tool cannot see that. The honest reading of every number there is "a lower bound from the JSC
  suite alone", which is what the `≥` says.

### `generate-inspector-js-coverage` (the protocol)

- **One page load, no interaction.** The front-end number is what booting the inspector runs.
  Driving it — opening tabs, selecting resources — would raise it, and there is no mechanism here
  for scripting that. Sending front-end actions over the same channel with `Runtime.evaluate` into
  the front-end page is the obvious next step and would need no protocol change.
- **No merge across runs.** Each run reports itself. `BuiltinsCoverage` on the jsc side accumulates
  over thousands of processes by taking the maximum count per range; the equivalent here would be
  merging per `(path, line)` since `sourceID`s are not stable across processes. Until then, running
  a directory of pages means reading N reports.
- **No lcov output, so no patch coverage.** Emitting one `SF:` record per resolved file and `DA:`
  per code line would make `compare-coverage-reports` and patch coverage work on Inspector
  front-end changes with no new code on that side. This is the single highest-value follow-up,
  and it applies to both tools.
- **The function total is a lower bound on both halves.** A function whose enclosing function was
  never compiled has no range at all, and a `function` expression whose extent is not itself a
  range (the range starts at the parameter list) is not counted. 401 of the front end's 13,838
  records are `(anonymous)`.
- **A source with no basic blocks is not a source at 0%.** It is a script the reload did not reach,
  and it is counted separately rather than dropped into the denominator.
- **It needs `WebKitTestRunner`, so macOS and a build.** `--log` re-reports a saved run with no
  build at all, which is how the 3.1 MB log from a front-end run is turned into a different view in
  2.7 seconds.

### Both

- **The reports are text and JSON, not HTML.** `--json` carries per-file `uncovered_lines` and
  `never_executed_functions`, which is what a line view would render.

---

## Files

| | |
|---|---|
| `Tools/Scripts/generate-javascript-coverage` | the JSC builtins tool |
| `Tools/Scripts/webkitpy/coverage_javascript.py` | offset recovery, attribution, report model, and the shared line rules |
| `Tools/Scripts/webkitpy/coverage_javascript_unittest.py` | 64 tests, no build required |
| `Tools/Scripts/generate-inspector-js-coverage` | the Inspector-protocol tool |
| `Tools/Scripts/webkitpy/coverage_inspector.py` | driver page, payload reader, path verification, report model |
| `Tools/Scripts/webkitpy/coverage_inspector_unittest.py` | 46 tests over a captured payload, no build required |

`TextLineIndex` and `is_code_line` live in `coverage_javascript.py` and are imported by
`coverage_inspector.py` rather than copied, because the two rules that were expensive to get right —
a range claims a line only where it overlaps non-whitespace, and punctuation-only lines are not in
the denominator — have to mean the same thing in both reports.

```sh
cd Tools/Scripts && python3 -m pytest webkitpy/coverage_javascript_unittest.py \
    webkitpy/coverage_inspector_unittest.py -q
```

### Two environment traps, both of which cost an hour

**`DYLD_FRAMEWORK_PATH` is not enough for `WebKitTestRunner`.** The Network, GPU and WebContent
processes are XPC services and read `__XPC_DYLD_FRAMEWORK_PATH` and `__XPC_DYLD_LIBRARY_PATH`.
With only the unprefixed names set, the run dies immediately with
`WebKit framework version mismatch: 626.1.6 != 22626.1.3` followed by
`com.apple.WebKit.Networking.Development terminated`. `webkitpy.port.base.setup_environ_for_server`
copies exactly those four names, for exactly this reason.

**`LLVM_PROFILE_FILE` is not enough either, and the failure is silent in the wrong direction.**
Every binary in a coverage build has `/private/tmp/WebKitCoverage/<Product>_%8m%c.profraw` baked
into `__llvm_profile_filename`, and that directory is machine-global. One `WebKitTestRunner` run
with `LLVM_PROFILE_FILE` set and `__XPC_LLVM_PROFILE_FILE` unset left **20 `.profraw` files, 409 MB**,
in the shared directory in the middle of another coverage run — the child processes never saw the
override. Both tools set both names on every process they start; there is no code path that leaves
either unset.

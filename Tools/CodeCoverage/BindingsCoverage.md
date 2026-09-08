# IDL-level coverage for WebKit's web-platform API

> Which web-facing attributes and operations does the test suite never call?

```sh
Tools/Scripts/generate-bindings-coverage --cmake \
    --profdata=WebKitBuild/coverage-report/coverage.profdata
```

Nine seconds, no build, no test run. It reads a profile that already exists.

`README.md` in this directory covers line coverage of hand-written source. This is a different
question with a different answer shape, and the two are complementary.

---

## Why this exists

`generate-coverage-report` excludes everything under `DerivedSources` and counts it separately.
That exclusion is right for what that report is. Line coverage of `JSDocument.cpp` is not a number
anybody can act on: nobody reads `JSDocument.cpp`, and its line count moves whenever
`CodeGeneratorJS.pm`'s templates move, so the same tests produce a different percentage after a
generator refactor that changed no behaviour.

But the exclusion is large and it hides something real. On the CMake coverage build measured here:

| | |
|---|---|
| Generated `JS*.cpp` files | **1,820** (37 MB) |
| Instrumented lines of generated bindings | **380,290** |
| …executed by a full-suite run | **304,471 (80.06%)** |
| Distinct generated-binding function records | **62,171** |
| …executed | **51,361 (82.61%)** |

None of that appears in the report. And underneath it there *is* an actionable question: **which
IDL attributes and operations do our tests never call.** That question is about the web platform
rather than about C++, it is stable across bindings-generator changes, and each answer maps onto
one missing test.

So this tool takes the coverage data that already exists for the generated bindings and *lifts* it
back onto the IDL declarations that produced it.

---

## The measurement, on this checkout

Full-suite CMake coverage run, `WebKitBuild/coverage-report/coverage.profdata` (217,752,088 bytes,
merged from 111 raw profiles, 0 unreadable), against `WebKitBuild/cmake-mac/Coverage`. Per that
report's own provenance record: revision `972dcb0f5343`, 12 uncommitted files, port
`mac-goldengate-wk2`, Apple LLVM 21.0.0.

```
attributes       7301 declared,   5463 measurable,   4832 executed (88.45%),   631 never executed,  1838 unattributed
operations       2321 declared,   2264 measurable,   2114 executed (93.37%),   150 never executed,    57 unattributed
constructors      285 declared,    265 measurable,    241 executed (90.94%),    24 never executed,    20 unattributed
interfaces        942 interfaces,    144 with at least one never-executed member
setters            32 attributes were read and never written
overloads         273 per-overload bodies measurable,     37 never executed

7187 of 7992 measurable IDL declaration(s) executed (89.93%); 805 never executed;
1915 unattributed of 9907 declared.
```

**805 web-facing declarations that no test in the whole suite touched.** Where they are:

| Interface | Never executed | of measurable |
|---|---|---|
| `WorkerGlobalScope` | 107 | 232 |
| `PaintWorkletGlobalScope` | 76 | 80 |
| `PaintRenderingContext2D` | 48 | 51 |
| `AudioWorkletGlobalScope` | 42 | 47 |
| `ShadowRealmGlobalScope` | 40 | 40 |
| `DOMWindow` | 34 | 1,015 |
| `MathMLElement` | 30 | 111 |
| `InspectorFrontendHost` | 24 | 58 |
| `HTMLModelElement` | 22 | 22 |
| `DOMCSSNamespace` | 18 | 68 |

**37 interfaces have no measurable member that any test executed at all** —
`ShadowRealmGlobalScope` (40), `HTMLModelElement` (22), `GestureEvent` (12),
`DeprecationReportBody` (7), `MutationEvent` (6), `SVGFEComponentTransferElement` (6),
`NavigatorUAData` (5), `Database` (4), `BarcodeDetector` (3)…

For contrast, `Document` is at **239 of 240 measurable declarations, 99.58%**, with one gap
(`onwebkitfullscreenerror`, getter and setter). `Source/WebCore/svg` is at **534 of 608, 87.83%**,
with 74 gaps across 19 of its 98 interfaces.

### Read but never written

32 attributes had their getter executed and their setter never. The attribute counts as *executed*,
so whole-declaration coverage — and any line-based report — hides these entirely:

```
SVGElement.onpointerdown  SVGElement.onpointerup  SVGElement.onpointermove
SVGElement.onpointerenter SVGElement.onpointerleave  SVGElement.onpointercancel
SVGElement.ontransitionrun  SVGElement.ontransitionstart  SVGElement.ontransitioncancel
SVGSVGElement.zoomAndPan  SVGScriptElement.async  WebKitPoint.x  WebKitPoint.y
FontFace.width  HTMLInputElement.autofillAvailable  MediaSession.playlist  …
```

Every one of those is a one-line test.

---

## The mechanism

### The join key

`CodeGeneratorJS.pm` names every function it emits after the IDL declaration it came from:

| IDL | generated symbols |
|---|---|
| `attribute USVString URL;` | `jsDocument_URL`, `jsDocument_URLGetter` |
| `attribute DOMString xmlVersion;` | + `setJSDocument_xmlVersion`, `setJSDocument_xmlVersionSetter` |
| `Element? getElementById(DOMString id);` | `jsDocumentPrototypeFunction_getElementById`, `…Body` |
| `static Document parseHTMLUnsafe(…);` | `jsDocumentConstructorFunction_parseHTMLUnsafe`, `…Body` |
| `undefined assign(USVString url);` on `[LegacyUnforgeable] interface Location` | `jsLocationInstanceFunction_assign`, `…Body` |
| `constructor();` | `JSDOMConstructor<JSDocument>::construct` |
| `[LegacyFactoryFunction=Image(…)]` | `JSDOMLegacyFactoryFunction<JSHTMLImageElement>::construct` |

`coverage_bindings.py` ports the four subs that produce those names —
`GetAttributeGetterName`, `GetAttributeSetterName`, `GetFunctionName`,
`OperationShouldBeOnInstance` — plus `WK_lcfirst`/`WK_ucfirst` and
`MangleAttributeOrFunctionName` from `CodeGenerator.pm`. Each has a comment citing the sub it
mirrors, and every expected string in the unit tests was read out of the build's own
`DerivedSources` rather than derived from the generator, so a test cannot pass by being
self-consistent with a wrong rule.

### The coverage side

`llvm-cov export --format=lcov` over `WebCore.framework` with the existing profdata, scoped with
`--sources` to the build's `DerivedSources` plus `Source/WebCore/bindings/js`. That is 2,837 files
and 66,246 function records — a 2.9 MB gzipped trace and about four seconds, against hundreds of
megabytes and minutes for an unscoped export.

The `--ignore-filename-regex=/DerivedSources/` that `generate-coverage-report` applies is
deliberately *not* applied. That filter is the reason the generated bindings are invisible to that
report and the reason this tool exists.

Names in the trace are mangled, and `mangled_identifiers()` reads the length-prefixed identifiers
out of them rather than shelling out to a demangler for 66,246 records. It is a scan, not a
demangler: it can invent a token that is not an identifier, which is harmless because a token is
only ever used as a key to look up in a set of names computed from the IDL.

Both the trampoline and its inner body carry counters, and a role is executed if **either** is.
They agree in practice — `jsDocument_URL` and `jsDocument_URLGetter` both read 32,175 on this run —
but not by construction: an attribute reached through a DOMJIT fast path can have its body run
without the trampoline being entered.

### The IDL side

This does **not** re-implement WebIDL. It drives the checkout's own `IDLParser.pm` — the same
parser the generator uses — through a small embedded Perl program that writes one JSON record per
file. Over 1,824 IDL files that takes about six seconds, and it reports **0 parse failures** on
this checkout.

A hand-written Python WebIDL parser would have been a second source of truth that could silently
drift from the one the build uses. `applyPreprocessor` in `preprocessor.pm` no longer forks a
compiler, so the Perl route costs nothing.

An interface's members are flattened the way `preprocess-idls.pl` flattens them: its own, plus
every `partial interface` of the same name, plus the members of every mixin an `includes` statement
attaches to it. One `GlobalEventHandlers.onclick` declaration becomes an `onclick` accessor on each
interface that includes it, and the generator emits one function per interface, so those really are
separate measurements.

### Which IDL files

By default, the ones the build says it compiled: `DerivedSources/supplemental_dependency.tmp`,
which the generator writes with one line per main IDL file followed by its supplements — 1,701 main
files and 202 distinct supplements here. This is better than walking the source tree in two ways
that both matter:

* It is the set *this configuration* compiled, so an interface only another port builds does not
  arrive as a false gap.
* It names files from outside the OpenSource checkout — four from
  `Internal/WebKit/WebKitAdditions` on this build — and the derived
  `CSSStyleProperties+PropertyNames.idl`, which a walk of `Source/WebCore` cannot find at all.

`--all-idl` walks the tree instead, and the difference is measurable: 7,185 declarations against
9,907, because it misses the 1,334 CSS property attributes in the derived IDL and the Internal
additions, while adding two interfaces (`SVGTRefElement`, `EventListener`) that no configuration
here builds.

---

## The three states

A declaration is in exactly one of three states, and the third one is the point.

| | |
|---|---|
| **executed** | some test called it |
| **never executed** | the generated code has a counter for it and the counter is zero |
| **unattributed** | there is no counter to read, for a reason that is named |

An unattributed declaration is **not** counted as 0%. A declaration with no coverage mapping has no
denominator, so calling it 0% invents a number. This is the same rule
`generate-coverage-report` applies to a file this configuration never compiled — see
`coverage_build_inventory.REASON_ORDER` — and following that precedent was deliberate.

The reasons, with the measured counts:

| Reason | Count | What it means |
|---|---:|---|
| `shared-synthetic-accessor` | 1,566 | See below. |
| `conditional-off` | 314 | `[Conditional=X]` false against this build's feature defines, so the generated code is inside an `#if` that did not compile. Evaluated against the build's own `DerivedSources/platform-feature-defines.txt` (692 truthy macros), not against a guess. The detail names the expression: `TOUCH_EVENTS`, `WEBXR`, `DEVICE_ORIENTATION`, `WEBXR_LAYERS`. |
| `js-builtin` | 31 | `[JSBuiltin]` on the member or its interface — the Streams interfaces. The implementation is JavaScript, so there is no C++ counter. Their coverage is real and lives where a C++ profile cannot see it. |
| `not-exposed-on-this-global` | 4 | A mixin member carrying `[Exposed]` that the generator emitted nothing for on this interface. `NavigatorID.vendor` is `[Exposed=Window]`, so it reaches `Navigator` and not `WorkerNavigator` even though both include the mixin. |
| `interface-not-built` | 0 (2 with `--all-idl`) | No `JS<Interface>.cpp` in the build directory: `SVGTRefElement`, `EventListener`. |
| `not-in-an-exported-binary` | 0 (1,590 with `--include-test-support`) | The symbol was emitted and *nothing* in its translation unit has a record, so that file went into a binary this export did not read. Almost all of them are `JSInternalSettingsGenerated.cpp` and `JSInternals.cpp`, which go into `libWebCoreTestSupport.dylib`. A fact about the export, not about the tests. |
| `no-coverage-record` | **0** | The symbol was emitted, other functions in the same file have records, and this one does not. |
| `symbol-not-emitted` | **0** | Nothing anywhere under the name this module derives. Evidence that the name mapping is wrong. |

The last two being zero is the health check on the whole thing: **every one of the 9,907
declarations either joined to a symbol or landed in a bucket whose reason is a fact about the build
rather than a gap in the mapping.** They are kept as separate buckets precisely so a mapping
regression cannot hide inside an explained one. Getting there took four fixes that this measurement
found and nothing else would have:

* `[JSBuiltin]` is read from the interface as well as the member (`IsJSBuiltin` does), which was 31
  Streams declarations.
* `[LegacyUnforgeable]` is read from the interface as well as the member (`IsLegacyUnforgeable`
  does), which was `Location.assign`, `replace` and `reload` looked up on the prototype instead of
  the instance.
* `[LegacyFactoryFunction]` is a different class template from `constructor()`, which was
  `HTMLImageElement`, `HTMLAudioElement` and `HTMLOptionElement`.
* The generator's own fixtures under `bindings/scripts/test/` are dropped when records are indexed
  and not when interfaces are selected, because one of them is a `partial interface` supplementing
  a real interface (`AudioWorkletGlobalScope`).

### The big one: `[DelegateToSharedSyntheticAttribute]`

1,566 of the 1,915 unattributed declarations are CSS property IDL attributes. They share **four**
generated accessors, which dispatch on the property name at run time:

```
CSSStyleProperties.-apple-color-filter  ->  jsCSSStyleProperties_propertyValueForDashedIDLAttribute (count 33063)
CSSStyleProperties.-epub-hyphens        ->  jsCSSStyleProperties_propertyValueForDashedIDLAttribute (count 33063)
CSSStyleProperties.font                 ->  jsCSSStyleProperties_propertyValueForCamelCasedIDLAttribute
…                                            …ForWebKitCasedIDLAttribute, …ForEpubCasedIDLAttribute
```

The shared accessor has a counter and the individual property does not. Reading the shared count as
the property's would report **every CSS property as covered the moment any one of them was read**,
which is why the delegation is checked *before* any count is looked up rather than after. Getting
this order wrong is the single most consequential way this tool could lie, and it is what the first
implementation did.

Per-property coverage of those would have to be observed on the JS side. That is not a small extra:
it is a different instrumentation entirely.

---

## What the join cannot see

Named honestly, because the denominator has to say what it covers.

**Overloads are one declaration.** The generator emits one trampoline and one
`...OverloadDispatcher` per overloaded operation *name*, plus a numbered body per overload
(`..._drawImage1Body`, `2Body`, `3Body`), so the bodies **are** individually measurable — 273 of
them here, 37 never executed. But attributing body 3 to a specific IDL line would mean reproducing
the generator's flattening order across an interface, its partials and its mixins. So the bodies
are counted in the totals and not attributed to a line, and an operation counts as executed if any
overload ran.

**Unnamed members are not enumerated at all**, and are counted so that the denominator says so:
36 anonymous special operations (indexed and named getters, setters, deleters), 13 `iterable<>`,
5 `setlike<>`, 4 `maplike<>`, 2 async iterables. These declare no member name, and the generator
implements them with shared template machinery (`JSDOMIterator<JSFoo>`) rather than with a function
named after the declaration. `DOMTokenList`'s `iterable<DOMString>` produces no
`jsDOMTokenListPrototypeFunction_values` for a join to find. An anonymous `stringifier;` *is*
enumerated, as `toString`, because that is a name a test can call and the generator does emit it
under that name.

**Constants are not members here.** A constants-only interface — the WebGL extension objects,
`GPUBufferUsage` — contributes nothing to the denominator. The generator emits no accessor for a
constant, so there is nothing to measure. That is why "did the build generate a class for this
interface" is decided by the presence of `JS<Interface>.cpp` and not by whether it contains any
symbols.

**Inherited members are attributed to the interface that declares them.** `Element.id` is measured
on `Element` and not again on `HTMLDivElement`, because the generator emits one accessor. That is
the right denominator — the web platform has one `Element.id` — but it means the number for one
interface is about *its own* declarations, not about everything reachable from an instance of it.

**A `[Custom]` or `[CustomGetter]` member is measurable**, because the generator still names its
trampoline and the hand-written body is in `Source/WebCore/bindings/js/JS*Custom.cpp` — all 97 of
them are in that one directory, which is why exporting it alongside `DerivedSources` closes the
gap.

**`[EnabledBySetting]`, `[EnabledForWorld]` and `[SecureContext]` do not affect measurability.**
The symbol is generated and the property is installed conditionally at run time, so a zero counter
means the tests did not reach it — which may be because the setting was off in every test, and this
tool cannot tell those apart.

**A selective run gives a lower bound.** Pass `--test-scope-file` and every percentage is written
`≥`, and the never-executed list is described as a candidate list. Adding tests can only turn a
counter from zero to nonzero, so in a subset run an *executed* declaration is exact and a *never
executed* one is unknown. This is `coverage_scope`'s rule, reused rather than restated.

**`llvm-cov` reported `1,553 functions have mismatched data`** on this export, and `profile data may
be out of date - object is newer`. That is 1,553 functions across the whole of WebCore whose
counters were dropped, an upper bound on how much of this join is missing; the export in this scope
carries 66,246 records. The tool prints both warnings rather than swallowing them.

---

## Being handed the wrong trace

`--lcov` will read any lcov trace, and there is one specific trace that is both the most likely
thing to be passed and the worst: a report's own `coverage.lcov.gz`. It is exported with
`--ignore-filename-regex=/DerivedSources/`, so it contains none of the generated bindings — but it
*does* contain `Source/WebCore/bindings/js`, the hand-written custom bodies. So a check for "does
this trace mention any binding symbol at all" passes, and the report comes out looking healthy:

```
1,174 of 1,287 measurable declarations executed (91.22%); 8,620 of 9,907 unattributed
```

91.22% over 13% of the surface. So the check is on generated files specifically, it is exact rather
than a threshold, and it is an **error**:

```
ERROR: …/coverage.lcov.gz has coverage records for 15954 file(s) and not one of them is under
       DerivedSources, so it contains none of the generated bindings.
```

---

## Cost

| | |
|---|---|
| Whole tree, from `--profdata` | **8.8 s** |
| …from a cached `--lcov` | **5.3 s** |
| One interface, from a cached `--lcov` | **5.2 s** |
| Exported trace | 2.9 MB gzipped |

Of the 8.8 s: about 6 s is the IDL parse (1,824 files through `IDLParser.pm`), about 4 s the
`llvm-cov` export, and the rest the scan and the join. `--save-lcov=PATH` then `--lcov=PATH` skips
the export. Scoping to one interface does not make it faster, because the parse and the export are
both whole-tree; it makes the output readable.

Nothing here builds anything or runs a test.

---

## Using it

```sh
# The whole surface.
Tools/Scripts/generate-bindings-coverage --cmake --profdata=WebKitBuild/coverage-report/coverage.profdata

# Keep the export, then query it repeatedly.
Tools/Scripts/generate-bindings-coverage --cmake --profdata=… --save-lcov=/tmp/bindings.lcov.gz
Tools/Scripts/generate-bindings-coverage --cmake --lcov=/tmp/bindings.lcov.gz --interface=Document
Tools/Scripts/generate-bindings-coverage --cmake --lcov=/tmp/bindings.lcov.gz --idl-directory=Source/WebCore/svg

# Why is something unattributed?
Tools/Scripts/generate-bindings-coverage --cmake --lcov=… --explain=all --explain-limit=20

# One row per declaration and accessor, to diff between two runs or load into a spreadsheet.
Tools/Scripts/generate-bindings-coverage --cmake --lcov=… --tsv=/tmp/bindings.tsv
```

The Xcode build works the same way with `--no-coverage-build` or `--build-directory`; the derived
sources are looked for at `WebCore/DerivedSources` and at `DerivedSources/WebCore`, so both layouts
resolve.

---

## Verified against the build, versus designed only

**Verified.** Every symbol-naming rule, against the 25,063 symbol definitions in this build's
generated and custom binding sources: `symbol-not-emitted` and `no-coverage-record` are both zero
over 9,907 declarations, which is the strongest statement available that the mapping is right.
The end-to-end join, by hand: `jsDocument_onwebkitfullscreenerror` and its `…Getter` both carry
`FNDA:0` in the trace and the tool reports the declaration never executed;
`jsDocument_title` carries `FNDA:2862` and the tool reports it executed. The IDL parse, 0 failures
over 1,824 files. The wrong-trace guard, against the real `coverage.lcov.gz`. Every count in this
document.

**Designed but not measured here.** The Xcode build's `DerivedSources/WebCore` layout — the
alternative path is coded and this checkout has no Xcode coverage build to exercise it. The
selective-run path: `--test-scope-file` is wired through `coverage_scope`, whose own behaviour is
covered by its tests, and this profile is from a full-suite run so the `≥` output is only exercised
by a unit test. Ports other than mac.

**Known incomplete.** `not-exposed-on-this-global` is *inferred from the absence of a symbol* on a
mixin member carrying `[Exposed]`, because this module does not model the exposure graph — WebKit
spells the `Window` global's interface `DOMWindow`, so the names do not even match. A genuine
mapping error on an `[Exposed]` member would land in that bucket rather than in
`symbol-not-emitted`. It is 4 declarations, all of them real exposure exclusions
(`WorkerNavigator.vendor`, `.vendorSub`, `.productSub`, `WorkerGlobalScope.webkitIndexedDB`), but
the bucket is softer than the rest and this is the first place to look if the mapping ever drifts.

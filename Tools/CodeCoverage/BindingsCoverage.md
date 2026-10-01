# IDL-level coverage for WebKit's web-platform API

> Which web-facing attributes and operations does the test suite never call?

```sh
Tools/Scripts/generate-bindings-coverage --cmake \
    --profdata=WebKitBuild/coverage-report/coverage.profdata
```

It needs no build and no test run. It reads a profile that already exists, and it finishes in
seconds.

`README.md` covers line coverage of hand-written source. This is a different question with a
different kind of answer. The two complement each other.

## Why this exists

`generate-coverage-report` excludes everything under `DerivedSources` and counts it separately.
That is right for that report. Line coverage of `JSDocument.cpp` is not a number anyone can act on.
Nobody reads `JSDocument.cpp`, and its line count moves whenever `CodeGeneratorJS.pm`'s templates
move. The same tests then give a different percentage after a generator refactor that changed no
behaviour.

But the exclusion is large. A CMake coverage build has about 1,800 generated `JS*.cpp` files and
about 380,000 instrumented lines of them. None of it appears in the report.

Underneath, there *is* a question someone can act on: **which IDL attributes and operations do the
tests never call?** It is about the web platform, not about C++. It does not change when the
bindings generator changes. Each answer maps to one missing test.

So this tool takes the coverage data that already exists for the generated bindings and *lifts* it
back onto the IDL declarations that produced it.

## Reading the output

The report has four parts, in this order.

1. **`NEVER EXECUTED`.** Every declaration whose counter stayed at zero, grouped by interface. Each
   interface line reads `Interface  X of Y measurable member(s) executed (NN.NN%)`. The members
   follow. This is the actionable part.
2. **`READ BUT NEVER WRITTEN`.** An attribute whose getter ran and whose setter never did counts as
   *executed*, so whole-declaration coverage and any line-based report miss it. The tool measures
   attributes per accessor and lists these on their own. Each one needs a one-line test.
3. **`TOTALS`**, then **`UNATTRIBUTED`** with a named reason for each group (see "The three
   states"):

   ```
   attributes       N declared,   N measurable,   N executed (NN.NN%),   N never executed,  N unattributed
   operations       N declared,   N measurable,   N executed (NN.NN%),   N never executed,  N unattributed
   constructors     N declared,   N measurable,   N executed (NN.NN%),   N never executed,  N unattributed
   interfaces       N interfaces,    N with at least one never-executed member
   setters          N attributes were read and never written
   overloads        N per-overload bodies measurable,     N never executed (counted, and not attributed to a line)
   ```
4. **One summary line:** `N of N measurable IDL declaration(s) executed (NN.NN%); N never executed;
   N unattributed of N declared.`

## The mechanism

### The join key

`CodeGeneratorJS.pm` names every function it emits after the IDL declaration it came from:

| IDL | Generated symbols |
|---|---|
| `attribute USVString URL;` | `jsDocument_URL`, `jsDocument_URLGetter` |
| `attribute DOMString xmlVersion;` | the same, plus `setJSDocument_xmlVersion`, `setJSDocument_xmlVersionSetter` |
| `Element? getElementById(DOMString id);` | `jsDocumentPrototypeFunction_getElementById`, `…Body` |
| `static Document parseHTMLUnsafe(…);` | `jsDocumentConstructorFunction_parseHTMLUnsafe`, `…Body` |
| `undefined assign(USVString url);` on `[LegacyUnforgeable] interface Location` | `jsLocationInstanceFunction_assign`, `…Body` |
| `constructor();` | `JSDOMConstructor<JSDocument>::construct` |
| `[LegacyFactoryFunction=Image(…)]` | `JSDOMLegacyFactoryFunction<JSHTMLImageElement>::construct` |

`coverage_bindings.py` ports the four subs that produce these names: `GetAttributeGetterName`,
`GetAttributeSetterName`, `GetFunctionName` and `OperationShouldBeOnInstance`. It also ports
`WK_lcfirst`, `WK_ucfirst` and `MangleAttributeOrFunctionName` from `CodeGenerator.pm`. A comment on
each cites the sub it mirrors. Every expected string in the unit tests was read from the build's own
`DerivedSources`, not derived from the generator. A test therefore cannot pass by agreeing with a
wrong rule.

Four mapping rules are easy to get wrong:

- `[JSBuiltin]` is read from the interface as well as the member (`IsJSBuiltin` does the same).
- `[LegacyUnforgeable]` is read from the interface as well as the member (`IsLegacyUnforgeable`
  does the same). Otherwise `Location.assign` is looked up on the prototype and not the instance.
- `[LegacyFactoryFunction]` uses a different class template from `constructor()`.
- The generator's own fixtures under `bindings/scripts/test/` are dropped when records are indexed,
  not when interfaces are selected. One fixture is a `partial interface` that supplements a real
  interface (`AudioWorkletGlobalScope`).

### The coverage side

The tool runs `llvm-cov export --format=lcov` over `WebCore.framework` with the existing profdata.
`--sources` scopes the export to the build's `DerivedSources` plus `Source/WebCore/bindings/js`. That
is a trace of about 3 MB gzipped, and it takes seconds. An unscoped export is hundreds of
megabytes and takes minutes.

The `--ignore-filename-regex=/DerivedSources/` that `generate-coverage-report` applies is
deliberately *not* applied here. That filter is why the generated bindings are invisible in that
report.

Names in the trace are mangled. `mangled_identifiers()` reads the length-prefixed identifiers out of
them, and does not shell out to a demangler for tens of thousands of records. It is a scan, not a
demangler. It can invent a token that is not an identifier. That is harmless, because the tool uses
a token only as a key into a set of names computed from the IDL.

Both the trampoline and its inner body carry counters. A role counts as executed if **either** ran.
They agree in practice (`jsDocument_URL` and `jsDocument_URLGetter` read the same count). They do not
agree by construction: an attribute reached through a DOMJIT fast path can run its body without
entering the trampoline.

### The IDL side

The tool does **not** re-implement WebIDL. It drives the checkout's own `IDLParser.pm`, the parser
the generator uses, through a small embedded Perl program that writes one JSON record per file. That
takes seconds over about 1,800 IDL files, and it reports 0 parse failures. A hand-written
Python parser would be a second source of truth that could drift from the build's. The Perl route is
cheap, because `applyPreprocessor` in `preprocessor.pm` no longer forks a compiler.

The tool flattens an interface's members the way `preprocess-idls.pl` does: the interface's own
members, plus every `partial interface` of the same name, plus the members of every mixin that an
`includes` statement attaches. One `GlobalEventHandlers.onclick` declaration becomes an `onclick`
accessor on each interface that includes it. The generator emits one function per interface, so
those are separate measurements.

### Which IDL files

By default, the files the build says it compiled. `DerivedSources/supplemental_dependency.tmp` lists
one line per main IDL file, followed by its supplements. This beats a walk of the source tree in two
ways:

- It is the set *this configuration* compiled, so an interface that only another port builds does
  not show up as a false gap.
- It names files from outside the open-source checkout (some from an additional source tree that is
  not part of it) and the derived `CSSStyleProperties+PropertyNames.idl`. A walk of `Source/WebCore` cannot find
  that one.

`--all-idl` walks the tree instead. It misses the derived CSS property IDL and the files from
that additional source tree. It adds interfaces that no configuration here builds.

## The three states

A declaration is in exactly one of three states. The third is the point.

| State | Meaning |
|---|---|
| **executed** | some test called it |
| **never executed** | the generated code has a counter for it, and the counter is zero |
| **unattributed** | there is no counter to read, for a reason the tool names |

An unattributed declaration is **not** counted as 0%. A declaration with no coverage mapping has no
denominator, so calling it 0% would invent a number. `generate-coverage-report` follows the same
rule for a file that this configuration never compiled (see
`coverage_build_inventory.REASON_ORDER`).

The reasons (`--explain=REASON` lists the declarations behind one):

| Reason | What it means |
|---|---|
| `shared-synthetic-accessor` | The largest group. See below. |
| `conditional-off` | `[Conditional=X]` is false against this build's feature defines, so the generated code sits inside an `#if` that did not compile. The tool evaluates it against the build's own `DerivedSources/platform-feature-defines.txt`. The detail names the expression, for example `TOUCH_EVENTS` or `WEBXR`. |
| `js-builtin` | `[JSBuiltin]` on the member or its interface (the Streams interfaces). The implementation is JavaScript, so there is no C++ counter. Their coverage is real and lives where a C++ profile cannot see it. |
| `not-exposed-on-this-global` | A mixin member with `[Exposed]` for which the generator emitted nothing on this interface. `NavigatorID.vendor` is `[Exposed=Window]`, so it reaches `Navigator` and not `WorkerNavigator`, even though both include the mixin. |
| `interface-not-built` | No `JS<Interface>.cpp` in the build directory. Appears with `--all-idl`, for interfaces no configuration builds. |
| `not-in-an-exported-binary` | The symbol was emitted and *nothing* in its translation unit has a record, so that file went into a binary this export did not read. These are mostly `JSInternalSettingsGenerated.cpp` and `JSInternals.cpp`, which go into `libWebCoreTestSupport.dylib`. Appears with `--include-test-support`. A fact about the export, not about the tests. |
| `no-coverage-record` | **Should be zero.** The symbol was emitted, other functions in its file have records, and this one does not. |
| `symbol-not-emitted` | **Should be zero.** Nothing anywhere under the name this module derives. Evidence that the name mapping is wrong. |

The last two are the health check on the whole tool. Every declaration should either join to a
symbol or land in a bucket whose reason is a fact about the build. The tool keeps those two as
separate buckets so that a mapping regression cannot hide inside an explained one.

### The big one: `[DelegateToSharedSyntheticAttribute]`

Most unattributed declarations are CSS property IDL attributes. They share **four** generated
accessors, which dispatch on the property name at run time:

```
CSSStyleProperties.-apple-color-filter  ->  jsCSSStyleProperties_propertyValueForDashedIDLAttribute
CSSStyleProperties.-epub-hyphens        ->  jsCSSStyleProperties_propertyValueForDashedIDLAttribute
CSSStyleProperties.font                 ->  jsCSSStyleProperties_propertyValueForCamelCasedIDLAttribute
…                                            …ForWebKitCasedIDLAttribute, …ForEpubCasedIDLAttribute
```

The shared accessor has a counter. The individual property does not. Reading the shared count as the
property's would report **every CSS property as covered as soon as any one is read**. So the tool
checks the delegation *before* it looks up any count. Getting this order wrong is the most
consequential way this tool could lie.

Per-property coverage needs observation on the JS side, which is a different instrumentation
entirely (see N5 in `Followups.md`).

## What the join cannot see

The denominator has to say what it covers.

**Overloads are one declaration.** The generator emits one trampoline and one `...OverloadDispatcher`
per overloaded operation *name*, plus a numbered body per overload (`..._drawImage1Body`, `2Body`,
`3Body`). The bodies **are** individually measurable. But attributing body 3 to a specific IDL line
needs the generator's flattening order across an interface, its partials and its mixins. So the tool
counts the bodies in the totals and does not attribute them to a line. An operation counts as
executed if any overload ran.

**Unnamed members are not enumerated.** The tool counts them, so the denominator says so:
anonymous special operations (indexed and named getters, setters, deleters), `iterable<>`,
`setlike<>`, `maplike<>` and async iterables. These declare no member name. The generator implements
them with shared template machinery (`JSDOMIterator<JSFoo>`), not with a function named after the
declaration. `DOMTokenList`'s `iterable<DOMString>` produces no `jsDOMTokenListPrototypeFunction_values`
for a join to find. An anonymous `stringifier;` *is* enumerated, as `toString`, because a test can
call that name and the generator emits it.

**Constants are not members here.** A constants-only interface (the WebGL extension objects,
`GPUBufferUsage`) adds nothing to the denominator. The generator emits no accessor for a constant.
So "did the build generate a class for this interface" is decided by the presence of
`JS<Interface>.cpp`, not by whether it holds any symbols.

**Inherited members belong to the interface that declares them.** `Element.id` is measured on
`Element` and not again on `HTMLDivElement`, because the generator emits one accessor. That is the
right denominator, since the web platform has one `Element.id`. But an interface's number covers
*its own* declarations, not everything reachable from an instance.

**A `[Custom]` or `[CustomGetter]` member is measurable.** The generator still names its trampoline,
and the hand-written body is in `Source/WebCore/bindings/js/JS*Custom.cpp`. That directory is why the
export includes `Source/WebCore/bindings/js` next to `DerivedSources`.

**`[EnabledBySetting]`, `[EnabledForWorld]` and `[SecureContext]` do not affect measurability.** The
symbol is generated, and the property is installed conditionally at run time. A zero counter means
the tests did not reach it. That may be because the setting was off in every test, and the tool
cannot tell the two apart.

**A selective run gives a lower bound.** With `--test-scope-file`, every percentage prints as `≥`,
and the never-executed list is a candidate list. Adding tests can only turn a counter from zero to
nonzero. In a subset run, an *executed* declaration is exact and a *never executed* one is unknown.
This is `coverage_scope`'s rule, reused.

**`llvm-cov` may warn `N functions have mismatched data`** and `profile data may be out of date -
object is newer`. Those functions' counters were dropped. N is an upper bound on how much of this
join is missing. The tool prints both warnings and does not swallow them.

## Being handed the wrong trace

`--lcov` reads any lcov trace. The likeliest one to pass, and the worst, is a report's own
`coverage.lcov.gz`. The report exports it with `--ignore-filename-regex=/DerivedSources/`, so it
holds none of the generated bindings. It *does* hold `Source/WebCore/bindings/js`, the hand-written
custom bodies. A check for "does this trace mention any binding symbol" therefore passes, and the
output looks healthy: a high percentage over a small part of the surface, with most declarations
unattributed.

So the check is on generated files specifically. It is exact and not a threshold, and it is an
**error**:

```
ERROR: …/coverage.lcov.gz has coverage records for N file(s) and not one of them is under
       DerivedSources, so it contains none of the generated bindings.
```

## Cost

The tool builds nothing and runs no test. The cost is the IDL parse plus the `llvm-cov` export, and
the two overlap. `--save-lcov=PATH`, then `--lcov=PATH`, skips the export. Scoping to one interface
does not speed it up, because the parse and the export are both whole-tree. It makes the output
readable.

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

The Xcode build works the same way with `--no-coverage-build` or `--build-directory`. The tool
looks for the derived sources at `WebCore/DerivedSources` and at `DerivedSources/WebCore`, so both
layouts resolve.

## What is verified

**Verified.**

- Every symbol-naming rule, against all the symbol definitions in a build's generated and custom
  binding sources. `symbol-not-emitted` and `no-coverage-record` are both zero over every
  declaration. That is the strongest statement available that the mapping is right.
- The end-to-end join, by hand: `jsDocument_onwebkitfullscreenerror` and its `…Getter` both carry
  `FNDA:0` in the trace, and the tool reports the declaration never executed. `jsDocument_title`
  carries a nonzero count, and the tool reports it executed.
- The IDL parse: 0 failures.
- The wrong-trace guard, against a real `coverage.lcov.gz`.

**Designed but not measured.**

- The Xcode build's `DerivedSources/WebCore` layout. The alternative path is coded, and nobody has
  run it on an Xcode coverage build.
- The selective-run path. `--test-scope-file` goes through `coverage_scope`, which has its own
  tests. The only profile tried was a full-suite one, so `≥` output is exercised by a unit test
  only.
- Ports other than mac.

**Known soft spot.** `not-exposed-on-this-global` is *inferred from the absence of a symbol* on a
mixin member that carries `[Exposed]`. This module does not model the exposure graph, and WebKit
spells the `Window` global's interface `DOMWindow`, so the names do not even match. A genuine
mapping error on an `[Exposed]` member would land in this bucket and not in `symbol-not-emitted`.
Look here first if the mapping ever drifts.

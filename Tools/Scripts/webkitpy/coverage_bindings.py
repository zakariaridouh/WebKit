#!/usr/bin/env python3
#
# Copyright (C) 2026 Apple Inc. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY APPLE INC. AND ITS CONTRIBUTORS ``AS IS''
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL APPLE INC. OR ITS CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF
# THE POSSIBILITY OF SUCH DAMAGE.

"""Which web-facing IDL attributes and operations did no test ever call?

`generate-coverage-report` excludes everything under `DerivedSources` and counts it
separately, so the generated JS bindings -- 1,820 `JS*.cpp` files, 37 MB, in the CMake
coverage build measured here -- are invisible to it. That exclusion is right for what that
report is: line coverage of `JSDocument.cpp` is not a number anybody can act on, because
nobody reads `JSDocument.cpp`, and its line count moves whenever the generator's templates
move.

But the bindings are where WebKit's web-platform API surface lives, and there IS an actionable
question hiding in them: *which IDL attributes and operations do our tests never call*. That
question is about the web platform rather than about C++, it is stable across
bindings-generator changes, and each answer maps onto one missing test. This module answers it
by taking the coverage data that already exists for the generated bindings and LIFTING it back
onto the IDL declarations that produced it.

The join
--------

`CodeGeneratorJS.pm` names every function it emits after the IDL declaration it came from, and
those names are the join key:

    attribute USVString URL;      ->  jsDocument_URL          (JSC custom getter trampoline)
                                      jsDocument_URLGetter    (the body)
    Element? getElementById(...)  ->  jsDocumentPrototypeFunction_getElementById
                                      jsDocumentPrototypeFunction_getElementByIdBody

`attribute_getter_symbol`, `attribute_setter_symbol` and `operation_symbol` below are ports of
`GetAttributeGetterName`, `GetAttributeSetterName` and `GetFunctionName` from
`Source/WebCore/bindings/scripts/CodeGeneratorJS.pm`, and of `WK_lcfirst`/`WK_ucfirst` from
`CodeGenerator.pm`. `llvm-cov export --format=lcov` over `WebCore.framework` emits one
`FN:`/`FNDA:` pair per compiled function, with a mangled name, so the counts come from the
existing profdata with no test run at all.

The IDL side does not re-implement WebIDL: it drives the checkout's own `IDLParser.pm`, the
same parser the generator uses, through a small embedded Perl program. Over 1,957 IDL files
that takes about 6 seconds and, measured on this checkout, reports 0 parse failures. A
hand-written Python WebIDL parser would have been a second source of truth that could silently
drift from the one the build uses.

What the join can see
---------------------

  * Attributes, per accessor. A getter and a setter are separate generated functions with
    separate counters, so "read but never written" is visible, and it is a real gap: of the 2,007
    setters with a counter on this run, 141 were never called, and 32 of those belong to an
    attribute whose getter WAS called -- which no line-based or whole-declaration number can show.
  * Operations, including static ones (`jsFooConstructorFunction_bar`) and the ones a
    `[Global]` interface puts on the instance (`jsFooInstanceFunction_bar`).
  * Constructors, through `JSDOMConstructor<JSFoo>::construct`, which is a template
    instantiation rather than a `js`-prefixed function and so needs a rule of its own.
  * `[Custom]` and `[CustomGetter]` members, whose trampoline the generator still names but
    whose body is hand-written in `Source/WebCore/bindings/js/JS*Custom.cpp`. Those files are
    ordinary first-party sources, so exporting that directory alongside `DerivedSources` picks
    them up.

What it cannot see, and reports as a third state instead of as 0%
----------------------------------------------------------------

The existing report does this for files it could not measure -- see
`coverage_build_inventory.REASON_ORDER` -- and the precedent is deliberate: a declaration with
no coverage mapping has no denominator, so calling it 0% invents a number.

  * `[DelegateToSharedSyntheticAttribute]`. 1,566 CSS property IDL attributes share four
    generated accessors that dispatch on the property name at run time, so the generated code
    carries no per-property counter. The shared accessor's count is reported as context and the
    property itself is unattributed.
  * `[JSBuiltin]` members, implemented in JavaScript (the Streams interfaces). There is no C++
    counter to read.
  * `[Conditional=X]` where X is off in this build. Evaluated against the build's own
    `DerivedSources/platform-feature-defines.txt`, which is the exact truthy-macro list the
    build handed the generators, rather than against a guess.
  * An interface the build generated no class for at all.
  * A symbol the generator emitted that llvm-cov has no record for -- reported separately from
    a symbol that was never emitted, because the first is a build fact and the second is
    evidence that this module's name mapping is wrong.

Overloads
---------

The generator emits one trampoline and one `...OverloadDispatcher` per overloaded operation
NAME, plus a numbered body per overload (`..._drawImage1Body`, `2Body`, `3Body`). So the
bodies are individually measurable, but attributing body *k* to a specific IDL line would mean
reproducing the generator's flattening order across an interface and its partials and mixins,
which this module does not do. Overloads are therefore reported as ONE operation, executed if
any overload ran, and the never-executed overload bodies are counted in the totals without
being attributed to a line.

Not enumerated at all
---------------------

`iterable<>`, `maplike<>`, `setlike<>`, serializers, and the anonymous indexed/named
getter/setter/deleter special operations declare no member name, and the code the generator
emits for them is shared template machinery (`JSDOMIterator<JSFoo>`) rather than a function
named after a declaration. They are counted as declarations this tool does not enumerate, so
that the denominator says what it covers.
"""

import logging
import os
import re
import subprocess

from collections import namedtuple

from webkitpy.coverage_lcov import open_lcov

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------
# The generator's naming rules, ported.
#
# Each function below cites the sub it mirrors. They are ported rather than called because the
# originals are Perl subs inside a 450,000-byte code generator that needs a fully constructed
# generator object, an output directory and a supplemental-dependency map before it will say
# anything -- and because the mapping is the thing this module has to be able to unit-test.
# ---------------------------------------------------------------------------------------------

# CodeGenerator.pm's WK_lcfirst: lowercase the first letter, then undo it for the acronyms
# WebKit spells in full caps.
_LCFIRST_ACRONYMS = (
    ('dOM', 'dom'), ('hTML', 'html'), ('uRL', 'url'), ('jS', 'js'),
    ('xML', 'xml'), ('xSLT', 'xslt'), ('cSS', 'css'), ('rTC', 'rtc'),
)

# WK_lcfirst also renames two whole words, because `create` and `exclusive` collide with
# something else in the generated code.
_LCFIRST_WHOLE_WORDS = {'create': 'isCreate', 'exclusive': 'isExclusive'}


def wk_lcfirst(name):
    """CodeGenerator.pm's WK_lcfirst. 'JSDocument' -> 'jsDocument'.

    Verified against the build's own output rather than against a reading of the generator: over
    the 9,907 IDL declarations this checkout's build compiles, no declaration ends up with a name
    that neither the 25,063 symbol definitions in the generated and custom binding sources nor the
    profile has anything under. See BindingsCoverage.md, `symbol-not-emitted`.
    """
    if not name:
        return name
    result = name[0].lower() + name[1:]
    for prefix, replacement in _LCFIRST_ACRONYMS:
        if result.startswith(prefix):
            result = replacement + result[len(prefix):]
            break
    return _LCFIRST_WHOLE_WORDS.get(result, result)


# CodeGenerator.pm's WK_ucfirst. The `Xml` rule is anchored differently from the rest in the
# original -- /^Xml[^a-z]/ rather than /^Xml/ -- so `XmlHttp` becomes `XMLHttp` and `Xmlns`
# does not become `XMLns`. Kept exactly, because the difference is load-bearing for names like
# `xmlStandalone`.
_UCFIRST_PREFIX_RULES = (
    (re.compile(r'^Xml(?![a-z])'), 'XML'),
    (re.compile(r'^Svg'), 'SVG'),
    (re.compile(r'^Srgb'), 'SRGB'),
    (re.compile(r'^Cenc'), 'cenc'),
    (re.compile(r'^Cbcs'), 'cbcs'),
    (re.compile(r'^Pq$'), 'PQ'),
    (re.compile(r'^Hlg'), 'HLG'),
    (re.compile(r'^Ios'), 'iOS'),
    (re.compile(r'^Hls'), 'HLS'),
)


def wk_ucfirst(name):
    """CodeGenerator.pm's WK_ucfirst. 'JSDocument' -> 'JSDocument', 'xmlVersion' -> 'XMLVersion'."""
    if not name:
        return name
    result = name[0].upper() + name[1:]
    for pattern, replacement in _UCFIRST_PREFIX_RULES:
        replaced, count = pattern.subn(replacement, result)
        if count:
            return replaced
    return result


def mangle_member_name(name):
    """CodeGeneratorJS.pm's MangleAttributeOrFunctionName.

    A leading underscore separates the interface from the member, so that `jsDocument_open`
    cannot collide with a hypothetical interface `DocumentOpen`'s constructor getter. A name
    that already starts with an underscore keeps its own, and a hyphen (which WebIDL allows
    and C++ does not) becomes `_dash_`.
    """
    name = name.replace('-', '_dash_')
    return name if name.startswith('_') else '_' + name


def class_name_for_interface(interface_name):
    """'Document' -> 'JSDocument'. CodeGeneratorJS.pm builds it with plain concatenation."""
    return 'JS' + interface_name


# The inner function the generator emits beside each trampoline, keyed by accessor role. The
# trampoline is a JSC entry point and the inner function holds the body, and BOTH carry
# counters, so a role is executed if either of them is. They agree in practice --
# jsDocument_URL and jsDocument_URLGetter both read 32,175 on the run measured here -- but not
# by construction: an attribute reachable through a DOMJIT fast path or through
# getOwnPropertySlot can have its body run without the trampoline being entered.
INNER_SUFFIX_FOR_ROLE = {'get': 'Getter', 'set': 'Setter', 'call': 'Body'}

GET = 'get'
SET = 'set'
CALL = 'call'
CONSTRUCT = 'construct'

ROLE_LABELS = {GET: 'getter', SET: 'setter', CALL: 'call', CONSTRUCT: 'constructor'}


def attribute_getter_symbol(interface_name, attribute):
    """CodeGeneratorJS.pm's GetAttributeGetterName.

    `attribute` is an IDLAttribute record as `parse_idl_files` returns it. The
    `[DelegateToSharedSyntheticAttribute]` case returns the SHARED accessor's name, which is
    the honest answer: that is the function the build emitted, and it is emitted once for
    hundreds of declarations.
    """
    prefix = wk_lcfirst(class_name_for_interface(interface_name))
    shared = attribute['extended_attributes'].get('DelegateToSharedSyntheticAttribute')
    if isinstance(shared, str) and shared:
        return prefix + mangle_member_name(shared)
    if attribute['is_static']:
        return prefix + 'Constructor' + mangle_member_name(attribute['name'])
    return prefix + mangle_member_name(attribute['name'])


def attribute_setter_symbol(interface_name, attribute):
    """CodeGeneratorJS.pm's GetAttributeSetterName. 'xmlVersion' -> 'setJSDocument_xmlVersion'."""
    prefix = 'set' + wk_ucfirst(class_name_for_interface(interface_name))
    shared = attribute['extended_attributes'].get('DelegateToSharedSyntheticAttribute')
    if isinstance(shared, str) and shared:
        return prefix + mangle_member_name(shared)
    if attribute['is_static']:
        return prefix + 'Constructor' + mangle_member_name(attribute['name'])
    return prefix + mangle_member_name(attribute['name'])


# The two IDL operation names that are not identifiers, and what GetFunctionName substitutes.
_OPERATION_NAME_SUBSTITUTIONS = {
    '[Symbol.Iterator]': 'SymbolIterator',
    '[Symbol.asyncIterator]': 'AsyncSymbolIterator',
}


def operation_symbol(interface_name, operation, placement):
    """CodeGeneratorJS.pm's GetFunctionName, with the placement decided by the caller.

    placement is 'Prototype', 'Instance' or 'Constructor'. It is a parameter rather than being
    derived here because OperationShouldBeOnInstance() consults the interface's `[Global]`
    extended attribute and the member's `[LegacyUnforgeable]`, which the caller has and this
    function does not need twice.
    """
    name = operation['name']
    name = _OPERATION_NAME_SUBSTITUTIONS.get(name, name)
    prefix = wk_lcfirst(class_name_for_interface(interface_name))
    return prefix + placement + 'Function' + mangle_member_name(name)


def operation_placement(interface_extended_attributes, operation):
    """'Constructor', 'Instance' or 'Prototype', mirroring OperationShouldBeOnInstance().

    A `[Global]` interface -- Window, the worker global scopes -- puts every operation on the
    instance rather than on a prototype, and so does a `[LegacyUnforgeable]` one.
    `[LegacyUnforgeable]` is read from the INTERFACE as well as from the member, because
    IsLegacyUnforgeable() is: Location carries it at interface level, and reading only the
    member put its assign(), replace() and reload() on the prototype and lost all three.
    """
    if operation['is_static']:
        return 'Constructor'
    if 'Global' in interface_extended_attributes:
        return 'Instance'
    if ('LegacyUnforgeable' in operation['extended_attributes']
            or 'LegacyUnforgeable' in interface_extended_attributes):
        return 'Instance'
    return 'Prototype'


# CodeGeneratorJS.pm's IsJSBuiltin: the member or its interface asks for a JS builtin, and no
# custom C++ accessor overrides it. A builtin's body is JavaScript, so the generator emits no
# C++ function for it and there is no counter anywhere in the profile to read.
_CUSTOM_ACCESSOR_ATTRIBUTES = ('Custom', 'CustomGetter', 'CustomSetter')


def is_js_builtin(interface_extended_attributes, member_extended_attributes):
    if any(name in member_extended_attributes for name in _CUSTOM_ACCESSOR_ATTRIBUTES):
        return False
    return ('JSBuiltin' in member_extended_attributes
            or 'JSBuiltin' in interface_extended_attributes)


# The two class templates a constructor is emitted as. They are the only entry points in the
# bindings NOT named after the IDL member -- `JSDOMConstructor<JSDocument>::construct` -- so they
# need a rule of their own, and they have to be told apart: an interface can have both a
# `constructor()` and a `[LegacyFactoryFunction]`, and three on this build
# (HTMLImageElement, HTMLAudioElement, HTMLOptionElement) have only the factory function.
_CONSTRUCTOR_TEMPLATE = 'JSDOMConstructor'
_LEGACY_FACTORY_TEMPLATE = 'JSDOMLegacyFactoryFunction'
_CONSTRUCTOR_TEMPLATES = (_CONSTRUCTOR_TEMPLATE, _LEGACY_FACTORY_TEMPLATE)
_CONSTRUCTOR_METHOD = 'construct'


# ---------------------------------------------------------------------------------------------
# [Conditional=...]
# ---------------------------------------------------------------------------------------------

def read_feature_defines(path):
    """The truthy feature macros this build handed the generators, from the build directory.

    `DerivedSources/platform-feature-defines.txt` is produced by preprocessing wtf/Platform.h
    with the same flags a real compile sees -- see webkit_generate_platform_feature_defines_file
    in Source/cmake/WebKitMacros.cmake -- and holds one macro name per line, 692 of them on the
    configuration measured here. Reading it beats re-deriving the feature set, and beats reading
    cmakeconfig.h, which is only one of its inputs.
    """
    with open(path) as handle:
        return frozenset(line.strip() for line in handle if line.strip())


def conditional_is_enabled(expression, enabled_features):
    """Is `[Conditional=EXPRESSION]` true for a build whose truthy macros are enabled_features?

    The grammar is CodeGenerator.pm's GenerateConditionalStringFromAttributeValue: a disjunction
    of conjunctions of optionally negated feature names, `A&B|C`, with `ENABLE_` implied and
    asserted-absent by the generator. An empty expression is unconditional.
    """
    if not expression:
        return True
    for conjunction in expression.split('|'):
        terms = [term.strip() for term in conjunction.split('&') if term.strip()]
        if not terms:
            continue
        satisfied = True
        for term in terms:
            negated = term.startswith('!')
            feature = term[1:] if negated else term
            present = ('ENABLE_' + feature) in enabled_features
            if present == negated:
                satisfied = False
                break
        if satisfied:
            return True
    return False


def _conditional_expressions(*extended_attribute_maps):
    """Every `[Conditional]` that guards a declaration: the member's, its construct's, the
    interface's. All of them have to hold, because the generator nests the `#if`s."""
    expressions = []
    for attributes in extended_attribute_maps:
        value = (attributes or {}).get('Conditional')
        if isinstance(value, str) and value:
            expressions.append(value)
    return expressions


# ---------------------------------------------------------------------------------------------
# IDL parsing, through the checkout's own parser.
# ---------------------------------------------------------------------------------------------

# The Perl program that drives IDLParser.pm and writes one JSON object per IDL file. Embedded
# rather than shipped as a .pl file so that there is one artifact to keep in step with this
# module's expectations about the shape of the JSON.
#
# It writes to a FILE named by its first argument rather than to standard output, and that is not
# fussiness: IDLParser.pm's assert() prints its diagnostic to STDOUT, so a file it rejects put
# `$VAR1 = ' at .../IDLParser.pm line 258.` in the middle of the JSON stream and the whole batch
# failed to parse -- including the records of every file that was fine. With a separate output
# file, anything the parser prints is just a diagnostic.
#
# It writes one record per file even on failure, with an `error` key, so that a file the parser
# chokes on is reported rather than silently dropped from the denominator.
_IDL_DUMP_PROGRAM = r'''
use strict;
use warnings;
use lib $ARGV[0];
use IDLParser;
use JSON::PP;

my $outputPath = $ARGV[1];
open(my $out, '>', $outputPath) or die "Cannot write $outputPath: $!";

my $attributesFile = $ARGV[2];
my $idlAttributes;
{
    local $/;
    open(my $handle, '<', $attributesFile) or die "Cannot open $attributesFile: $!";
    my $text = <$handle>;
    close($handle);
    $idlAttributes = JSON::PP->new->utf8->decode($text)->{attributes};
}
my $json = JSON::PP->new->canonical->allow_unknown->allow_blessed;

sub dumpOperation {
    my ($operation) = @_;
    return {
        name => $operation->name,
        is_static => ($operation->isStatic ? 1 : 0),
        is_stringifier => ($operation->isStringifier ? 1 : 0),
        specials => $operation->specials,
        argument_count => scalar(@{$operation->arguments}),
        extended_attributes => $operation->extendedAttributes,
    };
}

sub dumpAttribute {
    my ($attribute) = @_;
    return {
        name => $attribute->name,
        is_static => ($attribute->isStatic ? 1 : 0),
        is_read_only => ($attribute->isReadOnly ? 1 : 0),
        type => ($attribute->type ? $attribute->type->name : undef),
        extended_attributes => $attribute->extendedAttributes,
    };
}

for my $index (3 .. $#ARGV) {
    my $file = $ARGV[$index];
    my %record = (file => $file);
    my $document = eval { IDLParser->new->Parse($file, '', $idlAttributes) };
    if (!$document) {
        $record{error} = "$@" || 'unknown parse failure';
        print $out $json->encode(\%record), "\n";
        next;
    }
    my @interfaces;
    for my $interface (@{$document->interfaces}) {
        push @interfaces, {
            name => $interface->type->name,
            parent => ($interface->parentType ? $interface->parentType->name : undef),
            is_partial => ($interface->isPartial ? 1 : 0),
            is_mixin => ($interface->isMixin ? 1 : 0),
            is_callback => ($interface->isCallback ? 1 : 0),
            is_namespace_object => ($interface->isNamespaceObject ? 1 : 0),
            has_iterable => ($interface->iterable ? 1 : 0),
            has_async_iterable => ($interface->asyncIterable ? 1 : 0),
            has_map_like => ($interface->mapLike ? 1 : 0),
            has_set_like => ($interface->setLike ? 1 : 0),
            constant_count => scalar(@{$interface->constants}),
            extended_attributes => $interface->extendedAttributes,
            attributes => [map { dumpAttribute($_) } @{$interface->attributes}],
            operations => [map { dumpOperation($_) } @{$interface->operations}],
            anonymous_operation_count => scalar(@{$interface->anonymousOperations}),
            constructors => [map { dumpOperation($_) } @{$interface->constructors}],
        };
    }
    for my $namespace (@{$document->namespaces}) {
        push @interfaces, {
            name => $namespace->name,
            parent => undef,
            is_partial => ($namespace->isPartial ? 1 : 0),
            is_mixin => 0, is_callback => 0, is_namespace_object => 1,
            has_iterable => 0, has_async_iterable => 0, has_map_like => 0, has_set_like => 0,
            constant_count => scalar(@{$namespace->constants}),
            extended_attributes => $namespace->extendedAttributes,
            attributes => [map { dumpAttribute($_) } @{$namespace->attributes}],
            operations => [map { dumpOperation($_) } @{$namespace->operations}],
            anonymous_operation_count => 0,
            constructors => [],
        };
    }
    $record{interfaces} = \@interfaces;
    $record{includes} = [map { { interface => $_->interfaceIdentifier,
                                 mixin => $_->mixinIdentifier } } @{$document->includes}];
    print $out $json->encode(\%record), "\n";
}
close($out) or die "Cannot close $outputPath: $!";
'''

# argv has a length limit (ARG_MAX, 1 MB on macOS) and 1,957 absolute IDL paths is about
# 120 KB, so one batch would fit today. Batching anyway, because the cost of getting this
# wrong is an exec failure on somebody else's checkout with a longer prefix, and Perl start-up
# is 40 ms against the 6 seconds the parse takes.
_IDL_BATCH_SIZE = 400


class IDLParseError(RuntimeError):
    pass


def bindings_scripts_directory(source_root):
    return os.path.join(source_root, 'Source', 'WebCore', 'bindings', 'scripts')


def parse_idl_files(paths, source_root, perl='perl'):
    """[record] for a list of .idl paths, one per file, in the order given.

    Each record is {'file': path, 'interfaces': [...], 'includes': [...]} or, for a file the
    parser rejected, {'file': path, 'error': message}. A caller that wants to know about parse
    failures reads the 'error' key; nothing is dropped silently.
    """
    import json
    import tempfile

    paths = list(paths)
    if not paths:
        return []
    scripts = bindings_scripts_directory(source_root)
    attributes_file = os.path.join(scripts, 'IDLAttributes.json')
    if not os.path.exists(attributes_file):
        raise IDLParseError('{} does not exist, so the IDL parser cannot validate extended '
                            'attributes'.format(attributes_file))
    records = []
    handle, output_path = tempfile.mkstemp(prefix='idl-declarations-', suffix='.jsonl')
    os.close(handle)
    try:
        for start in range(0, len(paths), _IDL_BATCH_SIZE):
            batch = paths[start:start + _IDL_BATCH_SIZE]
            command = [perl, '-e', _IDL_DUMP_PROGRAM, '--', scripts, output_path,
                       attributes_file, *batch]
            try:
                completed = subprocess.run(command, capture_output=True, text=True)
            except OSError as error:
                raise IDLParseError('Could not run {}: {}'.format(perl, error))
            if completed.returncode:
                raise IDLParseError('{} failed on {} IDL file(s) starting at {}: {}'.format(
                    perl, len(batch), batch[0],
                    (completed.stderr or completed.stdout).strip() or 'no diagnostic'))
            if completed.stdout.strip():
                # IDLParser.pm's assert() prints here. Worth surfacing at debug level, and
                # harmless now that it cannot reach the record stream.
                logger.debug('IDL parser diagnostics: %s', completed.stdout.strip()[:2000])
            with open(output_path) as output:
                for line in output:
                    if line.strip():
                        records.append(json.loads(line))
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass
    return records


def read_supplemental_dependencies(path):
    """(main IDL paths, supplemental IDL paths) from the build's supplemental_dependency.tmp.

    The generator writes one line per MAIN idl file -- the ones that produce a JS class --
    followed by the partials and mixins that supplement it. So this file is the build's own
    answer to "which IDL files did this configuration compile", which is stronger than
    re-deriving it from CMakeLists.txt: 1,701 main files and 202 distinct supplements on the
    build measured here, including four from the Internal checkout that a source-tree walk of
    OpenSource alone would never find.
    """
    main, supplemental = [], set()
    with open(path) as handle:
        for line in handle:
            parts = line.split()
            if not parts:
                continue
            main.append(parts[0])
            supplemental.update(parts[1:])
    return main, supplemental


# A single IDL attribute or operation, as declared, with where it was declared.
#
# interface: the name of the interface it ends up on, which for a mixin member is the INCLUDING
#            interface -- one `GlobalEventHandlers.onclick` declaration becomes an `onclick` on
#            every interface that includes it, and the generator emits one accessor per
#            interface, so those are genuinely separate measurements.
# declared_in: 'interface', 'partial interface' or 'mixin NAME', for the report.
# overload_count: how many same-named operations the flattened interface has. 1 for an
#            attribute and for a non-overloaded operation.
# placement: for an operation, which of Prototype/Instance/Constructor the generator puts it on,
#            since that is part of the symbol name. For a constructor, the class template that
#            carries it -- JSDOMConstructor for `constructor(...)` and JSDOMLegacyFactoryFunction
#            for `[LegacyFactoryFunction=Image(...)]`, which are different generated entities
#            with different counters. None for an attribute.
# is_js_builtin: IsJSBuiltin() for this member, which reads the interface as well as the member.
#            Folded in here because the reason a builtin cannot be measured is the same wherever
#            the extended attribute was written.
Declaration = namedtuple('Declaration', (
    'interface', 'kind', 'name', 'idl_path', 'declared_in', 'extended_attributes',
    'is_static', 'is_read_only', 'overload_count', 'conditionals', 'placement',
    'is_js_builtin'))

ATTRIBUTE = 'attribute'
OPERATION = 'operation'
CONSTRUCTOR = 'constructor'


class IDLModel:
    """Every interface the parsed IDL declares, flattened the way the generator flattens them.

    An interface's members are its own, plus every `partial interface` of the same name, plus
    the members of every mixin an `includes` statement attaches to it. That is what
    preprocess-idls.pl does before the generator runs, and it is why a declaration cannot be
    attributed to a symbol without knowing which interfaces it lands on.
    """

    def __init__(self):
        self.interfaces = {}      # name -> [interface record], the main one first
        self.mixins = {}          # name -> [mixin record]
        self.includes = {}        # interface name -> [mixin name]
        self.parse_errors = []    # [(path, message)]

    @classmethod
    def from_records(cls, records, excluded_path_fragments=None):
        """Index parsed IDL records, dropping the files no build reads.

        The drop happens here rather than when interfaces are selected, because a fixture file
        can be a `partial interface` supplementing a REAL interface: the generator's own
        bindings/scripts/test/AudioWorkletGlobalScopeConstructors.idl declares
        `partial interface AudioWorkletGlobalScope`, and filtering only on the main declaration's
        path let its ExposedStar attribute into a real interface's member list, where it was the
        one and only declaration this module could find no symbol for.
        """
        if excluded_path_fragments is None:
            excluded_path_fragments = (GENERATOR_FIXTURE_FRAGMENT,)
        model = cls()
        for record in records:
            if any(fragment in record['file'] for fragment in excluded_path_fragments):
                continue
            if record.get('error'):
                model.parse_errors.append((record['file'], record['error'].strip()))
                continue
            for interface in record.get('interfaces') or []:
                interface = dict(interface, idl_path=record['file'])
                if interface['is_mixin']:
                    model.mixins.setdefault(interface['name'], []).append(interface)
                else:
                    bucket = model.interfaces.setdefault(interface['name'], [])
                    # The non-partial declaration goes first, so that main_declaration() does
                    # not depend on the order the files were parsed in. There can be more than
                    # one non-partial record for a name -- DOMWindow.idl declares DOMWindow
                    # twice, once for the interface and once for
                    # [StandaloneConstructorAttributes] -- and the first is the real one.
                    if interface['is_partial']:
                        bucket.append(interface)
                    else:
                        insert_at = sum(1 for entry in bucket if not entry['is_partial'])
                        bucket.insert(insert_at, interface)
            for statement in record.get('includes') or []:
                model.includes.setdefault(statement['interface'], []).append(statement['mixin'])
        return model

    def main_declaration(self, interface_name):
        """The non-partial declaration of an interface, or None if only partials were parsed."""
        for interface in self.interfaces.get(interface_name, ()):
            if not interface['is_partial']:
                return interface
        return None

    def constructs_for(self, interface_name):
        """[(declared_in, record)] for everything that contributes members to an interface."""
        constructs = []
        for interface in self.interfaces.get(interface_name, ()):
            constructs.append(('partial interface' if interface['is_partial'] else 'interface',
                               interface))
        for mixin_name in self.includes.get(interface_name, ()):
            for mixin in self.mixins.get(mixin_name, ()):
                label = 'mixin {}'.format(mixin_name)
                if mixin['is_partial']:
                    label = 'partial mixin {}'.format(mixin_name)
                constructs.append((label, mixin))
        return constructs

    def declarations_for(self, interface_name):
        """Every named attribute and operation on an interface, flattened, plus its constructors.

        Overloads collapse into one Declaration per name, carrying the count. See the module
        docstring: the per-overload bodies are measurable but not attributable to a line
        without reproducing the generator's flattening order.
        """
        main = self.main_declaration(interface_name)
        if main is None:
            return []
        interface_attributes = main['extended_attributes']
        interface_conditionals = _conditional_expressions(interface_attributes)
        declarations = []
        seen_attributes = set()
        operations = {}   # name -> [(declared_in, construct, operation)]
        for declared_in, construct in self.constructs_for(interface_name):
            construct_conditionals = _conditional_expressions(construct['extended_attributes'])
            for attribute in construct['attributes']:
                if not attribute['name'] or attribute['name'] in seen_attributes:
                    continue
                seen_attributes.add(attribute['name'])
                declarations.append(Declaration(
                    interface=interface_name, kind=ATTRIBUTE, name=attribute['name'],
                    idl_path=construct['idl_path'], declared_in=declared_in,
                    extended_attributes=attribute['extended_attributes'],
                    is_static=bool(attribute['is_static']),
                    is_read_only=bool(attribute['is_read_only']), overload_count=1,
                    conditionals=tuple(interface_conditionals + construct_conditionals
                                       + _conditional_expressions(
                                           attribute['extended_attributes'])),
                    placement=None,
                    is_js_builtin=is_js_builtin(interface_attributes,
                                                attribute['extended_attributes'])))
            for operation in construct['operations']:
                name = operation['name']
                if not name and operation['is_stringifier']:
                    # An anonymous `stringifier;` is emitted as toString(), which is a name the
                    # web platform can see and a test can call.
                    name = 'toString'
                if not name:
                    continue
                operations.setdefault(name, []).append((declared_in, construct, operation))
        for name, entries in operations.items():
            declared_in, construct, operation = entries[0]
            declarations.append(Declaration(
                interface=interface_name, kind=OPERATION, name=name,
                idl_path=construct['idl_path'], declared_in=declared_in,
                extended_attributes=operation['extended_attributes'],
                is_static=bool(operation['is_static']), is_read_only=False,
                overload_count=len(entries),
                conditionals=tuple(interface_conditionals
                                   + _conditional_expressions(
                                       construct['extended_attributes'],
                                       operation['extended_attributes'])),
                placement=operation_placement(interface_attributes, operation),
                is_js_builtin=is_js_builtin(interface_attributes,
                                            operation['extended_attributes'])))
        declarations.extend(self._constructor_declarations(
            interface_name, main, interface_conditionals))
        return declarations

    def _constructor_declarations(self, interface_name, main, interface_conditionals):
        """One declaration per constructor entity, which is not the same as per `constructor()`.

        `constructor(...)` and `[LegacyFactoryFunction=Image(...)]` are both in IDLParser's
        `constructors` list and they are emitted as two DIFFERENT class templates --
        JSDOMConstructor<JSFoo>::construct and JSDOMLegacyFactoryFunction<JSFoo>::construct --
        with counters of their own. Reporting them as one declaration would let `new Image()`
        being tested stand in for `new HTMLImageElement()` not being, and on this build there
        are three interfaces (HTMLImageElement, HTMLAudioElement, HTMLOptionElement) where the
        factory function is the only one of the two that exists.

        Overloaded `constructor()` declarations are one declaration, for the same reason
        overloaded operations are.
        """
        by_template = {}
        for constructor in main['constructors']:
            factory = constructor['extended_attributes'].get('LegacyFactoryFunction')
            if isinstance(factory, str) and factory:
                key = (_LEGACY_FACTORY_TEMPLATE, factory)
            else:
                key = (_CONSTRUCTOR_TEMPLATE, 'constructor')
            by_template.setdefault(key, []).append(constructor)
        declarations = []
        for (template, name), constructors in sorted(by_template.items()):
            declarations.append(Declaration(
                interface=interface_name, kind=CONSTRUCTOR, name=name,
                idl_path=main['idl_path'], declared_in='interface',
                extended_attributes=constructors[0]['extended_attributes'],
                is_static=False, is_read_only=False, overload_count=len(constructors),
                conditionals=tuple(interface_conditionals), placement=template,
                is_js_builtin=is_js_builtin(main['extended_attributes'],
                                            constructors[0]['extended_attributes'])))
        return declarations

    def unenumerated_declarations_for(self, interface_name):
        """How many members of an interface this tool does not enumerate, and why.

        `iterable<>`, `maplike<>`, `setlike<>` and the anonymous special operations declare no
        name, and the generator implements them with shared template machinery rather than with
        a function named after the declaration. Counted so that the denominator says what it
        covers.
        """
        counts = {}
        for _, construct in self.constructs_for(interface_name):
            for key, flag in (('iterable', 'has_iterable'), ('async iterable',
                                                             'has_async_iterable'),
                              ('maplike', 'has_map_like'), ('setlike', 'has_set_like')):
                if construct.get(flag):
                    counts[key] = counts.get(key, 0) + 1
            anonymous = construct.get('anonymous_operation_count') or 0
            if anonymous:
                counts['anonymous special operation'] = (
                    counts.get('anonymous special operation', 0) + anonymous)
        return counts

    def measurable_interface_names(self, include_test_support=False):
        """The interfaces worth reporting on: real, non-callback, from a real IDL file.

        Callback interfaces have no wrapper with accessors on it. The generator's own fixtures
        are already gone -- from_records() drops them, because a fixture can supplement a real
        interface -- and coverage_build_inventory calls that class of file a 'fixture' and
        excludes it for the same reason.
        """
        names = []
        for name in sorted(self.interfaces):
            main = self.main_declaration(name)
            if main is None or main['is_callback']:
                continue
            if not include_test_support and is_test_support_idl(main['idl_path']):
                continue
            names.append(name)
        return names


# The generator's own expected-output fixtures. Not a build input anywhere, and they declare
# interfaces (TestObj, TestGlobalObject) whose names would otherwise look like real gaps.
GENERATOR_FIXTURE_FRAGMENT = os.path.join('bindings', 'scripts', 'test') + os.sep

# Test-support IDL: Internals and friends, compiled into WebCoreTestSupport rather than into
# the web-facing surface. Excluded by default for the same reason generate-coverage-report
# excludes test-support binaries by default.
_TEST_SUPPORT_FRAGMENTS = (
    os.path.join('WebCore', 'testing') + os.sep,
    os.path.join('WebCore', 'workers', 'service', 'server') + os.sep,
)

# InternalSettingsGenerated.idl is generated from Settings.yaml INTO the build's DerivedSources,
# so it is not under WebCore/testing and no path fragment finds it -- but it is test scaffolding
# all the same, it goes into libWebCoreTestSupport.dylib, and it is large: its 710 setter
# operations were the single biggest unattributed bucket before it was excluded, all of them for
# a reason that describes the export rather than the tests.
_TEST_SUPPORT_BASENAMES = ('InternalSettingsGenerated.idl',)


def is_test_support_idl(path):
    return (any(fragment in path for fragment in _TEST_SUPPORT_FRAGMENTS)
            or os.path.basename(path) in _TEST_SUPPORT_BASENAMES)


# ---------------------------------------------------------------------------------------------
# The build side: what the generator emitted, and what the profile says about it.
# ---------------------------------------------------------------------------------------------

# The path component that identifies generated code, on both build systems: the CMake build puts
# it in WebCore/DerivedSources and the Xcode build in DerivedSources/WebCore. It is the string
# generate-coverage-report's default --ignore-filename-regex uses, which is exactly why a trace
# from that tool has nothing this module can read.
DERIVED_SOURCES_MARKER = 'DerivedSources'

# Definition sites in the generated bindings. Two shapes: the JSC entry-point macros, and the
# `static inline` inner function that holds the body. Matching definitions rather than every
# occurrence of an identifier is what makes "the generator never emitted this" a real finding
# instead of being defeated by a property-table reference or a comment.
_GENERATED_DEFINITION = re.compile(
    r'JSC_DEFINE_(?:CUSTOM_GETTER|CUSTOM_SETTER|HOST_FUNCTION|JIT_OPERATION(?:_WITHOUT_WTF_INTERNAL)?)'
    r'\((?P<macro>(?:js|setJS)\w+)'
    r'|^static inline [^(]*?\b(?P<inline>(?:js|setJS)\w+)\s*\(',
    re.MULTILINE)


def scan_generated_symbols(directories):
    """{symbol: the basename of the file that defines it} over the binding sources.

    Two directories matter: the build's DerivedSources, and Source/WebCore/bindings/js, where
    the hand-written body of a `[Custom]` or `[CustomGetter]` member lives while the generator
    still names its trampoline. All 97 JS*Custom.cpp files in the checkout are in the second
    one, so those two cover the whole surface.

    This is what separates 'the generator emitted nothing for this declaration' from 'it
    emitted something llvm-cov has no record of'. The first is a build fact; the second is
    evidence that the name mapping in this module is wrong, and conflating them would hide a
    regression in the mapping inside a bucket that looks explained.

    The value is a BASENAME rather than a full path, because it is compared against the file
    names in an lcov trace and the two spellings of one file need not be equal -- a build
    directory reached through a symlink, or a trace exported with -path-equivalence, gives the
    same file two absolute paths. Basenames are unambiguous across these two directories:
    everything here is JS<Interface>.cpp or JS<Interface>Custom.cpp.
    """
    symbols = {}
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                if not filename.endswith(('.cpp', '.mm')):
                    continue
                path = os.path.join(root, filename)
                try:
                    with open(path, 'r', errors='replace') as handle:
                        text = handle.read()
                except OSError:
                    continue
                for match in _GENERATED_DEFINITION.finditer(text):
                    symbol = match.group('macro') or match.group('inline')
                    symbols.setdefault(symbol, filename)
    return symbols


def mangled_identifiers(name):
    """The length-prefixed identifiers inside an Itanium-mangled name.

    `_ZN7WebCoreL14jsDocument_URLEPN3JSC14JSGlobalObjectExNS0_12PropertyNameE` yields
    ['WebCore', 'jsDocument_URL', 'JSC', 'JSGlobalObject', 'PropertyName'].

    A scan rather than a demangler: llvm-cov emits mangled names, `--demangler` would mean a
    second subprocess over 66,246 records, and every name this module looks for is a
    source-level identifier that Itanium mangling encodes verbatim behind its own length. The
    scan can invent a token that is not really an identifier -- it cannot tell a length prefix
    from a template's integer argument -- which is harmless here because a token is only ever
    used as a key to look up in a set of names computed from the IDL.
    """
    identifiers = []
    index, end = 0, len(name)
    while index < end:
        character = name[index]
        if character.isdigit() and character != '0':
            digits_end = index
            while digits_end < end and name[digits_end].isdigit():
                digits_end += 1
            length = int(name[index:digits_end])
            if digits_end + length <= end:
                identifiers.append(name[digits_end:digits_end + length])
                index = digits_end + length
                continue
        index += 1
    return identifiers


# The class template every generated constructor is an instantiation of is declared near the
# naming rules, beside the rest of the generator's vocabulary.


class SymbolCoverage:
    """Execution counts for the binding symbols in an lcov trace, keyed by source-level name.

    Counts are the maximum over every record carrying the name, not the sum: a name appears
    once per translation unit that compiled it, unified-source bundles put many generated files
    in one TU, and the question here is only whether something ran.
    """

    def __init__(self):
        self.counts = {}               # symbol -> execution count
        self.constructor_counts = {}   # (template, 'JSDocument') -> execution count
        self.records = 0               # FN: records read, for the health line
        self.source_files = set()      # basenames of the files the trace has records for
        # How many of those were generated. A trace exported with the report's usual
        # --ignore-filename-regex=/DerivedSources/ has records for the hand-written custom
        # bindings and NONE for the generated ones, which is not a small degradation: it turns
        # 87% of the declarations unattributed while still looking like a report. Counted
        # exactly rather than inferred from a threshold.
        self.derived_sources_files = 0

    def count_for(self, symbol):
        return self.counts.get(symbol)

    def constructor_count_for(self, template, class_name):
        return self.constructor_counts.get((template, class_name))

    def has_records_for_file(self, basename):
        """Did any function in this file get a coverage record?

        The discriminator between 'this symbol has no record' and 'nothing in its translation
        unit has one'. The second means the file was compiled into a binary this export did not
        read: with --include-test-support on the CMake build that is 1,590 declarations, almost
        all of them in JSInternalSettingsGenerated.cpp and JSInternals.cpp, which go into
        libWebCoreTestSupport.dylib and not into WebCore.framework. That is a fact about the
        export rather than about the tests, and it must not be confused with a mapping error.
        """
        return basename in self.source_files

    def _note(self, mangled, count):
        identifiers = mangled_identifiers(mangled)
        for index, identifier in enumerate(identifiers):
            if identifier.startswith('js') or identifier.startswith('setJS'):
                if count > self.counts.get(identifier, -1):
                    self.counts[identifier] = count
            elif identifier in _CONSTRUCTOR_TEMPLATES and index + 2 < len(identifiers):
                # JSDOMConstructor<JSFoo>::construct -- the class is the next identifier and
                # the method the one after it. Checking the method name matters:
                # prototypeForStructure and initializeProperties are instantiated for every
                # interface, constructible or not, so keying on the template alone would call
                # every interface's constructor covered.
                if _CONSTRUCTOR_METHOD in identifiers[index + 1:index + 3]:
                    key = (identifier, identifiers[index + 1])
                    if count > self.constructor_counts.get(key, -1):
                        self.constructor_counts[key] = count

    @classmethod
    def from_lcov(cls, path):
        """Read FN:/FNDA: records and nothing else.

        parse_lcov() would build a per-line map for each of the 2,837 files in this scope,
        which this has no use for. An FN: with no FNDA: is a compiled function the profile has
        no data for; llvm-cov emits both for everything it has a mapping for, so the FN:-only
        case seeds a zero and the FNDA: overwrites it.
        """
        coverage = cls()
        with open_lcov(path) as handle:
            for line in handle:
                if line.startswith('SF:'):
                    path = line[3:].rstrip('\n')
                    coverage.source_files.add(os.path.basename(path))
                    if DERIVED_SOURCES_MARKER in path:
                        coverage.derived_sources_files += 1
                elif line.startswith('FN:'):
                    _, _, name = line[3:].rstrip('\n').partition(',')
                    if name:
                        coverage.records += 1
                        coverage._note(name.rsplit(':', 1)[-1], 0)
                elif line.startswith('FNDA:'):
                    count, _, name = line[5:].rstrip('\n').partition(',')
                    if name:
                        try:
                            coverage._note(name.rsplit(':', 1)[-1], int(count))
                        except ValueError:
                            pass
        return coverage


# ---------------------------------------------------------------------------------------------
# The join.
# ---------------------------------------------------------------------------------------------

EXECUTED = 'executed'
NEVER_EXECUTED = 'never-executed'
UNATTRIBUTED = 'unattributed'

# Why a declaration could not be measured. Ordered most to least numerous on the run measured
# here, which is the order they are reported in -- the same convention as
# coverage_build_inventory.REASON_ORDER.
REASON_ORDER = (
    'shared-synthetic-accessor',
    'interface-not-built',
    'conditional-off',
    'js-builtin',
    'not-exposed-on-this-global',
    'not-in-an-exported-binary',
    'no-coverage-record',
    'symbol-not-emitted',
)

REASON_LABELS = {
    'shared-synthetic-accessor': 'Shares one generated accessor with many declarations',
    'interface-not-built': 'The build generated no class for this interface',
    'conditional-off': 'Guarded by a feature flag this build has off',
    'js-builtin': 'Implemented by a JavaScript builtin',
    'not-exposed-on-this-global': 'A mixin member [Exposed] elsewhere, not on this interface',
    'not-in-an-exported-binary': 'In a translation unit no exported binary contains',
    'no-coverage-record': 'Emitted, but llvm-cov has no record for it',
    'symbol-not-emitted': 'No generated symbol under the name the generator would use',
}

REASON_EXPLANATIONS = {
    'shared-synthetic-accessor': (
        '[DelegateToSharedSyntheticAttribute] points the declaration at one accessor that '
        'hundreds of declarations share, which dispatches on the property name at run time. '
        'The shared accessor has a counter and the individual property does not, so its '
        'coverage is unknowable from here -- and reading the shared count as the property\'s '
        'would call every CSS property covered the moment any one of them was. The detail names '
        'the accessor and gives its count. Per-property coverage would have to be observed on '
        'the JS side.'),
    'interface-not-built': (
        'No JS<Interface>.cpp in the build directory, so nothing in this configuration exposes '
        'the interface and there is no code for a test to reach. Usually an interface whose '
        'whole IDL file this port does not compile.'),
    'conditional-off': (
        '[Conditional=...] evaluates false against this build\'s feature defines, so the '
        'generated code is inside an #if that did not compile. Turning the flag on in the '
        'coverage configuration would make it measurable. The detail names the expression.'),
    'js-builtin': (
        '[JSBuiltin], on the member or on its interface -- the Streams interfaces -- so the '
        'implementation is JavaScript, the generator emits no C++ body, and there is no counter '
        'to read. Their coverage is real and lives on the JS side, where a C++ profile cannot '
        'see it.'),
    'not-exposed-on-this-global': (
        'A mixin member carrying [Exposed], for which the generator emitted nothing on this '
        'interface: NavigatorID.vendor is [Exposed=Window], so it reaches Navigator and not '
        'WorkerNavigator even though both include the mixin. This module does not model the '
        'exposure graph -- WebKit spells the Window global\'s interface DOMWindow, so the '
        'names do not even match -- so it infers the exclusion from the absence, and a genuine '
        'mapping error on an [Exposed] member would land here rather than in '
        'symbol-not-emitted.'),
    'not-in-an-exported-binary': (
        'The generator emitted the symbol, and nothing in its translation unit has a coverage '
        'record, so that file was compiled into a binary this export did not read. Almost all of '
        'these are JSInternalSettingsGenerated.cpp and JSInternals.cpp, which go into '
        'libWebCoreTestSupport.dylib rather than WebCore.framework. A fact about the export, '
        'not about the tests.'),
    'no-coverage-record': (
        'The generator emitted the symbol, other functions in the same file DO have records, '
        'and this one does not. Either the function was emitted in a form the coverage mapping '
        'does not name, or llvm-cov dropped its counters as mismatched. Reported at all because '
        'it would otherwise be indistinguishable from a mapping bug.'),
    'symbol-not-emitted': (
        'Neither the generated sources nor the profile has anything under the name this '
        'module derives for the declaration. That is evidence the name mapping is wrong -- a '
        'generator rule this module does not mirror -- and is deliberately its own bucket so '
        'that it cannot hide inside an explained one.'),
}


# One accessor or entry point of a declaration.
# state: EXECUTED, NEVER_EXECUTED or UNATTRIBUTED.
# count: the execution count when measured, else None.
# symbol: the name that was matched, or the primary candidate when nothing matched.
Accessor = namedtuple('Accessor', ('role', 'state', 'count', 'symbol', 'reason', 'detail'))

# A declaration and everything known about it.
# status: EXECUTED if any accessor ran, NEVER_EXECUTED if at least one was measurable and none
#         ran, UNATTRIBUTED if none was measurable.
MemberResult = namedtuple('MemberResult', (
    'declaration', 'status', 'accessors', 'reason', 'detail', 'never_executed_overloads',
    'overload_bodies'))


def _setter_is_expected(declaration):
    """Does the generator emit a setter for this attribute?

    A writable attribute always has one. A readonly attribute has one only for [Replaceable]
    (assignment shadows the property) and [PutForwards] (assignment forwards to another
    object). Getting this wrong in the generous direction would be harmless -- an unexpected
    setter symbol is still measured if it exists -- but getting it wrong in the strict
    direction would report a real never-called setter as absent.
    """
    if not declaration.is_read_only:
        return True
    return bool({'Replaceable', 'PutForwards'} & set(declaration.extended_attributes))


def candidate_symbols(declaration, role):
    """Every generated name that could carry this declaration's counter for one role.

    The trampoline and its inner body are both included, and for an operation so are the
    overload dispatcher and the numbered overload bodies. Executed-if-any, because they are
    alternative entry points into the same declaration rather than independent code.
    """
    attribute = {'name': declaration.name, 'is_static': declaration.is_static,
                 'extended_attributes': declaration.extended_attributes}
    if role == GET:
        primary = attribute_getter_symbol(declaration.interface, attribute)
        # A constructor-typed attribute -- `attribute HTMLImageElementConstructor Image` on a
        # global -- has `Constructor` appended after the member name rather than before it.
        return [primary, primary + INNER_SUFFIX_FOR_ROLE[GET],
                primary + 'Constructor', primary + 'Constructor' + INNER_SUFFIX_FOR_ROLE[GET]]
    if role == SET:
        primary = attribute_setter_symbol(declaration.interface, attribute)
        return [primary, primary + INNER_SUFFIX_FOR_ROLE[SET]]
    if role == CALL:
        operation = {'name': declaration.name, 'is_static': declaration.is_static,
                     'extended_attributes': declaration.extended_attributes}
        primary = operation_symbol(declaration.interface, operation,
                                   declaration.placement or 'Prototype')
        candidates = [primary, primary + INNER_SUFFIX_FOR_ROLE[CALL],
                      primary + 'OverloadDispatcher']
        for index in range(1, declaration.overload_count + 1):
            candidates.append('{}{}{}'.format(primary, index, INNER_SUFFIX_FOR_ROLE[CALL]))
        return candidates
    return []


def overload_body_symbols(declaration):
    """The numbered per-overload bodies, when there is more than one overload.

    Counted in the totals and never attributed to a line. See the module docstring.
    """
    if declaration.kind != OPERATION or declaration.overload_count < 2:
        return []
    operation = {'name': declaration.name, 'is_static': declaration.is_static,
                 'extended_attributes': declaration.extended_attributes}
    primary = operation_symbol(declaration.interface, operation,
                               declaration.placement or 'Prototype')
    return ['{}{}{}'.format(primary, index, INNER_SUFFIX_FOR_ROLE[CALL])
            for index in range(1, declaration.overload_count + 1)]


def roles_for(declaration):
    if declaration.kind == ATTRIBUTE:
        return (GET, SET) if _setter_is_expected(declaration) else (GET,)
    if declaration.kind == OPERATION:
        return (CALL,)
    return (CONSTRUCT,)


class BindingsCoverageJoin:
    """Lifts symbol-level counts onto IDL declarations.

    Constructed with the three things the join needs and nothing else, so that it can be tested
    without a build directory: a symbol -> count map, a symbol -> emitting file map, and the
    set of feature macros this build has on.
    """

    def __init__(self, coverage, generated_symbols, enabled_features,
                 generated_interface_classes=None):
        self.coverage = coverage
        self.generated_symbols = generated_symbols
        self.enabled_features = enabled_features
        # Class names the build produced a JS<Interface>.cpp for. None disables the
        # interface-not-built reason, which is what a unit test wants.
        self.generated_interface_classes = generated_interface_classes

    def shared_synthetic_accessor(self, declaration):
        """(symbol, count) for a [DelegateToSharedSyntheticAttribute] member, else None.

        Checked BEFORE any count is read, not after. The shared accessor almost always HAS a
        nonzero count -- something reads some CSS property -- so looking the count up first and
        only then noticing the delegation would report several hundred CSS property attributes
        as covered on the strength of one measurement that says nothing about any of them
        individually.
        """
        if declaration.kind != ATTRIBUTE:
            return None
        shared = declaration.extended_attributes.get('DelegateToSharedSyntheticAttribute')
        if not isinstance(shared, str) or not shared:
            return None
        attribute = {'name': declaration.name, 'is_static': declaration.is_static,
                     'extended_attributes': declaration.extended_attributes}
        symbol = attribute_getter_symbol(declaration.interface, attribute)
        return symbol, self.coverage.count_for(symbol)

    def _unattributed_reason(self, declaration, symbols):
        """Why a declaration with no coverage record could not be measured.

        Ordered most specific first. `js-builtin` before `conditional-off` because a builtin has
        no C++ counter whatever the flags say; `interface-not-built` before both because there is
        nothing to reason about in an interface the build did not emit; and the two
        emitted-but-unrecorded cases last, because they are the ones that need the evidence of a
        symbol scan rather than a reading of the IDL.
        """
        shared = self.shared_synthetic_accessor(declaration)
        if shared:
            symbol, count = shared
            return ('shared-synthetic-accessor',
                    '{} (count {})'.format(symbol, 'unknown' if count is None else count))
        if declaration.is_js_builtin:
            return 'js-builtin', ''
        if (self.generated_interface_classes is not None
                and class_name_for_interface(declaration.interface)
                not in self.generated_interface_classes):
            return 'interface-not-built', ''
        for expression in declaration.conditionals:
            if not conditional_is_enabled(expression, self.enabled_features):
                return 'conditional-off', expression
        for symbol in symbols:
            emitted_in = self.generated_symbols.get(symbol)
            if emitted_in is None:
                continue
            if not self.coverage.has_records_for_file(emitted_in):
                return 'not-in-an-exported-binary', '{} in {}'.format(symbol, emitted_in)
            return 'no-coverage-record', '{} in {}'.format(symbol, emitted_in)
        if 'Exposed' in declaration.extended_attributes and 'mixin' in declaration.declared_in:
            return 'not-exposed-on-this-global', '[Exposed={}]'.format(
                declaration.extended_attributes['Exposed'])
        return 'symbol-not-emitted', symbols[0] if symbols else ''

    def accessor_for(self, declaration, role):
        if role == CONSTRUCT:
            class_name = class_name_for_interface(declaration.interface)
            template = declaration.placement or _CONSTRUCTOR_TEMPLATE
            count = self.coverage.constructor_count_for(template, class_name)
            symbol = '{}<{}>::{}'.format(template, class_name, _CONSTRUCTOR_METHOD)
            if count is None:
                reason, detail = self._unattributed_reason(declaration, [])
                return Accessor(role, UNATTRIBUTED, None, symbol, reason, detail)
            state = EXECUTED if count else NEVER_EXECUTED
            return Accessor(role, state, count, symbol, None, '')
        symbols = candidate_symbols(declaration, role)
        if self.shared_synthetic_accessor(declaration):
            reason, detail = self._unattributed_reason(declaration, symbols)
            return Accessor(role, UNATTRIBUTED, None, symbols[0] if symbols else None,
                            reason, detail)
        best_count, best_symbol = None, None
        for symbol in symbols:
            count = self.coverage.count_for(symbol)
            if count is None:
                continue
            if best_count is None or count > best_count:
                best_count, best_symbol = count, symbol
        if best_count is None:
            reason, detail = self._unattributed_reason(declaration, symbols)
            return Accessor(role, UNATTRIBUTED, None, symbols[0] if symbols else None,
                            reason, detail)
        state = EXECUTED if best_count else NEVER_EXECUTED
        return Accessor(role, state, best_count, best_symbol, None, '')

    def result_for(self, declaration):
        accessors = [self.accessor_for(declaration, role) for role in roles_for(declaration)]
        measured = [accessor for accessor in accessors if accessor.state != UNATTRIBUTED]
        overload_bodies = overload_body_symbols(declaration)
        never_executed_overloads = [symbol for symbol in overload_bodies
                                    if self.coverage.count_for(symbol) == 0]
        if not measured:
            first = accessors[0] if accessors else None
            return MemberResult(declaration, UNATTRIBUTED, accessors,
                                first.reason if first else 'symbol-not-emitted',
                                first.detail if first else '', never_executed_overloads,
                                [symbol for symbol in overload_bodies
                                 if self.coverage.count_for(symbol) is not None])
        status = EXECUTED if any(accessor.state == EXECUTED for accessor in measured) \
            else NEVER_EXECUTED
        return MemberResult(declaration, status, accessors, None, '', never_executed_overloads,
                            [symbol for symbol in overload_bodies
                             if self.coverage.count_for(symbol) is not None])


# Everything a report needs, per interface.
InterfaceResult = namedtuple('InterfaceResult', ('name', 'idl_path', 'members', 'unenumerated'))


def join_interfaces(model, join, interface_names):
    """[InterfaceResult] for the named interfaces, in the order given."""
    results = []
    for name in interface_names:
        main = model.main_declaration(name)
        if main is None:
            continue
        members = [join.result_for(declaration)
                   for declaration in model.declarations_for(name)]
        members.sort(key=lambda result: (result.declaration.kind, result.declaration.name))
        results.append(InterfaceResult(name, main['idl_path'], members,
                                       model.unenumerated_declarations_for(name)))
    return results


class BindingsCoverageTotals:
    """The counts a report quotes, and the only place they are computed."""

    def __init__(self, interface_results):
        self.interfaces = 0
        self.interfaces_with_a_gap = 0
        self.by_kind = {}          # kind -> {status: count}
        self.by_reason = {}        # reason -> count
        self.unenumerated = {}     # kind of unnamed member -> count
        self.setter_gaps = 0       # attributes read but never written
        self.overload_bodies = 0
        self.never_executed_overload_bodies = 0
        for interface in interface_results:
            self.interfaces += 1
            if any(member.status == NEVER_EXECUTED for member in interface.members):
                self.interfaces_with_a_gap += 1
            for key, count in interface.unenumerated.items():
                self.unenumerated[key] = self.unenumerated.get(key, 0) + count
            for member in interface.members:
                bucket = self.by_kind.setdefault(member.declaration.kind, {})
                bucket[member.status] = bucket.get(member.status, 0) + 1
                if member.status == UNATTRIBUTED:
                    self.by_reason[member.reason] = self.by_reason.get(member.reason, 0) + 1
                if member.status == EXECUTED:
                    for accessor in member.accessors:
                        if accessor.role == SET and accessor.state == NEVER_EXECUTED:
                            self.setter_gaps += 1
                self.overload_bodies += len(member.overload_bodies)
                self.never_executed_overload_bodies += len(member.never_executed_overloads)

    def count(self, kind, status):
        return self.by_kind.get(kind, {}).get(status, 0)

    def declared(self, kind):
        return sum(self.by_kind.get(kind, {}).values())

    def measurable(self, kind):
        return self.count(kind, EXECUTED) + self.count(kind, NEVER_EXECUTED)

    def total_declared(self):
        return sum(self.declared(kind) for kind in self.by_kind)

    def total_measurable(self):
        return sum(self.measurable(kind) for kind in self.by_kind)

    def total_never_executed(self):
        return sum(self.count(kind, NEVER_EXECUTED) for kind in self.by_kind)

    def total_unattributed(self):
        return sum(self.count(kind, UNATTRIBUTED) for kind in self.by_kind)


# ---------------------------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------------------------

KIND_ORDER = (ATTRIBUTE, OPERATION, CONSTRUCTOR)

KIND_PLURALS = {ATTRIBUTE: 'attributes', OPERATION: 'operations',
                CONSTRUCTOR: 'constructors'}


def format_percent(covered, total, scope=None):
    """'61.54%', or a lower bound when the run was selective.

    Routed through CoverageScope.format_percent for exactly the reason coverage_scope exists: a
    number from a subset run that is printed unqualified will be quoted as though it were
    exact. A never-executed member in a selective run is unknown, not untested.
    """
    if not total:
        return '-'
    value = 100.0 * covered / total
    if scope is not None:
        return scope.format_percent(value)
    return '{:.2f}%'.format(value)


def member_signature(member):
    """'Document.getElementById()' or 'Document.title', with the accessor detail."""
    declaration = member.declaration
    if declaration.kind == OPERATION:
        name = '{}.{}()'.format(declaration.interface, declaration.name)
        if declaration.overload_count > 1:
            name += ' [{} overloads]'.format(declaration.overload_count)
        return name
    if declaration.kind == CONSTRUCTOR:
        if declaration.placement == _LEGACY_FACTORY_TEMPLATE:
            # `new Image()` and `new HTMLImageElement()` are different entities, so the name has
            # to say which one this is rather than being derived from the interface.
            return 'new {}()  [{} legacy factory function]'.format(
                declaration.name, declaration.interface)
        return 'new {}()'.format(declaration.interface)
    return '{}.{}'.format(declaration.interface, declaration.name)


def never_executed_lines(interface_results, scope=None):
    """The headline: every declaration nothing called, grouped by interface.

    This is the actionable output, so it comes first and it is a list of names rather than a
    percentage. A percentage tells you there is a gap; a name tells you what test to write.
    """
    lines = []
    for interface in interface_results:
        gaps = [member for member in interface.members if member.status == NEVER_EXECUTED]
        if not gaps:
            continue
        measurable = sum(1 for member in interface.members if member.status != UNATTRIBUTED)
        executed = measurable - len(gaps)
        lines.append('{}  {} of {} measurable member(s) executed ({})'.format(
            interface.name, executed, measurable,
            format_percent(executed, measurable, scope)))
        for member in gaps:
            roles = ', '.join(
                ROLE_LABELS[accessor.role] for accessor in member.accessors
                if accessor.state == NEVER_EXECUTED)
            lines.append('    {:<58} {}'.format(member_signature(member), roles))
    return lines


def setter_gap_lines(interface_results):
    """Attributes something read and nothing ever wrote.

    A separate list because it is a separate kind of missing test, and because the attribute
    itself is covered: whole-declaration coverage cannot show it.
    """
    lines = []
    for interface in interface_results:
        gaps = [member for member in interface.members
                if member.status == EXECUTED
                and any(accessor.role == SET and accessor.state == NEVER_EXECUTED
                        for accessor in member.accessors)]
        if not gaps:
            continue
        lines.append('{}'.format(interface.name))
        for member in gaps:
            lines.append('    {}'.format(member_signature(member)))
    return lines


def totals_lines(totals, scope=None):
    lines = []
    for kind in KIND_ORDER:
        declared = totals.declared(kind)
        if not declared:
            continue
        measurable = totals.measurable(kind)
        executed = totals.count(kind, EXECUTED)
        lines.append(
            '{:<14} {:>6} declared, {:>6} measurable, {:>6} executed ({}), '
            '{:>5} never executed, {:>5} unattributed'.format(
                KIND_PLURALS[kind], declared, measurable, executed,
                format_percent(executed, measurable, scope),
                totals.count(kind, NEVER_EXECUTED), totals.count(kind, UNATTRIBUTED)))
    lines.append('{:<14} {:>6} interfaces, {:>6} with at least one never-executed member'.format(
        'interfaces', totals.interfaces, totals.interfaces_with_a_gap))
    if totals.setter_gaps:
        lines.append('{:<14} {:>6} attributes were read and never written'.format(
            'setters', totals.setter_gaps))
    if totals.overload_bodies:
        lines.append(
            '{:<14} {:>6} per-overload bodies measurable, {:>6} never executed (counted, and '
            'not attributed to a line)'.format(
                'overloads', totals.overload_bodies, totals.never_executed_overload_bodies))
    return lines


def unattributed_lines(totals):
    """The third state, by reason. Never rolled into the never-executed count."""
    lines = []
    for reason in REASON_ORDER:
        count = totals.by_reason.get(reason)
        if not count:
            continue
        lines.append('{:>6}  {}'.format(count, REASON_LABELS[reason]))
        lines.append('        {}'.format(REASON_EXPLANATIONS[reason]))
    for reason, count in sorted(totals.by_reason.items()):
        if reason not in REASON_ORDER:
            lines.append('{:>6}  {}'.format(count, reason))
    if totals.unenumerated:
        lines.append('')
        lines.append('Declarations this tool does not enumerate at all:')
        for kind, count in sorted(totals.unenumerated.items()):
            lines.append('{:>6}  {} declaration(s)'.format(count, kind))
        lines.append('        These declare no member name and the generator implements them '
                     'with shared template machinery, so there is no symbol named after the '
                     'declaration to join against.')
    return lines


def unattributed_detail_lines(interface_results, reason, limit=None):
    """The declarations behind one unattributed reason, for `--explain`."""
    lines = []
    for interface in interface_results:
        members = [member for member in interface.members
                   if member.status == UNATTRIBUTED and member.reason == reason]
        if not members:
            continue
        for member in members:
            if limit is not None and len(lines) >= limit:
                return lines + ['    ... and more']
            detail = ' -- {}'.format(member.detail) if member.detail else ''
            lines.append('    {:<58}{}'.format(member_signature(member), detail))
    return lines


def tsv_rows(interface_results):
    """One row per declaration, for a spreadsheet or a diff between two runs.

    Tab-separated for the same reason not-built.tsv is: the values contain spaces, commas and
    parentheses, and nothing here needs quoting rules.
    """
    yield ('interface', 'kind', 'name', 'status', 'reason', 'detail', 'role', 'role_status',
           'count', 'symbol', 'declared_in', 'idl_file')
    for interface in interface_results:
        for member in interface.members:
            declaration = member.declaration
            for accessor in member.accessors:
                yield (declaration.interface, declaration.kind, declaration.name, member.status,
                       member.reason or '', member.detail or '', accessor.role, accessor.state,
                       '' if accessor.count is None else str(accessor.count),
                       accessor.symbol or '', declaration.declared_in, declaration.idl_path)

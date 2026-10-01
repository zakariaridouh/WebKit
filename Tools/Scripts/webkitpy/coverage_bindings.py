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

"""Reports which IDL attributes, operations and constructors no test called, by lifting the
lcov counts of the generated JS bindings onto the IDL declarations that produced them.
Declarations with no coverage mapping are reported as unattributed rather than as 0%."""

import logging
import os
import re
import subprocess

from collections import namedtuple

from webkitpy.coverage_lcov import open_lcov

logger = logging.getLogger(__name__)

# Acronym fixups from CodeGenerator.pm's WK_lcfirst.
_LCFIRST_ACRONYMS = (
    ('dOM', 'dom'), ('hTML', 'html'), ('uRL', 'url'), ('jS', 'js'),
    ('xML', 'xml'), ('xSLT', 'xslt'), ('cSS', 'css'), ('rTC', 'rtc'),
)

_LCFIRST_WHOLE_WORDS = {'create': 'isCreate', 'exclusive': 'isExclusive'}


def wk_lcfirst(name):
    """CodeGenerator.pm's WK_lcfirst. 'JSDocument' -> 'jsDocument'."""
    if not name:
        return name
    result = name[0].lower() + name[1:]
    for prefix, replacement in _LCFIRST_ACRONYMS:
        if result.startswith(prefix):
            result = replacement + result[len(prefix):]
            break
    return _LCFIRST_WHOLE_WORDS.get(result, result)


# CodeGenerator.pm's WK_ucfirst. The original anchors the Xml rule as /^Xml[^a-z]/, so
# `Xmlns` does not become `XMLns`.
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
    """CodeGeneratorJS.pm's MangleAttributeOrFunctionName."""
    name = name.replace('-', '_dash_')
    return name if name.startswith('_') else '_' + name


def class_name_for_interface(interface_name):
    """'Document' -> 'JSDocument'."""
    return 'JS' + interface_name


# The generator emits an inner function beside each trampoline and both carry counters. They
# need not agree: a DOMJIT or getOwnPropertySlot path can run the body without the trampoline.
INNER_SUFFIX_FOR_ROLE = {'get': 'Getter', 'set': 'Setter', 'call': 'Body'}

GET = 'get'
SET = 'set'
CALL = 'call'
CONSTRUCT = 'construct'

ROLE_LABELS = {GET: 'getter', SET: 'setter', CALL: 'call', CONSTRUCT: 'constructor'}


def attribute_getter_symbol(interface_name, attribute):
    """CodeGeneratorJS.pm's GetAttributeGetterName. A [DelegateToSharedSyntheticAttribute]
    attribute yields the SHARED accessor's name, which is the function the build emitted."""
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


_OPERATION_NAME_SUBSTITUTIONS = {
    '[Symbol.Iterator]': 'SymbolIterator',
    '[Symbol.asyncIterator]': 'AsyncSymbolIterator',
}


def operation_symbol(interface_name, operation, placement):
    """CodeGeneratorJS.pm's GetFunctionName; placement comes from operation_placement()."""
    name = operation['name']
    name = _OPERATION_NAME_SUBSTITUTIONS.get(name, name)
    prefix = wk_lcfirst(class_name_for_interface(interface_name))
    return prefix + placement + 'Function' + mangle_member_name(name)


def operation_placement(interface_extended_attributes, operation):
    """'Constructor', 'Instance' or 'Prototype', as OperationShouldBeOnInstance() decides.
    [LegacyUnforgeable] counts on the interface as well as on the member."""
    if operation['is_static']:
        return 'Constructor'
    if 'Global' in interface_extended_attributes:
        return 'Instance'
    if ('LegacyUnforgeable' in operation['extended_attributes']
            or 'LegacyUnforgeable' in interface_extended_attributes):
        return 'Instance'
    return 'Prototype'


# CodeGeneratorJS.pm's IsJSBuiltin. A builtin's body is JavaScript, so the generator emits no
# C++ function and there is no counter to read.
_CUSTOM_ACCESSOR_ATTRIBUTES = ('Custom', 'CustomGetter', 'CustomSetter')


def is_js_builtin(interface_extended_attributes, member_extended_attributes):
    if any(name in member_extended_attributes for name in _CUSTOM_ACCESSOR_ATTRIBUTES):
        return False
    return ('JSBuiltin' in member_extended_attributes
            or 'JSBuiltin' in interface_extended_attributes)


# Constructors are not named after the IDL member but are JSDOMConstructor<JSFoo>::construct
# or JSDOMLegacyFactoryFunction<JSFoo>::construct, and an interface can have either or both.
_CONSTRUCTOR_TEMPLATE = 'JSDOMConstructor'
_LEGACY_FACTORY_TEMPLATE = 'JSDOMLegacyFactoryFunction'
_CONSTRUCTOR_TEMPLATES = (_CONSTRUCTOR_TEMPLATE, _LEGACY_FACTORY_TEMPLATE)
_CONSTRUCTOR_METHOD = 'construct'


def read_feature_defines(path):
    """The truthy feature macros the build handed the generators (platform-feature-defines.txt)."""
    with open(path) as handle:
        return frozenset(line.strip() for line in handle if line.strip())


def conditional_is_enabled(expression, enabled_features):
    """Evaluates `A&B|C` (with `!` and an implied ENABLE_ prefix) as CodeGenerator.pm's
    GenerateConditionalStringFromAttributeValue does. An empty expression is true."""
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
    """Every [Conditional] guarding a declaration. All must hold: the generator nests the #ifs."""
    expressions = []
    for attributes in extended_attribute_maps:
        value = (attributes or {}).get('Conditional')
        if isinstance(value, str) and value:
            expressions.append(value)
    return expressions


# Embedded Perl that drives the checkout's IDLParser.pm. It writes JSON lines to a file, not
# stdout, because IDLParser.pm's assert() prints to STDOUT. A rejected file still gets a record.
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

# Batched to stay under ARG_MAX on checkouts with a long path prefix.
_IDL_BATCH_SIZE = 400


class IDLParseError(RuntimeError):
    pass


def bindings_scripts_directory(source_root):
    return os.path.join(source_root, 'Source', 'WebCore', 'bindings', 'scripts')


def parse_idl_files(paths, source_root, perl='perl'):
    """One record per .idl path, in order: {'file', 'interfaces', 'includes'}, or
    {'file', 'error'} for a file the parser rejected."""
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
    Each line is a main IDL file followed by the partials and mixins that supplement it."""
    main, supplemental = [], set()
    with open(path) as handle:
        for line in handle:
            parts = line.split()
            if not parts:
                continue
            main.append(parts[0])
            supplemental.update(parts[1:])
    return main, supplemental


# interface is the INCLUDING interface for a mixin member. placement is the symbol placement
# for an operation, the class template for a constructor, and None for an attribute.
Declaration = namedtuple('Declaration', (
    'interface', 'kind', 'name', 'idl_path', 'declared_in', 'extended_attributes',
    'is_static', 'is_read_only', 'overload_count', 'conditionals', 'placement',
    'is_js_builtin'))

ATTRIBUTE = 'attribute'
OPERATION = 'operation'
CONSTRUCTOR = 'constructor'


class IDLModel:
    """The parsed IDL, flattened as preprocess-idls.pl does: an interface's members are its own,
    its partials', and those of every mixin it includes."""

    def __init__(self):
        self.interfaces = {}      # name -> [interface record], the main one first
        self.mixins = {}          # name -> [mixin record]
        self.includes = {}        # interface name -> [mixin name]
        self.parse_errors = []    # [(path, message)]

    @classmethod
    def from_records(cls, records, excluded_path_fragments=None):
        """Index parsed IDL records, dropping the files no build reads.
        Fixtures are dropped here, not at selection: one can be a partial of a real interface."""
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
                    # Non-partial first, so main_declaration() does not depend on parse order.
                    # DOMWindow.idl declares DOMWindow twice; the first is the real one.
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
        Overloads collapse into one Declaration per name, carrying the count."""
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
                    # An anonymous `stringifier;` is emitted as toString().
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
        """One declaration per constructor template: `constructor()` and a
        [LegacyFactoryFunction] are emitted as different entities with their own counters."""
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
        """Counts of unnamed members (iterable<>, maplike<>, setlike<>, anonymous operations),
        which the generator implements with shared templates, not a named function."""
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
        """The interfaces worth reporting on: real, non-callback, from a real IDL file."""
        names = []
        for name in sorted(self.interfaces):
            main = self.main_declaration(name)
            if main is None or main['is_callback']:
                continue
            if not include_test_support and is_test_support_idl(main['idl_path']):
                continue
            names.append(name)
        return names


# The generator's expected-output fixtures. Not a build input.
GENERATOR_FIXTURE_FRAGMENT = os.path.join('bindings', 'scripts', 'test') + os.sep

# Test-support IDL (Internals and friends), excluded by default.
_TEST_SUPPORT_FRAGMENTS = (
    os.path.join('WebCore', 'testing') + os.sep,
    os.path.join('WebCore', 'workers', 'service', 'server') + os.sep,
)

# Generated into DerivedSources, so no path fragment matches it, but it is test support too.
_TEST_SUPPORT_BASENAMES = ('InternalSettingsGenerated.idl',)


def is_test_support_idl(path):
    return (any(fragment in path for fragment in _TEST_SUPPORT_FRAGMENTS)
            or os.path.basename(path) in _TEST_SUPPORT_BASENAMES)


# CMake puts generated code in WebCore/DerivedSources, Xcode in DerivedSources/WebCore.
# generate-coverage-report ignores this path, so its traces have nothing this module can read.
DERIVED_SOURCES_MARKER = 'DerivedSources'

# Definition sites only: matching every occurrence would let property-table references and
# comments hide a symbol the generator never emitted.
_GENERATED_DEFINITION = re.compile(
    r'JSC_DEFINE_(?:CUSTOM_GETTER|CUSTOM_SETTER|HOST_FUNCTION|JIT_OPERATION(?:_WITHOUT_WTF_INTERNAL)?)'
    r'\((?P<macro>(?:js|setJS)\w+)'
    r'|^static inline [^(]*?\b(?P<inline>(?:js|setJS)\w+)\s*\(',
    re.MULTILINE)


def scan_generated_symbols(directories):
    """{symbol: basename of the file defining it}. Basenames, not paths, because an lcov trace
    can spell one file's path several ways (symlinked build directory, -path-equivalence)."""
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
    """The length-prefixed identifiers in an Itanium-mangled name, by scanning, not demangling.
    It may yield tokens that are not identifiers; they are only used as lookup keys."""
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


class SymbolCoverage:
    """Execution counts for the binding symbols in an lcov trace, keyed by source-level name.
    A name's count is the maximum over its records, not the sum: the question is whether it ran."""

    def __init__(self):
        self.counts = {}               # symbol -> execution count
        self.constructor_counts = {}   # (template, 'JSDocument') -> execution count
        self.records = 0               # FN: records read, for the health line
        self.source_files = set()      # basenames of the files the trace has records for
        # Files under DerivedSources. A trace made with generate-coverage-report's usual ignore
        # regex has none, and most declarations would come out unattributed.
        self.derived_sources_files = 0

    def count_for(self, symbol):
        return self.counts.get(symbol)

    def constructor_count_for(self, template, class_name):
        return self.constructor_counts.get((template, class_name))

    def has_records_for_file(self, basename):
        """Did any function in this file get a coverage record?
        False means the file was compiled into a binary this export did not read."""
        return basename in self.source_files

    def _note(self, mangled, count):
        identifiers = mangled_identifiers(mangled)
        for index, identifier in enumerate(identifiers):
            if identifier.startswith('js') or identifier.startswith('setJS'):
                if count > self.counts.get(identifier, -1):
                    self.counts[identifier] = count
            elif identifier in _CONSTRUCTOR_TEMPLATES and index + 2 < len(identifiers):
                # JSDOMConstructor<JSFoo>::construct: the class is the next identifier, the method
                # the one after. Check the method: other members exist for every interface.
                if _CONSTRUCTOR_METHOD in identifiers[index + 1:index + 3]:
                    key = (identifier, identifiers[index + 1])
                    if count > self.constructor_counts.get(key, -1):
                        self.constructor_counts[key] = count

    @classmethod
    def from_lcov(cls, path):
        """Read FN:/FNDA: records only. FN: seeds a zero and FNDA: raises it."""
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


EXECUTED = 'executed'
NEVER_EXECUTED = 'never-executed'
UNATTRIBUTED = 'unattributed'

# Why a declaration could not be measured, in report order.
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


# state: EXECUTED, NEVER_EXECUTED or UNATTRIBUTED. count is None unless measured. symbol is the
# matched name, or the primary candidate when nothing matched.
Accessor = namedtuple('Accessor', ('role', 'state', 'count', 'symbol', 'reason', 'detail'))

# status: EXECUTED if any accessor ran; NEVER_EXECUTED if some were measurable and none ran;
# UNATTRIBUTED if none was measurable.
MemberResult = namedtuple('MemberResult', (
    'declaration', 'status', 'accessors', 'reason', 'detail', 'never_executed_overloads',
    'overload_bodies'))


def _setter_is_expected(declaration):
    """A readonly attribute has a setter only with [Replaceable] or [PutForwards]."""
    if not declaration.is_read_only:
        return True
    return bool({'Replaceable', 'PutForwards'} & set(declaration.extended_attributes))


def candidate_symbols(declaration, role):
    """Every generated name that could carry this declaration's counter for one role.
    They are alternative entry points, so the declaration is executed if ANY of them ran."""
    attribute = {'name': declaration.name, 'is_static': declaration.is_static,
                 'extended_attributes': declaration.extended_attributes}
    if role == GET:
        primary = attribute_getter_symbol(declaration.interface, attribute)
        # `attribute HTMLImageElementConstructor Image` on a global appends Constructor after the
        # member name.
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
    """The numbered per-overload bodies: counted in the totals, never attributed to a line."""
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
    """Lifts symbol-level counts onto IDL declarations."""

    def __init__(self, coverage, generated_symbols, enabled_features,
                 generated_interface_classes=None):
        self.coverage = coverage
        self.generated_symbols = generated_symbols
        self.enabled_features = enabled_features
        # Classes the build generated. None disables interface-not-built.
        self.generated_interface_classes = generated_interface_classes

    def shared_synthetic_accessor(self, declaration):
        """(symbol, count) for a [DelegateToSharedSyntheticAttribute] member, else None.
        Check this before reading counts: the shared accessor's count says nothing per property."""
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
        The checks run most specific first: the IDL-level reasons, then the symbol-scan ones."""
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


InterfaceResult = namedtuple('InterfaceResult', ('name', 'idl_path', 'members', 'unenumerated'))


def join_interfaces(model, join, interface_names):
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


KIND_ORDER = (ATTRIBUTE, OPERATION, CONSTRUCTOR)

KIND_PLURALS = {ATTRIBUTE: 'attributes', OPERATION: 'operations',
                CONSTRUCTOR: 'constructors'}


def format_percent(covered, total, scope=None):
    """'61.54%', or a lower bound when the run was selective (via CoverageScope.format_percent)."""
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
            # `new Image()` is not `new HTMLImageElement()`: name the factory, not the interface.
            return 'new {}()  [{} legacy factory function]'.format(
                declaration.name, declaration.interface)
        return 'new {}()'.format(declaration.interface)
    return '{}.{}'.format(declaration.interface, declaration.name)


def never_executed_lines(interface_results, scope=None):
    """The headline: every declaration nothing called, grouped by interface."""
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
    """Attributes something read and nothing ever wrote."""
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
    """One row per declaration accessor, tab-separated for a spreadsheet or a diff between runs."""
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

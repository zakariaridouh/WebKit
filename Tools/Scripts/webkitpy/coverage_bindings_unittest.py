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

"""Tests for coverage_bindings.

Where a test asserts a generated symbol name, the expected string was taken from the build's
own output -- WebKitBuild/cmake-mac/Coverage/WebCore/DerivedSources -- and not from reading the
generator. The whole module is a claim about what CodeGeneratorJS.pm emits, so a test that
restates this module's rules would prove only that it is self-consistent.
"""

import gzip
import os
import shutil
import tempfile
import unittest

from webkitpy.common.system.filesystem import FileSystem
from webkitpy.common.webkit_finder import WebKitFinder
from webkitpy.coverage_bindings import (
    ATTRIBUTE, CALL, CONSTRUCT, CONSTRUCTOR, EXECUTED, GET, NEVER_EXECUTED, OPERATION,
    REASON_EXPLANATIONS, REASON_LABELS, REASON_ORDER, SET, UNATTRIBUTED, BindingsCoverageJoin,
    BindingsCoverageTotals, IDLModel, SymbolCoverage, attribute_getter_symbol,
    attribute_setter_symbol, candidate_symbols, class_name_for_interface,
    conditional_is_enabled, is_js_builtin, join_interfaces, mangle_member_name,
    mangled_identifiers, member_signature, never_executed_lines, operation_placement,
    operation_symbol, parse_idl_files, read_feature_defines, read_supplemental_dependencies,
    scan_generated_symbols, setter_gap_lines, totals_lines, tsv_rows, unattributed_lines,
    wk_lcfirst, wk_ucfirst)
from webkitpy.coverage_scope import CoverageScope


# -- record builders ---------------------------------------------------------------------------
#
# The shape parse_idl_files() produces, built by hand so that the model and join tests need no
# Perl and no checkout. Kept as functions rather than as literals so that adding a field to the
# Perl program's output only has to be reflected in one place here.

def attribute(name, read_only=False, static=False, extended_attributes=None, type_name='DOMString'):
    return {'name': name, 'is_static': 1 if static else 0,
            'is_read_only': 1 if read_only else 0, 'type': type_name,
            'extended_attributes': extended_attributes or {}}


def operation(name, static=False, extended_attributes=None, arguments=0, stringifier=False):
    return {'name': name, 'is_static': 1 if static else 0,
            'is_stringifier': 1 if stringifier else 0, 'specials': [],
            'argument_count': arguments, 'extended_attributes': extended_attributes or {}}


def interface(name, attributes=(), operations=(), constructors=(), partial=False, mixin=False,
              callback=False, extended_attributes=None, iterable=False, map_like=False,
              set_like=False, anonymous_operations=0, parent=None):
    return {'name': name, 'parent': parent, 'is_partial': 1 if partial else 0,
            'is_mixin': 1 if mixin else 0, 'is_callback': 1 if callback else 0,
            'is_namespace_object': 0, 'has_iterable': 1 if iterable else 0,
            'has_async_iterable': 0, 'has_map_like': 1 if map_like else 0,
            'has_set_like': 1 if set_like else 0, 'constant_count': 0,
            'extended_attributes': extended_attributes or {},
            'attributes': list(attributes), 'operations': list(operations),
            'anonymous_operation_count': anonymous_operations,
            'constructors': list(constructors)}


def record(path, interfaces=(), includes=(), error=None):
    entry = {'file': path}
    if error is not None:
        entry['error'] = error
        return entry
    entry['interfaces'] = list(interfaces)
    entry['includes'] = [{'interface': target, 'mixin': mixin} for target, mixin in includes]
    return entry


def coverage_from(counts=None, constructors=None, source_files=()):
    """A SymbolCoverage with the counts a test wants, without going through an lcov trace."""
    coverage = SymbolCoverage()
    coverage.counts = dict(counts or {})
    coverage.constructor_counts = dict(constructors or {})
    coverage.source_files = set(source_files)
    return coverage


# -- the generator's naming rules --------------------------------------------------------------

class WKLcfirstTest(unittest.TestCase):
    def test_a_class_name_becomes_the_symbol_prefix(self):
        self.assertEqual(wk_lcfirst('JSDocument'), 'jsDocument')

    def test_an_acronym_that_the_lowercasing_broke_is_repaired(self):
        # lcfirst('JSDOMWindow') is 'jSDOMWindow', and the jS rule puts it back.
        self.assertEqual(wk_lcfirst('JSDOMWindow'), 'jsDOMWindow')

    def test_an_interior_acronym_is_left_alone(self):
        self.assertEqual(wk_lcfirst('JSXMLHttpRequest'), 'jsXMLHttpRequest')

    def test_only_one_acronym_rule_applies(self):
        # The rules are alternatives, not a pipeline: whichever prefix matches wins and the rest
        # are not tried against the result.
        self.assertEqual(wk_lcfirst('JSCSSStyleProperties'), 'jsCSSStyleProperties')

    def test_create_is_renamed_because_it_collides_in_the_generated_code(self):
        self.assertEqual(wk_lcfirst('create'), 'isCreate')
        self.assertEqual(wk_lcfirst('exclusive'), 'isExclusive')

    def test_a_word_that_merely_starts_with_create_is_not_renamed(self):
        self.assertEqual(wk_lcfirst('createElement'), 'createElement')

    def test_an_empty_name_is_not_an_error(self):
        self.assertEqual(wk_lcfirst(''), '')


class WKUcfirstTest(unittest.TestCase):
    def test_a_class_name_that_is_already_capitalized_is_unchanged(self):
        self.assertEqual(wk_ucfirst('JSDocument'), 'JSDocument')

    def test_xml_is_capitalized(self):
        self.assertEqual(wk_ucfirst('xmlVersion'), 'XMLVersion')

    def test_xml_followed_by_a_lowercase_letter_is_not_capitalized(self):
        # The original anchors this rule as /^Xml[^a-z]/, so `xmlns` must not become `XMLns`.
        # Kept because the difference decides a real setter name.
        self.assertEqual(wk_ucfirst('xmlns'), 'Xmlns')

    def test_pq_is_capitalized_only_as_a_whole_word(self):
        self.assertEqual(wk_ucfirst('pq'), 'PQ')
        self.assertEqual(wk_ucfirst('pqrs'), 'Pqrs')


class MangleMemberNameTest(unittest.TestCase):
    def test_a_member_name_is_separated_from_the_interface_by_an_underscore(self):
        self.assertEqual(mangle_member_name('title'), '_title')

    def test_a_name_that_already_begins_with_an_underscore_keeps_its_own(self):
        self.assertEqual(mangle_member_name('__proto__'), '__proto__')

    def test_a_hyphen_becomes_dash_because_a_symbol_cannot_hold_one(self):
        # CSS property attributes are declared with hyphens: -apple-color-filter.
        self.assertEqual(mangle_member_name('-webkit-box'), '_dash_webkit_dash_box')


class SymbolNameTest(unittest.TestCase):
    """Every expected string here was read out of the build's generated sources."""

    def test_an_attribute_getter(self):
        self.assertEqual(attribute_getter_symbol('Document', attribute('URL', read_only=True)),
                         'jsDocument_URL')

    def test_an_attribute_setter(self):
        self.assertEqual(attribute_setter_symbol('Document', attribute('xmlVersion')),
                         'setJSDocument_xmlVersion')

    def test_a_static_attribute_goes_through_the_constructor(self):
        self.assertEqual(
            attribute_getter_symbol('CSS', attribute('paintWorklet', static=True)),
            'jsCSSConstructor_paintWorklet')

    def test_a_delegated_attribute_names_the_shared_accessor_and_not_itself(self):
        # This is the whole reason DelegateToSharedSyntheticAttribute cannot be measured
        # per property: 1,566 CSSStyleProperties declarations map onto four symbols.
        delegated = attribute('-apple-color-filter', extended_attributes={
            'DelegateToSharedSyntheticAttribute': 'propertyValueForDashedIDLAttribute'})
        self.assertEqual(attribute_getter_symbol('CSSStyleProperties', delegated),
                         'jsCSSStyleProperties_propertyValueForDashedIDLAttribute')
        self.assertEqual(attribute_setter_symbol('CSSStyleProperties', delegated),
                         'setJSCSSStyleProperties_propertyValueForDashedIDLAttribute')

    def test_an_operation_on_the_prototype(self):
        self.assertEqual(
            operation_symbol('Document', operation('getElementById'), 'Prototype'),
            'jsDocumentPrototypeFunction_getElementById')

    def test_a_static_operation_is_on_the_constructor(self):
        self.assertEqual(
            operation_symbol('Document', operation('parseHTMLUnsafe', static=True), 'Constructor'),
            'jsDocumentConstructorFunction_parseHTMLUnsafe')

    def test_an_operation_on_a_global_is_on_the_instance(self):
        self.assertEqual(
            operation_symbol('DedicatedWorkerGlobalScope', operation('postMessage'), 'Instance'),
            'jsDedicatedWorkerGlobalScopeInstanceFunction_postMessage')

    def test_the_iterator_symbol_becomes_an_identifier(self):
        self.assertEqual(
            operation_symbol('NodeList', operation('[Symbol.Iterator]'), 'Prototype'),
            'jsNodeListPrototypeFunction_SymbolIterator')

    def test_the_class_name_is_the_interface_name_with_JS_in_front(self):
        self.assertEqual(class_name_for_interface('Document'), 'JSDocument')


class OperationPlacementTest(unittest.TestCase):
    def test_an_ordinary_operation_is_on_the_prototype(self):
        self.assertEqual(operation_placement({}, operation('foo')), 'Prototype')

    def test_a_static_operation_is_on_the_constructor(self):
        self.assertEqual(operation_placement({}, operation('foo', static=True)), 'Constructor')

    def test_a_global_interface_puts_everything_on_the_instance(self):
        self.assertEqual(operation_placement({'Global': 'Window'}, operation('foo')), 'Instance')

    def test_legacy_unforgeable_on_the_member_moves_it_to_the_instance(self):
        self.assertEqual(
            operation_placement({}, operation('foo', extended_attributes={
                'LegacyUnforgeable': 'VALUE_IS_MISSING'})),
            'Instance')

    def test_legacy_unforgeable_on_the_interface_moves_every_member(self):
        # Location carries it at interface level and its members do not. Reading only the member
        # put assign(), replace() and reload() on the prototype and lost all three.
        self.assertEqual(
            operation_placement({'LegacyUnforgeable': 'VALUE_IS_MISSING'}, operation('assign')),
            'Instance')


class IsJSBuiltinTest(unittest.TestCase):
    def test_a_plain_member_is_not_a_builtin(self):
        self.assertFalse(is_js_builtin({}, {}))

    def test_the_extended_attribute_on_the_member(self):
        self.assertTrue(is_js_builtin({}, {'JSBuiltin': 'VALUE_IS_MISSING'}))

    def test_the_extended_attribute_on_the_interface_reaches_every_member(self):
        # ReadableStreamDefaultController is [JSBuiltin] and its members are not, so reading
        # only the member left all 31 of the Streams declarations looking like a mapping error.
        self.assertTrue(is_js_builtin({'JSBuiltin': 'VALUE_IS_MISSING'}, {}))

    def test_a_custom_accessor_beats_the_builtin(self):
        self.assertFalse(is_js_builtin({'JSBuiltin': 'VALUE_IS_MISSING'},
                                       {'CustomGetter': 'VALUE_IS_MISSING'}))


class ConditionalTest(unittest.TestCase):
    FEATURES = frozenset(('ENABLE_VIDEO', 'ENABLE_WEBGL', 'ENABLE_MATHML'))

    def test_no_conditional_is_enabled(self):
        self.assertTrue(conditional_is_enabled('', self.FEATURES))
        self.assertTrue(conditional_is_enabled(None, self.FEATURES))

    def test_the_enable_prefix_is_implied(self):
        # The generator asserts that a Conditional value does not carry one.
        self.assertTrue(conditional_is_enabled('VIDEO', self.FEATURES))
        self.assertFalse(conditional_is_enabled('TOUCH_EVENTS', self.FEATURES))

    def test_a_conjunction_needs_every_term(self):
        self.assertTrue(conditional_is_enabled('VIDEO&WEBGL', self.FEATURES))
        self.assertFalse(conditional_is_enabled('VIDEO&TOUCH_EVENTS', self.FEATURES))

    def test_a_disjunction_needs_only_one(self):
        self.assertTrue(conditional_is_enabled('TOUCH_EVENTS|VIDEO', self.FEATURES))
        self.assertFalse(conditional_is_enabled('TOUCH_EVENTS|WEBXR', self.FEATURES))

    def test_a_disjunction_of_conjunctions(self):
        self.assertTrue(conditional_is_enabled('TOUCH_EVENTS&VIDEO|WEBGL&MATHML', self.FEATURES))
        self.assertFalse(conditional_is_enabled('TOUCH_EVENTS&VIDEO|WEBXR&MATHML', self.FEATURES))

    def test_a_negated_term(self):
        self.assertTrue(conditional_is_enabled('!TOUCH_EVENTS', self.FEATURES))
        self.assertFalse(conditional_is_enabled('!VIDEO', self.FEATURES))

    def test_an_empty_feature_set_disables_everything_guarded(self):
        # Which is why the tool warns loudly when it cannot find the build's defines file: this
        # would otherwise make conditional-off the largest bucket, as an artefact.
        self.assertFalse(conditional_is_enabled('VIDEO', frozenset()))


class ReadFeatureDefinesTest(unittest.TestCase):
    def test_one_macro_per_line_with_blanks_ignored(self):
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as handle:
            handle.write('ENABLE_VIDEO\n\nENABLE_WEBGL\n')
            path = handle.name
        try:
            self.assertEqual(read_feature_defines(path),
                             frozenset(('ENABLE_VIDEO', 'ENABLE_WEBGL')))
        finally:
            os.unlink(path)


class ReadSupplementalDependenciesTest(unittest.TestCase):
    def test_the_first_path_is_the_main_file_and_the_rest_supplement_it(self):
        with tempfile.NamedTemporaryFile('w', suffix='.tmp', delete=False) as handle:
            handle.write('/a/GPUCommandEncoder.idl /a/GPUCommandsMixin.idl /a/GPUObjectBase.idl\n')
            handle.write('/a/Document.idl \n')
            handle.write('\n')
            path = handle.name
        try:
            main, supplemental = read_supplemental_dependencies(path)
            self.assertEqual(main, ['/a/GPUCommandEncoder.idl', '/a/Document.idl'])
            self.assertEqual(supplemental,
                             {'/a/GPUCommandsMixin.idl', '/a/GPUObjectBase.idl'})
        finally:
            os.unlink(path)


# -- mangled names -----------------------------------------------------------------------------

class MangledIdentifiersTest(unittest.TestCase):
    def test_a_real_attribute_getter(self):
        # Taken verbatim from the export: WebCore's internal-linkage jsDocument_URL.
        self.assertIn('jsDocument_URL', mangled_identifiers(
            '_ZN7WebCoreL14jsDocument_URLEPN3JSC14JSGlobalObjectExNS0_12PropertyNameE'))

    def test_a_constructor_template_instantiation(self):
        identifiers = mangled_identifiers(
            '_ZN7WebCore16JSDOMConstructorINS_10JSDocumentEE9constructEPN3JSC14JSGlobalObjectE'
            'PNS3_9CallFrameE')
        self.assertEqual(identifiers[:4],
                         ['WebCore', 'JSDOMConstructor', 'JSDocument', 'construct'])

    def test_a_legacy_factory_function_instantiation(self):
        identifiers = mangled_identifiers(
            '_ZN7WebCore26JSDOMLegacyFactoryFunctionINS_19JSHTMLOptionElementEE9constructE'
            'PN3JSC14JSGlobalObjectEPNS3_9CallFrameE')
        self.assertEqual(identifiers[:4], ['WebCore', 'JSDOMLegacyFactoryFunction',
                                           'JSHTMLOptionElement', 'construct'])

    def test_an_unmangled_name_yields_nothing_rather_than_failing(self):
        self.assertEqual(mangled_identifiers('main'), [])

    def test_a_leading_zero_is_not_a_length_prefix(self):
        # Itanium lengths never start with 0, and treating one as a prefix would shift every
        # identifier after it by one character.
        self.assertEqual(mangled_identifiers('_Z0abc'), [])


class SymbolCoverageTest(unittest.TestCase):
    def trace(self, text):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, 'trace.lcov')
        with open(path, 'w') as handle:
            handle.write(text)
        self.addCleanup(shutil.rmtree, directory)
        return path

    def test_a_function_record_with_no_data_is_a_zero_counter(self):
        # An FN: with no FNDA: is a compiled function nothing executed, which is exactly the
        # never-executed case and must not read as "no counter at all".
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/b/JSDocument.cpp\nFN:10,_ZN7WebCoreL14jsDocument_URLEv\nend_of_record\n'))
        self.assertEqual(coverage.count_for('jsDocument_URL'), 0)

    def test_a_counter_is_read_from_the_data_record(self):
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/b/JSDocument.cpp\n'
            'FN:10,_ZN7WebCoreL14jsDocument_URLEv\n'
            'FNDA:32175,_ZN7WebCoreL14jsDocument_URLEv\n'
            'end_of_record\n'))
        self.assertEqual(coverage.count_for('jsDocument_URL'), 32175)

    def test_the_maximum_wins_across_translation_units(self):
        # A generated file is compiled into a unified-source bundle and its symbols can appear
        # in more than one record. Summing would double-count; the question is only whether it
        # ran, so the maximum is the answer that cannot be wrong in either direction.
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/b/one.cpp\nFNDA:0,_ZN7WebCoreL14jsDocument_URLEv\nend_of_record\n'
            'SF:/b/two.cpp\nFNDA:7,_ZN7WebCoreL14jsDocument_URLEv\nend_of_record\n'))
        self.assertEqual(coverage.count_for('jsDocument_URL'), 7)

    def test_a_symbol_with_no_record_at_all_is_None_and_not_zero(self):
        coverage = SymbolCoverage.from_lcov(self.trace('SF:/b/JSDocument.cpp\nend_of_record\n'))
        self.assertIsNone(coverage.count_for('jsDocument_title'))

    def test_a_constructor_is_recorded_under_its_template_and_class(self):
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/b/JSDocument.cpp\n'
            'FNDA:5,_ZN7WebCore16JSDOMConstructorINS_10JSDocumentEE9constructEv\n'
            'end_of_record\n'))
        self.assertEqual(coverage.constructor_count_for('JSDOMConstructor', 'JSDocument'), 5)

    def test_the_two_constructor_templates_are_kept_apart(self):
        # `new Image()` and `new HTMLImageElement()` are different entities and only one of them
        # exists for HTMLImageElement, so merging the two templates would report a constructor
        # that does not exist as covered.
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/b/JSHTMLImageElement.cpp\n'
            'FNDA:3,_ZN7WebCore26JSDOMLegacyFactoryFunctionINS_18JSHTMLImageElementEE'
            '9constructEv\n'
            'end_of_record\n'))
        self.assertEqual(coverage.constructor_count_for('JSDOMLegacyFactoryFunction',
                                                        'JSHTMLImageElement'), 3)
        self.assertIsNone(coverage.constructor_count_for('JSDOMConstructor',
                                                         'JSHTMLImageElement'))

    def test_another_method_of_the_constructor_template_is_not_a_constructor_call(self):
        # prototypeForStructure and initializeProperties are instantiated for every interface,
        # constructible or not, so keying on the template alone would call every constructor
        # covered.
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/b/JSDocument.cpp\n'
            'FNDA:15,_ZN7WebCore16JSDOMConstructorINS_10JSDocumentEE21prototypeForStructureEv\n'
            'end_of_record\n'))
        self.assertIsNone(coverage.constructor_count_for('JSDOMConstructor', 'JSDocument'))

    def test_the_files_with_records_are_remembered_by_basename(self):
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/b/DerivedSources/JSDocument.cpp\nend_of_record\n'))
        self.assertTrue(coverage.has_records_for_file('JSDocument.cpp'))
        self.assertFalse(coverage.has_records_for_file('JSElement.cpp'))

    def test_generated_files_are_counted_apart_from_the_rest(self):
        # The check that catches being handed a report's coverage.lcov.gz, which is exported with
        # --ignore-filename-regex=/DerivedSources/. It still holds the hand-written custom
        # bindings, so "does this trace mention any binding symbol" answers yes and the report
        # comes out looking plausible with 87% of the declarations unattributed.
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/s/Source/WebCore/bindings/js/JSDocumentCustom.cpp\nend_of_record\n'
            'SF:/b/WebCore/DerivedSources/JSDocument.cpp\nend_of_record\n'))
        self.assertEqual(coverage.derived_sources_files, 1)
        self.assertEqual(len(coverage.source_files), 2)

    def test_a_trace_with_no_generated_files_says_so(self):
        coverage = SymbolCoverage.from_lcov(self.trace(
            'SF:/s/Source/WebCore/bindings/js/JSDocumentCustom.cpp\n'
            'FNDA:4,_ZN7WebCoreL14jsDocument_URLEv\n'
            'end_of_record\n'))
        self.assertEqual(coverage.derived_sources_files, 0)
        # And it does have binding symbols, which is why the check cannot be "any symbol at all".
        self.assertEqual(coverage.count_for('jsDocument_URL'), 4)

    def test_a_gzipped_trace_is_read_transparently(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, 'trace.lcov.gz')
        with gzip.open(path, 'wt') as handle:
            handle.write('SF:/b/JSDocument.cpp\nFNDA:1,_ZN7WebCoreL14jsDocument_URLEv\n')
        self.assertEqual(SymbolCoverage.from_lcov(path).count_for('jsDocument_URL'), 1)


class ScanGeneratedSymbolsTest(unittest.TestCase):
    def directory_with(self, filename, text):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        with open(os.path.join(directory, filename), 'w') as handle:
            handle.write(text)
        return directory

    def test_the_jsc_entry_point_macros_are_definition_sites(self):
        directory = self.directory_with('JSDocument.cpp', (
            'JSC_DEFINE_CUSTOM_GETTER(jsDocument_URL, (JSGlobalObject*))\n'
            'JSC_DEFINE_CUSTOM_SETTER(setJSDocument_xmlVersion, (JSGlobalObject*))\n'
            'JSC_DEFINE_HOST_FUNCTION(jsDocumentPrototypeFunction_open, (JSGlobalObject*))\n'))
        symbols = scan_generated_symbols([directory])
        self.assertEqual(sorted(symbols), ['jsDocumentPrototypeFunction_open', 'jsDocument_URL',
                                           'setJSDocument_xmlVersion'])
        self.assertEqual(symbols['jsDocument_URL'], 'JSDocument.cpp')

    def test_the_inner_body_is_a_definition_site_too(self):
        directory = self.directory_with('JSDocument.cpp', (
            'static inline JSValue jsDocument_URLGetter(JSGlobalObject& g, JSDocument& t)\n'))
        self.assertIn('jsDocument_URLGetter', scan_generated_symbols([directory]))

    def test_an_inner_body_that_is_not_on_the_first_line_is_still_found(self):
        # The `static inline` branch is anchored with ^, and without re.MULTILINE that anchor is
        # the start of the FILE: the scan found 12,820 definitions instead of 25,063 and every
        # inner body in the tree read as never emitted.
        directory = self.directory_with('JSDocument.cpp', (
            '#include "config.h"\n'
            '\n'
            'static inline JSValue jsDocument_URLGetter(JSGlobalObject& g, JSDocument& t)\n'
            'static inline bool setJSDocument_titleSetter(JSGlobalObject& g, JSDocument& t)\n'))
        symbols = scan_generated_symbols([directory])
        self.assertIn('jsDocument_URLGetter', symbols)
        self.assertIn('setJSDocument_titleSetter', symbols)

    def test_a_declaration_is_not_a_definition(self):
        # The point of scanning definitions is that "the generator emitted nothing for this" is a
        # real finding. A forward declaration, or a mention in the property table, is not.
        directory = self.directory_with('JSDocument.cpp', (
            'static JSC_DECLARE_CUSTOM_GETTER(jsDocument_URL);\n'
            '    { "URL"_s, ..., jsDocument_URL, 0 },\n'))
        self.assertEqual(scan_generated_symbols([directory]), {})

    def test_a_directory_that_does_not_exist_is_not_an_error(self):
        self.assertEqual(scan_generated_symbols(['/no/such/directory']), {})


# -- the model ---------------------------------------------------------------------------------

class IDLModelTest(unittest.TestCase):
    def model(self, *records):
        return IDLModel.from_records(list(records))

    def names(self, declarations, kind=None):
        return sorted(declaration.name for declaration in declarations
                      if kind is None or declaration.kind == kind)

    def test_an_interfaces_own_members(self):
        model = self.model(record('/s/Document.idl', [interface(
            'Document', attributes=[attribute('title')], operations=[operation('open')])]))
        declarations = model.declarations_for('Document')
        self.assertEqual(self.names(declarations, ATTRIBUTE), ['title'])
        self.assertEqual(self.names(declarations, OPERATION), ['open'])

    def test_a_partial_interfaces_members_land_on_the_interface(self):
        model = self.model(
            record('/s/Document.idl', [interface('Document', attributes=[attribute('title')])]),
            record('/s/Document+Fullscreen.idl',
                   [interface('Document', attributes=[attribute('fullscreen')], partial=True)]))
        declarations = model.declarations_for('Document')
        self.assertEqual(self.names(declarations, ATTRIBUTE), ['fullscreen', 'title'])
        found = {d.name: d for d in declarations}
        self.assertEqual(found['fullscreen'].declared_in, 'partial interface')
        self.assertEqual(found['fullscreen'].idl_path, '/s/Document+Fullscreen.idl')

    def test_a_mixin_members_land_on_every_including_interface(self):
        # One GlobalEventHandlers.onclick declaration becomes an onclick accessor on each
        # interface that includes it, and the generator emits one function per interface, so
        # those really are separate measurements rather than one shared with several names.
        model = self.model(
            record('/s/Document.idl', [interface('Document')],
                   includes=[('Document', 'GlobalEventHandlers')]),
            record('/s/HTMLElement.idl', [interface('HTMLElement')],
                   includes=[('HTMLElement', 'GlobalEventHandlers')]),
            record('/s/GlobalEventHandlers.idl',
                   [interface('GlobalEventHandlers', attributes=[attribute('onclick')],
                              mixin=True)]))
        for name in ('Document', 'HTMLElement'):
            declarations = model.declarations_for(name)
            self.assertEqual(self.names(declarations, ATTRIBUTE), ['onclick'])
            self.assertEqual(declarations[0].declared_in, 'mixin GlobalEventHandlers')
            self.assertEqual(declarations[0].interface, name)

    def test_a_member_the_interface_redeclares_is_not_counted_twice(self):
        model = self.model(
            record('/s/Document.idl', [interface('Document', attributes=[attribute('title')])]),
            record('/s/Document+More.idl',
                   [interface('Document', attributes=[attribute('title')], partial=True)]))
        self.assertEqual(len(model.declarations_for('Document')), 1)

    def test_overloads_are_one_declaration_carrying_the_count(self):
        model = self.model(record('/s/Canvas.idl', [interface(
            'CanvasRenderingContext2D',
            operations=[operation('drawImage', arguments=3), operation('drawImage', arguments=5),
                        operation('drawImage', arguments=9)])]))
        declarations = model.declarations_for('CanvasRenderingContext2D')
        self.assertEqual(len(declarations), 1)
        self.assertEqual(declarations[0].overload_count, 3)

    def test_an_anonymous_stringifier_is_named_toString(self):
        # `stringifier;` with no identifier is emitted as toString(), which is a name a test can
        # call, so dropping it would lose a measurable declaration.
        model = self.model(record('/s/DOMTokenList.idl', [interface(
            'DOMTokenList', operations=[operation('', stringifier=True)])]))
        self.assertEqual(self.names(model.declarations_for('DOMTokenList')), ['toString'])

    def test_an_operation_with_no_name_and_no_stringifier_is_not_a_declaration(self):
        model = self.model(record('/s/NodeList.idl', [interface(
            'NodeList', operations=[operation('')])]))
        self.assertEqual(model.declarations_for('NodeList'), [])

    def test_a_constructor_is_a_declaration_of_its_own(self):
        model = self.model(record('/s/Event.idl', [interface(
            'Event', constructors=[operation(None)])]))
        declarations = model.declarations_for('Event')
        self.assertEqual([d.kind for d in declarations], [CONSTRUCTOR])
        self.assertEqual(declarations[0].name, 'constructor')

    def test_a_legacy_factory_function_is_a_second_constructor_declaration(self):
        # Image() and new HTMLImageElement() are emitted as two different class templates with
        # counters of their own, and for HTMLImageElement only the factory exists. Reporting them
        # as one declaration would let a test of Image() stand in for the other.
        model = self.model(record('/s/HTMLImageElement.idl', [interface(
            'HTMLImageElement',
            constructors=[operation('LegacyFactoryFunction',
                                    extended_attributes={'LegacyFactoryFunction': 'Image'}),
                          operation(None)])]))
        declarations = model.declarations_for('HTMLImageElement')
        self.assertEqual(sorted((d.name, d.placement) for d in declarations),
                         [('Image', 'JSDOMLegacyFactoryFunction'),
                          ('constructor', 'JSDOMConstructor')])

    def test_a_conditional_accumulates_from_interface_construct_and_member(self):
        model = self.model(record('/s/X.idl', [
            interface('X', extended_attributes={'Conditional': 'A'}),
            interface('X', partial=True, extended_attributes={'Conditional': 'B'},
                      attributes=[attribute('y', extended_attributes={'Conditional': 'C'})])]))
        declaration = model.declarations_for('X')[0]
        self.assertEqual(sorted(declaration.conditionals), ['A', 'B', 'C'])

    def test_a_generator_fixture_partial_does_not_reach_a_real_interface(self):
        # bindings/scripts/test/AudioWorkletGlobalScopeConstructors.idl declares a partial
        # AudioWorkletGlobalScope. Filtering only on the main declaration's path let its one
        # attribute into a real interface, where it was the only declaration in the whole
        # checkout this module could find no symbol for.
        fixture = os.path.join('/s', 'bindings', 'scripts', 'test', 'Constructors.idl')
        model = self.model(
            record('/s/AudioWorkletGlobalScope.idl', [interface('AudioWorkletGlobalScope')]),
            record(fixture, [interface('AudioWorkletGlobalScope', partial=True,
                                       attributes=[attribute('ExposedStar')])]))
        self.assertEqual(model.declarations_for('AudioWorkletGlobalScope'), [])

    def test_a_parse_failure_is_recorded_rather_than_dropped(self):
        model = self.model(record('/s/Broken.idl', error='syntax error near }'))
        self.assertEqual(model.parse_errors, [('/s/Broken.idl', 'syntax error near }')])

    def test_a_callback_interface_is_not_measurable(self):
        model = self.model(record('/s/EventListener.idl', [interface(
            'EventListener', callback=True, operations=[operation('handleEvent')])]))
        self.assertEqual(model.measurable_interface_names(), [])

    def test_test_support_idl_is_out_of_scope_unless_asked_for(self):
        path = os.path.join('/s', 'Source', 'WebCore', 'testing', 'Internals.idl')
        model = self.model(record(path, [interface('Internals')]))
        self.assertEqual(model.measurable_interface_names(), [])
        self.assertEqual(model.measurable_interface_names(include_test_support=True),
                         ['Internals'])

    def test_the_generated_internal_settings_idl_is_test_support_despite_its_path(self):
        # It is generated from Settings.yaml into the build's DerivedSources, so no source-path
        # fragment finds it, and its 710 setters were the largest unattributed bucket until it
        # was excluded -- for a reason that described the export, not the tests.
        path = '/build/WebCore/DerivedSources/InternalSettingsGenerated.idl'
        model = self.model(record(path, [interface('InternalSettingsGenerated')]))
        self.assertEqual(model.measurable_interface_names(), [])

    def test_unnamed_members_are_counted_rather_than_ignored(self):
        model = self.model(record('/s/NodeList.idl', [interface(
            'NodeList', iterable=True, anonymous_operations=2)]))
        self.assertEqual(model.unenumerated_declarations_for('NodeList'),
                         {'iterable': 1, 'anonymous special operation': 2})


class CandidateSymbolsTest(unittest.TestCase):
    def declaration(self, **kwargs):
        model = IDLModel.from_records([record('/s/X.idl', [interface('Document', **kwargs)])])
        return model.declarations_for('Document')[0]

    def test_a_getter_offers_the_trampoline_and_the_body(self):
        declaration = self.declaration(attributes=[attribute('URL', read_only=True)])
        self.assertEqual(candidate_symbols(declaration, GET)[:2],
                         ['jsDocument_URL', 'jsDocument_URLGetter'])

    def test_a_getter_also_offers_the_constructor_typed_spelling(self):
        # `attribute HTMLImageElementConstructor Image` on a global appends Constructor AFTER
        # the member name, which is a different string from the static-attribute spelling.
        declaration = self.declaration(attributes=[attribute('Image', read_only=True)])
        self.assertIn('jsDocument_ImageConstructor', candidate_symbols(declaration, GET))

    def test_an_operation_offers_the_overload_dispatcher_and_the_numbered_bodies(self):
        declaration = self.declaration(operations=[operation('drawImage'),
                                                   operation('drawImage')])
        candidates = candidate_symbols(declaration, CALL)
        self.assertIn('jsDocumentPrototypeFunction_drawImageOverloadDispatcher', candidates)
        self.assertIn('jsDocumentPrototypeFunction_drawImage1Body', candidates)
        self.assertIn('jsDocumentPrototypeFunction_drawImage2Body', candidates)

    def test_a_readonly_attribute_has_no_setter_role(self):
        from webkitpy.coverage_bindings import roles_for

        declaration = self.declaration(attributes=[attribute('URL', read_only=True)])
        # candidate_symbols() will still name one if asked, which is why roles_for() is what
        # decides whether the setter is measured at all.
        self.assertEqual(candidate_symbols(declaration, SET),
                         ['setJSDocument_URL', 'setJSDocument_URLSetter'])
        self.assertEqual(roles_for(declaration), (GET,))

    def test_a_replaceable_readonly_attribute_does_have_a_setter(self):
        from webkitpy.coverage_bindings import roles_for
        declaration = self.declaration(attributes=[attribute(
            'status', read_only=True, extended_attributes={'Replaceable': 'VALUE_IS_MISSING'})])
        self.assertEqual(roles_for(declaration), (GET, SET))


# -- the join ----------------------------------------------------------------------------------

class JoinTest(unittest.TestCase):
    def join(self, coverage, generated=None, features=('ENABLE_VIDEO',), classes=None):
        return BindingsCoverageJoin(coverage, generated or {}, frozenset(features), classes)

    def declaration(self, **kwargs):
        model = IDLModel.from_records([record('/s/X.idl', [interface('Document', **kwargs)])])
        return model.declarations_for('Document')[0]

    def test_a_nonzero_counter_is_executed(self):
        declaration = self.declaration(attributes=[attribute('URL', read_only=True)])
        result = self.join(coverage_from({'jsDocument_URL': 12})).result_for(declaration)
        self.assertEqual(result.status, EXECUTED)
        self.assertEqual(result.accessors[0].count, 12)

    def test_a_zero_counter_is_never_executed(self):
        declaration = self.declaration(attributes=[attribute('URL', read_only=True)])
        result = self.join(coverage_from({'jsDocument_URL': 0})).result_for(declaration)
        self.assertEqual(result.status, NEVER_EXECUTED)

    def test_the_body_counts_when_the_trampoline_does_not_exist(self):
        # The two agree in practice but not by construction: an attribute reached through a
        # DOMJIT fast path can have its body run without the trampoline being entered.
        declaration = self.declaration(attributes=[attribute('URL', read_only=True)])
        result = self.join(coverage_from({'jsDocument_URLGetter': 4})).result_for(declaration)
        self.assertEqual(result.status, EXECUTED)
        self.assertEqual(result.accessors[0].symbol, 'jsDocument_URLGetter')

    def test_an_executed_getter_and_a_never_executed_setter_is_still_executed(self):
        declaration = self.declaration(attributes=[attribute('title')])
        result = self.join(coverage_from({'jsDocument_title': 9,
                                          'setJSDocument_title': 0})).result_for(declaration)
        self.assertEqual(result.status, EXECUTED)
        self.assertEqual([(a.role, a.state) for a in result.accessors],
                         [(GET, EXECUTED), (SET, NEVER_EXECUTED)])

    def test_a_declaration_with_no_measurable_accessor_is_unattributed(self):
        declaration = self.declaration(attributes=[attribute('title')])
        result = self.join(coverage_from()).result_for(declaration)
        self.assertEqual(result.status, UNATTRIBUTED)
        self.assertEqual(result.reason, 'symbol-not-emitted')

    def test_a_delegated_attribute_is_unattributed_even_though_the_shared_accessor_ran(self):
        # The single most important case. The shared accessor is almost always nonzero, so
        # reading its count would report every CSS property as covered on the strength of one
        # measurement that says nothing about any of them.
        delegated = attribute('-apple-color-filter', extended_attributes={
            'DelegateToSharedSyntheticAttribute': 'propertyValueForDashedIDLAttribute'})
        model = IDLModel.from_records([record(
            '/s/CSSStyleProperties.idl',
            [interface('CSSStyleProperties', attributes=[delegated])])])
        declaration = model.declarations_for('CSSStyleProperties')[0]
        coverage = coverage_from(
            {'jsCSSStyleProperties_propertyValueForDashedIDLAttribute': 33063})
        result = self.join(coverage).result_for(declaration)
        self.assertEqual(result.status, UNATTRIBUTED)
        self.assertEqual(result.reason, 'shared-synthetic-accessor')
        self.assertIn('33063', result.detail)

    def test_a_js_builtin_is_unattributed(self):
        model = IDLModel.from_records([record('/s/CompressionStream.idl', [interface(
            'CompressionStream', attributes=[attribute('readable', read_only=True)],
            extended_attributes={'JSBuiltin': 'VALUE_IS_MISSING'})])])
        declaration = model.declarations_for('CompressionStream')[0]
        result = self.join(coverage_from()).result_for(declaration)
        self.assertEqual((result.status, result.reason), (UNATTRIBUTED, 'js-builtin'))

    def test_a_conditional_that_is_off_is_unattributed_and_names_the_expression(self):
        declaration = self.declaration(
            attributes=[attribute('ontouchstart',
                                  extended_attributes={'Conditional': 'TOUCH_EVENTS'})])
        result = self.join(coverage_from()).result_for(declaration)
        self.assertEqual((result.status, result.reason), (UNATTRIBUTED, 'conditional-off'))
        self.assertEqual(result.detail, 'TOUCH_EVENTS')

    def test_a_conditional_that_is_on_does_not_excuse_a_missing_symbol(self):
        declaration = self.declaration(
            attributes=[attribute('video', extended_attributes={'Conditional': 'VIDEO'})])
        result = self.join(coverage_from()).result_for(declaration)
        self.assertEqual(result.reason, 'symbol-not-emitted')

    def test_an_interface_the_build_generated_no_class_for(self):
        declaration = self.declaration(attributes=[attribute('title')])
        result = self.join(coverage_from(), classes=frozenset(('JSElement',))
                           ).result_for(declaration)
        self.assertEqual((result.status, result.reason), (UNATTRIBUTED, 'interface-not-built'))

    def test_a_symbol_in_a_file_with_no_records_blames_the_export(self):
        # JSInternalSettingsGenerated.cpp goes into libWebCoreTestSupport.dylib, not
        # WebCore.framework, so nothing in it has a record. That is a fact about which binaries
        # were exported and not about the tests, and it must not read as a mapping error.
        declaration = self.declaration(operations=[operation('setFooEnabled')])
        generated = {'jsDocumentPrototypeFunction_setFooEnabled': 'JSDocument.cpp'}
        result = self.join(coverage_from(source_files=('JSElement.cpp',)),
                           generated=generated).result_for(declaration)
        self.assertEqual(result.reason, 'not-in-an-exported-binary')

    def test_a_symbol_whose_file_has_records_but_it_does_not(self):
        declaration = self.declaration(operations=[operation('setFooEnabled')])
        generated = {'jsDocumentPrototypeFunction_setFooEnabled': 'JSDocument.cpp'}
        result = self.join(coverage_from(source_files=('JSDocument.cpp',)),
                           generated=generated).result_for(declaration)
        self.assertEqual(result.reason, 'no-coverage-record')

    def test_a_mixin_member_exposed_elsewhere_is_not_a_mapping_error(self):
        # NavigatorID.vendor is [Exposed=Window], so it reaches Navigator and not
        # WorkerNavigator even though both include the mixin.
        model = IDLModel.from_records([
            record('/s/WorkerNavigator.idl', [interface('WorkerNavigator')],
                   includes=[('WorkerNavigator', 'NavigatorID')]),
            record('/s/NavigatorID.idl', [interface(
                'NavigatorID', mixin=True,
                attributes=[attribute('vendor', read_only=True,
                                      extended_attributes={'Exposed': 'Window'})])])])
        declaration = model.declarations_for('WorkerNavigator')[0]
        result = self.join(coverage_from()).result_for(declaration)
        self.assertEqual(result.reason, 'not-exposed-on-this-global')

    def test_a_constructor_is_measured_through_its_template(self):
        model = IDLModel.from_records([record('/s/Event.idl', [interface(
            'Event', constructors=[operation(None)])])])
        declaration = model.declarations_for('Event')[0]
        coverage = coverage_from(constructors={('JSDOMConstructor', 'JSEvent'): 6})
        result = self.join(coverage).result_for(declaration)
        self.assertEqual((result.status, result.accessors[0].role), (EXECUTED, CONSTRUCT))

    def test_a_legacy_factory_function_is_not_measured_by_the_ordinary_constructor(self):
        model = IDLModel.from_records([record('/s/HTMLImageElement.idl', [interface(
            'HTMLImageElement',
            constructors=[operation('LegacyFactoryFunction',
                                    extended_attributes={'LegacyFactoryFunction': 'Image'})])])])
        declaration = model.declarations_for('HTMLImageElement')[0]
        ordinary = coverage_from(
            constructors={('JSDOMConstructor', 'JSHTMLImageElement'): 99})
        self.assertEqual(self.join(ordinary).result_for(declaration).status, UNATTRIBUTED)
        factory = coverage_from(
            constructors={('JSDOMLegacyFactoryFunction', 'JSHTMLImageElement'): 1})
        self.assertEqual(self.join(factory).result_for(declaration).status, EXECUTED)

    def test_overload_bodies_are_counted_and_not_attributed(self):
        declaration = self.declaration(operations=[operation('drawImage'),
                                                   operation('drawImage'),
                                                   operation('drawImage')])
        coverage = coverage_from({'jsDocumentPrototypeFunction_drawImage': 4,
                                  'jsDocumentPrototypeFunction_drawImage1Body': 4,
                                  'jsDocumentPrototypeFunction_drawImage2Body': 0,
                                  'jsDocumentPrototypeFunction_drawImage3Body': 0})
        result = self.join(coverage).result_for(declaration)
        self.assertEqual(result.status, EXECUTED)
        self.assertEqual(len(result.overload_bodies), 3)
        self.assertEqual(len(result.never_executed_overloads), 2)


# -- totals and reporting ----------------------------------------------------------------------

class TotalsTest(unittest.TestCase):
    def results(self):
        model = IDLModel.from_records([record('/s/Document.idl', [interface(
            'Document',
            attributes=[attribute('title'), attribute('missing', read_only=True),
                        attribute('guarded', read_only=True,
                                  extended_attributes={'Conditional': 'TOUCH_EVENTS'})],
            operations=[operation('open'), operation('close')])])])
        coverage = coverage_from({'jsDocument_title': 3, 'setJSDocument_title': 0,
                                  'jsDocument_missing': 0,
                                  'jsDocumentPrototypeFunction_open': 5,
                                  'jsDocumentPrototypeFunction_close': 0})
        join = BindingsCoverageJoin(coverage, {}, frozenset(('ENABLE_VIDEO',)))
        return join_interfaces(model, join, ['Document'])

    def test_the_three_states_partition_the_declarations(self):
        totals = BindingsCoverageTotals(self.results())
        self.assertEqual(totals.declared(ATTRIBUTE), 3)
        self.assertEqual(totals.count(ATTRIBUTE, EXECUTED), 1)
        self.assertEqual(totals.count(ATTRIBUTE, NEVER_EXECUTED), 1)
        self.assertEqual(totals.count(ATTRIBUTE, UNATTRIBUTED), 1)
        self.assertEqual(totals.measurable(ATTRIBUTE), 2)

    def test_unattributed_is_never_added_to_never_executed(self):
        # Because a declaration with no coverage mapping has no denominator, so counting it as
        # a gap would invent a number -- the rule generate-coverage-report applies to a file
        # this configuration never compiled.
        totals = BindingsCoverageTotals(self.results())
        self.assertEqual(totals.total_never_executed(), 2)
        self.assertEqual(totals.total_unattributed(), 1)
        self.assertEqual(totals.total_measurable() + totals.total_unattributed(),
                         totals.total_declared())

    def test_an_interface_with_a_gap_is_counted_once(self):
        totals = BindingsCoverageTotals(self.results())
        self.assertEqual((totals.interfaces, totals.interfaces_with_a_gap), (1, 1))

    def test_a_setter_gap_is_counted_separately_from_a_never_executed_declaration(self):
        totals = BindingsCoverageTotals(self.results())
        self.assertEqual(totals.setter_gaps, 1)

    def test_every_reason_a_total_can_report_has_a_label_and_an_explanation(self):
        for reason in REASON_ORDER:
            self.assertIn(reason, REASON_LABELS)
            self.assertIn(reason, REASON_EXPLANATIONS)


class ReportTest(unittest.TestCase):
    def results(self, count=0):
        model = IDLModel.from_records([record('/s/Document.idl', [interface(
            'Document', attributes=[attribute('title')],
            operations=[operation('open'), operation('write'), operation('write')])])])
        coverage = coverage_from({'jsDocument_title': 7, 'setJSDocument_title': 0,
                                  'jsDocumentPrototypeFunction_open': count,
                                  'jsDocumentPrototypeFunction_write': 1})
        join = BindingsCoverageJoin(coverage, {}, frozenset())
        return join_interfaces(model, join, ['Document'])

    def test_a_never_executed_declaration_is_named_with_its_interface(self):
        lines = never_executed_lines(self.results())
        self.assertTrue(lines[0].startswith('Document '))
        self.assertIn('Document.open()', '\n'.join(lines))

    def test_the_role_that_was_never_executed_is_named(self):
        self.assertIn('call', '\n'.join(never_executed_lines(self.results())))

    def test_an_interface_with_no_gap_is_not_listed_at_all(self):
        self.assertEqual(never_executed_lines(self.results(count=1)), [])

    def test_an_overload_count_is_shown_in_the_signature(self):
        results = self.results()
        write = [member for member in results[0].members
                 if member.declaration.name == 'write'][0]
        self.assertIn('[2 overloads]', member_signature(write))

    def test_a_setter_gap_is_listed_under_its_interface(self):
        self.assertEqual(setter_gap_lines(self.results()),
                         ['Document', '    Document.title'])

    def test_a_selective_scope_marks_every_percentage_as_a_lower_bound(self):
        # A zero counter in a subset run means "no test in this run", not "no test exists", so a
        # number printed unqualified would be quoted as though it were exact.
        scope = CoverageScope.selective(['fast/dom'], tests_run=10, tests_in_suite=1000)
        text = '\n'.join(totals_lines(BindingsCoverageTotals(self.results()), scope))
        self.assertIn('≥ ', text)
        self.assertNotIn(' 66.67%', text)

    def test_a_full_suite_scope_prints_a_bare_percentage(self):
        text = '\n'.join(totals_lines(BindingsCoverageTotals(self.results()),
                                      CoverageScope.full_suite()))
        self.assertNotIn('≥', text)

    def test_the_unattributed_section_explains_every_reason_it_reports(self):
        model = IDLModel.from_records([record('/s/Document.idl', [interface(
            'Document', attributes=[attribute('guarded', read_only=True, extended_attributes={
                'Conditional': 'TOUCH_EVENTS'})])])])
        join = BindingsCoverageJoin(coverage_from(), {}, frozenset())
        totals = BindingsCoverageTotals(join_interfaces(model, join, ['Document']))
        text = '\n'.join(unattributed_lines(totals))
        self.assertIn(REASON_LABELS['conditional-off'], text)
        self.assertIn(REASON_EXPLANATIONS['conditional-off'], text)

    def test_the_tsv_has_a_row_per_accessor_and_a_header(self):
        rows = list(tsv_rows(self.results()))
        self.assertEqual(rows[0][:4], ('interface', 'kind', 'name', 'status'))
        # title has two accessors, open and write one each: four rows plus the header.
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))

    def test_every_tsv_field_is_a_string_so_the_file_can_be_written_directly(self):
        for row in tsv_rows(self.results()):
            for field in row:
                self.assertIsInstance(field, str)


# -- the checkout's own IDL parser --------------------------------------------------------------

def _checkout_root():
    try:
        return WebKitFinder(FileSystem()).webkit_base()
    except Exception:
        return None


def _perl_can_load_the_idl_parser(source_root):
    import subprocess
    scripts = os.path.join(source_root, 'Source', 'WebCore', 'bindings', 'scripts')
    if not os.path.exists(os.path.join(scripts, 'IDLParser.pm')):
        return False
    try:
        return subprocess.run(['perl', '-I', scripts, '-e', 'use IDLParser; use JSON::PP;'],
                              capture_output=True).returncode == 0
    except OSError:
        return False


_SOURCE_ROOT = _checkout_root()
_HAVE_PARSER = bool(_SOURCE_ROOT) and _perl_can_load_the_idl_parser(_SOURCE_ROOT)


@unittest.skipUnless(_HAVE_PARSER, 'needs perl and the checkout\'s IDLParser.pm')
class ParseIDLFilesTest(unittest.TestCase):
    """Drives the real parser, because the alternative was a second WebIDL parser to keep in
    step with the one the build uses."""

    def write_idl(self, text):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, 'TestBindingsCoverage.idl')
        with open(path, 'w') as handle:
            handle.write(text)
        return path

    def parse(self, text):
        return parse_idl_files([self.write_idl(text)], _SOURCE_ROOT)[0]

    def test_an_interface_with_an_attribute_and_an_operation(self):
        parsed = self.parse('''
            [Exposed=Window] interface Widget {
                readonly attribute DOMString name;
                attribute long size;
                undefined poke(long times);
            };
        ''')
        self.assertNotIn('error', parsed)
        widget = parsed['interfaces'][0]
        self.assertEqual(widget['name'], 'Widget')
        self.assertEqual([(a['name'], a['is_read_only']) for a in widget['attributes']],
                         [('name', 1), ('size', 0)])
        self.assertEqual([o['name'] for o in widget['operations']], ['poke'])
        self.assertEqual(widget['operations'][0]['argument_count'], 1)

    def test_extended_attributes_survive_the_round_trip(self):
        parsed = self.parse('''
            [Conditional=VIDEO, Exposed=Window] interface Widget {
                [Conditional=WEB_AUDIO] readonly attribute DOMString name;
            };
        ''')
        widget = parsed['interfaces'][0]
        self.assertEqual(widget['extended_attributes']['Conditional'], 'VIDEO')
        self.assertEqual(widget['attributes'][0]['extended_attributes']['Conditional'],
                         'WEB_AUDIO')

    def test_a_partial_interface_and_a_mixin_and_an_includes_statement(self):
        parsed = self.parse('''
            [Exposed=Window] interface Widget { };
            partial interface Widget { readonly attribute long extra; };
            interface mixin Pokeable { undefined poke(); };
            Widget includes Pokeable;
        ''')
        kinds = {(i['name'], i['is_partial'], i['is_mixin']) for i in parsed['interfaces']}
        self.assertIn(('Widget', 1, 0), kinds)
        self.assertIn(('Pokeable', 0, 1), kinds)
        self.assertEqual(parsed['includes'],
                         [{'interface': 'Widget', 'mixin': 'Pokeable'}])

    def test_a_constructor_and_a_legacy_factory_function(self):
        parsed = self.parse('''
            [Exposed=Window, LegacyFactoryFunction=Widgetify(long size)]
            interface Widget { constructor(); };
        ''')
        names = {c['name'] for c in parsed['interfaces'][0]['constructors']}
        self.assertIn('LegacyFactoryFunction', names)

    def test_iterable_and_maplike_are_flagged(self):
        parsed = self.parse('''
            [Exposed=Window] interface Widget { iterable<DOMString>; };
        ''')
        self.assertEqual(parsed['interfaces'][0]['has_iterable'], 1)

    def test_a_file_that_does_not_parse_is_reported_and_not_dropped(self):
        # A parse failure is a hole in the denominator, so it has to come back as a record with
        # an error rather than as a missing line, which nothing downstream could notice.
        parsed = self.parse('interface Widget { this is not IDL };')
        self.assertIn('error', parsed)
        self.assertTrue(parsed['error'].strip())

    def test_a_batch_returns_one_record_per_file_in_order(self):
        paths = [self.write_idl('[Exposed=Window] interface One { };'),
                 self.write_idl('[Exposed=Window] interface Two { };')]
        parsed = parse_idl_files(paths, _SOURCE_ROOT)
        self.assertEqual([entry['file'] for entry in parsed], paths)

    def test_the_real_documents_idl_maps_onto_the_symbols_the_build_emitted(self):
        """End to end on the checkout: Document.idl -> jsDocument_URL and friends.

        The one test that would catch the naming rules drifting away from CodeGeneratorJS.pm,
        because it takes the IDL from the tree rather than from a fixture. Every expected symbol
        was read out of WebKitBuild/cmake-mac/Coverage/WebCore/DerivedSources/JSDocument.cpp, and
        every one of them is declared in Document.idl itself -- getElementById is not, because it
        comes from the NonElementParentNode mixin, and asserting on it here would be asserting
        that this test's scope includes a file it does not read.
        """
        from webkitpy.coverage_bindings import roles_for

        path = os.path.join(_SOURCE_ROOT, 'Source', 'WebCore', 'dom', 'Document.idl')
        if not os.path.exists(path):
            self.skipTest('Document.idl has moved')
        model = IDLModel.from_records(parse_idl_files([path], _SOURCE_ROOT))
        symbols = set()
        for declaration in model.declarations_for('Document'):
            for role in roles_for(declaration):
                symbols.update(candidate_symbols(declaration, role))
        for expected in ('jsDocument_URL',
                         'jsDocument_URLGetter',
                         'jsDocument_xmlVersion',
                         'setJSDocument_xmlVersion',
                         'setJSDocument_xmlVersionSetter',
                         'jsDocumentPrototypeFunction_createElement',
                         'jsDocumentPrototypeFunction_createElementBody',
                         'jsDocumentConstructorFunction_parseHTMLUnsafe'):
            self.assertIn(expected, symbols)

    def test_a_readonly_attribute_of_the_real_document_gets_no_setter_symbol(self):
        # The complement of the test above: a name the generator does NOT emit must not be in the
        # candidate set, or "never executed" would be reported against a symbol that cannot exist.
        from webkitpy.coverage_bindings import roles_for

        path = os.path.join(_SOURCE_ROOT, 'Source', 'WebCore', 'dom', 'Document.idl')
        if not os.path.exists(path):
            self.skipTest('Document.idl has moved')
        model = IDLModel.from_records(parse_idl_files([path], _SOURCE_ROOT))
        url = [d for d in model.declarations_for('Document')
               if d.kind == ATTRIBUTE and d.name == 'URL']
        self.assertEqual(len(url), 1)
        self.assertEqual(roles_for(url[0]), (GET,))


if __name__ == '__main__':
    unittest.main()

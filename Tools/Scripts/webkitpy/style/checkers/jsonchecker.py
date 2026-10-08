# Copyright (C) 2011-2023 Apple Inc. All rights reserved.
# Copyright (C) 2025-2026 Samuel Weinig <sam@webkit.org>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1.  Redistributions of source code must retain the above copyright
#     notice, this list of conditions and the following disclaimer.
# 2.  Redistributions in binary form must reproduce the above copyright
#     notice, this list of conditions and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY APPLE INC. AND ITS CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL APPLE INC. OR ITS CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Checks WebKit style for JSON files."""

import json
import os
import re
from collections import defaultdict


class JSONChecker(object):
    """Processes JSON lines for checking style."""

    categories = set(('json/syntax',))

    def __init__(self, file_path, handle_style_error):
        self._file_path = file_path
        self._handle_style_error = handle_style_error
        self._handle_style_error.turn_off_line_filtering()

    def check(self, lines, line_numbers=None):
        try:
            json.loads('\n'.join(lines) + '\n')
        except ValueError as e:
            self._handle_style_error(self.line_number_from_json_exception(e), 'json/syntax', 5, str(e))

    @staticmethod
    def line_number_from_json_exception(error):
        match = re.search(r': line (?P<line>\d+) column \d+', str(error))
        if not match:
            return 0
        return int(match.group('line'))


class JSONContributorsChecker(JSONChecker):
    """Processes contributors.json lines"""

    def check(self, lines, line_numbers=None):
        super(JSONContributorsChecker, self).check(lines)
        self._handle_style_error(0, 'json/syntax', 5, 'contributors.json should not be modified through the commit queue')


class JSONFeaturesChecker(JSONChecker):
    """Processes the features.json lines"""

    def check(self, lines, line_numbers=None):
        super(JSONFeaturesChecker, self).check(lines)

        try:
            features_definition = json.loads('\n'.join(lines) + '\n')
            if 'features' not in features_definition:
                self._handle_style_error(0, 'json/syntax', 5, '"features" key not found, the key is mandatory.')
                return

            specification_name_set = set()
            if 'specification' in features_definition:
                previous_specification_name = ''
                for specification_object in features_definition['specification']:
                    if 'name' not in specification_object or not specification_object['name']:
                        self._handle_style_error(0, 'json/syntax', 5, 'The "name" field is mandatory for specifications.')
                        continue
                    name = specification_object['name']

                    if name < previous_specification_name:
                        self._handle_style_error(0, 'json/syntax', 5, 'The specifications should be sorted alphabetically by name, "%s" appears after "%s".' % (name, previous_specification_name))
                    previous_specification_name = name

                    specification_name_set.add(name)
                    if 'url' not in specification_object or not specification_object['url']:
                        self._handle_style_error(0, 'json/syntax', 5, 'The specifciation "%s" does not have an URL' % name)
                        continue

            features_list = features_definition['features']
            previous_feature_name = ''
            for i in range(len(features_list)):
                feature = features_list[i]
                feature_name = 'Feature %s' % i
                if 'name' not in feature or not feature['name']:
                    self._handle_style_error(0, 'json/syntax', 5, 'The feature %d does not have the mandatory field "name".' % i)
                else:
                    feature_name = feature['name']

                    if feature_name < previous_feature_name:
                        self._handle_style_error(0, 'json/syntax', 5, 'The features should be sorted alphabetically by name, "%s" appears after "%s".' % (feature_name, previous_feature_name))
                    previous_feature_name = feature_name

                if 'status' not in feature or not feature['status']:
                    self._handle_style_error(0, 'json/syntax', 5, 'The feature "%s" does not have the mandatory field "status".' % feature_name)
                if 'specification' in feature:
                    if feature['specification'] not in specification_name_set:
                        self._handle_style_error(0, 'json/syntax', 5, 'The feature "%s" has a specification field but no specification of that name exists.' % feature_name)
        except Exception as e:
            print(e)
            pass


class JSONCSSPropertiesChecker(JSONChecker):
    """Processes CSSProperties.json"""

    def check(self, lines, line_numbers=None):
        super(JSONCSSPropertiesChecker, self).check(lines)

        try:
            properties_definition = json.loads('\n'.join(lines) + '\n')

            if 'categories' not in properties_definition:
                self._handle_style_error(0, 'json/syntax', 5, '"categories" key not found, the key is mandatory.')
                return
            self._categories = properties_definition['categories']
            self.check_categories()

            if 'shared-grammar-rules' not in properties_definition:
                self._handle_style_error(0, 'json/syntax', 5, '"shared-grammar-rules" key not found, the key is mandatory.')
                return
            self._shared_grammar_rules = properties_definition['shared-grammar-rules']
            self.check_shared_grammar_rules()

            if 'descriptors' not in properties_definition:
                self._handle_style_error(0, 'json/syntax', 5, '"descriptors" key not found, the key is mandatory.')
                return
            self._descriptors = properties_definition['descriptors']
            self.check_descriptors()

            if 'properties' not in properties_definition:
                self._handle_style_error(0, 'json/syntax', 5, '"properties" key not found, the key is mandatory.')
                return
            self._properties = properties_definition['properties']
            self.check_properties()

        except Exception as e:
            print(e)
            pass

    def check_category(self, category_name, category_value):
        keys_and_validators = {
            'shortname': self.validate_string,
            'longname': self.validate_string,
            'url': self.validate_url,
            'status': self.validate_status,
        }
        for key, value in category_value.items():
            if key not in keys_and_validators:
                self._handle_style_error(0, 'json/syntax', 5, 'dictionary for category "%s" has unexpected key "%s".' % (category_name, key))
                return

            keys_and_validators[key](category_name, "", key, value)

    def check_categories(self):
        if not isinstance(self._categories, dict):
            self._handle_style_error(0, 'json/syntax', 5, '"categories" is not a dictionary.')
            return

        for key, value in self._categories.items():
            self.check_category(key, value)

    def check_shared_grammar_rule(self, rule_name, rule_value):
        keys_and_validators = {
            'aliased-to': self.validate_string,
            'comment': self.validate_comment,
            'exported': self.validate_boolean,
            'grammar': self.validate_string,
            'grammar-comment': self.validate_comment,
            'grammar-function': self.validate_string,
            'grammar-unused': self.validate_string,
            'grammar-unused-reason': self.validate_string,
            'specification': self.validate_specification,
            'status': self.validate_status,
        }
        for key, value in rule_value.items():
            if key not in keys_and_validators:
                self._handle_style_error(0, 'json/syntax', 5, 'dictionary for shared property rule "%s" has unexpected key "%s".' % (rule_name, key))
                return

            keys_and_validators[key](rule_name, "", key, value)

    def check_descriptor(self, descriptor_name, descriptor_value):
        keys_and_validators = {
            'values': self.validate_array,
            'codegen-properties': self.validate_codegen_properties,
            'status': self.validate_status,
            'specification': self.validate_specification,
        }

        for key, value in descriptor_value.items():
            if key not in keys_and_validators:
                self._handle_style_error(0, 'json/syntax', 5, 'dictionary for descriptor "%s" has unexpected key "%s".' % (property_name, key))
                return

            keys_and_validators[key](descriptor_name, "", key, value)

    def check_descriptor_kind(self, descriptor_kind, descriptors):
        if descriptor_kind[0] != "@":
            self._handle_style_error(0, 'json/syntax', 5, '"descriptors" key "%s" does not begin with an "@".' % (descriptor_kind))
            return

        if not isinstance(descriptors, dict):
            self._handle_style_error(0, 'json/syntax', 5, '"descriptors.%s" is not a dictionary.' % (descriptor_kind))
            return

        for descriptor_name, descriptor_value in descriptors.items():
            self.check_descriptor(descriptor_name, descriptor_value)

    def check_descriptors(self):
        if not isinstance(self._descriptors, dict):
            self._handle_style_error(0, 'json/syntax', 5, '"descriptors" is not a dictionary.')
            return

        for descriptor_kind, descriptors in self._descriptors.items():
            self.check_descriptor_kind(descriptor_kind, descriptors)

    def check_shared_grammar_rules(self):
        if not isinstance(self._shared_grammar_rules, dict):
            self._handle_style_error(0, 'json/syntax', 5, '"shared-grammar-rules" is not a dictionary.')
            return

        for rule_name, rule_value in self._shared_grammar_rules.items():
            self.check_shared_grammar_rule(rule_name, rule_value)

    def check_properties(self):
        if not isinstance(self._properties, dict):
            self._handle_style_error(0, 'json/syntax', 5, '"properties" is not a dictionary.')
            return

        for property_name, property_value in self._properties.items():
            self.check_property(property_name, property_value)

    def validate_type(self, property_name, property_key, key, value, expected_type):
        if not isinstance(value, expected_type):
            self._handle_style_error(0, 'json/syntax', 5, '"%s" in "%s" %s is not of %s.' % (key, property_name, property_key, expected_type))

    def validate_boolean(self, property_name, property_key, key, value):
        self.validate_type(property_name, property_key, key, value, bool)

    def validate_string(self, property_name, property_key, key, value):
        self.validate_type(property_name, property_key, key, value, str)

    def validate_array(self, property_name, property_key, key, value):
        self.validate_type(property_name, property_key, key, value, list)

    def validate_url(self, property_name, property_key, key, value):
        self.validate_string(property_name, property_key, key, value)
        # FIXME: make sure it's url-like.

    def validate_status_type(self, property_name, property_key, key, value):
        self.validate_string(property_name, property_key, key, value)

        allowed_statuses = {
            'supported',
            'in development',
            'under consideration',
            'experimental',
            'non-standard',
            'not implemented',
            'not considering',
            'obsolete',
            'removed',
        }
        if value not in allowed_statuses:
            self._handle_style_error(0, 'json/syntax', 5, 'status "%s" for property "%s" is not one of the recognized status values' % (value, property_name))

    def validate_comment(self, property_name, property_key, key, value):
        self.validate_string(property_name, property_key, key, value)

    def validate_codegen_properties(self, property_name, property_key, key, value):
        if isinstance(value, list):
            for entry in value:
                self.check_codegen_properties(property_name, entry)
        else:
            self.check_codegen_properties(property_name, value)

    def validate_logical_property_group(self, property_name, property_key, key, value):
        self.validate_type(property_name, property_key, key, value, dict)

        for subKey, value in value.items():
            if subKey in ('name', 'resolver'):
                self.validate_string(property_name, key, subKey, value)
            else:
                self._handle_style_error(0, 'json/syntax', 5, 'dictionary for "%s" of property "%s" has unexpected key "%s".' % (key, property_name, subKey))
                return

    def validate_status(self, property_name, property_key, key, value):
        if isinstance(value, dict):
            keys_and_validators = {
                'comment': self.validate_comment,
                'enabled-by-default': self.validate_boolean,
                'status': self.validate_status_type,
            }

            for key, value in value.items():
                if key not in keys_and_validators:
                    self._handle_style_error(0, 'json/syntax', 5, 'dictionary for "status" of property "%s" has unexpected key "%s".' % (property_name, key))
                    return

                keys_and_validators[key](property_name, "", key, value)
        else:
            self.validate_status_type(property_name, property_key, key, value)

    def validate_property_category(self, property_name, property_key, key, value):
        self.validate_string(property_name, property_key, key, value)

        if value not in self._categories:
            self._handle_style_error(0, 'json/syntax', 5, 'property "%s" has category "%s" which is not in the set of categories.' % (property_name, value))
            return

    def validate_specification(self, property_name, property_key, key, value):
        self.validate_type(property_name, property_key, key, value, dict)

        keys_and_validators = {
            'category': self.validate_property_category,
            'url': self.validate_url,
            'obsolete-category': self.validate_property_category,
            'obsolete-url': self.validate_url,
            'documentation-url': self.validate_url,
            'keywords': self.validate_array,
            'description': self.validate_string,
            'comment': self.validate_comment,
            'non-canonical-url': self.validate_url,
        }

        for key, value in value.items():
            if key not in keys_and_validators:
                self._handle_style_error(0, 'json/syntax', 5, 'dictionary for "specification" of property "%s" has unexpected key "%s".' % (property_name, key))
                return

            keys_and_validators[key](property_name, "specification", key, value)

            # redundant urls

    def check_property(self, property_name, value):
        keys_and_validators = {
            'animation-type': self.validate_string,
            'animation-type-comment': self.validate_comment,
            'codegen-properties': self.validate_codegen_properties,
            'comment': self.validate_comment,
            'inherited': self.validate_boolean,
            'initial': self.validate_string,
            'initial-comment': self.validate_comment,
            'specification': self.validate_specification,
            'status': self.validate_status,
            'values': self.validate_array,
        }

        for key, value in value.items():
            if key not in keys_and_validators:
                self._handle_style_error(0, 'json/syntax', 5, 'dictionary for property "%s" has unexpected key "%s".' % (property_name, key))
                return

            keys_and_validators[key](property_name, "", key, value)

    def check_codegen_properties(self, property_name, codegen_properties):
        if not isinstance(codegen_properties, (dict, list)):
            self._handle_style_error(0, 'json/syntax', 5, '"codegen-properties" for property "%s" is not a dictionary or array.' % property_name)
            return

        keys_and_validators = {
            'accepts-quirky-angle': self.validate_boolean,
            'accepts-quirky-color': self.validate_boolean,
            'accepts-quirky-length': self.validate_boolean,
            'aliases': self.validate_array,
            'animation-wrapper-acceleration': self.validate_string,
            'animation-wrapper-comment': self.validate_comment,
            'animation-wrapper-requires-additional-parameters': self.validate_array,
            'animation-wrapper-requires-getter': self.validate_string,
            'animation-wrapper-requires-non-additive-or-cumulative-interpolation': self.validate_boolean,
            'animation-wrapper-requires-non-normalized-discrete-interpolation': self.validate_boolean,
            'animation-wrapper-requires-override-parameters': self.validate_array,
            'animation-wrapper-requires-setter': self.validate_string,
            'animation-wrapper': self.validate_string,
            'applies-to-highlight-pseudo-elements': self.validate_string,
            'cascade-alias': self.validate_string,
            'color-property-traits-color-custom': self.validate_boolean,
            'color-property-traits-requires-excludes-visited-link-color': self.validate_boolean,
            'color-property-traits-requires-resolving-current-color': self.validate_boolean,
            'color-property-traits-visited-link-color-custom': self.validate_boolean,
            'color-property': self.validate_boolean,
            'comment': self.validate_string,
            'computed-style-changed-for-animation-custom': self.validate_boolean,
            'computed-style-getter-constexpr': self.validate_boolean,
            'computed-style-getter-custom': self.validate_boolean,
            'computed-style-getter-exported': self.validate_boolean,
            'computed-style-getter-inline': self.validate_boolean,
            'computed-style-getter-nodelete': self.validate_boolean,
            'computed-style-getter': self.validate_string,
            'computed-style-has-explicitly-set-getter-custom': self.validate_boolean,
            'computed-style-has-explicitly-set-policy': self.validate_string,
            'computed-style-has-explicitly-set-setter-custom': self.validate_boolean,
            'computed-style-has-explicitly-set-storage-container': self.validate_string,
            'computed-style-has-explicitly-set-storage-name': self.validate_string,
            'computed-style-has-explicitly-set-storage-path': self.validate_array,
            'computed-style-initial-constexpr': self.validate_boolean,
            'computed-style-initial-custom': self.validate_boolean,
            'computed-style-initial-exported': self.validate_boolean,
            'computed-style-initial-inline': self.validate_boolean,
            'computed-style-initial': self.validate_string,
            'computed-style-name-for-methods': self.validate_string,
            'computed-style-resolving-current-color-applying-color-filter-exported': self.validate_boolean,
            'computed-style-resolving-current-color-exported': self.validate_boolean,
            'computed-style-setter-constexpr': self.validate_boolean,
            'computed-style-setter-custom': self.validate_boolean,
            'computed-style-setter-exported': self.validate_boolean,
            'computed-style-setter-inline': self.validate_boolean,
            'computed-style-setter-requires-did-set': self.validate_boolean,
            'computed-style-setter-returns-if-changed': self.validate_boolean,
            'computed-style-setter': self.validate_string,
            'computed-style-storage-container': self.validate_string,
            'computed-style-storage-kind': self.validate_string,
            'computed-style-storage-name': self.validate_string,
            'computed-style-storage-path': self.validate_array,
            'computed-style-type': self.validate_string,
            'computed-style-visited-dependent-applying-color-filter-exported': self.validate_boolean,
            'computed-style-visited-dependent-exported': self.validate_boolean,
            'computed-style-visited-link-getter-custom': self.validate_boolean,
            'computed-style-visited-link-resolving-current-color-applying-color-filter-exported': self.validate_boolean,
            'computed-style-visited-link-resolving-current-color-exported': self.validate_boolean,
            'computed-style-visited-link-setter-custom': self.validate_boolean,
            'computed-style-visited-link-storage-container': self.validate_string,
            'computed-style-visited-link-storage-name': self.validate_string,
            'computed-style-visited-link-storage-path': self.validate_array,
            'coordinated-value-list-property-getter': self.validate_string,
            'coordinated-value-list-property-initial': self.validate_string,
            'coordinated-value-list-property-item-type': self.validate_string,
            'coordinated-value-list-property-name-for-methods': self.validate_string,
            'coordinated-value-list-property-setter': self.validate_string,
            'coordinated-value-list-property': self.validate_boolean,
            'disables-native-appearance': self.validate_boolean,
            'enable-if': self.validate_string,
            'fast-path-inherited': self.validate_boolean,
            'font-description-getter': self.validate_string,
            'font-description-initial': self.validate_string,
            'font-description-name-for-methods': self.validate_string,
            'font-description-setter': self.validate_string,
            'font-property': self.validate_boolean,
            'high-priority': self.validate_boolean,
            'internal-only': self.validate_boolean,
            'logical-property-group': self.validate_logical_property_group,
            'longhands': self.validate_array,
            'medium-priority': self.validate_boolean,
            'parser-exported': self.validate_boolean,
            'parser-function-allows-number-or-integer-input': self.validate_boolean,
            'parser-function-comment': self.validate_comment,
            'parser-function': self.validate_string,
            'parser-grammar-comment': self.validate_comment,
            'parser-grammar-unused-comment': self.validate_comment,
            'parser-grammar-unused-reason': self.validate_string,
            'parser-grammar-unused': self.validate_string,
            'parser-grammar': self.validate_string,
            'parser-shorthand': self.validate_string,
            'runtime-flag': self.validate_string,
            'separator': self.validate_string,
            'settings-flag': self.validate_string,
            'shorthand-parser-pattern': self.validate_string,
            'shorthand-pattern-comment': self.validate_comment,
            'shorthand-pattern': self.validate_string,
            'shorthand-style-extractor-pattern': self.validate_string,
            'sink-priority': self.validate_boolean,
            'skip-codegen': self.validate_boolean,
            'skip-computed-style-getter': self.validate_boolean,
            'skip-computed-style-initial': self.validate_boolean,
            'skip-computed-style-setter': self.validate_boolean,
            'skip-computed-style': self.validate_boolean,
            'skip-parser': self.validate_boolean,
            'skip-render-style-getter': self.validate_boolean,
            'skip-render-style-setter': self.validate_boolean,
            'skip-render-style': self.validate_boolean,
            'skip-style-builder': self.validate_boolean,
            'skip-style-extractor-comment': self.validate_comment,
            'skip-style-extractor': self.validate_boolean,
            'style-builder-custom': self.validate_string,
            'style-builder-requires-system-font-shorthand-check': self.validate_boolean,
            'style-extractor-custom': self.validate_boolean,
            'top-priority-reason': self.validate_string,
            'top-priority': self.validate_boolean,
            'used-style': self.validate_string,
            'visited-link-color-support': self.validate_boolean,
        }

        for key, value in codegen_properties.items():
            if key not in keys_and_validators:
                self._handle_style_error(0, 'json/syntax', 5, 'codegen-properties for property "%s" has unexpected key "%s".' % (property_name, key))
                return

            keys_and_validators[key](property_name, 'codegen-properties', key, value)


class JSONImportExpectationsChecker(JSONChecker):
    """Processes the import-expectations.json lines"""

    def check(self, lines, line_numbers=None):
        super(JSONImportExpectationsChecker, self).check(lines)

        try:
            expectations = json.loads("\n".join(lines) + "\n")
        except ValueError:
            # Skip the rest, the parent class will have logged for this
            return

        if not isinstance(expectations, dict):
            self._handle_style_error(
                0, "json/syntax", 5, "The top-level data must be an object"
            )
            return

        # This will find all JSON strings, as quotation marks can _only_ appear in
        # strings. Thus, iterating over non-overlapping matches of this regex will give
        # all strings, and it can be applied on a per line basis as strings cannot
        # contain line breaks.
        json_string_re = re.compile(
            u'"(?:[\\x20-\\x21\\x23-\\x5B\\x5D-\U0010FFFF]|\\\\(?:[\\x22\\x5C\\x2F\\x08\\x0C\\x0A\\x0D\\x09]|u[0-9a-fA-F]{4}))*"'
        )

        string_to_lines = defaultdict(set)
        for i, line in enumerate(lines):
            for m in json_string_re.finditer(line):
                string_to_lines[json.loads(m.group(0))].add(i + 1)

        parsed_expectations = {}

        for key, value in expectations.items():
            if len(string_to_lines[key]) == 1:
                line_no = next(iter(string_to_lines[key]))
            else:
                line_no = 0

            valid = True

            # This is an assert because JSON requires it, and thus should be infallible.
            assert isinstance(key, str)

            if key != "web-platform-tests" and not key.startswith(
                "web-platform-tests/"
            ):
                self._handle_style_error(
                    line_no,
                    "json/syntax",
                    5,
                    "Each key must start with 'web-platform-tests/', got '{}'".format(
                        key
                    ),
                )
                valid = False

            if value not in ("import", "import-no-rewrite", "skip", "skip-new-directories"):
                self._handle_style_error(
                    line_no,
                    "json/syntax",
                    5,
                    'Each value must be one of "import", "import-no-rewrite", "skip", or "skip-new-directories"',
                )
                valid = False

            if valid:
                parsed_expectations[tuple(key.split("/"))] = value

        for parsed_key, value in parsed_expectations.items():
            key = "/".join(parsed_key)

            if len(string_to_lines[key]) == 1:
                line_no = next(iter(string_to_lines[key]))
            else:
                line_no = 0

            if not parsed_key[-1]:
                self._handle_style_error(
                    line_no,
                    "json/syntax",
                    5,
                    "'{}' has trailing slash".format(key),
                )

            if value != "skip-new-directories":
                for i in range(len(parsed_key) - 1, 0, -1):
                    parent_key = parsed_key[:i]
                    if parent_key in parsed_expectations:
                        if value == parsed_expectations[parent_key]:
                            self._handle_style_error(
                                line_no,
                                "json/syntax",
                                5,
                                "'{}' is redundant, '{}' already defines '{}'".format(
                                    key, "/".join(parent_key), value
                                ),
                            )
                        break

            is_skipped = value in ("skip", "skip-new-directories")
            is_prefix = any(
                parsed_key == k[: len(parsed_key)]
                and len(k) > len(parsed_key)
                and v in ("import", "import-no-rewrite")
                for k, v in parsed_expectations.items()
            )
            should_exist = not is_skipped or is_prefix

            full_path = os.path.join(
                os.path.dirname(self._file_path), "..", *parsed_key
            )
            exists = os.path.exists(full_path)
            if exists != should_exist:
                self._handle_style_error(
                    line_no,
                    "json/syntax",
                    5,
                    "'{}' does {}exist and is {}skipped".format(
                        key,
                        "" if exists else "not ",
                        "" if is_skipped else "not ",
                    ),
                )
            elif should_exist and not os.path.isdir(full_path):
                self._handle_style_error(
                    line_no,
                    "json/syntax",
                    5,
                    "'{}' is not a directory".format(key),
                )


class JSONQuirkTableChecker(JSONChecker):
    """Processes QuirkTable.json, the site-specific quirk table in Source/WebCore/page.

    Mirrors the runtime parser in QuirkTable.cpp, which remains the authority. Match pattern syntax
    is left to the parser and its API test. Behaviors come from QuirkBehaviors.yaml, build condition
    names from the BuildCondition constants in QuirkBehaviors.h, and URL environment names from the
    URLEnvironment enum in QuirkMatchPattern.h, all next to the table.
    """

    ROW_FIELDS = ('bugs', 'comment', 'matches', 'embeddedMatches', 'excludeMatches', 'queryContains', 'fragmentContains', 'environment', 'available', 'behaviors')
    PATTERN_FIELDS = ('matches', 'embeddedMatches', 'excludeMatches')
    SUBSTRING_FIELDS = ('queryContains', 'fragmentContains')
    PARAMETER_FIELDS = {'script': 'Script', 'userAgent': 'UserAgent', 'chromeCompatibilityVersion': 'ChromeCompatibilityVersion', 'cookieNames': 'CookieNames'}
    CONDITION_FIELDS = {'elementSelector': 'ElementSelector', 'secondaryURL': 'SecondaryURL', 'documentSelector': 'DocumentSelector'}
    LIST_FIELDS = ('cookieNames', 'secondaryURL')
    BEHAVIOR_FIELDS = ('id',) + tuple(PARAMETER_FIELDS) + tuple(CONDITION_FIELDS) + ('bugs', 'comment')
    BUG = re.compile(r'^(rdar://\d+|https://webkit\.org/b/\d+)$')
    AVAILABLE_EXPRESSION = re.compile(r'^\w+( (\|\||&&) \w+)*$')
    BUILD_CONDITION = re.compile(r'^constexpr bool (\w+) = (?:true|false);', re.MULTILINE)
    URL_ENVIRONMENT_ENUM = re.compile(r'enum class URLEnvironment[^{]*\{([^}]*)\}')

    def check(self, lines, line_numbers=None):
        super(JSONQuirkTableChecker, self).check(lines)
        try:
            table = json.loads('\n'.join(lines) + '\n')
        except ValueError:
            return

        directory = os.path.dirname(self._file_path)
        self._behaviors = self._read_behaviors(os.path.join(directory, 'QuirkBehaviors.yaml'))
        self._build_conditions = self._read_build_conditions(os.path.join(directory, 'QuirkBehaviors.h'))
        self._environments = self._read_environments(os.path.join(directory, 'QuirkMatchPattern.h'))
        if self._behaviors is None or self._build_conditions is None or self._environments is None:
            return

        if not isinstance(table, dict) or list(table.keys()) != ['quirks'] or not isinstance(table['quirks'], list):
            self._error('The top level must be an object whose only key is "quirks", an array of rows.')
            return

        for index, row in enumerate(table['quirks']):
            self._check_row(row, index)

    def _error(self, message):
        self._handle_style_error(0, 'json/syntax', 5, message)

    def _read_file(self, path):
        try:
            with open(path) as file:
                return file.read()
        except (IOError, OSError):
            self._error('Could not read %s, which QuirkTable.json is checked against.' % os.path.basename(path))
            return None

    def _read_behaviors(self, path):
        # QuirkBehaviors.yaml is a flat mapping of behavior IDs to simple fields; read only what the table needs.
        contents = self._read_file(path)
        if contents is None:
            return None

        behaviors = {}
        current = None
        for line in contents.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.match(r'^(\w+):\s*$', line)
            if match:
                current = behaviors[match.group(1)] = {'parameters': [], 'conditions': [], 'conditionsRequired': False}
                continue
            match = re.match(r'^\s+(\w+):\s*(.*?)\s*$', line)
            if not match or current is None:
                continue
            key, value = match.groups()
            if key in ('parameters', 'conditions'):
                current[key] = [item.strip() for item in value.strip('[]').split(',') if item.strip()]
            elif key == 'conditionsRequired':
                current[key] = value == 'true'
        return behaviors

    def _read_build_conditions(self, path):
        contents = self._read_file(path)
        if contents is None:
            return None
        return set(self.BUILD_CONDITION.findall(contents))

    def _read_environments(self, path):
        contents = self._read_file(path)
        if contents is None:
            return None
        enum = self.URL_ENVIRONMENT_ENUM.search(contents)
        return set(re.findall(r'\w+', enum.group(1))) if enum else set()

    @staticmethod
    def _is_non_empty_string(value):
        return isinstance(value, str) and bool(value)

    @classmethod
    def _is_non_empty_string_list(cls, value):
        return isinstance(value, list) and bool(value) and all(cls._is_non_empty_string(element) for element in value)

    @staticmethod
    def _describe(row, index):
        patterns = row.get('matches', row.get('embeddedMatches')) if isinstance(row, dict) else None
        if isinstance(patterns, list) and patterns and isinstance(patterns[0], str):
            return 'quirks[%d] (%s)' % (index, patterns[0])
        return 'quirks[%d]' % index

    def _check_notes(self, entry, location):
        if 'comment' in entry and not self._is_non_empty_string(entry['comment']):
            self._error('%s: "comment" must be a non-empty string.' % location)
        if 'bugs' not in entry:
            return
        bugs = entry['bugs']
        if not self._is_non_empty_string_list(bugs) or not all(self.BUG.match(bug) for bug in bugs):
            self._error('%s: "bugs" must be a non-empty array of rdar://N or https://webkit.org/b/N references.' % location)
        elif len(set(bugs)) != len(bugs):
            self._error('%s: "bugs" lists a bug more than once.' % location)

    def _check_row(self, row, index):
        location = self._describe(row, index)
        if not isinstance(row, dict):
            self._error('%s: a row must be an object.' % location)
            return

        for field in row:
            if field not in self.ROW_FIELDS:
                self._error('%s: unknown field "%s".' % (location, field))
        self._check_notes(row, location)

        for field in self.PATTERN_FIELDS:
            if field in row and not self._is_non_empty_string_list(row[field]):
                self._error('%s: "%s" must be a non-empty array of match patterns.' % (location, field))
        if 'matches' not in row and 'embeddedMatches' not in row:
            self._error('%s: a row must have "matches" or "embeddedMatches".' % location)
        for field in self.SUBSTRING_FIELDS:
            if field in row and not self._is_non_empty_string(row[field]):
                self._error('%s: "%s" must be a non-empty string.' % (location, field))

        if 'environment' in row and row['environment'] not in self._environments:
            self._error('%s: "environment" must name a URLEnvironment.' % location)

        if 'available' in row:
            available = row['available']
            if not isinstance(available, str) or not self.AVAILABLE_EXPRESSION.match(available) or any(name not in self._build_conditions for name in re.findall(r'\w+', available)):
                self._error('%s: "available" must be build condition names joined by " || " or " && ".' % location)

        behaviors = row.get('behaviors')
        if not isinstance(behaviors, list) or not behaviors:
            self._error('%s: "behaviors" must be a non-empty array.' % location)
            return
        for behavior_index, behavior in enumerate(behaviors):
            self._check_behavior(behavior, '%s.behaviors[%d]' % (location, behavior_index))

    def _check_behavior(self, entry, location):
        if not isinstance(entry, dict):
            self._error('%s: a behavior must be an object.' % location)
            return

        for field in entry:
            if field not in self.BEHAVIOR_FIELDS:
                self._error('%s: unknown field "%s".' % (location, field))
        self._check_notes(entry, location)

        behavior = self._behaviors.get(entry.get('id'))
        if behavior is None:
            self._error('%s: "id" %s is not a behavior in QuirkBehaviors.yaml.' % (location, json.dumps(entry.get('id'))))
            return
        location = '%s %s' % (location, entry['id'])

        for field in tuple(self.PARAMETER_FIELDS) + tuple(self.CONDITION_FIELDS):
            if field not in entry:
                continue
            is_list = field in self.LIST_FIELDS
            if not (self._is_non_empty_string_list(entry[field]) if is_list else self._is_non_empty_string(entry[field])):
                self._error('%s: "%s" must be a non-empty %s.' % (location, field, 'array of strings' if is_list else 'string'))

        for field, parameter in self.PARAMETER_FIELDS.items():
            if parameter in behavior['parameters'] and field not in entry:
                self._error('%s: needs "%s".' % (location, field))
            elif parameter not in behavior['parameters'] and field in entry:
                self._error('%s: does not take "%s".' % (location, field))

        for field, condition in self.CONDITION_FIELDS.items():
            if field in entry and condition not in behavior['conditions']:
                self._error('%s: does not take "%s".' % (location, field))
            elif field not in entry and behavior['conditionsRequired'] and condition in behavior['conditions']:
                self._error('%s: needs "%s".' % (location, field))

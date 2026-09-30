"""Disposable generated implementation-test suite contract tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_SCHEMA,
    MAJOR_REBUILD_GENERATION_MODE,
    GeneratedTestSuiteError,
    validate_generated_test_suite,
)

RECIPE_IDENTITY = "sha256:" + "1" * 64
SPECIFICATION_REFERENCES = ("openspec/spec.md", "cpp/openspec/spec.md")


def valid_suite() -> dict[str, object]:
    return {
        "schema": GENERATED_TEST_SUITE_SCHEMA,
        "recipe_identity": RECIPE_IDENTITY,
        "generation_mode": MAJOR_REBUILD_GENERATION_MODE,
        "cases": [
            {
                "case_id": "ordinary-example",
                "category": "example",
                "specification_refs": ["openspec/spec.md"],
                "arguments": [{"value": 7}],
                "expected_result": {"value": 14},
            },
            {
                "case_id": "zero-boundary",
                "category": "boundary",
                "specification_refs": ["openspec/spec.md", "cpp/openspec/spec.md"],
                "arguments": [{"value": 0}],
                "expected_result": {"value": 0},
            },
            {
                "case_id": "scaling-invariant",
                "category": "invariant",
                "specification_refs": ["openspec/spec.md"],
                "arguments": [{"value": 19}],
                "expected_result": {"value": 38},
            },
        ],
    }


def validate(value: dict[str, object], *, acceptance_arguments=()):
    content = json.dumps(value, sort_keys=True)
    return validate_generated_test_suite(
        content,
        recipe_identity=RECIPE_IDENTITY,
        specification_references=SPECIFICATION_REFERENCES,
        acceptance_arguments=acceptance_arguments,
    )


class GeneratedTestSuiteTests(unittest.TestCase):
    def test_valid_suite_returns_exact_content_receipt(self):
        value = valid_suite()
        content = json.dumps(value, sort_keys=True)
        result = validate_generated_test_suite(
            content,
            recipe_identity=RECIPE_IDENTITY,
            specification_references=SPECIFICATION_REFERENCES,
        )
        self.assertEqual(result.recipe_identity, RECIPE_IDENTITY)
        self.assertEqual(result.generation_mode, "major-rebuild")
        self.assertEqual(
            result.categories, frozenset({"example", "boundary", "invariant"})
        )
        self.assertEqual(
            [item.case_id for item in result.cases],
            ["ordinary-example", "zero-boundary", "scaling-invariant"],
        )
        self.assertEqual(result.cases[0].arguments, ({"value": 7},))
        self.assertEqual(
            result.content_identity,
            "sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        )

    def test_top_level_and_case_fields_are_exact(self):
        for target, key in (("suite", "extra"), ("case", "extra")):
            with self.subTest(target=target):
                value = valid_suite()
                if target == "suite":
                    value[key] = True
                else:
                    value["cases"][0][key] = True  # type: ignore[index]
                with self.assertRaises(GeneratedTestSuiteError) as raised:
                    validate(value)
                self.assertEqual(
                    raised.exception.code, "generated_tests.invalid_fields"
                )

    def test_recipe_schema_and_generation_mode_are_exact(self):
        mutations = {
            "schema": "urn:other",
            "recipe_identity": "sha256:" + "2" * 64,
            "generation_mode": "incremental",
        }
        expected_codes = {
            "schema": "generated_tests.schema_mismatch",
            "recipe_identity": "generated_tests.recipe_identity_mismatch",
            "generation_mode": "generated_tests.generation_mode_mismatch",
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                value = valid_suite()
                value[field] = replacement
                with self.assertRaises(GeneratedTestSuiteError) as raised:
                    validate(value)
                self.assertEqual(raised.exception.code, expected_codes[field])

    def test_case_count_and_all_three_categories_are_required(self):
        too_short = valid_suite()
        too_short["cases"] = too_short["cases"][:2]  # type: ignore[index]
        with self.assertRaises(GeneratedTestSuiteError) as raised:
            validate(too_short)
        self.assertEqual(raised.exception.code, "generated_tests.case_count")

        missing_category = valid_suite()
        missing_category["cases"][2]["category"] = "example"  # type: ignore[index]
        with self.assertRaises(GeneratedTestSuiteError) as raised:
            validate(missing_category)
        self.assertEqual(raised.exception.code, "generated_tests.missing_category")

    def test_case_ids_and_argument_vectors_must_each_be_unique(self):
        duplicate_id = valid_suite()
        duplicate_id["cases"][1]["case_id"] = "ordinary-example"  # type: ignore[index]
        with self.assertRaises(GeneratedTestSuiteError) as raised:
            validate(duplicate_id)
        self.assertEqual(raised.exception.code, "generated_tests.duplicate_case_id")

        duplicate_arguments = valid_suite()
        duplicate_arguments["cases"][1]["arguments"] = copy.deepcopy(  # type: ignore[index]
            duplicate_arguments["cases"][0]["arguments"]  # type: ignore[index]
        )
        with self.assertRaises(GeneratedTestSuiteError) as raised:
            validate(duplicate_arguments)
        self.assertEqual(raised.exception.code, "generated_tests.duplicate_arguments")

    def test_references_are_limited_to_current_non_acceptance_documents(self):
        value = valid_suite()
        value["cases"][0]["specification_refs"] = [  # type: ignore[index]
            "acceptance/execution.json"
        ]
        with self.assertRaises(GeneratedTestSuiteError) as raised:
            validate(value)
        self.assertEqual(
            raised.exception.code, "generated_tests.invalid_specification_refs"
        )

    def test_acceptance_argument_overlap_preserves_independent_suite_authority(self):
        value = valid_suite()
        validated = validate(
            value,
            acceptance_arguments=([{"value": 7}], [{"value": 99}]),
        )
        self.assertEqual(validated.case_ids[0], "ordinary-example")

    def test_generated_cases_cover_every_value_free_invocation_signature(self):
        value = valid_suite()
        for case in value["cases"]:  # type: ignore[union-attr]
            case["arguments"] = ["recipient", ["message"]]
        value["cases"][1]["arguments"] = ["another", []]  # type: ignore[index]
        value["cases"][2]["arguments"] = ["third", ["one", "two"]]  # type: ignore[index]

        with self.assertRaises(GeneratedTestSuiteError) as raised:
            validate(
                value,
                acceptance_arguments=([{"name": "Ada", "messages": ["Hello"]}],),
            )

        self.assertEqual(
            raised.exception.code, "generated_tests.acceptance_signature_missing"
        )
        message = str(raised.exception)
        self.assertIn('"arity":1', message)
        self.assertIn('"name"', message)
        self.assertIn('"messages"', message)
        self.assertNotIn("Ada", message)
        self.assertNotIn("Hello", message)

    def test_generated_expected_results_match_complete_execution_shape(self):
        value = valid_suite()
        value["cases"][0]["expected_result"] = {"value": 14, "invented": True}  # type: ignore[index]

        with self.assertRaises(GeneratedTestSuiteError) as raised:
            content = json.dumps(value, sort_keys=True)
            validate_generated_test_suite(
                content,
                recipe_identity=RECIPE_IDENTITY,
                specification_references=SPECIFICATION_REFERENCES,
                result_shape={"value": "integer"},
            )

        self.assertEqual(
            raised.exception.code,
            "generated_tests.expected_result_shape_mismatch",
        )

    def test_duplicate_json_object_keys_are_rejected(self):
        content = (
            '{"schema":"urn:literate-ai:schema:v1:generated-test-suite",'
            '"schema":"urn:literate-ai:schema:v1:generated-test-suite"}'
        )
        with self.assertRaises(GeneratedTestSuiteError) as raised:
            validate_generated_test_suite(
                content,
                recipe_identity=RECIPE_IDENTITY,
                specification_references=SPECIFICATION_REFERENCES,
            )
        self.assertEqual(raised.exception.code, "generated_tests.invalid_json")


if __name__ == "__main__":
    unittest.main()

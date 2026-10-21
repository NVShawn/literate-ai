from __future__ import annotations

import copy
import itertools
import json
import unittest
from unittest.mock import patch

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator
from jsonschema import Draft7Validator
from jsonschema.validators import extend

from literate_ai.adapters.dependencies import build_cyclonedx_bom, json_uniqueness
from literate_ai.adapters.dependencies.cyclonedx import _strict_validator
from literate_ai.contracts import CycloneDxLifecycle
from tests.smoke.test_cyclonedx_sbom import _managed_graph


class CycloneDxUniquenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.optimized = extend(
            Draft7Validator,
            {"uniqueItems": json_uniqueness.unique_json_items},
        )({"uniqueItems": True})
        self.original = Draft7Validator({"uniqueItems": True})

    def test_json_equality_matches_original_including_nested_values(self) -> None:
        values = [
            None,
            False,
            True,
            0,
            -0.0,
            1,
            1.0,
            2**100,
            "1",
            "é",
            "e\u0301",
            [],
            {},
            [1, True],
            [True, 1],
            {"x": 1, "y": [False]},
            {"y": [False], "x": 1.0},
            {"x": True, "y": [False]},
        ]
        for left, right in itertools.product(values, repeat=2):
            with self.subTest(left=left, right=right):
                value = [left, right]
                self.assertEqual(
                    [error.message for error in self.optimized.iter_errors(value)],
                    [error.message for error in self.original.iter_errors(value)],
                )

    def test_non_json_values_retain_original_behavior(self) -> None:
        for value in [[float("inf"), float("inf")], [(1,), (1,)], [{1: 2}, {1: 2}]]:
            with self.subTest(value=value):
                self.assertEqual(
                    self.optimized.is_valid(value), self.original.is_valid(value)
                )

    def test_disabled_keyword_and_non_arrays_are_unchanged(self) -> None:
        for value in [None, "text", {"a": 1}, 1]:
            self.assertTrue(self.optimized.is_valid(value))
        disabled = extend(
            Draft7Validator, {"uniqueItems": json_uniqueness.unique_json_items}
        )({"uniqueItems": False})
        self.assertTrue(disabled.is_valid([{}, {}]))

    def test_official_schema_registry_formats_and_globals_are_preserved(self) -> None:
        official = JsonStrictValidator(SchemaVersion.V1_7)
        selected = _strict_validator()
        self.assertEqual(selected._validator.schema, official._validator.schema)
        self.assertIs(
            selected._validator.format_checker,
            official._validator.format_checker,
        )
        self.assertEqual(
            dict(selected._validator._registry), dict(official._validator._registry)
        )
        self.assertIsNot(
            Draft7Validator.VALIDATORS["uniqueItems"],
            json_uniqueness.unique_json_items,
        )
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=_managed_graph()
        )
        document = json.loads(content)
        fixtures = [document]
        extra = copy.deepcopy(document)
        extra["unexpected"] = True
        fixtures.append(extra)
        duplicate = copy.deepcopy(document)
        duplicate["components"].append(copy.deepcopy(duplicate["components"][0]))
        fixtures.append(duplicate)
        for identifier in ["MIT", "not-an-spdx-license"]:
            licensed = copy.deepcopy(document)
            licensed["components"][0]["licenses"] = [{"license": {"id": identifier}}]
            fixtures.append(licensed)
        for fixture in fixtures:
            text = json.dumps(fixture)
            self.assertEqual(
                selected.validate_str(text, all_errors=True) is None,
                official.validate_str(text, all_errors=True) is None,
            )
        self.assertIsNone(selected.validate_str(json.dumps(fixtures[-2])))
        self.assertIsNotNone(selected.validate_str(json.dumps(fixtures[-1])))

    def test_large_object_inventory_visits_each_value_once(self) -> None:
        values = [
            {
                "path": f"include/file-{index}.h",
                "hashes": [{"alg": "SHA-256", "content": str(index)}],
            }
            for index in range(6000)
        ]
        original_key = json_uniqueness._json_key
        with patch.object(json_uniqueness, "_json_key", wraps=original_key) as observed:
            self.assertTrue(self.optimized.is_valid(values))
        self.assertEqual(observed.call_count, 6 * len(values))
        values.append(copy.deepcopy(values[0]))
        self.assertFalse(self.optimized.is_valid(values))


if __name__ == "__main__":
    unittest.main()

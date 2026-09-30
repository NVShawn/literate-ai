"""Real fractional product invocation, exact comparison and identity compatibility."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.lifecycle import (
    LocalIndependentAcceptanceCase,
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.product_json import (
    product_json_bytes,
    product_json_identity,
    product_json_values_equal,
)


class ProductJsonAcceptanceTests(unittest.TestCase):
    def test_integer_only_bytes_and_case_identities_remain_identical(self):
        arguments = [{"unicode": "λ", "values": [1, -2, True, None]}, 7]
        result = {"sum": 5}
        self.assertEqual(product_json_bytes(arguments), canonical_json_bytes(arguments))
        self.assertEqual(
            product_json_identity(arguments), canonical_identity(arguments)
        )
        case = LocalIndependentAcceptanceCase.create("legacy", arguments, result)
        self.assertEqual(
            case.identity,
            canonical_identity(
                {
                    "schema": "literate-ai/local-independent-acceptance-case@1",
                    "case_id": "legacy",
                    "arguments_identity": canonical_identity(arguments).uri,
                    "expected_result_identity": canonical_identity(result).uri,
                }
            ),
        )

    def test_fractional_values_stable_reordered_and_distinct_when_changed(self):
        value = {"z": 1.5, "a": [-0.0, 1e-100]}
        self.assertEqual(json.loads(product_json_bytes(value)), value)
        self.assertEqual(
            product_json_identity(value),
            product_json_identity({"a": [-0.0, 1e-100], "z": 1.5}),
        )
        self.assertNotEqual(
            product_json_identity(value), product_json_identity({**value, "z": 1.25})
        )
        self.assertNotEqual(product_json_identity(-0.0), product_json_identity(0.0))
        with self.assertRaises(ValueError):
            canonical_json_bytes(value)

    def test_nonfinite_or_invalid_nested_values_fail_declaration(self):
        for value in (
            float("nan"),
            float("inf"),
            -float("inf"),
            {1: 2},
            b"bytes",
            2**63,
        ):
            for arguments, result in (
                ([{"nested": [value]}], True),
                ([], {"nested": [value]}),
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    LocalIndependentAcceptanceCase.create("invalid", arguments, result)

    def test_numeric_comparison_is_exact_without_rewriting_identities(self):
        self.assertTrue(product_json_values_equal({"v": [1.0, 0]}, {"v": [1, 0.0]}))
        self.assertNotEqual(product_json_identity(1.0), product_json_identity(1))
        for left, right in (
            (True, 1),
            (False, 0.0),
            (-0.0, 0),
            (1.0, 1.0000000000000002),
            (2**53 + 1, float(2**53 + 1)),
            ({"v": [1]}, {"v": [1, 2]}),
            ({"v": 1}, {"other": 1}),
        ):
            with self.subTest(left=left, right=right):
                self.assertFalse(product_json_values_equal(left, right))
        for invalid in (float("nan"), float("inf"), 2**63, "\ud800"):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                product_json_values_equal({"v": invalid}, {"v": invalid})

    def _accept(self, expression, *, expected_result=1.5):
        case = LocalIndependentAcceptanceCase.create(
            "fraction", [{"time": 1.5}], {"value": expected_result}
        )
        oracle = SimpleNamespace(
            identity=canonical_identity("oracle"), cases=lambda _: (case,)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package-marker").write_bytes(b"sealed-product")
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=(),
                independent_acceptance_oracle=oracle,
            )
            custody = SimpleNamespace(
                root=root,
                tree_identity=local_tree_identity(root),
                native_sdk_resources=None,
            )
            plan = SimpleNamespace(
                identity=canonical_identity("plan"),
                entrypoints=(SimpleNamespace(kind="portable-application"),),
            )
            result = SimpleNamespace(identity=canonical_identity("package"))
            command = (
                sys.executable,
                "-c",
                "import json,sys; data=json.loads(sys.argv[1]); "
                "print(json.dumps({'value': " + expression + "}))",
            )
            with (
                patch.object(ports, "project_package_custody", return_value=custody),
                patch.object(ports, "_packaged_argv", return_value=command),
                patch.object(ports, "_packaged_environment", return_value={}),
            ):
                return ports.accept_project_independently(
                    None,
                    None,
                    None,
                    plan,
                    result,
                    canonical_identity("root-test"),
                    canonical_identity("execution"),
                )

    def test_real_application_receives_numeric_fraction_and_passes(self):
        self.assertTrue(self._accept("data[0]['time']").uri.startswith("sha256:"))

    def test_real_application_accepts_equal_integer_and_float_results(self):
        self.assertTrue(
            self._accept("int(data[0]['time'])", expected_result=1.0).uri.startswith(
                "sha256:"
            )
        )
        with self.assertRaisesRegex(LocalStandardLifecycleError, "differs"):
            self._accept("True", expected_result=1.0)

    def test_fractional_mismatch_reports_exact_product_identities(self):
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "differs for 'fraction'"
        ) as error:
            self._accept("data[0]['time'] + 0.5")
        self.assertIn(product_json_identity({"value": 1.5}).uri, str(error.exception))
        self.assertIn(product_json_identity({"value": 2.0}).uri, str(error.exception))

    def test_nonfinite_observed_application_result_cannot_be_accepted(self):
        with self.assertRaisesRegex(ValueError, "must be finite"):
            self._accept("float('nan')")


if __name__ == "__main__":
    unittest.main()

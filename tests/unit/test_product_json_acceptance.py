"""Real fractional product invocation, exact comparison and identity compatibility."""

from __future__ import annotations

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
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.product_json import (
    product_json_identity,
)


class ProductJsonAcceptanceTests(unittest.TestCase):
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

    def test_fractional_mismatch_reports_exact_product_identities(self):
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "differs for 'fraction'"
        ) as error:
            self._accept("data[0]['time'] + 0.5")
        self.assertIn(product_json_identity({"value": 1.5}).uri, str(error.exception))
        self.assertIn(product_json_identity({"value": 2.0}).uri, str(error.exception))


if __name__ == "__main__":
    unittest.main()

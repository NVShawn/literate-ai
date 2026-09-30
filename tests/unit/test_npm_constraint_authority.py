"""Standard npm discovery consumes selected Flavor/SBOM version authority."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from literate_ai.contracts.flavors import ToolchainConstraint

REPO = Path(__file__).resolve().parents[2]


class NpmConstraintAuthorityTests(unittest.TestCase):
    def test_catalog_and_template_constraints_match_the_host_sbom(self) -> None:
        sbom = json.loads(
            (REPO / "flavors" / "package-npm" / "host-toolchain.cdx.json").read_text(
                encoding="utf-8"
            )
        )
        expected = sbom["components"][0]["version"]
        for relative in (
            Path("flavors") / "package-npm" / "toolchain.json",
            Path("src/literate_ai/project_template/flavors/package-npm/toolchain.json"),
        ):
            with self.subTest(path=relative.as_posix()):
                constraint = ToolchainConstraint.from_dict(
                    json.loads((REPO / relative).read_text(encoding="utf-8"))
                )
                self.assertEqual(constraint.toolchain, "npm")
                self.assertEqual(constraint.version_range(), expected)

    def test_python_adapter_does_not_duplicate_the_product_range(self) -> None:
        source = (REPO / "src/literate_ai/adapters/builders/javascript.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("DEFAULT_NPM_MINIMUM_VERSION", source)
        self.assertNotIn("DEFAULT_NPM_MAXIMUM_EXCLUSIVE_VERSION", source)
        self.assertNotIn(">=9,<13", source)


if __name__ == "__main__":
    unittest.main()

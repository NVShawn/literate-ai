"""Verifier-owned acceptance contracts for importable library artifacts."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.component_acceptance import (
    LIBRARY_SCHEMA,
    ComponentAcceptanceError,
    LibraryAcceptance,
    load_library_acceptance,
)
from literate_ai.contracts import canonical_identity


class LibraryAcceptanceTests(unittest.TestCase):
    def test_integer_identity_compatibility_and_fractional_oracle_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, document = self._write_oracle(Path(temporary))
            oracle = load_library_acceptance(path, "math-library")
            self.assertEqual(
                oracle.identity,
                canonical_identity(
                    {key: value for key, value in document.items() if key != "harness"}
                ),
            )
            document["cases"][0].update(arguments=[1.25, 0.25], expected_result=1.5)
            path.write_text(json.dumps(document), encoding="utf-8")
            fractional = load_library_acceptance(path, "math-library")
            self.assertNotEqual(fractional.identity, oracle.identity)
            self.assertEqual(fractional.cases[0].arguments, [1.25, 0.25])
            for value in (float("nan"), float("inf"), -float("inf")):
                document["cases"][0]["expected_result"] = {"nested": [value]}
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_library_acceptance(path, "math-library")

    def _write_oracle(self, root: Path) -> tuple[Path, dict[str, object]]:
        harness = root / "library_harness.py"
        harness.write_text("print('reviewed harness')\n", encoding="utf-8")
        specification = canonical_identity({"specification": "library"})
        interface = canonical_identity({"interface": "library"})
        surface = canonical_identity({"surface": "library"})
        document: dict[str, object] = {
            "schema": LIBRARY_SCHEMA,
            "component": "math-library",
            "specification_set_identity": specification.uri,
            "public_interface_identities": [interface.uri],
            "import_surface_identity": surface.uri,
            "language": "python",
            "harness": harness.name,
            "harness_identity": (
                "sha256:" + hashlib.sha256(harness.read_bytes()).hexdigest()
            ),
            "cases": [
                {
                    "case_id": "adds-two-values",
                    "capability": "math.add",
                    "arguments": [2, 3],
                    "expected_result": 5,
                }
            ],
        }
        path = root / "math-library.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path, document

    def test_loads_exact_reviewed_harness_and_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path, document = self._write_oracle(Path(temporary))
            oracle = load_library_acceptance(path, "math-library")

        self.assertIsInstance(oracle, LibraryAcceptance)
        self.assertEqual(oracle.language, "python")
        self.assertEqual(oracle.cases[0].capability, "math.add")
        self.assertEqual(oracle.cases[0].expected_result, 5)
        identity_document = {
            key: value for key, value in document.items() if key != "harness"
        }
        self.assertEqual(oracle.identity_document(), identity_document)
        self.assertEqual(oracle.identity, canonical_identity(identity_document))

    def test_cpp_oracle_binds_the_reviewed_native_harness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path, document = self._write_oracle(Path(temporary))
            document["language"] = "cpp"
            harness = path.parent / "harness.cpp"
            harness.write_text("int main(){return 0;}\n")
            document["harness"] = harness.name
            document["harness_identity"] = (
                "sha256:" + hashlib.sha256(harness.read_bytes()).hexdigest()
            )
            path.write_text(json.dumps(document))
            oracle = load_library_acceptance(path, "math-library")
            self.assertEqual(oracle.language, "cpp")
            self.assertEqual(oracle.harness_content, harness.read_bytes())

    def test_rejects_harness_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, _document = self._write_oracle(root)
            (root / "library_harness.py").write_text(
                "print('changed harness')\n", encoding="utf-8"
            )
            with self.assertRaises(ComponentAcceptanceError) as raised:
                load_library_acceptance(path, "math-library")

        self.assertEqual(raised.exception.code, "component_acceptance.harness_changed")

    def test_rejects_unreviewed_fields_and_noncanonical_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path, document = self._write_oracle(root)
            document["unreviewed"] = True
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ComponentAcceptanceError) as raised:
                load_library_acceptance(path, "math-library")
            self.assertEqual(
                raised.exception.code, "component_acceptance.contract_invalid"
            )

            document.pop("unreviewed")
            cases = document["cases"]
            assert isinstance(cases, list)
            cases.insert(
                0,
                {
                    "case_id": "z-last",
                    "capability": "math.add",
                    "arguments": [1, 1],
                    "expected_result": 2,
                },
            )
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ComponentAcceptanceError) as raised:
                load_library_acceptance(path, "math-library")
            self.assertEqual(raised.exception.code, "component_acceptance.case_invalid")


if __name__ == "__main__":
    unittest.main()

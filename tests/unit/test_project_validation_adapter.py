"""Focused tests for the filesystem project-validation adapter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    _validate_sample_taxonomy,
    validate_project,
)
from literate_ai.contracts import SourceIntelligenceStage

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class FilesystemProjectValidationAdapterTests(unittest.TestCase):
    def test_sample_taxonomy_keeps_reusable_components_out_of_samples(self) -> None:
        root = REPOSITORY_ROOT
        project = SimpleNamespace(root=root)
        manifest = root / "samples" / "demo" / "component.md"
        with self.assertRaises(ProjectValidationError) as raised:
            _validate_sample_taxonomy(
                manifest,
                SimpleNamespace(
                    sample=False,
                    inheritable=False,
                    coordinate=SimpleNamespace(name="demo"),
                ),
                project,
            )
        self.assertEqual(raised.exception.code, "project.sample_taxonomy_invalid")

    def test_only_hello_sample_is_inheritable(self) -> None:
        project = SimpleNamespace(root=REPOSITORY_ROOT)
        cases = (
            ("demo", True, "project.sample_inheritance_invalid"),
            ("hello-component", False, "project.sample_inheritance_invalid"),
        )
        for name, inheritable, code in cases:
            with (
                self.subTest(name=name),
                self.assertRaises(ProjectValidationError) as raised,
            ):
                _validate_sample_taxonomy(
                    REPOSITORY_ROOT / "samples" / name / "component.md",
                    SimpleNamespace(
                        sample=True,
                        inheritable=inheritable,
                        coordinate=SimpleNamespace(name=name),
                    ),
                    project,
                )
            self.assertEqual(raised.exception.code, code)

    def test_repository_sample_manifests_declare_their_inheritance_policy(self) -> None:
        samples = REPOSITORY_ROOT / "samples"
        manifests = tuple(sorted(samples.glob("*/component.md")))
        self.assertTrue(manifests)

        for manifest in manifests:
            text = manifest.read_text(encoding="utf-8")
            expected = "true" if manifest.parent.name == "hello-component" else "false"
            with self.subTest(sample=manifest.parent.name):
                self.assertIn("sample: true\n", text)
                self.assertIn(f"inheritable: {expected}\n", text)

        starter = (
            REPOSITORY_ROOT
            / "src/literate_ai/project_template/samples/hello-component/component.md"
        ).read_text(encoding="utf-8")
        self.assertIn("sample: true\n", starter)
        self.assertIn("inheritable: true\n", starter)

    def test_validation_preserves_the_existing_project_report(self) -> None:
        options = {
            "require_authority_review": False,
            "include_test_receipt": False,
            "synchronize_source_intelligence": False,
            "source_intelligence_stage": SourceIntelligenceStage.STRUCTURAL_REVIEW,
        }

        first = validate_project(REPOSITORY_ROOT, **options)
        second = validate_project(REPOSITORY_ROOT, **options)

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], "literate-ai/project-validation@6")
        self.assertEqual(first["project_id"], "literate-ai")
        self.assertIsInstance(first["authority_review"], dict)

    def test_missing_project_raises_adapter_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProjectValidationError) as raised:
                validate_project(
                    Path(directory),
                    require_authority_review=False,
                    include_test_receipt=False,
                    synchronize_source_intelligence=False,
                )

        self.assertEqual(raised.exception.code, "project.not_found")


if __name__ == "__main__":
    unittest.main()

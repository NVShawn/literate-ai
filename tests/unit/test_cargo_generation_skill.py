"""Regression coverage for Cargo generation authority."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CargoGenerationSkillTests(unittest.TestCase):
    def test_catalog_and_template_skills_are_byte_identical(self) -> None:
        for name in ("cargo-build-system", "rust-ecosystem"):
            with self.subTest(skill=name):
                catalog = ROOT / "skills/specification-to-source" / name / "SKILL.md"
                template = (
                    ROOT
                    / "src/literate_ai/project_template/skills/specification-to-source"
                    / name
                    / "SKILL.md"
                )
                self.assertEqual(catalog.read_bytes(), template.read_bytes())

    def test_cargo_contract_is_locked_and_keeps_derived_state_external(self) -> None:
        skill = (
            ROOT / "skills/specification-to-source/cargo-build-system/SKILL.md"
        ).read_text(encoding="utf-8")
        ecosystem = (
            ROOT / "skills/specification-to-source/rust-ecosystem/SKILL.md"
        ).read_text(encoding="utf-8")

        self.assertIn("cargo metadata --locked", skill)
        self.assertIn("cargo build --locked", skill)
        self.assertIn("--litai-test", skill)
        self.assertIn("CARGO_TARGET_DIR", ecosystem)
        self.assertIn("never create a\n`target/` directory", ecosystem)
        self.assertIn("cargo test --locked --all-targets", skill)
        self.assertIn("cargo test --locked --all-targets", ecosystem)
        self.assertIn("separate integration-test crate", ecosystem)
        self.assertIn("appropriate nested unit-test modules", ecosystem)


if __name__ == "__main__":
    unittest.main()

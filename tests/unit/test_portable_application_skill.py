"""Regression coverage for shared portable-application generation authority."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = (
    ROOT
    / "skills/specification-to-source/portable-application-implementation/SKILL.md",
    ROOT
    / "src/literate_ai/project_template/skills/specification-to-source"
    / "portable-application-implementation/SKILL.md",
)


class PortableApplicationSkillTests(unittest.TestCase):
    def test_generated_manifest_documents_canonical_json_numeric_rules(self) -> None:
        for skill in SKILLS:
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn(
                    "Canonical JSON v1 forbids floating-point numbers", normalized
                )
                self.assertIn("at every depth", normalized)
                self.assertIn("fractional domain value as a decimal string", normalized)
                self.assertIn(
                    "integer with a scale defined by a cited specification", normalized
                )
                self.assertIn("without rounding or changing its semantics", normalized)

    def test_catalog_and_template_skills_are_byte_identical(self) -> None:
        catalog, template = SKILLS
        self.assertEqual(catalog.read_bytes(), template.read_bytes())


if __name__ == "__main__":
    unittest.main()

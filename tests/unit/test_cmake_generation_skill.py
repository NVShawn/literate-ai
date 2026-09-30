"""Regression coverage for CMake generation authority."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = (
    ROOT / "skills/specification-to-source/cmake-build-system/SKILL.md",
    ROOT
    / "src/literate_ai/project_template/skills/specification-to-source"
    / "cmake-build-system/SKILL.md",
)


class CMakeGenerationSkillTests(unittest.TestCase):
    def test_locked_cmake_target_is_validated_before_generation_returns(self) -> None:
        for skill in SKILLS:
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn("exactly one root", normalized)
                self.assertIn("add_executable(litai_artifact", normalized)
                self.assertIn("POST_BUILD", normalized)
                self.assertIn(
                    "Require the build to create the exact export", normalized
                )
                self.assertIn(
                    "test suite to pass with a nonzero discovered-test count",
                    normalized,
                )
                self.assertIn("must not fetch dependencies", normalized)
                self.assertIn("advisory cache prefixes", normalized)
                self.assertIn("load-bearing project authority", normalized)
                self.assertIn(
                    "Generated application CMakeLists.txt stays in source/", normalized
                )

    def test_catalog_and_template_cmake_skills_are_byte_identical(self) -> None:
        catalog, template = SKILLS
        self.assertEqual(catalog.read_bytes(), template.read_bytes())


if __name__ == "__main__":
    unittest.main()

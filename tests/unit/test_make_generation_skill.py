"""Regression coverage for GNU Make generation authority."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = (
    ROOT / "skills/specification-to-source/make-build-system/SKILL.md",
    ROOT
    / "src/literate_ai/project_template/skills/specification-to-source"
    / "make-build-system/SKILL.md",
)


class MakeGenerationSkillTests(unittest.TestCase):
    def test_locked_make_targets_are_validated_before_generation_returns(self) -> None:
        for skill in SKILLS:
            with self.subTest(skill=skill):
                normalized = " ".join(skill.read_text(encoding="utf-8").split())
                self.assertIn("Require `all` to create the exact export", normalized)
                self.assertIn(
                    "`test` to pass with a nonzero discovered-test count", normalized
                )
                self.assertIn("Never substitute a `build` target", normalized)
                self.assertIn("must not fetch dependencies", normalized)
                self.assertIn('must never report "Nothing to be done"', normalized)
                self.assertIn("always run the rule or helper", normalized)
                self.assertIn("advisory cache prefixes", normalized)
                self.assertIn("load-bearing project authority", normalized)
                self.assertIn("Interpreter caches count as build output", normalized)
                self.assertIn("PYTHONDONTWRITEBYTECODE=1", normalized)
                self.assertIn("PYTHONPYCACHEPREFIX=$(OBJECT_ROOT)/pycache", normalized)
                self.assertIn("byte-for-byte unchanged", normalized)
                self.assertIn('double-quoted `"$(LITAI_LANGUAGE_TOOL)"`', normalized)
                self.assertIn(
                    "absolute Windows paths commonly contain spaces", normalized
                )
                self.assertIn(
                    "Generated application Makefile stays in source/", normalized
                )
                self.assertNotIn("Always generate a thin second", normalized)
                self.assertNotIn("## Root-level delegation Makefile", normalized)

    def test_catalog_and_template_make_skills_are_byte_identical(self) -> None:
        catalog, template = SKILLS
        self.assertEqual(catalog.read_bytes(), template.read_bytes())

    def test_every_portable_language_inherits_the_quoted_tool_contract(self) -> None:
        names = (
            "cpp17-portable-json-application",
            "go-portable-application",
            "javascript-portable-json-application",
            "python-portable-application",
            "rust-portable-json-application",
            "swift-portable-json-application",
            "typescript-portable-application",
            "zig-portable-application",
        )
        for root in (
            ROOT / "skills/specification-to-source",
            ROOT / "src/literate_ai/project_template/skills/specification-to-source",
        ):
            common = (root / "portable-application-implementation/SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                'exact double-quoted expansion\n`"$(LITAI_LANGUAGE_TOOL)"`', common
            )
            self.assertIn("applies to every language descendant", common)
            for name in names:
                with self.subTest(root=root, name=name):
                    child = (root / name / "SKILL.md").read_text(encoding="utf-8")
                    self.assertIn(
                        'skill_id: "portable-application-implementation"', child
                    )
                    self.assertIn('version: "1.8.3"', child)
                    self.assertIn(
                        "3d393c054d241eda78df8789144e75476d3131ae1b2e404fa33c6b02ef66d4af",
                        child,
                    )

    def test_layout_skills_treat_build_dir_as_advisory(self) -> None:
        canonical_skills = (
            ROOT / "skills/specification-to-source/repository-layout/SKILL.md",
            ROOT
            / "src/literate_ai/project_template/skills/specification-to-source"
            / "repository-layout/SKILL.md",
        )
        dependent_skills = (
            ROOT / "skills/specification-to-source/python-repository-layout/SKILL.md",
            ROOT
            / "src/literate_ai/project_template/skills/specification-to-source"
            / "python-repository-layout/SKILL.md",
        )
        pointer_skills = (
            ROOT
            / "skills/specification-to-source/portable-application-implementation"
            / "SKILL.md",
            ROOT
            / "src/literate_ai/project_template/skills/specification-to-source"
            / "portable-application-implementation/SKILL.md",
        )
        for skill in canonical_skills:
            with self.subTest(skill=skill):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("load-bearing project authority", text)
                self.assertNotIn(
                    "Put accepted generated source only beneath the configured",
                    text,
                )
        for skill in dependent_skills:
            with self.subTest(skill=skill):
                text = skill.read_text(encoding="utf-8")
                self.assertIn('skill_id: "repository-layout"', text)
        for skill in pointer_skills:
            with self.subTest(skill=skill):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("repository-layout", text)


if __name__ == "__main__":
    unittest.main()

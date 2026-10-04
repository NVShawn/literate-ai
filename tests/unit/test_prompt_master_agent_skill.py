"""Prompt Master is pinned, attributed, and routed only for direct agent work."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "agent" / "prompt-master" / "SKILL.md"
TEMPLATE = (
    ROOT
    / "src"
    / "literate_ai"
    / "project_template"
    / "skills"
    / "agent"
    / "prompt-master"
)


class PromptMasterAgentSkillTests(unittest.TestCase):
    def test_upstream_license_and_template_are_byte_equal(self) -> None:
        self.assertEqual(SKILL.read_bytes(), (TEMPLATE / "SKILL.md").read_bytes())
        source_license = SKILL.parent / "LICENSE.prompt-master"
        template_license = TEMPLATE / "LICENSE.prompt-master"
        self.assertEqual(source_license.read_bytes(), template_license.read_bytes())
        license_text = source_license.read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Nidhin Joseph Nelson", license_text)
        self.assertIn("MIT License", license_text)


if __name__ == "__main__":
    unittest.main()

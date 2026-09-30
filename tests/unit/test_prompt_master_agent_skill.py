"""Prompt Master is pinned, attributed, and routed only for direct agent work."""

from __future__ import annotations

import re
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
UPSTREAM_COMMIT = "d15eabbe5d2122eedc060bae8a771381e9873d1b"


class PromptMasterAgentSkillTests(unittest.TestCase):
    def test_pin_attribution_direct_trigger_and_mac_bypass_are_explicit(self) -> None:
        content = SKILL.read_text(encoding="utf-8")

        self.assertIn("name: prompt-master", content)
        self.assertIn(UPSTREAM_COMMIT, content)
        self.assertIn('upstream_version: "1.7.0"', content)
        self.assertIn('license: "MIT"', content)
        self.assertIn("interacting directly with Literate AI", content)
        self.assertIn("When MAC supplies the task, do not invoke this skill", content)
        self.assertIn("cannot add or rewrite product behavior", content)
        self.assertIn("cannot", content)
        self.assertNotIn("Chain of Thought", content)

    def test_upstream_license_and_template_are_byte_equal(self) -> None:
        self.assertEqual(SKILL.read_bytes(), (TEMPLATE / "SKILL.md").read_bytes())
        source_license = SKILL.parent / "LICENSE.prompt-master"
        template_license = TEMPLATE / "LICENSE.prompt-master"
        self.assertEqual(source_license.read_bytes(), template_license.read_bytes())
        license_text = source_license.read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Nidhin Joseph Nelson", license_text)
        self.assertIn("MIT License", license_text)

    def test_onboarding_requires_windows_portable_paths(self) -> None:
        onboarding = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        template = (
            ROOT / "src" / "literate_ai" / "project_template" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for content in (onboarding, template):
            self.assertIn("## Keep Windows-portable paths", content)
            self.assertIn("MAX_PATH", content)
            self.assertIn("260", content)
            self.assertIn(
                "do not assume the target host has done either",
                " ".join(content.split()),
            )

    def test_onboarding_and_boundary_docs_select_exactly_one_translation_path(
        self,
    ) -> None:
        onboarding = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        template = (
            ROOT / "src" / "literate_ai" / "project_template" / "SKILL.md"
        ).read_text(encoding="utf-8")
        boundary = (
            ROOT / "docs" / "architecture" / "agent-ledger-boundary.md"
        ).read_text(encoding="utf-8")
        for content in (onboarding, template):
            self.assertIn("skills/agent/prompt-master/SKILL.md", content)
            self.assertRegex(content, re.compile(r"Bypass|Do not apply"))
            self.assertIn("MAC", content)
        self.assertIn("never applies both paths", boundary)


if __name__ == "__main__":
    unittest.main()

"""Issue-first upstream escalation remains explicit and template-portable."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class IssueFirstWorkSkillTests(unittest.TestCase):
    def test_issue_first_rule_preserves_scope_and_authorization_boundaries(
        self,
    ) -> None:
        skills = (
            ROOT / "skills/agent/record-user-directed-work/SKILL.md",
            ROOT
            / "src/literate_ai/project_template/skills/agent"
            / "record-user-directed-work/SKILL.md",
        )
        for skill in skills:
            with self.subTest(skill=skill):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("execute its `issue_search` command", text)
                self.assertIn("`gh issue create` or `glab issue create`", text)
                self.assertIn("before source changes", text)
                self.assertIn("secret-shaped values", text)
                self.assertIn("Record reciprocal links", text)
                self.assertIn("triage authority, not permission", text)
                self.assertIn("next blocked action", text)
        self.assertEqual(skills[0].read_bytes(), skills[1].read_bytes())


if __name__ == "__main__":
    unittest.main()

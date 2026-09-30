"""The framework-owned worker-health skill ships in repository and templates."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WorkerHealthSkillTests(unittest.TestCase):
    def test_skill_and_project_template_match_and_retain_safety_boundaries(self):
        source = ROOT / "skills/agent/worker-health/SKILL.md"
        template = (
            ROOT
            / "src/literate_ai/project_template/skills/agent/worker-health/SKILL.md"
        )

        content = source.read_text(encoding="utf-8")
        self.assertEqual(content, template.read_text(encoding="utf-8"))
        self.assertIn("litai worker health", content)
        self.assertIn("Candidate discovery never authorizes deletion", content)
        self.assertIn("exact cleanup authorization", content)
        self.assertIn("Do not create a background daemon", content)


if __name__ == "__main__":
    unittest.main()

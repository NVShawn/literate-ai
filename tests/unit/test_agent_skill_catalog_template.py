"""Catalog agent skills stay byte-identical to the init template copies."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "skills" / "agent"
TEMPLATE = ROOT / "src" / "literate_ai" / "project_template" / "skills" / "agent"


class AgentSkillCatalogTemplateParityTests(unittest.TestCase):
    def test_release_and_nested_posture_skills_match_the_init_template(self) -> None:
        relatives = (
            Path("SKILL.md"),
            Path("release-project") / "SKILL.md",
            Path("release-project") / "backport" / "SKILL.md",
            Path("release-project") / "evidence" / "SKILL.md",
            Path("release-project") / "advance" / "SKILL.md",
            Path("release-project") / "ci-status" / "SKILL.md",
            Path("release-project") / "verify-published" / "SKILL.md",
            Path("release-project") / "notify-descendants" / "SKILL.md",
            Path("associate-release-jira") / "SKILL.md",
            Path("configure-operator-mcp") / "SKILL.md",
            Path("ingest-channel-work") / "SKILL.md",
            Path("record-user-directed-work") / "SKILL.md",
            Path("develop-in-production-workflow") / "SKILL.md",
            Path("develop-in-production-workflow") / "staging" / "SKILL.md",
            Path("develop-in-production-workflow") / "staging" / "dev" / "SKILL.md",
            Path("develop-in-production-workflow")
            / "staging"
            / "dev"
            / "land"
            / "SKILL.md",
            Path("develop-in-production-workflow")
            / "staging"
            / "dev"
            / "survey-peer-work"
            / "SKILL.md",
            Path("ci-test-plan") / "SKILL.md",
            Path("ci-test-plan") / "shard" / "SKILL.md",
            Path("ci-test-plan") / "impact" / "SKILL.md",
            Path("render-html-observability") / "SKILL.md",
        )
        for relative in relatives:
            with self.subTest(relative=relative.as_posix()):
                self.assertEqual(
                    (CATALOG / relative).read_bytes(),
                    (TEMPLATE / relative).read_bytes(),
                )

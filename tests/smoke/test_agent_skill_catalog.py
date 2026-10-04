from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)


def _skill(name: str, body: str = "# Skill\n\nDo the bounded work.") -> bytes:
    return (f"---\nname: {name}\ndescription: Bounded skill.\n---\n{body}\n").encode()


class AgentSkillCatalogTests(unittest.TestCase):
    def test_stale_typed_dependency_and_unresolved_reference_fail_closed(self) -> None:
        stale = (
            "---\nname: dependent\nskill_id: dependent\nversion: 1.0.0\n"
            "dependencies:\n  - skill_id: base\n    version: 1.0.0\n"
            '    identity:\n      algorithm: sha256\n      digest: "'
            + "0" * 64
            + '"\n---\n# Dependent\n'
        ).encode()
        with self.assertRaises(AgentSkillCatalogError) as caught:
            AgentSkillCatalog.from_documents(
                {
                    "skills/base/SKILL.md": _skill("base").replace(
                        b"description: Bounded skill.\n",
                        b"description: Bounded skill.\n"
                        b"skill_id: base\nversion: 1.0.0\n",
                    ),
                    "skills/dependent/SKILL.md": stale,
                }
            )
        self.assertEqual(caught.exception.code, "agent_skill.dependency_identity_stale")

        with self.assertRaises(AgentSkillCatalogError) as missing:
            AgentSkillCatalog.from_documents(
                {"SKILL.md": _skill("root", "Follow `skills/agent/missing/SKILL.md`.")},
                validate_references=True,
            )
        self.assertEqual(missing.exception.code, "agent_skill.reference_missing")

    def test_repository_and_shipped_template_are_closed_catalogs(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        framework = AgentSkillCatalog.discover(
            repository,
            catalog_roots=(repository / "skills",),
            root_manifests=(repository / "SKILL.md",),
            validate_references=True,
        )
        template = repository / "src/literate_ai/project_template"
        initialized = AgentSkillCatalog.discover(
            template,
            catalog_roots=(template / "skills",),
            root_manifests=(template / "SKILL.md",),
            validate_references=True,
        )
        self.assertGreater(len(framework.skills), len(initialized.skills))


if __name__ == "__main__":
    unittest.main()

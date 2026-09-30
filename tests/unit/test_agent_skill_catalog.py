from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from literate_ai.application import agent_skill_catalog
from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)


def _skill(name: str, body: str = "# Skill\n\nDo the bounded work.") -> bytes:
    return (f"---\nname: {name}\ndescription: Bounded skill.\n---\n{body}\n").encode()


class AgentSkillCatalogTests(unittest.TestCase):
    def test_resource_ownership_is_computed_once_per_document(self) -> None:
        documents = {
            f"skills/skill-{index}/SKILL.md": _skill(f"skill-{index}")
            for index in range(24)
        }
        documents.update(
            {f"skills/skill-{index}/reference.md": b"owned\n" for index in range(24)}
        )
        with mock.patch.object(
            agent_skill_catalog,
            "_owner_directory",
            wraps=agent_skill_catalog._owner_directory,
        ) as owner_directory:
            catalog = AgentSkillCatalog.from_documents(documents)

        self.assertEqual(len(catalog.skills), 24)
        self.assertEqual(owner_directory.call_count, len(documents))

    def test_nested_sentinels_own_disjoint_resource_closures_and_inherit(self) -> None:
        catalog = AgentSkillCatalog.from_documents(
            {
                "skills/agent/SKILL.md": _skill("agent"),
                "skills/agent/reference.md": b"parent\n",
                "skills/agent/release/SKILL.md": _skill("release"),
                "skills/agent/release/script.py": b"print('child')\n",
            }
        )
        parent = catalog.by_name("agent")
        child = catalog.by_name("release")
        self.assertEqual(child.ancestors, ("agent",))
        self.assertIn(PurePosixPath("skills/agent/reference.md"), parent.owned_paths)
        self.assertNotIn(child.logical_manifest, parent.owned_paths)
        self.assertIn(
            PurePosixPath("skills/agent/release/script.py"), child.owned_paths
        )

    def test_impact_propagates_inheritance_and_typed_dependents(self) -> None:
        base = _skill("base")
        identity = hashlib.sha256(base).hexdigest()
        dependent = (
            "---\nname: dependent\ndescription: Dependent skill.\n"
            "skill_id: dependent\nversion: 1.0.0\ndependencies:\n"
            "  - skill_id: base\n    version: 1.0.0\n"
            f"    identity:\n      algorithm: sha256\n      digest: {identity}\n"
            "---\n# Dependent\n\nUse base.\n"
        ).encode()
        typed_base = base.replace(
            b"description: Bounded skill.\n",
            b"description: Bounded skill.\nskill_id: base\nversion: 1.0.0\n",
        )
        # The dependency identity is over the actual target bytes.
        identity = hashlib.sha256(typed_base).hexdigest()
        dependent = dependent.replace(
            hashlib.sha256(base).hexdigest().encode(), identity.encode()
        )
        catalog = AgentSkillCatalog.from_documents(
            {
                "skills/base/SKILL.md": typed_base,
                "skills/base/child/SKILL.md": _skill("child"),
                "skills/dependent/SKILL.md": dependent,
            }
        )
        impacted = {item.name for item in catalog.impacted(("skills/base/SKILL.md",))}
        self.assertEqual(impacted, {"base", "child", "dependent"})

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

    def test_manifest_inventory_precedes_domain_specific_metadata_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_root = root / "skills"
            for directory in (catalog_root / "first", catalog_root / "second"):
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_bytes(_skill("duplicate"))

            manifests = AgentSkillCatalog.discover_manifest_paths(
                root, catalog_roots=(catalog_root,)
            )
            with self.assertRaises(AgentSkillCatalogError) as strict:
                AgentSkillCatalog.discover(root, catalog_roots=(catalog_root,))

        self.assertEqual(len(manifests), 2)
        self.assertEqual(strict.exception.code, "agent_skill.name_ambiguous")

    def test_projection_excludes_child_sentinel_and_carries_ancestor_context(
        self,
    ) -> None:
        catalog = AgentSkillCatalog.from_documents(
            {
                "SKILL.md": _skill("root"),
                "skills/agent/SKILL.md": _skill("agent"),
                "skills/agent/reference.md": b"owned\n",
                "skills/agent/child/SKILL.md": _skill("child"),
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            projected = catalog.materialize("agent", Path(temporary))
            self.assertTrue((projected / "reference.md").is_file())
            self.assertFalse((projected / "child" / "SKILL.md").exists())
            self.assertTrue(
                (projected / ".literate-ancestors" / "0000-root" / "SKILL.md").is_file()
            )

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

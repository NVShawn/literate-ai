"""Skill updates preview dependent pins and preserve explicit template ownership."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.normalize_skill_authoring import (
    apply_changes,
    plan_identity,
    plan_repository,
)

REPO = Path(__file__).resolve().parents[2]


class SkillUpdatePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.catalog = self.root / "skills/specification-to-source"
        self.template = (
            self.root
            / "src/literate_ai/project_template/skills/specification-to-source"
        )
        for base in (self.catalog, self.template):
            for name in (
                "portable-application-implementation",
                "python-portable-application",
            ):
                destination = base / name / "SKILL.md"
                destination.parent.mkdir(parents=True)
                shutil.copy2(
                    REPO / "skills/specification-to-source" / name / "SKILL.md",
                    destination,
                )

    def test_parent_change_plans_dependent_pin_without_writing(self) -> None:
        parent = self.catalog / "portable-application-implementation/SKILL.md"
        parent.write_bytes(parent.read_bytes() + b"\nA revised normative rule.\n")
        child = self.catalog / "python-portable-application/SKILL.md"
        original = child.read_bytes()
        changes = plan_repository(self.root)
        self.assertEqual([item.path for item in changes], [child])
        self.assertEqual(child.read_bytes(), original)
        apply_changes(changes)
        self.assertNotEqual(child.read_bytes(), original)
        self.assertEqual(plan_repository(self.root), ())

    def test_changed_files_invalidate_a_previously_reviewed_plan(self) -> None:
        parent = self.catalog / "portable-application-implementation/SKILL.md"
        parent.write_bytes(parent.read_bytes() + b"\nA revised normative rule.\n")
        changes = plan_repository(self.root)
        child = changes[0].path
        child.write_bytes(child.read_bytes() + b"\nUser edit.\n")
        user_content = child.read_bytes()
        with self.assertRaisesRegex(ValueError, "stale"):
            apply_changes(changes)
        self.assertEqual(child.read_bytes(), user_content)

    def test_divergent_templates_are_only_mirrored_explicitly(self) -> None:
        name = "python-portable-application"
        template = self.template / name / "SKILL.md"
        template.write_bytes(template.read_bytes() + b"\nTemplate-specific rule.\n")
        self.assertEqual(plan_repository(self.root), ())
        changes = plan_repository(self.root, mirrors=(name,))
        self.assertEqual([item.path for item in changes], [template])
        apply_changes(changes)
        self.assertEqual(
            template.read_bytes(), (self.catalog / name / "SKILL.md").read_bytes()
        )

    def test_unchanged_output_does_not_hide_changed_reviewed_inputs(self) -> None:
        parent = self.catalog / "portable-application-implementation/SKILL.md"
        parent.write_bytes(parent.read_bytes() + b"\nNew rule.\n")
        original_plan = plan_repository(self.root)
        template_parent = self.template / "portable-application-implementation/SKILL.md"
        template_child = self.template / "python-portable-application/SKILL.md"
        template_parent.write_bytes(
            template_parent.read_bytes() + b"\nTemplate rule.\n"
        )
        # Reconcile that independent catalog: the original planned output is unchanged,
        # but the reviewed catalog input set is not.
        apply_changes(
            tuple(
                item
                for item in plan_repository(self.root)
                if item.path == template_child
            )
        )
        current_plan = plan_repository(self.root)
        self.assertEqual(
            [(item.path, item.new_digest) for item in original_plan],
            [(item.path, item.new_digest) for item in current_plan],
        )
        self.assertNotEqual(
            plan_identity(self.root, original_plan),
            plan_identity(self.root, current_plan),
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            apply_changes(original_plan)

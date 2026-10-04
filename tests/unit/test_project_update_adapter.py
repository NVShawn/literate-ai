"""Safe three-way planning for projects created by ``litai init``."""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from literate_ai.adapters import (
    project_updates,
)
from literate_ai.adapters.project_updates import (
    FilesystemProjectUpdateAdapter,
    ProjectUpdateError,
)
from literate_ai.contracts import (
    ProjectInitializationOrigin,
    ProjectUpdateClassification,
    ProjectUpdatePlan,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)


def _origin(revision: str, version: str) -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        "ssh://git.example.test/operator/literate-ai.git",
        revision * 40,
        "literate-ai",
        version,
    )


class FilesystemProjectUpdateAdapterTests(unittest.TestCase):
    def test_plan_classifies_three_way_state_without_mutating_project(self) -> None:
        previous = _origin("a", "0.2.0")
        upstream = _origin("b", "0.3.0")
        original_template_text = project_updates._template_text

        def changed_template(resource: str) -> str:
            content = original_template_text(resource)
            if resource in {"AGENTS.md", "CLAUDE.md"}:
                return content + f"\nupstream-{resource}\n"
            return content

        template_files = {
            **project_updates._TEMPLATE_FILES,
            "UPSTREAM.md": "AGENTS.md",
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+bazel", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            skill = target / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal\n")
            claude = target / "CLAUDE.md"
            claude.write_text(claude.read_text(encoding="utf-8") + "\nlocal\n")
            before = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }

            with (
                mock.patch.object(project_updates, "_TEMPLATE_FILES", template_files),
                mock.patch.object(
                    project_updates, "_template_text", side_effect=changed_template
                ),
            ):
                plan = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)
                reassigned = ProjectInitializationOrigin(
                    "ssh://git.example.test/other/literate-ai.git",
                    "c" * 40,
                    "literate-ai",
                    "0.3.0",
                )
                with self.assertRaisesRegex(
                    ProjectUpdateError, "differs from the initializing origin"
                ):
                    FilesystemProjectUpdateAdapter(
                        origin_provider=lambda: reassigned
                    ).plan(target)

            after = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }

        by_path = {item.path: item.classification for item in plan.files}
        self.assertEqual(
            by_path["AGENTS.md"], ProjectUpdateClassification.UPSTREAM_ONLY
        )
        self.assertEqual(by_path["SKILL.md"], ProjectUpdateClassification.LOCAL_ONLY)
        self.assertEqual(by_path["CLAUDE.md"], ProjectUpdateClassification.CONFLICT)
        self.assertEqual(
            by_path["UPSTREAM.md"], ProjectUpdateClassification.UPSTREAM_ADDED
        )
        self.assertEqual(
            by_path["literate.project.json"],
            ProjectUpdateClassification.PRESERVED_DYNAMIC,
        )
        self.assertEqual(before, after)
        self.assertEqual(plan.previous_origin, previous)
        self.assertEqual(plan.upstream_origin, upstream)
        self.assertFalse(plan.to_dict()["apply_supported"])
        SchemaCatalog().validate(plan.SCHEMA, plan.to_dict())
        self.assertEqual(ProjectUpdatePlan.from_dict(plan.to_dict()), plan)
        tampered = deepcopy(plan.to_dict())
        tampered["counts"]["conflict"] += 1
        with self.assertRaisesRegex(ValueError, "counts does not match"):
            ProjectUpdatePlan.from_dict(tampered)

    def test_project_initialized_before_repository_move_plans_from_successor(
        self,
    ) -> None:
        previous = ProjectInitializationOrigin(
            "https://github.com/NVIDIA-dev/literate-ai",
            "a" * 40,
            "literate-ai",
            "1.0.1",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+make", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            successor = ProjectInitializationOrigin(
                "https://github.com/jordanhubbard/literate-ai",
                "b" * 40,
                "literate-ai",
                "1.1.0",
            )
            plan = FilesystemProjectUpdateAdapter(
                origin_provider=lambda: successor
            ).plan(target)
            self.assertEqual(plan.previous_origin, previous)
            self.assertEqual(plan.upstream_origin, successor)
            for unrelated in (
                "https://github.com/other/literate-ai",
                "https://github.com/jordanhubbard/other",
            ):
                with self.subTest(unrelated=unrelated):
                    with self.assertRaises(ProjectUpdateError) as raised:
                        FilesystemProjectUpdateAdapter(
                            origin_provider=lambda unrelated=unrelated: (
                                ProjectInitializationOrigin(
                                    unrelated, "b" * 40, "literate-ai", "1.1.0"
                                )
                            )
                        ).plan(target)
                    self.assertEqual(
                        raised.exception.code, "project.update_origin_changed"
                    )


if __name__ == "__main__":
    unittest.main()

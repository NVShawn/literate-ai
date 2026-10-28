"""Roadmap mutation, import and gate-order regression contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.project_update_work_items import (
    ProjectUpdateWorkItemError,
    record_work_items,
)
from literate_ai.adapters.roadmap_dependencies import require_project_roadmap_acyclic
from literate_ai.application.roadmap_dependencies import (
    RoadmapDependencyError,
    validate_roadmap_dependencies,
)
from literate_ai.application.roadmap_work import (
    RoadmapWorkRecord,
    close_work,
    record_work,
)
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.work import work_from_args
from literate_ai.documentation import DocumentationError, validate_documentation_graph
from literate_ai.release_contributions import (
    ReleaseContributionsError,
    _roadmap_release_goals,
)


def item(work_id, requires="none", target=None, state=" "):
    return (
        f"### [{state}] {work_id} — Work\n\n"
        + (f"- **Release target:** {target}\n" if target else "")
        + f"- **Completion prerequisites:** {requires}\n"
    )


class RoadmapDependencyTests(unittest.TestCase):
    def assert_cycle(self, content, expected, manifest=None):
        with self.assertRaises(RoadmapDependencyError) as caught:
            validate_roadmap_dependencies(content, manifest)
        self.assertEqual(caught.exception.code, "roadmap.dependency_cycle")
        self.assertIn(expected, caught.exception.message)

    def test_self_transitive_and_completed_cycles_have_actionable_paths(self):
        self.assert_cycle(item("AAA-001", "AAA-001"), "AAA-001 -> AAA-001")
        self.assert_cycle(
            item("AAA-001", "BBB-001", state="x")
            + item("BBB-001", "CCC-001", state="x")
            + item("CCC-001", "AAA-001", state="x"),
            "AAA-001 -> BBB-001 -> CCC-001 -> AAA-001",
        )

    def test_incident_exact_requirements_normalized_to_explicit_prerequisites(self):
        fixture = json.loads(
            (
                Path(__file__).parents[1] / "fixtures/roadmap-release-cycle-1.3.1.json"
            ).read_text()
        )
        for case in fixture["cases"]:
            with self.subTest(goal=case["id"]):
                self.assert_cycle(
                    item(case["id"], case["modeled_prerequisite"], "1.3.1"),
                    case["cycle"],
                )

    def test_cross_release_cycle_includes_implicit_phase_edges(self):
        self.assert_cycle(
            item("AAA-001", "release:1.4.0:published", "1.3.1")
            + item("BBB-001", "release:1.3.1:verified", "1.4.0"),
            "AAA-001 -> release:1.4.0:published -> release:1.4.0:checked",
        )

    def test_manifest_membership_cannot_hide_implicit_cycle_by_omitting_target(self):
        manifest = {"releases": {"1.3.1": [{"id": "AAA-001"}]}}
        self.assert_cycle(
            item("AAA-001", "release:1.3.1:ready"),
            "AAA-001 -> release:1.3.1:ready -> AAA-001",
            manifest,
        )

    def test_valid_diamond_cross_release_and_separate_execution(self):
        content = (
            item("AAA-001", "BBB-001, CCC-001", "1.3.1")
            + item("BBB-001", "DDD-001")
            + item("CCC-001", "DDD-001")
            + item("DDD-001")
            + item("NEXT-001", "release:1.3.1:verified", "1.4.0")
            + item("EXEC-001", "release:1.4.0:synchronized")
        )
        validate_roadmap_dependencies(content)
        # A source qualification can precede readiness; `qualify_release` does
        # not require a prepared candidate. Do not invent an implicit cycle.
        validate_roadmap_dependencies(
            item("AAA-001", "release:1.3.1:qualified", "1.3.1")
        )

    def test_invalid_typed_references_fail_closed(self):
        for value in (
            "MISSING-001",
            "release:1.3.1:publshed",
            "release:9.9.9:ready",
            "",
        ):
            with self.subTest(value=value), self.assertRaises(RoadmapDependencyError):
                validate_roadmap_dependencies(item("AAA-001", value, "1.3.1"))

    def test_fences_comments_other_heading_and_legacy_inverse_prose(self):
        valid = (
            item("AAA-001")
            + "- **Depends on:** none (no dependency on BBB-001)\n"
            + item("BBB-001")
            + "- **Depends on:** AAA-001; required before CCC-001\n"
            + item("CCC-001", "BBB-001")
            + "\n### Other program\n- **Completion prerequisites:** MISSING-001\n"
        )
        hidden = item("BAD-001", "BAD-001")
        validate_roadmap_dependencies(valid + f"\n```markdown\n{hidden}```\n")
        validate_roadmap_dependencies(valid + f"\n<!--\n{hidden}-->\n")
        self.assert_cycle(
            "```text\n<!-- literal in code\n```\n" + hidden + "-->\n",
            "BAD-001 -> BAD-001",
        )
        # A would-be closing fence with an info string is still code content.
        validate_roadmap_dependencies("```text\n```not-a-close\n" + hidden + "```\n")

    def test_legacy_edges_and_repeated_sections_are_not_lost(self):
        content = item("AAA-001") + "- **Depends on:** BBB-001\n"
        content += item("BBB-001") + "- **Depends on:** AAA-001\n"
        self.assert_cycle(content, "AAA-001 -> BBB-001 -> AAA-001")
        self.assert_cycle(content + item("AAA-001"), "AAA-001 -> BBB-001 -> AAA-001")
        self.assert_cycle(
            item("AAA-001", target="1.3.1") + "- **Depends on:** release:1.3.1:ready\n",
            "AAA-001 -> release:1.3.1:ready -> AAA-001",
        )

    def test_deep_acyclic_graph_is_iterative(self):
        validate_roadmap_dependencies(
            "".join(
                item(f"TASK-{i:04}", f"TASK-{i + 1:04}" if i < 1200 else "none")
                for i in range(1201)
            )
        )

    def test_record_and_close_do_not_return_cyclic_proposals(self):
        record = RoadmapWorkRecord(
            "AAA-001",
            "work",
            "P0",
            "framework",
            "direction",
            "conclusion",
            ("AAA-001",),
            ("implement",),
            ("qualify",),
        )
        with self.assertRaises(RoadmapDependencyError):
            record_work("# Queue\n", record)
        with self.assertRaises(RoadmapDependencyError):
            close_work(item("AAA-001", "AAA-001", state="x"), "AAA-001")

    def project(self, root, content, *, custom=False):
        queue = "docs/roadmap/custom.md" if custom else "docs/roadmap/active-work.md"
        target = root / queue
        target.parent.mkdir(parents=True)
        target.write_text(content)
        manifest = "docs/roadmap/goals.json"
        (root / manifest).write_text(
            json.dumps(
                {
                    "schema": "literate-ai/release-roadmap-goals@1",
                    "roadmap": queue,
                    "releases": {"1.3.1": [{"id": "AAA-001"}]},
                }
            )
        )
        (root / "literate.release.json").write_text(
            json.dumps(
                {
                    "contributions": {"roadmap_goal_manifest": manifest},
                }
            )
        )
        return queue, target

    def test_policy_selected_manifest_and_prospective_overlay_preserve_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue, target = self.project(root, item("AAA-001"), custom=True)
            before = target.read_bytes()
            with self.assertRaises(RoadmapDependencyError):
                require_project_roadmap_acyclic(
                    root,
                    {
                        queue: item("AAA-001", "release:1.3.1:published").encode(),
                    },
                )
            self.assertEqual(target.read_bytes(), before)
            require_project_roadmap_acyclic(root)

    def test_release_readiness_rejects_manual_cycle_even_with_checked_goals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.project(root, item("AAA-001", "release:1.3.1:verified", state="x"))
            with self.assertRaises(ReleaseContributionsError) as caught:
                _roadmap_release_goals(
                    root, release="1.3.1", manifest_path="docs/roadmap/goals.json"
                )
            self.assertEqual(caught.exception.code, "roadmap.dependency_cycle")

    def test_documentation_validation_rejects_manual_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("Read [queue](docs/roadmap/active-work.md).\n")
            with self.assertRaises(DocumentationError) as caught:
                validate_documentation_graph(
                    project_root=root,
                    agent_skill=skill,
                    documents={
                        "docs/roadmap/active-work.md": item(
                            "AAA-001", "AAA-001"
                        ).encode()
                    },
                    assets={},
                )
            self.assertEqual(caught.exception.code, "roadmap.dependency_cycle")

    def test_cli_rejected_write_has_no_temporary_or_partial_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, target = self.project(root, item("AAA-001", state="x"))
            before = target.read_bytes()
            args = SimpleNamespace(
                project=str(root),
                work_command="record",
                work_id="BBB-001",
                title="work",
                priority="P0",
                owner="core",
                direction="direction",
                conclusion="conclusion",
                depends_on=[],
                implementation=["implement"],
                evidence=["qualify"],
                completion_prerequisite=["BBB-001"],
            )
            with patch(
                "literate_ai.cli.work.ProjectConfigurationStore.discover",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(CliFailure):
                    work_from_args(args)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])
            args.completion_prerequisite = ["AAA-001"]
            with patch(
                "literate_ai.cli.work.ProjectConfigurationStore.discover",
                return_value=SimpleNamespace(root=root),
            ):
                result, status = work_from_args(args)
            self.assertEqual(status, 0)
            self.assertTrue(result["changed"])
            self.assertIn("BBB-001", target.read_text())

    def test_update_derived_import_rejects_bad_existing_data_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, target = self.project(root, item("AAA-001", "AAA-001"))
            before = target.read_bytes()
            proposed = SimpleNamespace(
                item_id="UPSTREAM-12345678",
                title="Update",
                direction="Review",
                paths=("README.md",),
            )
            for items in ((proposed,), ()):
                with self.subTest(items=bool(items)):
                    with self.assertRaises(ProjectUpdateWorkItemError):
                        record_work_items(root, items)
                    self.assertEqual(target.read_bytes(), before)

    def test_current_repository_dependency_graph(self):
        require_project_roadmap_acyclic(Path(__file__).resolve().parents[2])

    def test_custom_import_queue_is_checked_without_conventional_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "custom.md"
            queue.write_text(item("AAA-001", "AAA-001"))
            before = queue.read_bytes()
            with self.assertRaises(ProjectUpdateWorkItemError):
                record_work_items(root, (), queue_path="custom.md")
            self.assertEqual(queue.read_bytes(), before)

    def test_cyclic_initialization_template_is_rejected_before_scaffold_writes(self):
        from literate_ai.adapters import project_initialization
        from tests.support.root_parent_adapter import (
            RootParentProjectInitializationAdapter,
        )

        original = project_initialization._template_text

        def template(path):
            if path == "docs/roadmap/active-work.md":
                return item("AAA-001", "AAA-001")
            return original(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            sentinel = root / "README.md"
            sentinel.write_bytes(b"Existing user content\n")
            with patch.object(
                project_initialization, "_template_text", side_effect=template
            ):
                with self.assertRaises(
                    project_initialization.ProjectInitializationError
                ) as caught:
                    RootParentProjectInitializationAdapter(
                        standard_binding_provider=lambda: None,
                    ).initialize(root, empty=True, source_intelligence_provider="none")
            self.assertEqual(caught.exception.code, "roadmap.dependency_cycle")
            self.assertEqual(sentinel.read_bytes(), b"Existing user content\n")
            self.assertFalse((root / "literate.project.json").exists())
            self.assertFalse((root / "docs/roadmap/active-work.md").exists())

    def test_project_authority_validation_rejects_manually_edited_cycle(self):
        from literate_ai.adapters.project_validation import (
            ProjectValidationError,
            validate_project,
        )
        from tests.support.root_parent_adapter import (
            RootParentProjectInitializationAdapter,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            RootParentProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
            ).initialize(root, empty=True, source_intelligence_provider="none")
            queue = root / "docs/roadmap/active-work.md"
            queue.write_text(item("AAA-001", "AAA-001"))
            before = queue.read_bytes()
            with self.assertRaises(ProjectValidationError) as caught:
                validate_project(root, require_authority_review=False)
            self.assertEqual(caught.exception.code, "roadmap.dependency_cycle")
            self.assertEqual(queue.read_bytes(), before)

    def test_native_rewrite_adoption_rejects_before_any_companion_write(self):
        from literate_ai.adapters.harness_inventory import (
            prepare_native_rewrite_program,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "retained").mkdir()
            (root / "docs/decisions").mkdir(parents=True)
            _, queue = self.project(root, item("AAA-001", "AAA-001"))
            decision = root / "docs/decisions/0002-native-literate-ai-rewrite.md"
            decision.write_bytes(b"Existing decision\n")
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with self.assertRaises(RoadmapDependencyError) as caught:
                prepare_native_rewrite_program(
                    {"implementation_directory": "retained"}, project_root=root
                )
            self.assertEqual(caught.exception.code, "roadmap.dependency_cycle")
            self.assertEqual(queue.read_text(), item("AAA-001", "AAA-001"))
            self.assertEqual(
                before,
                {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )

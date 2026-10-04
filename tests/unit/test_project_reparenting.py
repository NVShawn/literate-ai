from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
)
from literate_ai.adapters.project_reparenting import (
    FilesystemRepositoryReparentAdapter,
    RepositoryReparentError,
)
from literate_ai.adapters.project_validation import ProjectValidationError
from literate_ai.adapters.repository_lineage import (
    REPOSITORY_LINEAGE_FILE,
    REPOSITORY_PARENT_FILE,
    FilesystemRepositoryLineageStore,
)
from literate_ai.application.repository_lineage import RepositoryLineageResolutionError
from literate_ai.cli.project import reparent_project_from_args, update_project_from_args
from literate_ai.contracts import (
    ProjectInitializationOrigin,
    RepositoryLineage,
    RepositoryParentSelection,
    RepositoryReparentDisposition,
)
from tests.support.fixtures_test_repository_lineage import fixture
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        "https://example.test/literate-ai.git",
        "a" * 40,
        "literate-ai",
        "0.2.0",
    )


def initialize_root(target: Path) -> None:
    root = RepositoryParentSelection.root()
    FilesystemProjectInitializationAdapter(
        standard_binding_provider=lambda: None,
        initialization_origin_provider=origin,
        repository_lineage_resolver=lambda selection: RepositoryLineage(
            selection, (), ()
        ),
    ).initialize(
        target,
        flavor_selectors=("+python", "+macos"),
        source_intelligence_provider="none",
        empty=True,
        parent_selection=root,
    )


def remove_lineage_evidence(target: Path) -> None:
    (target / REPOSITORY_PARENT_FILE).unlink()
    (target / REPOSITORY_LINEAGE_FILE).unlink()


class RepositoryReparentTests(unittest.TestCase):
    def test_cli_none_is_explicit_root_and_planning_does_not_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            before = FilesystemRepositoryLineageStore(target).load()

            result = reparent_project_from_args(
                Namespace(parent="none", project=str(target), apply=False)
            )

            self.assertEqual(result["mode"], "read-only-plan")
            self.assertEqual(result["prospective_selection"]["mode"], "root")
            self.assertFalse(result["changed"])
            self.assertNotIn("applied", result)
            self.assertEqual(FilesystemRepositoryLineageStore(target).load(), before)

    def test_plan_is_read_only_and_apply_rechecks_then_changes_authority(self) -> None:
        prospective, root_node, child_node, lineage = fixture()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: lineage
            )

            plan = adapter.plan(target, prospective)
            before = FilesystemRepositoryLineageStore(target).load()

            self.assertTrue(plan.changed)
            self.assertTrue(plan.previous_evidence_present)
            self.assertEqual(before[0].mode.value, "root")
            self.assertEqual(
                [item.disposition for item in plan.changes],
                [
                    RepositoryReparentDisposition.ADDED,
                    RepositoryReparentDisposition.ADDED,
                ],
            )
            self.assertEqual(
                {item.prospective_node for item in plan.changes},
                {root_node, child_node},
            )
            SchemaCatalog().validate(plan.SCHEMA, plan.to_dict())

            applied = adapter.apply(target, plan)

            self.assertEqual(applied["state"], "applied")
            self.assertTrue(applied["authority_review_required"])
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load(),
                (prospective, lineage),
            )

    def test_legacy_project_bootstraps_inherited_lineage_explicitly(self) -> None:
        prospective, _root_node, _child_node, lineage = fixture()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            remove_lineage_evidence(target)
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: lineage
            )

            plan = adapter.plan(target, prospective)

            self.assertFalse(plan.previous_evidence_present)
            self.assertTrue(plan.changed)
            self.assertIsNone(FilesystemRepositoryLineageStore(target).load_optional())

            applied = adapter.apply(target, plan)

            self.assertEqual(applied["state"], "applied")
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load(),
                (prospective, lineage),
            )

    def test_legacy_project_reparent_none_materializes_explicit_root(self) -> None:
        root = RepositoryParentSelection.root()
        root_lineage = RepositoryLineage(root, (), ())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            remove_lineage_evidence(target)
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: root_lineage
            )

            plan = adapter.plan(target, root)
            applied = adapter.apply(target, plan)

            self.assertTrue(plan.changed)
            self.assertEqual(applied["state"], "applied")
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load(),
                (root, root_lineage),
            )

    def test_partial_legacy_lineage_evidence_is_rejected(self) -> None:
        root = RepositoryParentSelection.root()
        root_lineage = RepositoryLineage(root, (), ())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            (target / REPOSITORY_LINEAGE_FILE).unlink()
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: root_lineage
            )

            with self.assertRaises(RepositoryReparentError) as raised:
                adapter.plan(target, root)
        self.assertEqual(
            raised.exception.code, "repository_lineage.evidence_incomplete"
        )

    def test_legacy_apply_rejects_lineage_created_after_planning(self) -> None:
        prospective, _root_node, _child_node, lineage = fixture()
        root = RepositoryParentSelection.root()
        root_lineage = RepositoryLineage(root, (), ())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            remove_lineage_evidence(target)
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: lineage
            )
            plan = adapter.plan(target, prospective)
            FilesystemRepositoryLineageStore(target).replace(
                root, root_lineage, expected_absent=True
            )

            with self.assertRaises(RepositoryReparentError) as raised:
                adapter.apply(target, plan)
        self.assertEqual(raised.exception.code, "repository_lineage.concurrent_change")

    def test_apply_refuses_a_changed_prospective_chain(self) -> None:
        prospective, root_node, child_node, lineage = fixture()
        moved_child = replace(child_node, resolved_revision="c" * 40)
        moved_lineage = RepositoryLineage(
            prospective, (root_node, moved_child), (moved_child.identity,)
        )
        calls = 0

        def moving(selection):
            nonlocal calls
            calls += 1
            return lineage if calls == 1 else moved_lineage

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            adapter = FilesystemRepositoryReparentAdapter(resolver=moving)
            plan = adapter.plan(target, prospective)

            with self.assertRaises(RepositoryReparentError) as raised:
                adapter.apply(target, plan)

            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load()[0].mode.value,
                "root",
            )
        self.assertEqual(
            raised.exception.code, "repository_reparent.prospective_changed"
        )

    def test_cycle_fails_during_plan_before_lineage_authority_changes(self) -> None:
        prospective, _root_node, _child_node, _lineage = fixture()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            before = FilesystemRepositoryLineageStore(target).load()

            def cyclic(_selection):
                raise RepositoryLineageResolutionError(
                    "repository_lineage.cycle",
                    "repository parent declarations contain a cycle: "
                    "repository:a -> repository:b -> repository:a",
                )

            with self.assertRaises(RepositoryReparentError) as raised:
                FilesystemRepositoryReparentAdapter(resolver=cyclic).plan(
                    target, prospective
                )

            self.assertEqual(raised.exception.code, "repository_lineage.cycle")
            self.assertEqual(FilesystemRepositoryLineageStore(target).load(), before)

    def test_failed_validation_rolls_back_both_lineage_documents(self) -> None:
        prospective, _root_node, _child_node, lineage = fixture()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            before = FilesystemRepositoryLineageStore(target).load()
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: lineage
            )
            plan = adapter.plan(target, prospective)

            with mock.patch(
                "literate_ai.adapters.project_reparenting.validate_project",
                side_effect=ProjectValidationError("injected", "failure"),
            ):
                with self.assertRaises(RepositoryReparentError) as raised:
                    adapter.apply(target, plan)

            self.assertEqual(FilesystemRepositoryLineageStore(target).load(), before)
        self.assertEqual(raised.exception.code, "repository_reparent.validation_failed")

    def test_composite_update_can_stage_and_explicitly_rollback_reparent(self) -> None:
        prospective, _root_node, _child_node, lineage = fixture()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            before = FilesystemRepositoryLineageStore(target).load()
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: lineage
            )
            plan = adapter.plan(target, prospective)

            with mock.patch(
                "literate_ai.adapters.project_reparenting.validate_project"
            ) as validation:
                applied = adapter.apply(target, plan, validate_authority=False)

            validation.assert_not_called()
            self.assertEqual(applied["state"], "applied")
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load(),
                (prospective, lineage),
            )

            adapter.rollback_staged_update(target, plan)

            self.assertEqual(FilesystemRepositoryLineageStore(target).load(), before)

    def test_failed_legacy_validation_rolls_back_to_absence(self) -> None:
        prospective, _root_node, _child_node, lineage = fixture()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)
            remove_lineage_evidence(target)
            store = FilesystemRepositoryLineageStore(target)
            adapter = FilesystemRepositoryReparentAdapter(
                resolver=lambda selection: lineage
            )
            plan = adapter.plan(target, prospective)

            with mock.patch(
                "literate_ai.adapters.project_reparenting.validate_project",
                side_effect=ProjectValidationError("injected", "failure"),
            ):
                with self.assertRaises(RepositoryReparentError) as raised:
                    adapter.apply(target, plan)

            self.assertIsNone(store.load_optional())
        self.assertEqual(raised.exception.code, "repository_reparent.validation_failed")

    def test_explicit_root_makes_update_a_typed_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            initialize_root(target)

            result = update_project_from_args(Namespace(path=target))

        self.assertEqual(result["schema"], "literate-ai/project-update-noop@1")
        self.assertEqual(result["mode"], "no-op")
        self.assertIn("explicit-root", result["reason"])


if __name__ == "__main__":
    unittest.main()

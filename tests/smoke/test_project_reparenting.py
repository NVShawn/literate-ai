from __future__ import annotations

import tempfile
import unittest
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
    FilesystemRepositoryLineageStore,
)
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


class RepositoryReparentTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

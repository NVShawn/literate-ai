from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
)
from literate_ai.adapters.project_updates import FilesystemProjectUpdateAdapter
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
)
from literate_ai.adapters.repository_catalogs import (
    InheritedCatalogFile,
    InheritedCatalogItem,
    InheritedCatalogPlan,
    catalog_imports_for_plan,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.adapters.repository_updates import (
    FilesystemRepositoryUpdateAdapter,
    RepositoryUpdateError,
)
from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.project import update_project_from_args
from literate_ai.contracts import (
    CatalogImportsFile,
    ProjectInitializationOrigin,
    RepositoryLineage,
)
from tests.support.fixtures_test_repository_lineage import fixture, identity

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "literate_ai" / "project_template"


def origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        "https://example.test/literate-ai.git",
        "a" * 40,
        "literate-ai",
        "0.2.0",
    )


def flavor_item(node, name: str, source_name: str) -> InheritedCatalogItem:
    source = TEMPLATE / "flavors" / source_name
    return InheritedCatalogItem(
        "flavor",
        name,
        node,
        tuple(
            InheritedCatalogFile(
                f"flavors/{name}/{path.relative_to(source).as_posix()}",
                path.read_bytes(),
                False,
            )
            for path in sorted(source.rglob("*"))
            if path.is_file()
        ),
    )


def skill_item(node, name: str, source_name: str) -> InheritedCatalogItem:
    source = TEMPLATE / "skills" / "specification-to-source" / source_name
    return InheritedCatalogItem(
        "skill",
        name,
        node,
        (
            InheritedCatalogFile(
                f"skills/specification-to-source/{source_name}/SKILL.md",
                (source / "SKILL.md").read_bytes(),
                False,
            ),
        ),
    )


class RepositoryLineageUpdateTests(unittest.TestCase):
    def test_unchanged_apply_preserves_import_bytes_and_timestamp(self) -> None:
        selection, _root_node, child_node, lineage = fixture()
        item = flavor_item(child_node, "lang-python", "lang-python")
        catalogs = InheritedCatalogPlan(lineage, (item,))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: lineage,
                repository_catalog_planner=lambda _lineage: catalogs,
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: lineage,
                catalog_planner=lambda _lineage: catalogs,
            )
            imports_path = target / CatalogImportsFile.PATH
            before = imports_path.read_bytes(), imports_path.stat().st_mtime_ns
            finalized = []
            with patch(
                "literate_ai.adapters.repository_updates.catalog_imports_for_plan",
                side_effect=lambda plan: catalog_imports_for_plan(
                    plan, materialized_at="2040-01-01T00:00:00+00:00"
                ),
            ):
                applied = adapter.apply(
                    target, adapter.plan(target), finalizer=finalized.append
                )
            self.assertEqual(finalized, [target.resolve()])
            self.assertFalse(applied.applied)
            self.assertEqual(
                (imports_path.read_bytes(), imports_path.stat().st_mtime_ns), before
            )

    def test_cli_takes_reviewed_catalog_conflict_in_dependency_closed_transaction(
        self,
    ) -> None:
        """A new dependent skill and its reviewed conflict commit atomically (#244)."""

        selection, root_node, child_node, previous = fixture()
        updated_child = replace(
            child_node,
            project_identity=identity("8"),
            resolved_revision="8" * 40,
        )
        prospective = RepositoryLineage(
            selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        mcp = skill_item(
            updated_child,
            "specification-to-source/mcp-application",
            "mcp-application",
        )
        rust = skill_item(
            updated_child,
            "specification-to-source/backend-application/rust-service-application",
            "backend-application/rust-service-application",
        )
        backend = skill_item(
            updated_child,
            "specification-to-source/backend-application",
            "backend-application",
        )
        prospective_plan = InheritedCatalogPlan(prospective, (backend, mcp, rust))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, ()
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            mcp_path = target / mcp.files[0].destination
            rust_path = target / rust.files[0].destination
            mcp_path.write_bytes(mcp_path.read_bytes() + b"\nlocal staged parent\n")
            local_mcp = mcp_path.read_bytes()
            rust_path.unlink()
            imports_before = (target / CatalogImportsFile.PATH).read_bytes()

            real_framework_adapter = FilesystemProjectUpdateAdapter

            def framework_adapter(**kwargs):
                return real_framework_adapter(origin_provider=origin, **kwargs)

            def dependency_validation(root: Path, **_kwargs):
                documents = {
                    item.files[0].destination: (
                        root / item.files[0].destination
                    ).read_bytes()
                    for item in (backend, mcp, rust)
                }
                try:
                    AgentSkillCatalog.from_documents(
                        documents,
                        validate_dependencies=True,
                        validate_references=False,
                    )
                except AgentSkillCatalogError as exc:
                    raise ProjectValidationError(exc.code, exc.message) from exc
                return {}

            def invoke(take_upstream: list[str]):
                with (
                    patch(
                        "literate_ai.cli.project._repository_fetch_provider",
                        return_value=SimpleNamespace(
                            deadline_evidence={"fixture": True},
                            update_blobs=lambda *_args: {},
                        ),
                    ),
                    patch(
                        "literate_ai.cli.project.resolve_repository_lineage",
                        return_value=prospective,
                    ),
                    patch(
                        "literate_ai.cli.project.plan_inherited_catalogs",
                        return_value=prospective_plan,
                    ),
                    patch(
                        "literate_ai.cli.project.FilesystemProjectUpdateAdapter",
                        side_effect=framework_adapter,
                    ),
                    patch(
                        "literate_ai.cli.project.validate_project",
                        side_effect=dependency_validation,
                    ),
                ):
                    return update_project_from_args(
                        Namespace(
                            path=target,
                            apply=True,
                            adopt_added=True,
                            take_upstream=take_upstream,
                            record_work_items=False,
                            unpin=False,
                            follow_ref=None,
                            allow_major=False,
                            review_conflicts=False,
                        )
                    )

            with self.assertRaises(CliFailure) as raised:
                invoke([])

            self.assertEqual(
                raised.exception.code, "repository_update.validation_failed"
            )
            self.assertEqual(mcp_path.read_bytes(), local_mcp)
            self.assertFalse(rust_path.exists())
            self.assertEqual(
                (target / CatalogImportsFile.PATH).read_bytes(), imports_before
            )
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load(),
                (selection, previous),
            )

            with self.assertRaises(CliFailure) as invalid:
                invoke([rust.files[0].destination])
            self.assertEqual(
                invalid.exception.code,
                "repository_update.take_upstream_not_conflict",
            )
            self.assertEqual(mcp_path.read_bytes(), local_mcp)
            self.assertFalse(rust_path.exists())

            result = invoke([mcp.files[0].destination])

            self.assertEqual(result["mode"], "applied")
            self.assertEqual(mcp_path.read_bytes(), mcp.files[0].content)
            self.assertEqual(rust_path.read_bytes(), rust.files[0].content)
            self.assertEqual(
                result["repository_lineage"]["applied"]["taken_upstream"],
                [mcp.files[0].destination],
            )
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load()[1], prospective
            )
            dependency_validation(target)

    def test_retired_catalog_removal_rolls_back_files_provenance_and_lineage(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        retired = flavor_item(child_node, "retired-platform", "os-linux")
        updated_child = replace(
            child_node,
            project_identity=identity("9"),
            resolved_revision="9" * 40,
        )
        prospective = RepositoryLineage(
            selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (retired,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            retired_content = {
                file.destination: (target / file.destination).read_bytes()
                for file in retired.files
            }
            imports_path = target / CatalogImportsFile.PATH
            imports_content = imports_path.read_bytes()
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(prospective, ()),
            )
            planned = adapter.plan(target)

            with patch(
                "literate_ai.adapters.repository_updates.validate_project",
                side_effect=ProjectValidationError(
                    "project.invalid", "invalid catalog path flavors/retired-platform"
                ),
            ):
                with self.assertRaises(RepositoryUpdateError) as raised:
                    adapter.apply(target, planned)

            self.assertEqual(
                raised.exception.code, "repository_update.validation_failed"
            )
            self.assertIn("project.invalid", raised.exception.message)
            self.assertIn("flavors/retired-platform", raised.exception.message)
            self.assertEqual(imports_path.read_bytes(), imports_content)
            self.assertEqual(
                {path: (target / path).read_bytes() for path in retired_content},
                retired_content,
            )
            _selection, current = FilesystemRepositoryLineageStore(target).load()
            self.assertEqual(current, previous)


if __name__ == "__main__":
    unittest.main()

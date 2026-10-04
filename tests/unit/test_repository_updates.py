from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters import project_updates
from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
    _template_text,
)
from literate_ai.adapters.project_reparenting import FilesystemRepositoryReparentAdapter
from literate_ai.adapters.project_updates import FilesystemProjectUpdateAdapter
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    validate_project,
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
from literate_ai.application.repository_lineage import RepositoryLineageResolutionError
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.project import update_project_from_args
from literate_ai.contracts import (
    CatalogImportsFile,
    ProjectInitializationOrigin,
    ProjectUpdateClassification,
    RepositoryLineage,
    RepositoryLineageUpdatePlan,
    RepositoryParentReference,
    RepositoryParentSelection,
)
from tests.support.fixtures_test_repository_lineage import fixture, identity
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

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

    def test_changed_import_provenance_gets_a_new_timestamp_only_once(self) -> None:
        for change_content in (False, True):
            with self.subTest(change_content=change_content):
                selection, root_node, child_node, previous = fixture()
                previous_item = flavor_item(child_node, "lang-python", "lang-python")
                updated_child = replace(
                    child_node,
                    project_identity=identity("7"),
                    resolved_revision="7" * 40,
                )
                prospective = RepositoryLineage(
                    selection, (root_node, updated_child), (updated_child.identity,)
                )
                updated_item = replace(previous_item, source=updated_child)
                if change_content:
                    updated_item = replace(
                        updated_item,
                        files=tuple(
                            replace(file, content=file.content + b"\n<!-- update -->\n")
                            if file.destination.endswith("flavor.md")
                            else file
                            for file in updated_item.files
                        ),
                    )
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory) / "project"
                    FilesystemProjectInitializationAdapter(
                        standard_binding_provider=lambda: None,
                        initialization_origin_provider=origin,
                        repository_lineage_resolver=Mock(return_value=previous),
                        repository_catalog_planner=Mock(
                            return_value=InheritedCatalogPlan(
                                previous, (previous_item,)
                            )
                        ),
                    ).initialize(
                        target,
                        flavor_selectors=("+python", "+macos"),
                        source_intelligence_provider="none",
                        empty=True,
                        parent_selection=selection,
                    )
                    adapter = FilesystemRepositoryUpdateAdapter(
                        lineage_resolver=Mock(return_value=prospective),
                        catalog_planner=Mock(
                            return_value=InheritedCatalogPlan(
                                prospective, (updated_item,)
                            )
                        ),
                    )
                    for timestamp in (
                        "2040-01-01T00:00:00+00:00",
                        "2040-01-02T00:00:00+00:00",
                    ):
                        with patch(
                            "literate_ai.adapters.repository_updates.catalog_imports_for_plan",
                            side_effect=lambda plan, timestamp=timestamp: (
                                catalog_imports_for_plan(
                                    plan, materialized_at=timestamp
                                )
                            ),
                        ):
                            adapter.apply(target, adapter.plan(target))
                        imported = CatalogImportsFile.load(target).imports[0]
                        self.assertEqual(
                            imported.copied_at, "2040-01-01T00:00:00+00:00"
                        )
                        self.assertEqual(
                            imported.source.ref,
                            f"git:{updated_child.repository_url}@{updated_child.resolved_revision}",
                        )
                        for file in updated_item.files:
                            self.assertEqual(
                                (target / file.destination).read_bytes(), file.content
                            )

    def test_unchanged_imports_survive_failed_final_validation_without_rewrite(
        self,
    ) -> None:
        selection, _root_node, child_node, lineage = fixture()
        catalogs = InheritedCatalogPlan(
            lineage, (flavor_item(child_node, "lang-python", "lang-python"),)
        )
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

            def reject(_root: Path) -> None:
                raise ProjectValidationError("project.invalid", "intentional failure")

            with self.assertRaises(RepositoryUpdateError) as raised:
                adapter.apply(target, adapter.plan(target), finalizer=reject)
            self.assertEqual(
                raised.exception.code, "repository_update.validation_failed"
            )
            self.assertEqual(
                (imports_path.read_bytes(), imports_path.stat().st_mtime_ns), before
            )

    def test_cli_take_upstream_requires_apply(self) -> None:
        with self.assertRaises(CliFailure) as raised:
            update_project_from_args(
                Namespace(path="missing", apply=False, take_upstream=["skill/SKILL.md"])
            )

        self.assertEqual(
            raised.exception.code,
            "repository_update.resolution_requires_apply",
        )

    def test_cli_rolls_back_staged_follow_when_combined_planning_fails(self) -> None:
        selection, root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")
        follow_selection = RepositoryParentSelection.inherit(
            (RepositoryParentReference(child_node.repository_url, "next"),)
        )
        followed_child = replace(
            child_node,
            requested_revision="next",
            resolved_revision="d" * 40,
            project_identity=identity("d"),
        )
        followed = RepositoryLineage(
            follow_selection,
            (root_node, followed_child),
            (followed_child.identity,),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
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
                    return_value=followed,
                ),
                patch(
                    "literate_ai.adapters.repository_updates."
                    "FilesystemRepositoryUpdateAdapter.plan",
                    side_effect=RepositoryUpdateError("injected", "planning failed"),
                ),
            ):
                with self.assertRaises(CliFailure) as raised:
                    update_project_from_args(
                        Namespace(
                            path=target,
                            apply=True,
                            adopt_added=True,
                            record_work_items=False,
                            unpin=False,
                            follow_ref="next",
                            allow_major=False,
                            review_conflicts=False,
                        )
                    )

            self.assertEqual(raised.exception.code, "injected")
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load(),
                (selection, previous),
            )

    def test_cli_validates_parent_and_framework_changes_as_one_complete_tree(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")
        retired = flavor_item(child_node, "retired-platform", "os-linux")
        updated_child = replace(
            child_node,
            project_identity=identity("c"),
            resolved_revision="c" * 40,
        )
        prospective = RepositoryLineage(
            selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        updated_files = tuple(
            replace(file, content=file.content + b"\n<!-- current parent -->\n")
            if file.destination.endswith("flavor.md")
            else file
            for file in previous_item.files
        )
        added = flavor_item(updated_child, "added-platform", "os-linux")
        prospective_plan = InheritedCatalogPlan(
            prospective,
            (
                InheritedCatalogItem(
                    "flavor", "lang-python", updated_child, updated_files
                ),
                added,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item, retired)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            real_framework_adapter = FilesystemProjectUpdateAdapter

            def framework_adapter(**kwargs):
                return real_framework_adapter(origin_provider=origin, **kwargs)

            def final_validation(root: Path, **_kwargs):
                self.assertIn(
                    "current parent",
                    (root / "flavors/lang-python/flavor.md").read_text(),
                )
                self.assertTrue((root / "flavors/added-platform/flavor.md").is_file())
                self.assertTrue(
                    all((root / item.destination).is_file() for item in retired.files)
                )
                self.assertTrue((root / "UPSTREAM.md").is_file())
                return {}

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
                    "literate_ai.adapters.project_updates._TEMPLATE_FILES",
                    {
                        **project_updates._TEMPLATE_FILES,
                        "UPSTREAM.md": "AGENTS.md",
                    },
                ),
                patch(
                    "literate_ai.adapters.repository_updates.validate_project",
                    side_effect=AssertionError("intermediate validation ran"),
                ) as intermediate_validation,
                patch(
                    "literate_ai.cli.project.validate_project",
                    side_effect=final_validation,
                ) as complete_validation,
            ):
                result = update_project_from_args(
                    Namespace(
                        path=target,
                        apply=True,
                        adopt_added=True,
                        keep_local=[item.destination for item in retired.files],
                        record_work_items=False,
                        unpin=False,
                        follow_ref=None,
                        allow_major=False,
                        review_conflicts=False,
                    )
                )

            self.assertEqual(result["mode"], "applied")
            self.assertEqual(
                result["repository_lineage"]["applied"]["kept_local"],
                sorted(item.destination for item in retired.files),
            )
            intermediate_validation.assert_not_called()
            complete_validation.assert_called_once()

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

    def test_cycle_fails_during_plan_before_import_or_lineage_mutation(self) -> None:
        selection, _root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            lineage_before = FilesystemRepositoryLineageStore(target).load()
            imports_before = (target / CatalogImportsFile.PATH).read_bytes()

            def cyclic(_selection):
                raise RepositoryLineageResolutionError(
                    "repository_lineage.cycle",
                    "repository parent declarations contain a cycle: "
                    "repository:a -> repository:b -> repository:a",
                )

            with self.assertRaises(RepositoryUpdateError) as raised:
                FilesystemRepositoryUpdateAdapter(
                    lineage_resolver=cyclic,
                    catalog_planner=lambda _lineage: self.fail(
                        "catalog planning must not follow a failed graph solve"
                    ),
                ).plan(target)

            self.assertEqual(raised.exception.code, "repository_lineage.cycle")
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load(), lineage_before
            )
            self.assertEqual(
                (target / CatalogImportsFile.PATH).read_bytes(), imports_before
            )

    def test_apply_removes_retired_unchanged_catalog_files_and_provenance(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        retired = flavor_item(child_node, "retired-platform", "os-linux")
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
            retired_paths = tuple(file.destination for file in retired.files)
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(prospective, ()),
            )

            planned = adapter.plan(target)
            by_path = {
                item.path: item.classification for item in planned.contract.files
            }
            self.assertTrue(retired_paths)
            self.assertTrue(
                all(
                    by_path[path] is ProjectUpdateClassification.UPSTREAM_ONLY
                    for path in retired_paths
                )
            )
            applied = adapter.apply(target, planned)

            self.assertEqual(applied.applied, tuple(sorted(retired_paths)))
            self.assertTrue(all(not (target / path).exists() for path in retired_paths))
            self.assertNotIn(
                ("flavor", "retired-platform"),
                {
                    (item.kind, item.name)
                    for item in CatalogImportsFile.load(target).imports
                },
            )
            _selection, current = FilesystemRepositoryLineageStore(target).load()
            self.assertEqual(current, prospective)

    def test_apply_can_keep_reviewed_retired_catalog_paths_as_local_authority(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        retired = flavor_item(child_node, "retired-platform", "os-linux")
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
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(prospective, ()),
            )
            planned = adapter.plan(target)
            kept = frozenset(retired_content)

            applied = adapter.apply(
                target,
                planned,
                keep_local=kept,
                finalizer=lambda root: self.assertTrue(
                    all((root / path).is_file() for path in kept)
                ),
            )

            self.assertEqual(applied.applied, ())
            self.assertEqual(applied.kept_local, tuple(sorted(kept)))
            self.assertEqual(
                {path: (target / path).read_bytes() for path in kept},
                retired_content,
            )
            self.assertNotIn(
                ("flavor", "retired-platform"),
                {
                    (item.kind, item.name)
                    for item in CatalogImportsFile.load(target).imports
                },
            )
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load()[1], prospective
            )

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

    def test_finalizer_observes_complete_catalog_and_rolls_back_as_one_unit(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        retired = flavor_item(child_node, "retired-platform", "os-linux")
        updated_child = replace(
            child_node,
            project_identity=identity("b"),
            resolved_revision="b" * 40,
        )
        prospective = RepositoryLineage(
            selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        added = flavor_item(updated_child, "added-platform", "os-macos")
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
            before_imports = (target / CatalogImportsFile.PATH).read_bytes()
            retired_content = {
                item.destination: (target / item.destination).read_bytes()
                for item in retired.files
            }
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    prospective, (added,)
                ),
            )
            plan = adapter.plan(target)

            def reject_complete_tree(root: Path) -> None:
                self.assertTrue(
                    all(
                        not (root / item.destination).exists() for item in retired.files
                    )
                )
                self.assertTrue(
                    all((root / item.destination).is_file() for item in added.files)
                )
                self.assertEqual(
                    FilesystemRepositoryLineageStore(root).load()[1], prospective
                )
                raise ProjectValidationError("project.invalid", "complete rejection")

            with self.assertRaises(RepositoryUpdateError) as raised:
                adapter.apply(
                    target,
                    plan,
                    adopt_added=True,
                    finalizer=reject_complete_tree,
                )

            self.assertEqual(
                raised.exception.code, "repository_update.validation_failed"
            )
            self.assertEqual(
                {path: (target / path).read_bytes() for path in retired_content},
                retired_content,
            )
            self.assertTrue(
                all(not (target / item.destination).exists() for item in added.files)
            )
            self.assertEqual(
                (target / CatalogImportsFile.PATH).read_bytes(), before_imports
            )
            self.assertEqual(
                FilesystemRepositoryLineageStore(target).load()[1], previous
            )

    def test_failed_added_catalog_removes_transaction_directory_skeletons(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")
        updated_child = replace(
            child_node,
            project_identity=identity("7"),
            resolved_revision="7" * 40,
        )
        prospective = RepositoryLineage(
            selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        added = flavor_item(updated_child, "lang-rust", "lang-rust")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            added_root = target / "flavors" / "lang-rust"
            self.assertFalse(added_root.exists())
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    prospective,
                    (
                        flavor_item(updated_child, "lang-python", "lang-python"),
                        added,
                    ),
                ),
            )
            planned = adapter.plan(target)

            with patch(
                "literate_ai.adapters.repository_updates.validate_project",
                side_effect=ProjectValidationError(
                    "project.invalid", "invalid catalog path flavors/lang-rust"
                ),
            ):
                with self.assertRaises(RepositoryUpdateError):
                    adapter.apply(target, planned, adopt_added=True)

            self.assertFalse(added_root.exists())
            self.assertFalse(any(target.rglob("*lang-rust*")))
            _selection, current = FilesystemRepositoryLineageStore(target).load()
            self.assertEqual(current, previous)

    def test_retired_locally_changed_catalog_file_becomes_local_authority(self) -> None:
        selection, root_node, child_node, previous = fixture()
        retired = flavor_item(child_node, "retired-platform", "os-linux")
        updated_child = replace(
            child_node,
            project_identity=identity("a"),
            resolved_revision="a" * 40,
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
            changed_path = retired.files[0].destination
            changed = target / changed_path
            changed.write_bytes(changed.read_bytes() + b"\nlocal authority\n")
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(prospective, ()),
            )

            planned = adapter.plan(target)

            by_path = {
                item.path: item.classification for item in planned.contract.files
            }
            self.assertEqual(
                by_path[changed_path], ProjectUpdateClassification.LOCAL_ONLY
            )
            self.assertTrue(
                all(
                    by_path[file.destination] is ProjectUpdateClassification.LOCAL_ONLY
                    for file in retired.files[1:]
                )
            )
            applied = adapter.apply(target, planned)
            self.assertTrue(
                all((target / file.destination).is_file() for file in retired.files)
            )
            refused = dict(applied.refused)[ProjectUpdateClassification.LOCAL_ONLY]
            self.assertEqual(refused, tuple(file.destination for file in retired.files))
            remaining = CatalogImportsFile.load(target).imports
            self.assertEqual(len(remaining), 0)

    def test_update_after_exact_same_repository_reparent_replaces_old_provenance(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")
        revision = "f" * 40
        updated_child = replace(
            child_node,
            project_identity=identity("7"),
            requested_revision=revision,
            resolved_revision=revision,
        )
        prospective_selection = RepositoryParentSelection.inherit(
            (RepositoryParentReference(child_node.repository_url, revision),)
        )
        prospective = RepositoryLineage(
            prospective_selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        updated_files = tuple(
            replace(file, content=file.content + b"\n<!-- reparented -->\n")
            if file.destination.endswith("flavor.md")
            else file
            for file in previous_item.files
        )
        updated_plan = InheritedCatalogPlan(
            prospective,
            (
                InheritedCatalogItem(
                    "flavor", "lang-python", updated_child, updated_files
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            reparent = FilesystemRepositoryReparentAdapter(
                resolver=lambda _selection: prospective
            )
            reparent.apply(target, reparent.plan(target, prospective_selection))
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: updated_plan,
            )

            planned = adapter.plan(target)
            by_path = {
                item.path: item.classification for item in planned.contract.files
            }
            self.assertEqual(
                by_path["flavors/lang-python/flavor.md"],
                ProjectUpdateClassification.UPSTREAM_ONLY,
            )
            adapter.apply(target, planned)

            self.assertIn(
                "<!-- reparented -->",
                (target / "flavors" / "lang-python" / "flavor.md").read_text(),
            )
            imports = CatalogImportsFile.load(target)
            imported = next(
                item
                for item in imports.imports
                if item.kind == "flavor" and item.name == "lang-python"
            )
            self.assertEqual(
                imported.source.ref,
                f"git:{updated_child.repository_url}@{revision}",
            )

    def test_different_repository_import_collision_keeps_the_local_entry(self) -> None:
        # #133: a single catalog-import identity collision (here: a locally
        # re-attributed foreign source for the same (kind, name) an inherited
        # recomputation would also claim) must not abort the whole apply.
        # The project's existing local provenance record wins for that one
        # key; every file (safe or not) is still governed independently by
        # its own per-file classification, exactly as before this fix.
        selection, _root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            before_content = (
                target / "flavors" / "lang-python" / "flavor.md"
            ).read_text()
            imports = CatalogImportsFile.load(target)
            foreign_ref = "git:https://example.test/other.git@" + "c" * 40
            foreign = replace(
                imports.imports[0],
                source=replace(imports.imports[0].source, ref=foreign_ref),
            )
            CatalogImportsFile((foreign,)).save(target)
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: previous,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            )
            planned = adapter.plan(target)

            result = adapter.apply(target, planned)

            self.assertEqual(result.applied, ())
            self.assertEqual(result.refused, ())
            self.assertEqual(
                (target / "flavors" / "lang-python" / "flavor.md").read_text(),
                before_content,
            )
            final_imports = CatalogImportsFile.load(target)
            kept = next(
                item
                for item in final_imports.imports
                if item.kind == "flavor" and item.name == "lang-python"
            )
            self.assertEqual(kept.source.ref, foreign_ref)

    def test_catalog_import_collision_does_not_block_unrelated_safe_files(self) -> None:
        # #133: a real reproduction combining a catalog-import collision on
        # one item (python, locally re-attributed to a foreign source) with a
        # genuine, unrelated upstream content change on a second item
        # (macos). The macos file must still apply; the collision must not
        # abort it.
        selection, root_node, child_node, previous = fixture()
        previous_python = flavor_item(child_node, "lang-python", "lang-python")
        previous_macos = flavor_item(child_node, "os-macos", "os-macos")
        revision = "f" * 40
        updated_child = replace(
            child_node,
            project_identity=identity("7"),
            requested_revision=revision,
            resolved_revision=revision,
        )
        prospective_selection = RepositoryParentSelection.inherit(
            (RepositoryParentReference(child_node.repository_url, revision),)
        )
        prospective = RepositoryLineage(
            prospective_selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        updated_macos_files = tuple(
            replace(file, content=file.content + b"\n<!-- reparented -->\n")
            if file.destination.endswith("flavor.md")
            else file
            for file in previous_macos.files
        )
        updated_plan = InheritedCatalogPlan(
            prospective,
            (
                InheritedCatalogItem(
                    "flavor", "lang-python", updated_child, previous_python.files
                ),
                InheritedCatalogItem(
                    "flavor", "os-macos", updated_child, updated_macos_files
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_python, previous_macos)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            imports = CatalogImportsFile.load(target)
            python_import = next(
                item
                for item in imports.imports
                if item.kind == "flavor" and item.name == "lang-python"
            )
            foreign_ref = "git:https://example.test/other.git@" + "c" * 40
            foreign = replace(
                python_import,
                source=replace(python_import.source, ref=foreign_ref),
            )
            other_imports = tuple(
                item
                for item in imports.imports
                if not (item.kind == "flavor" and item.name == "lang-python")
            )
            CatalogImportsFile((foreign, *other_imports)).save(target)
            reparent = FilesystemRepositoryReparentAdapter(
                resolver=lambda _selection: prospective
            )
            reparent.apply(target, reparent.plan(target, prospective_selection))
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: updated_plan,
            )
            planned = adapter.plan(target)
            by_path = {
                item.path: item.classification for item in planned.contract.files
            }
            self.assertEqual(
                by_path["flavors/os-macos/flavor.md"],
                ProjectUpdateClassification.UPSTREAM_ONLY,
            )

            result = adapter.apply(target, planned)

            self.assertIn("flavors/os-macos/flavor.md", result.applied)
            self.assertIn(
                "<!-- reparented -->",
                (target / "flavors" / "os-macos" / "flavor.md").read_text(),
            )
            final_imports = CatalogImportsFile.load(target)
            kept_python = next(
                item
                for item in final_imports.imports
                if item.kind == "flavor" and item.name == "lang-python"
            )
            self.assertEqual(kept_python.source.ref, foreign_ref)

    def test_re_resolves_complete_lineage_and_classifies_inherited_files(self) -> None:
        selection, root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")
        updated_child = replace(
            child_node,
            project_identity=identity("5"),
            resolved_revision="e" * 40,
        )
        prospective = RepositoryLineage(
            selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        changed_files = []
        for file in previous_item.files:
            content = file.content
            if file.destination.endswith("flavor.md"):
                content += b"\n<!-- upstream -->\n"
            if file.destination.endswith("toolchain.json"):
                content += b"\n"
            changed_files.append(replace(file, content=content))
        prospective_items = (
            InheritedCatalogItem(
                "flavor", "lang-python", updated_child, tuple(changed_files)
            ),
            flavor_item(updated_child, "new-platform", "os-linux"),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            local = target / "flavors" / "lang-python" / "toolchain.json"
            local.write_bytes(local.read_bytes() + b"local\n")

            planned = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    prospective, prospective_items
                ),
            ).plan(target)

        by_path = {item.path: item.classification for item in planned.contract.files}
        self.assertEqual(
            by_path["flavors/lang-python/flavor.md"],
            ProjectUpdateClassification.UPSTREAM_ONLY,
        )
        self.assertEqual(
            by_path["flavors/lang-python/toolchain.json"],
            ProjectUpdateClassification.CONFLICT,
        )
        self.assertEqual(
            by_path["flavors/new-platform/flavor.md"],
            ProjectUpdateClassification.UPSTREAM_ADDED,
        )
        self.assertEqual(planned.contract.previous_lineage, previous)
        self.assertEqual(planned.contract.prospective_lineage, prospective)
        self.assertTrue(planned.contract.changed)
        self.assertEqual(
            RepositoryLineageUpdatePlan.from_dict(planned.contract.to_dict()),
            planned.contract,
        )
        SchemaCatalog().validate(planned.contract.SCHEMA, planned.contract.to_dict())

    def test_apply_advances_lineage_but_preserves_conflicts_and_opt_in_additions(
        self,
    ) -> None:
        selection, root_node, child_node, previous = fixture()
        previous_item = flavor_item(child_node, "lang-python", "lang-python")
        updated_child = replace(
            child_node,
            project_identity=identity("6"),
            resolved_revision="f" * 40,
        )
        prospective = RepositoryLineage(
            selection,
            (root_node, updated_child),
            (updated_child.identity,),
        )
        changed_files = tuple(
            replace(
                file,
                content=(
                    file.content + b"\n<!-- upstream -->\n"
                    if file.destination.endswith("flavor.md")
                    else file.content + b"\n\n"
                    if file.destination.endswith("toolchain.json")
                    else file.content
                ),
            )
            for file in previous_item.files
        )
        prospective_plan = InheritedCatalogPlan(
            prospective,
            (
                InheritedCatalogItem(
                    "flavor", "lang-python", updated_child, changed_files
                ),
                flavor_item(updated_child, "new-platform", "os-linux"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda _lineage: InheritedCatalogPlan(
                    previous, (previous_item,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            toolchain = target / "flavors" / "lang-python" / "toolchain.json"
            toolchain.write_bytes(toolchain.read_bytes() + b"\n")
            local_toolchain = toolchain.read_bytes()
            adapter = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: prospective,
                catalog_planner=lambda _lineage: prospective_plan,
            )
            planned = adapter.plan(target)

            with patch(
                "literate_ai.adapters.repository_updates.validate_project",
                wraps=validate_project,
            ) as project_validation:
                applied = adapter.apply(target, planned)

            self.assertIn(
                "<!-- upstream -->",
                (target / "flavors" / "lang-python" / "flavor.md").read_text(),
            )
            self.assertEqual(toolchain.read_bytes(), local_toolchain)
            self.assertFalse((target / "flavors" / "new-platform").exists())
            _selection, current = FilesystemRepositoryLineageStore(target).load()
            self.assertEqual(current, prospective)
            refused = {
                classification: paths for classification, paths in applied.refused
            }
            self.assertIn(
                "flavors/lang-python/toolchain.json",
                refused[ProjectUpdateClassification.CONFLICT],
            )
            self.assertIn(
                "flavors/new-platform/flavor.md",
                refused[ProjectUpdateClassification.UPSTREAM_ADDED],
            )
            imports = CatalogImportsFile.load(target)
            inherited_paths = {
                file.path for imported in imports.imports for file in imported.files
            }
            self.assertNotIn("flavors/lang-python/toolchain.json", inherited_paths)
            self.assertTrue(applied.to_dict()["authority_review_required"])
            self.assertTrue(
                project_validation.call_args.kwargs["synchronize_source_intelligence"]
            )
            SchemaCatalog().validate(applied.SCHEMA, applied.to_dict())
            replanned = adapter.plan(target)
            by_path = {
                item.path: item.classification for item in replanned.contract.files
            }
            self.assertEqual(
                by_path["flavors/new-platform/flavor.md"],
                ProjectUpdateClassification.UPSTREAM_ADDED,
            )

    def test_catalog_inherited_file_matching_parent_is_not_a_framework_conflict(
        self,
    ) -> None:
        """Catalog-inherited local bytes matching parent are not a template conflict."""

        selection, _root_node, child_node, previous = fixture()
        python_item = flavor_item(child_node, "lang-python", "lang-python")
        catalog_files = []
        catalog_body = None
        for inherited_file in python_item.files:
            if inherited_file.destination.endswith("flavor.md"):
                catalog_body = (
                    inherited_file.content + b"\n<!-- catalog-at-target -->\n"
                )
                catalog_files.append(
                    InheritedCatalogFile(
                        inherited_file.destination,
                        catalog_body,
                        inherited_file.executable,
                    )
                )
            else:
                catalog_files.append(inherited_file)
        inherited = replace(python_item, files=tuple(catalog_files))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _selection: previous,
                repository_catalog_planner=lambda lineage: InheritedCatalogPlan(
                    lineage, (inherited,)
                ),
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            self.assertEqual(
                (target / "flavors/lang-python/flavor.md").read_bytes(), catalog_body
            )
            self.assertNotEqual(
                catalog_body,
                _template_text("flavors/lang-python/flavor.md").encode("utf-8"),
            )
            framework = FilesystemProjectUpdateAdapter(origin_provider=origin).plan(
                target
            )
            framework_paths = {item.path for item in framework.files}
            self.assertNotIn("flavors/lang-python/flavor.md", framework_paths)
            lineage = FilesystemRepositoryUpdateAdapter(
                lineage_resolver=lambda _selection: previous,
                catalog_planner=lambda lineage: InheritedCatalogPlan(
                    lineage, (inherited,)
                ),
            ).plan(target)
            lineage_class = {
                item.path: item.classification for item in lineage.contract.files
            }
            self.assertIn(
                lineage_class["flavors/lang-python/flavor.md"],
                {
                    ProjectUpdateClassification.ALREADY_CURRENT,
                    ProjectUpdateClassification.UNCHANGED,
                },
            )


if __name__ == "__main__":
    unittest.main()

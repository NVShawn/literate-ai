"""New scaffold dependencies respect pinned catalogs before any write."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from literate_ai.adapters import project_updates
from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
)
from literate_ai.adapters.project_update_apply import apply_project_update
from literate_ai.adapters.project_update_compatibility import (
    classify_skill_compatibility,
)
from literate_ai.adapters.project_updates import (
    FilesystemProjectUpdateAdapter,
    ProjectUpdateError,
    _identity,
)
from literate_ai.adapters.repository_catalogs import (
    InheritedCatalogFile,
    InheritedCatalogItem,
    InheritedCatalogPlan,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.adapters.repository_updates import FilesystemRepositoryUpdateAdapter
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.project import update_project_from_args
from literate_ai.contracts import (
    ProjectUpdateClassification,
    ProjectUpdateFile,
    ProjectUpdatePlan,
    canonical_identity,
)
from literate_ai.projects import load_project
from tests.e2e.test_project_update_adapter import _origin
from tests.support.fixtures_test_repository_lineage import fixture
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class ProjectUpdateCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        self.origin = _origin("a", "1.0.0")
        self.installed_origin = _origin("b", "2.0.0")
        self.prefix = "skills/specification-to-source/"
        self.a = self.prefix + "fixture-a/SKILL.md"
        self.b = self.prefix + "fixture-b/SKILL.md"
        self.c = self.prefix + "fixture-c/SKILL.md"
        self.old = self.skill("fixture-a")
        self.new = self.skill("fixture-a", version="2.0.0")
        self.selection, _, parent, self.lineage = fixture()
        self.parent_catalog = InheritedCatalogPlan(
            self.lineage,
            (
                InheritedCatalogItem(
                    "skill",
                    "specification-to-source/fixture-a",
                    parent,
                    (InheritedCatalogFile(self.a, self.old, False),),
                ),
            ),
        )
        FilesystemProjectInitializationAdapter(
            standard_binding_provider=lambda: None,
            initialization_origin_provider=lambda: self.origin,
            repository_lineage_resolver=lambda _: self.lineage,
            repository_catalog_planner=lambda _: self.parent_catalog,
        ).initialize(
            self.root,
            source_intelligence_provider="none",
            empty=True,
            parent_selection=self.selection,
        )

    def skill(self, name, *, version="1.1.0", dependency=None):
        source = (
            Path(__file__).resolve().parents[2]
            / "skills/specification-to-source/repository-layout/SKILL.md"
        ).read_text()
        source = source.replace('name: "repository-layout"', f'name: "{name}"')
        source = source.replace('skill_id: "repository-layout"', f'skill_id: "{name}"')
        source = source.replace('version: "1.1.0"', f'version: "{version}"')
        if dependency is not None:
            target, target_version, content = dependency
            digest = hashlib.sha256(content).hexdigest()
            source = source.replace(
                "dependencies: []",
                f'''dependencies:
  - schema: "urn:literate-ai:schema:v1:skill-reference"
    skill_id: "{target}"
    version: "{target_version}"
    identity:
      schema: "urn:literate-ai:schema:v1:content-identity"
      algorithm: "sha256"
      digest: "{digest}"''',
            )
        return source.encode()

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def planned(self, additions, **options):
        original = project_updates._upstream_template

        def upstream(paths):
            return original(paths) | additions

        with mock.patch.object(
            project_updates, "_upstream_template", side_effect=upstream
        ):
            plan = FilesystemProjectUpdateAdapter(
                origin_provider=lambda: self.installed_origin, **options
            ).plan(self.root)
        return plan, upstream

    def test_old_parent_new_skill_is_reported_and_safe_subset_applies(self):
        bad = self.skill("fixture-b", dependency=("fixture-a", "2.0.0", self.new))
        good = self.skill("fixture-c")
        before = self.snapshot()
        plan, upstream = self.planned({self.b: bad, self.c: good})
        self.assertEqual(self.snapshot(), before)
        blocked = next(x for x in plan.files if x.path == self.b)
        self.assertEqual(
            blocked.compatibility[0], "project.skill_dependency_identity_mismatch"
        )
        self.assertEqual(ProjectUpdatePlan.from_dict(plan.to_dict()), plan)
        catalog = SchemaCatalog()
        schema, _ = catalog.resolve(plan.SCHEMA)
        registry = Registry().with_resources(
            (uri, Resource.from_contents(document, default_specification=DRAFT202012))
            for uri, document in catalog.resources.items()
        )
        Draft202012Validator(schema, registry=registry).validate(plan.to_dict())
        with (
            mock.patch.object(
                project_updates, "_upstream_template", side_effect=upstream
            ),
            mock.patch(
                "literate_ai.adapters.project_update_apply._upstream_template",
                side_effect=upstream,
            ),
        ):
            applied = apply_project_update(plan, self.root, adopt_added=True)
        self.assertEqual(applied.refused["dependency-incompatible"], (self.b,))
        self.assertFalse((self.root / self.b).exists())
        self.assertEqual((self.root / self.a).read_bytes(), self.old)
        self.assertEqual((self.root / self.c).read_bytes(), good)
        self.assertEqual(
            FilesystemRepositoryLineageStore(self.root).load(),
            (self.selection, self.lineage),
        )

    def test_current_v2_wire_without_diagnostics_keeps_its_previous_identity(self):
        item = ProjectUpdateFile(
            "SKILL.md",
            ProjectUpdateClassification.UPSTREAM_ONLY,
            canonical_identity("base"),
            canonical_identity("base"),
            canonical_identity("upstream"),
        )
        self.assertNotIn("compatibility", item.to_dict())
        self.assertEqual(
            item.identity.uri,
            "sha256:8878f7e688f090827fcfaaf4fca8e547839e0e6cc506c35435b6f3aff3844876",
        )

    def invoke_public_update(
        self, additions, *, apply, repository_adapter=None, path=None
    ):
        original_template = project_updates._upstream_template

        def upstream(paths):
            return original_template(paths) | additions

        def adapter(**options):
            return FilesystemProjectUpdateAdapter(
                origin_provider=lambda: self.installed_origin, **options
            )

        with (
            mock.patch(
                "literate_ai.cli.project._repository_fetch_provider",
                return_value=SimpleNamespace(
                    deadline_evidence={"fixture": True}, update_blobs=lambda *_: {}
                ),
            ),
            mock.patch(
                "literate_ai.cli.project.resolve_repository_lineage",
                return_value=self.lineage,
            ),
            mock.patch(
                "literate_ai.cli.project.plan_inherited_catalogs",
                return_value=self.parent_catalog,
            ),
            mock.patch(
                "literate_ai.cli.project.FilesystemProjectUpdateAdapter",
                side_effect=adapter,
            ),
            mock.patch(
                "literate_ai.cli.project.FilesystemRepositoryUpdateAdapter",
                side_effect=repository_adapter or FilesystemRepositoryUpdateAdapter,
            ),
            mock.patch.object(
                project_updates, "_upstream_template", side_effect=upstream
            ),
            mock.patch(
                "literate_ai.adapters.project_update_apply._upstream_template",
                side_effect=upstream,
            ),
        ):
            return update_project_from_args(
                Namespace(
                    path=path or self.root,
                    apply=apply,
                    adopt_added=True,
                    take_upstream=[],
                    keep_local=[],
                    record_work_items=False,
                    unpin=False,
                    follow_ref=None,
                    allow_major=False,
                    review_conflicts=False,
                )
            )

    def test_public_update_from_nested_path_preserves_parent_catalog(self):
        nested = self.root / "components"
        nested.mkdir(exist_ok=True)
        item = self.parent_catalog.items[0]
        self.parent_catalog = replace(
            self.parent_catalog,
            items=(
                replace(item, files=(InheritedCatalogFile(self.a, self.new, False),)),
            ),
        )
        good = self.skill("fixture-c")
        planned = self.invoke_public_update({self.c: good}, apply=False, path=nested)
        self.assertEqual(planned["mode"], "read-only-plan")
        self.invoke_public_update({self.c: good}, apply=True, path=nested)
        self.assertEqual((self.root / self.a).read_bytes(), self.new)
        self.assertEqual((self.root / self.c).read_bytes(), good)

    def test_public_update_plan_and_apply_keep_old_pin_and_report_same_incompatibility(
        self,
    ):
        bad = self.skill("fixture-b", dependency=("fixture-a", "2.0.0", self.new))
        good = self.skill("fixture-c")
        before = self.snapshot()
        planned = self.invoke_public_update({self.b: bad, self.c: good}, apply=False)
        self.assertEqual(self.snapshot(), before)
        expected = next(
            x["compatibility"]
            for x in planned["framework"]["files"]
            if x["path"] == self.b
        )
        applied = self.invoke_public_update({self.b: bad, self.c: good}, apply=True)
        self.assertEqual(
            next(
                x["compatibility"]
                for x in applied["framework"]["files"]
                if x["path"] == self.b
            ),
            expected,
        )
        self.assertFalse((self.root / self.b).exists())
        self.assertEqual((self.root / self.a).read_bytes(), self.old)
        self.assertEqual((self.root / self.c).read_bytes(), good)
        self.assertEqual(
            FilesystemRepositoryLineageStore(self.root).load(),
            (self.selection, self.lineage),
        )

    def test_local_provider_drift_refuses_before_inherited_provenance_is_written(self):
        local = self.prefix + "fixture-local/SKILL.md"
        old = self.skill("fixture-local")
        changed = self.skill("fixture-local", version="2.0.0")
        path = self.root / local
        path.parent.mkdir()
        path.write_bytes(old)
        addition = self.skill("fixture-b", dependency=("fixture-local", "1.1.0", old))
        snapshots = []
        owner = self

        class ConcurrentEdit(FilesystemRepositoryUpdateAdapter):
            def apply(self, *args, **kwargs):
                path.write_bytes(changed)
                snapshots.append(owner.snapshot())
                return super().apply(*args, **kwargs)

        with (
            mock.patch.object(
                FilesystemRepositoryLineageStore,
                "replace",
                side_effect=AssertionError("preflight reached lineage writer"),
            ) as writer,
            self.assertRaises(CliFailure) as raised,
        ):
            self.invoke_public_update(
                {self.b: addition}, apply=True, repository_adapter=ConcurrentEdit
            )
        self.assertEqual(
            raised.exception.code, "project.skill_dependency_identity_mismatch"
        )
        self.assertEqual(writer.call_count, 0)
        self.assertEqual(self.snapshot(), snapshots[0])

    def test_selected_parent_addition_takes_precedence_over_installed_scaffold(self):
        parent = self.skill("fixture-b")
        installed = self.skill("fixture-b", dependency=("missing", "1.1.0", self.old))
        dependent = self.skill("fixture-c", dependency=("fixture-b", "1.1.0", parent))
        before = self.snapshot()
        plan, _ = self.planned(
            {self.b: installed, self.c: dependent}, catalog_overrides={self.b: parent}
        )
        self.assertNotIn(self.b, {item.path for item in plan.files})
        self.assertIsNone(
            next(item for item in plan.files if item.path == self.c).compatibility
        )
        self.assertEqual(self.snapshot(), before)

    def test_current_dependency_change_refuses_before_any_write(self):
        b = self.skill("fixture-b", dependency=("fixture-a", "1.1.0", self.old))
        plan, upstream = self.planned({self.b: b})
        (self.root / self.a).write_bytes(self.new)
        before = self.snapshot()
        with (
            mock.patch(
                "literate_ai.adapters.project_update_apply._upstream_template",
                side_effect=upstream,
            ),
            mock.patch(
                "literate_ai.adapters.project_update_apply._write_exact",
                side_effect=AssertionError("preflight wrote a file"),
            ),
            self.assertRaises(ProjectUpdateError) as raised,
        ):
            apply_project_update(plan, self.root, adopt_added=True)
        self.assertEqual(
            raised.exception.code, "project.skill_dependency_identity_mismatch"
        )
        self.assertEqual(self.snapshot(), before)

    def test_unchanged_consumer_prevents_incompatible_provider_replacement(self):
        consumer = self.root / self.b
        consumer.parent.mkdir()
        consumer.write_bytes(
            self.skill("fixture-b", dependency=("fixture-a", "1.1.0", self.old))
        )
        item = ProjectUpdateFile(
            self.a,
            ProjectUpdateClassification.UPSTREAM_ONLY,
            _identity(self.old),
            _identity(self.old),
            _identity(self.new),
        )
        before = self.snapshot()
        result = classify_skill_compatibility(
            load_project(self.root), (item,), {self.a: self.new}
        )
        self.assertEqual(
            result[0].compatibility[0], "project.skill_dependency_identity_mismatch"
        )
        self.assertEqual(self.snapshot(), before)

    def test_compatible_additions_are_resolved_as_one_cohort(self):
        b = self.skill("fixture-b")
        c = self.skill("fixture-c", dependency=("fixture-b", "1.1.0", b))
        plan, _ = self.planned({self.b: b, self.c: c})
        self.assertTrue(all(x.compatibility is None for x in plan.files))

    def test_selected_parent_update_is_used_without_mutating_parent(self):
        b = self.skill("fixture-b", dependency=("fixture-a", "2.0.0", self.new))
        item = ProjectUpdateFile(
            self.b, ProjectUpdateClassification.UPSTREAM_ADDED, None, None, _identity(b)
        )
        result = classify_skill_compatibility(
            load_project(self.root),
            (item,),
            {self.b: b},
            catalog_overrides={self.a: self.new},
        )
        self.assertIsNone(result[0].compatibility)
        self.assertEqual((self.root / self.a).read_bytes(), self.old)

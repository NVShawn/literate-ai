"""Portable root lock candidates and real-Git, read-only reobservation."""

from __future__ import annotations

import copy
import json
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from literate_ai.adapters import repository_lock_planning as planning
from literate_ai.adapters.orchestration_planning import plan_orchestration
from literate_ai.adapters.orchestration_scaffold import prepare_orchestration_scaffold
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.repository_lock import RepositoryLock
from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryRelationship,
)
from literate_ai.project_authority_graph import project_authority_graph
from tests.unit import test_orchestration_planning as fixtures
from tests.unit.test_repository_orchestration import git, snapshot
from tests.unit.test_repository_orchestration_contracts import authority
from tests.unit.test_schema_catalog import SchemaCatalog


class RepositoryLockContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registry = Registry().with_resources(
            (uri, Resource.from_contents(value, default_specification=DRAFT202012))
            for uri, value in SchemaCatalog().resources.items()
        )
        cls.validator = Draft202012Validator(
            {"$ref": "urn:literate-ai:schema:v2:repository-lock"}, registry=registry
        )

    def lock(self):
        return RepositoryLock(
            "super",
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            authority(),
        )

    def test_roundtrip_and_exact_identity_without_local_observations(self):
        lock = self.lock()
        wire = lock.to_dict()
        self.assertEqual(list(self.validator.iter_errors(wire)), [])
        self.assertEqual(
            RepositoryLock.from_dict(json.loads(canonical_json_bytes(wire))), lock
        )
        self.assertEqual(lock.identity, canonical_identity(wire).uri)
        self.assertEqual(
            set(wire),
            {
                "schema",
                "project_id",
                "project_identity",
                "authority_identity",
                "authority_graph_identity",
                "repository_orchestration",
            },
        )
        for field in (
            "project_identity",
            "authority_identity",
            "authority_graph_identity",
        ):
            self.assertNotEqual(
                replace(lock, **{field: "sha256:" + "4" * 64}).identity, lock.identity
            )
        self.assertNotEqual(replace(lock, project_id="other").identity, lock.identity)
        self.assertNotEqual(
            replace(
                lock, repository_orchestration=replace(authority(), relationships=())
            ).identity,
            lock.identity,
        )

    def test_strict_wire_refuses_unknown_missing_and_forged_fields(self):
        for field in self.lock().to_dict():
            wire = self.lock().to_dict()
            wire.pop(field)
            self.assertTrue(list(self.validator.iter_errors(wire)))
            with self.subTest(missing=field), self.assertRaises(ValueError):
                RepositoryLock.from_dict(wire)
        for change in (
            {"schema": "literate-ai/component-lock@1"},
            {"project_id": False},
            {"project_identity": "sha256:" + "A" * 64},
            {"authority_identity": "latest"},
            {"authority_graph_identity": None},
            {"checked_out_commit": "a" * 40},
            {"execution": True},
        ):
            self.assertTrue(
                list(self.validator.iter_errors({**self.lock().to_dict(), **change}))
            )
            with (
                self.subTest(change=change),
                self.assertRaises((TypeError, ValueError)),
            ):
                RepositoryLock.from_dict({**self.lock().to_dict(), **change})
        with self.assertRaises(TypeError):
            replace(self.lock(), repository_orchestration=authority().to_dict())
        wire = copy.deepcopy(self.lock().to_dict())
        wire["repository_orchestration"]["repositories"][0]["commit"] = "main"
        with self.assertRaises(ValueError):
            RepositoryLock.from_dict(wire)


class RepositoryLockPlanningTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.OrchestrationPlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base
        self.binding = RepositoryOrchestration.from_dict(
            plan_orchestration(self.root, fixture.declaration)["repository_authority"]
        )
        self.materialize(self.binding)

    def materialize(self, binding):
        scaffold = prepare_orchestration_scaffold(
            binding, project_id="super", version="1.0.0"
        )
        for relative, content in scaffold.files:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def test_prepares_exact_reviewed_root_without_writes_or_component_invention(self):
        before = snapshot(self.base)
        prepared = planning.prepare_repository_lock(self.root)
        self.assertEqual(prepared.lock.repository_orchestration, self.binding)
        self.assertEqual(
            prepared.lock.authority_graph_identity,
            project_authority_graph(self.root, include_component_locks=False).identity,
        )
        plan = prepared.to_plan()
        self.assertEqual(plan, planning.prepare_repository_lock(self.root).to_plan())
        self.assertFalse(plan["writes"])
        self.assertFalse(plan["execution"])
        self.assertEqual(plan["execution_order"], "not-inferred")
        self.assertEqual(plan["publication"], "not-checked")
        self.assertEqual(plan["child_authority"], "independent")
        self.assertNotIn("components", plan)
        self.assertEqual(snapshot(self.base), before)

    def test_local_child_head_changes_observation_not_portable_lock(self):
        initial = planning.prepare_repository_lock(self.root)
        child = self.root / "app"
        git(self.base, "clone", "-q", "--no-hardlinks", str(self.root), str(child))
        git(child, "checkout", "-q", self.fixture.pin)
        (child / "literate.project.json").write_bytes(
            b"invalid independent child catalog"
        )
        initialized = planning.prepare_repository_lock(self.root)
        self.assertEqual(initial.lock, initialized.lock)
        self.assertNotEqual(
            initial.to_plan()["plan_identity"], initialized.to_plan()["plan_identity"]
        )
        git(child, "checkout", "-q", "main")
        advanced = planning.prepare_repository_lock(self.root)
        self.assertEqual(initialized.lock, advanced.lock)
        self.assertNotEqual(initialized.inventory, advanced.inventory)
        self.assertEqual(advanced.to_plan()["publication"], "not-checked")
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.require_repository_lock_inputs_unchanged(initialized)
        self.assertEqual(caught.exception.code, "orchestration.inputs_changed")

    def test_shallow_clone_reconstructs_identical_lock_and_plan(self):
        git(
            self.root,
            "add",
            "SKILL.md",
            "PROJECT.md",
            "literate.project.json",
            ".literate",
        )
        git(self.root, "commit", "-q", "-m", "root authority")
        original = planning.prepare_repository_lock(self.root)
        clone = self.base / "clone"
        git(self.base, "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone))
        reconstructed = planning.prepare_repository_lock(clone)
        self.assertEqual(original.lock, reconstructed.lock)
        self.assertEqual(original.to_plan(), reconstructed.to_plan())
        self.assertNotEqual(original.root, reconstructed.root)

    def test_index_pin_drift_refuses_without_refreshing_manifest(self):
        new_pin = git(self.root, "rev-parse", "HEAD").decode().strip()
        git(self.root, "update-index", "--cacheinfo", "160000", new_pin, "app")
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.prepare_repository_lock(self.root)
        self.assertEqual(caught.exception.code, "orchestration.binding_stale")
        self.assertEqual(snapshot(self.base), before)

    def test_indexed_configuration_drift_refuses(self):
        modules = self.root / ".gitmodules"
        modules.write_bytes(
            modules.read_bytes().replace(b"../app.git", b"../other.git")
        )
        git(self.root, "add", ".gitmodules")
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.prepare_repository_lock(self.root)
        self.assertEqual(caught.exception.code, "orchestration.binding_stale")
        self.assertEqual(snapshot(self.base), before)

    def test_external_declaration_is_not_post_initialization_authority(self):
        original = planning.prepare_repository_lock(self.root)
        self.fixture.declaration.write_bytes(b"not consulted after initialization")
        self.assertEqual(planning.prepare_repository_lock(self.root), original)

    def test_cyclic_relationships_bind_lock_without_execution_order(self):
        original = planning.prepare_repository_lock(self.root)
        self.materialize(
            replace(
                self.binding,
                relationships=(
                    RepositoryRelationship("app", "lib"),
                    RepositoryRelationship("lib", "app"),
                ),
            )
        )
        changed = planning.prepare_repository_lock(self.root)
        self.assertNotEqual(original.lock.identity, changed.lock.identity)
        self.assertEqual(changed.to_plan()["execution_order"], "not-inferred")

    def test_stale_root_review_refuses_without_repair(self):
        guide = self.root / ".literate/orchestration/docs/overview.md"
        guide.write_bytes(guide.read_bytes() + b"\nChanged root intent.\n")
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.prepare_repository_lock(self.root)
        self.assertEqual(caught.exception.code, "orchestration.root_authority_invalid")
        self.assertEqual(snapshot(self.base), before)

    def test_preparation_reobserves_exact_manifest_bytes(self):
        observe = planning._observe_checked
        count = 0

        def change_before_second(root):
            nonlocal count
            count += 1
            if count == 2:
                manifest = root / "literate.project.json"
                manifest.write_bytes(manifest.read_bytes() + b"\n")
            return observe(root)

        with patch.object(
            planning, "_observe_checked", side_effect=change_before_second
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                planning.prepare_repository_lock(self.root)
        self.assertEqual(caught.exception.code, "orchestration.inputs_changed")

    def test_selection_requires_exact_root_and_explicit_binding(self):
        child = self.root / "app"
        child.mkdir()
        with self.assertRaises(OrchestrationInventoryError):
            planning.prepare_repository_lock(child)
        manifest = self.root / "literate.project.json"
        wire = json.loads(manifest.read_bytes())
        wire.pop("repository_orchestration")
        manifest.write_bytes(canonical_json_bytes(wire) + b"\n")
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.prepare_repository_lock(self.root)
        self.assertEqual(caught.exception.code, "orchestration.binding_required")

    def test_replaced_root_is_not_the_prepared_custody_even_with_identical_bytes(self):
        prepared = planning.prepare_repository_lock(self.root)
        saved = self.base / "saved"
        self.root.rename(saved)
        shutil.copytree(saved, self.root)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.require_repository_lock_inputs_unchanged(prepared)
        self.assertEqual(caught.exception.code, "orchestration.inputs_changed")

    def test_root_symlink_is_refused_without_following_it(self):
        alias = self.base / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink creation is unavailable")
        with self.assertRaises(OrchestrationInventoryError) as caught:
            planning.prepare_repository_lock(alias)
        self.assertEqual(caught.exception.code, "orchestration.lock_inputs_invalid")

    def test_child_catalogs_are_not_read_by_root_validation(self):
        child = self.root / "app"
        child.mkdir()
        (child / "literate.project.json").write_bytes(b"invalid child authority")
        original_open = Path.open

        def guarded(path, *args, **kwargs):
            if path.is_relative_to(child):
                raise AssertionError("child catalog read")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=guarded):
            prepared = planning.prepare_repository_lock(self.root)
        self.assertEqual(prepared.lock.repository_orchestration, self.binding)

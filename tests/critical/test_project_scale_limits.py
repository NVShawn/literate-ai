"""Real filesystem scale-policy controls; not native retained-tree qualification."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
    _component_markdown_paths,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts import RepositoryLineage, RepositoryParentSelection
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.projects import ProjectDefinition, ProjectScaleLimits
from literate_ai.projects import PinnedInputClosure, PinnedInputClosureError
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]


def limits(**updates):
    data = dict(
        pinned_input_files=2000,
        pinned_input_total_bytes=64 * 1024**2,
        catalog_files=5000,
        catalog_entries=20000,
        catalog_total_bytes=64 * 1024**2,
    )
    return ProjectScaleLimits(**(data | updates))


class ProjectScaleLimitsTests(unittest.TestCase):
    def test_opt_in_roundtrips_schema_and_binds_identity_without_default_wire_drift(
        self,
    ):
        original = ProjectDefinition.from_dict(
            json.loads((ROOT / "literate.project.json").read_text())
        )
        self.assertNotIn("scale_limits", original.to_dict())
        enlarged = replace(original, scale_limits=limits())
        self.assertNotEqual(enlarged.identity, original.identity)
        self.assertEqual(ProjectDefinition.from_dict(enlarged.to_dict()), enlarged)
        SchemaCatalog().validate(enlarged.SCHEMA, enlarged.to_dict())
        self.assertEqual(
            replace(enlarged, scale_limits=None).to_dict(), original.to_dict()
        )
        value = enlarged.to_dict()
        value["scale_limits"]["unknown"] = 1
        with self.assertRaises(ValueError):
            ProjectDefinition.from_dict(value)

    def test_aggregate_bytes_and_per_file_bounds_remain_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = SimpleNamespace(
                definition=SimpleNamespace(
                    scale_limits=limits(pinned_input_total_bytes=2 * 1024**2)
                )
            )
            closure = PinnedInputClosure.for_project(project)
            first = root / "first"
            first.write_bytes(b"x" * (2 * 1024**2))
            closure.pin(first, boundary=root, label="first")
            second = root / "second"
            second.write_bytes(b"x")
            with self.assertRaises(PinnedInputClosureError) as rejected:
                closure.pin(second, boundary=root, label="second")
            self.assertEqual(rejected.exception.code, "inputs.closure_total_size_limit")
            self.assertIn("observed 2097153, allowed 2097152", str(rejected.exception))
            self.assertEqual(closure.total_bytes, 2 * 1024**2)
            oversized = root / "oversized"
            oversized.write_bytes(b"x" * (2 * 1024**2 + 1))
            with self.assertRaises(PinnedInputClosureError) as rejected:
                PinnedInputClosure.for_project(project).pin(
                    oversized, boundary=root, label="oversized"
                )
            self.assertEqual(rejected.exception.code, "inputs.closure_file_size_limit")

    def test_bounds_and_exact_integer_types_refuse(self):
        for name, bad in [
            ("pinned_input_files", True),
            ("catalog_files", 1000001),
            ("catalog_entries", 4000001),
            ("catalog_total_bytes", 4 * 1024**3 + 1),
            ("pinned_input_total_bytes", 0),
            ("pinned_input_files", 1.5),
            ("catalog_entries", 1),
        ]:
            with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                limits(**{name: bad})

    def test_default_file_bound_and_opt_in_exact_boundary_fork_and_duplicate_accounting(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default = PinnedInputClosure()
            configured = PinnedInputClosure.for_project(
                SimpleNamespace(
                    definition=SimpleNamespace(
                        scale_limits=limits(pinned_input_files=1025)
                    )
                )
            )
            for index in range(1025):
                path = root / str(index)
                path.write_bytes(b"abc")
                configured.pin(path, boundary=root, label=str(index))
                if index < 1024:
                    default.pin(path, boundary=root, label=str(index))
                else:
                    with self.assertRaises(PinnedInputClosureError) as refused:
                        default.pin(path, boundary=root, label=str(index))
                    self.assertIn("observed 1025, allowed 1024", str(refused.exception))
            configured.pin(root / "0", boundary=root, label="alias")
            self.assertEqual(configured.total_bytes, 1025 * 3)
            fork = configured.fork()
            self.assertEqual(fork.identity, configured.identity)
            self.assertEqual(fork.maximum_files, 1025)
            extra = root / "extra"
            extra.write_bytes(b"x")
            with self.assertRaises(PinnedInputClosureError):
                fork.pin(extra, boundary=root, label="extra")
            self.assertEqual(fork.total_bytes, 1025 * 3)
            configured.require_unchanged()

    def test_real_catalog_exceeds_default_authority_file_bound_only_when_declared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(4097):
                unit = root / str(index)
                unit.mkdir()
                (unit / "component.md").write_text("bounded catalog member")
            with self.assertRaises(ComponentLockPlanningError) as refused:
                _component_markdown_paths((root,), boundary=root)
            self.assertIn("observed 4097, allowed 4096", str(refused.exception))
            admitted = limits(catalog_files=4097, catalog_entries=9000)
            self.assertEqual(
                len(
                    _component_markdown_paths(
                        (root,), boundary=root, scale_limits=admitted
                    )
                ),
                4097,
            )
            with self.assertRaises(ComponentLockPlanningError):
                _component_markdown_paths(
                    (root,),
                    boundary=root,
                    scale_limits=replace(admitted, catalog_entries=4097),
                )

    def test_actual_snapshot_and_lock_plan_cross_legacy_pinned_input_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            component, flavors = _fixture(root)
            catalog = root / "components"
            catalog.mkdir()
            component.rename(catalog / "greeting")
            component = catalog / "greeting"
            text = (component / "component.md").read_text()
            for index in range(1050):
                unit = catalog / f"unit-{index}"
                unit.mkdir()
                (unit / "component.md").write_text(text)
            original = ProjectDefinition.from_dict(
                json.loads((ROOT / "literate.project.json").read_text())
            )
            definition = replace(
                original,
                component_roots=("components",),
                flavor_roots=("flavors",),
                skill_roots=("skills",),
                workflow_roots=("workflows",),
                routing_roots=("routing",),
                lifecycle_driver=None,
                test_receipt=None,
                test_receipt_policy=None,
                default_flavor_selectors=(),
                component_flavor_selectors={},
                scale_limits=None,
            )
            selection = RepositoryParentSelection.root()
            FilesystemRepositoryLineageStore(root).replace(
                selection, RepositoryLineage(selection, (), ()), expected_absent=True
            )
            manifest = root / "literate.project.json"
            manifest.write_text(json.dumps(definition.to_dict()))
            planner = FilesystemComponentLockPlanner()
            with self.assertRaises(ComponentLockPlanningError) as refused:
                planner.snapshot(component, flavor_roots=(flavors,))
            self.assertIn("pinned-input", str(refused.exception))
            manifest.write_text(
                json.dumps(replace(definition, scale_limits=limits()).to_dict())
            )
            snapshot = planner.snapshot(component, flavor_roots=(flavors,))
            self.assertEqual(len(snapshot.authorings), 1051)
            plan = planner.plan_snapshot(
                snapshot,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
            )
            self.assertIsNotNone(plan)
            resolved = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )
            (component / "component.lock.json").write_bytes(
                canonical_json_bytes(resolved.lock.to_dict()) + b"\n"
            )
            captured = snapshot.locked_input_closure(resolved.lock)
            self.assertGreater(captured.file_count, 0)
            manifest.write_text(
                json.dumps(
                    replace(
                        definition, scale_limits=limits(pinned_input_files=1500)
                    ).to_dict()
                )
            )
            with self.assertRaises(PinnedInputClosureError):
                captured.require_unchanged()
            with self.assertRaises(ComponentLockPlanningError):
                snapshot.locked_input_closure(resolved.lock)
            with self.assertRaises(ComponentLockPlanningError):
                snapshot.require_unchanged()

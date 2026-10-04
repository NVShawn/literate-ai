"""Exact importer review is independent of discovered test-binary output."""

import copy
import hashlib
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.retained_cargo_test_authority import (
    read_retained_cargo_test_authority,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.contracts.retained_cargo_tests import (
    RetainedCargoTestInventory,
    RetainedCargoTestTarget,
    retained_cargo_test_targets,
)
from tests.support import fixtures_test_retained_cargo_execution as fixtures


def blob(content):
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type="application/json"
    )


class RetainedCargoTestAuthorityTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RetainedCargoExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        targets = tuple(
            sorted(
                (
                    RetainedCargoTestTarget(
                        p.root, t.name, tuple(sorted(t.kinds)), ("reviewed_case",)
                    )
                    for p, t in retained_cargo_test_targets(
                        fixture.materialized.plan.graph
                    )
                ),
                key=lambda t: t.key,
            )
        )
        self.inventory = RetainedCargoTestInventory(
            fixture.importer.project.definition.project_id,
            fixture.materialized.plan.identity,
            fixture.importer.gates.identity,
            targets,
        )
        self.path = self.root / "test-inventory.json"
        self.content = canonical_json_bytes(self.inventory.to_dict())
        self.path.write_bytes(self.content)

    def read(self, **changes):
        return read_retained_cargo_test_authority(
            self.fixture.materialized,
            self.fixture.importer,
            **{
                "inventory_path": "test-inventory.json",
                "reviewed_inventory": blob(self.content),
                **changes,
            },
        )

    def test_review_roundtrip_and_current_read_preserve_files(self):
        before = self.path.read_bytes()
        self.assertEqual(
            RetainedCargoTestInventory.from_dict(self.inventory.to_dict()),
            self.inventory,
        )
        current = self.read()
        current.require_unchanged()
        self.assertEqual(current.inventory, self.inventory)
        self.assertEqual(current.reviewed_inventory, blob(self.content))
        self.assertEqual(self.path.read_bytes(), before)
        self.fixture.process.assert_not_called()

    def test_consumer_session_preserves_gates_and_brackets_reviewed_tests(self):
        authority = self.read()

        def observe(current, metadata, cargo, *, rustc, environment, offline, run):
            self.assertIs(current, authority)
            self.assertIs(cargo, self.fixture.cargo)
            self.assertTrue(offline)
            self.assertEqual(self.fixture.process.call_count, 2)
            self.assertEqual(metadata, {"packages": []})
            authority.require_unchanged()

        with patch.object(fixtures.execution, "observe_retained_cargo_tests", observe):
            observations = self.fixture.run_execution(test_authority=authority)
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(o.executable_authority for o in observations))

    def test_foreign_test_session_refuses_before_any_process(self):
        authority = replace(self.read(), importer=replace(self.fixture.importer))
        with self.assertRaisesRegex(ValueError, "test-authority-mismatch"):
            self.fixture.run_execution(test_authority=authority)
        self.fixture.process.assert_not_called()

    def test_inventory_drift_during_tests_refuses_final_metadata(self):
        authority = self.read()

        def mutate(*args, **kwargs):
            self.path.write_bytes(self.content + b"\n")

        with (
            patch.object(fixtures.execution, "observe_retained_cargo_tests", mutate),
            self.assertRaises(fixtures.execution.RetainedCargoExecutionError),
        ):
            self.fixture.run_execution(test_authority=authority)
        self.assertEqual(self.fixture.process.call_count, 2)

    def test_foreign_project_plan_and_gate_policy_refuse_even_with_matching_bytes(self):
        for changes in (
            {"importer_project_id": "foreign"},
            {"workspace_plan_identity": canonical_identity("other plan")},
            {"gate_policy_identity": canonical_identity("other gate policy")},
        ):
            content = canonical_json_bytes(replace(self.inventory, **changes).to_dict())
            self.path.write_bytes(content)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "inventory-authority-mismatch"),
            ):
                self.read(reviewed_inventory=blob(content))

    def test_changed_case_inventory_requires_new_review_and_invalidates_old_reader(
        self,
    ):
        current = self.read()
        target = replace(self.inventory.targets[0], cases=("changed_case",))
        updated = replace(self.inventory, targets=(target, *self.inventory.targets[1:]))
        content = canonical_json_bytes(updated.to_dict())
        self.assertNotEqual(updated.identity, self.inventory.identity)
        self.path.write_bytes(content)
        with self.assertRaises(ValueError):
            current.require_unchanged()
        with self.assertRaises(ValueError):
            self.read()
        self.assertEqual(self.read(reviewed_inventory=blob(content)).inventory, updated)

    def test_missing_or_foreign_targets_cannot_reduce_coverage(self):
        self.assertGreater(len(self.inventory.targets), 1)
        variants = (
            replace(self.inventory, targets=self.inventory.targets[:-1]),
            replace(
                self.inventory,
                targets=tuple(
                    sorted(
                        (
                            replace(self.inventory.targets[0], target_name="foreign"),
                            *self.inventory.targets[1:],
                        ),
                        key=lambda t: t.key,
                    )
                ),
            ),
        )
        for inventory in variants:
            content = canonical_json_bytes(inventory.to_dict())
            self.path.write_bytes(content)
            with (
                self.subTest(targets=len(inventory.targets)),
                self.assertRaisesRegex(ValueError, "every selected"),
            ):
                self.read(reviewed_inventory=blob(content))

    def test_empty_targets_allowed_but_empty_overall_or_duplicate_names_refuse(self):
        partial = replace(
            self.inventory,
            targets=(
                replace(self.inventory.targets[0], cases=()),
                *self.inventory.targets[1:],
            ),
        )
        partial.require_graph(self.fixture.materialized.plan.graph)
        with self.assertRaises(ValueError):
            replace(
                self.inventory,
                targets=tuple(replace(t, cases=()) for t in self.inventory.targets),
            )
        with self.assertRaises(ValueError):
            replace(self.inventory.targets[0], cases=("duplicate", "duplicate"))

    def test_malformed_unknown_duplicate_fields_and_wrong_review_bounds_refuse(self):
        wire = self.inventory.to_dict()
        malformed = []
        for changes in ({"unknown": True}, {"schema": "unrecognized"}, {"targets": []}):
            malformed.append(canonical_json_bytes({**wire, **changes}))
        changed = copy.deepcopy(wire)
        changed["targets"][0]["cases"] = ["unprintable\ncase"]
        malformed.append(canonical_json_bytes(changed))
        malformed.append(b'{"schema":"duplicate",' + self.content[1:])
        for content in malformed:
            self.path.write_bytes(content)
            with (
                self.subTest(content=content[:40]),
                self.assertRaisesRegex(ValueError, "inventory-invalid"),
            ):
                self.read(reviewed_inventory=blob(content))
        self.path.write_bytes(self.content)
        for changes in (
            {"maximum_bytes": True},
            {"maximum_bytes": 1},
            {
                "reviewed_inventory": replace(
                    blob(self.content), size=len(self.content) + 1
                )
            },
            {"inventory_path": "../foreign.json"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(**changes)

    def test_symlinked_inventory_refuses(self):
        self.path.rename(self.root / "actual.json")
        try:
            self.path.symlink_to(self.root / "actual.json")
        except OSError:
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            self.read()

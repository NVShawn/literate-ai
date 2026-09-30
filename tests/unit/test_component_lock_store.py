"""Atomic Component lock filesystem adapter tests."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import literate_ai.adapters.component_locks as component_lock_store_module
from literate_ai.adapters.component_locks import (
    ComponentLockStore,
    ComponentLockStoreError,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.unit.test_component_lock_contracts import component_lock
from tests.unit.test_component_lock_resolution import resolution_plan


class ComponentLockStoreTests(unittest.TestCase):
    def test_large_mostly_equal_lock_diff_reports_only_changed_leaf(self) -> None:
        current = {
            "nodes": [
                {"identity": f"node-{index}", "state": "unchanged"}
                for index in range(component_lock_store_module._MAX_DIFF_ENTRIES + 1)
            ]
        }
        expected = json.loads(json.dumps(current))
        expected["nodes"][-1]["state"] = "changed"

        differences = component_lock_store_module._semantic_diff(current, expected)

        self.assertEqual(len(differences), 1)
        self.assertEqual(
            differences[0].path,
            f"/nodes/{component_lock_store_module._MAX_DIFF_ENTRIES}/state",
        )

    def test_actual_lock_diff_over_review_limit_still_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            ComponentLockStoreError, "bounded review limit"
        ) as raised:
            component_lock_store_module._semantic_diff(
                [],
                list(range(component_lock_store_module._MAX_DIFF_ENTRIES + 1)),
            )

        self.assertEqual(raised.exception.code, "component_lock.diff_limit")

    def test_lock_diff_traversal_limit_is_independent(self) -> None:
        with (
            patch.object(component_lock_store_module, "_MAX_DIFF_TRAVERSAL_ENTRIES", 2),
            self.assertRaisesRegex(
                ComponentLockStoreError, "bounded traversal limit"
            ) as raised,
        ):
            component_lock_store_module._semantic_diff(
                {"node": {"state": "before"}},
                {"node": {"state": "after"}},
            )

        self.assertEqual(raised.exception.code, "component_lock.diff_traversal_limit")

    def test_update_is_canonical_idempotent_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = component_lock()
            store = ComponentLockStore(root)

            self.assertTrue(store.update(candidate))
            self.assertFalse(store.update(candidate))
            self.assertEqual(
                store.path.read_bytes(),
                canonical_json_bytes(candidate.to_dict()) + b"\n",
            )
            self.assertEqual(
                store.read(authorings=candidate.authorings).identity,
                candidate.identity,
            )
            self.assertTrue(
                store.check(candidate, authorings=candidate.authorings).current
            )

    def test_missing_and_stale_checks_return_structured_differences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = component_lock()
            store = ComponentLockStore(root)
            missing = store.check(candidate, authorings=candidate.authorings)
            self.assertEqual(missing.state, "missing")

            store.update(candidate)
            changed = replace(
                candidate,
                resolver_identity=canonical_identity({"resolver": "replacement"}),
            )
            stale = store.check(changed, authorings=changed.authorings)
            self.assertEqual(stale.state, "stale")
            self.assertEqual(
                tuple(item.path for item in stale.differences),
                ("/resolver_identity/digest",),
            )

    def test_failed_revalidation_preserves_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = component_lock()
            store = ComponentLockStore(root)
            store.update(original)
            original_bytes = store.path.read_bytes()
            replacement = replace(
                original,
                resolver_identity=canonical_identity({"resolver": "replacement"}),
            )

            def fail() -> None:
                raise RuntimeError("injected input drift")

            with self.assertRaisesRegex(RuntimeError, "injected input drift"):
                store.update(replacement, revalidate=fail)
            self.assertEqual(store.path.read_bytes(), original_bytes)
            self.assertFalse(tuple(root.glob(f".{store.path.name}.*")))

    def test_operation_lock_serializes_the_complete_component_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ComponentLockStore(Path(directory))
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()

            def first() -> None:
                with store.operation():
                    first_entered.set()
                    self.assertTrue(release_first.wait(timeout=5))

            def second() -> None:
                self.assertTrue(first_entered.wait(timeout=5))
                with store.operation():
                    second_entered.set()

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_result = executor.submit(first)
                second_result = executor.submit(second)
                self.assertTrue(first_entered.wait(timeout=5))
                self.assertFalse(second_entered.wait(timeout=0.1))
                release_first.set()
                first_result.result(timeout=5)
                second_result.result(timeout=5)
            self.assertTrue(second_entered.is_set())

    def test_persisted_read_is_bounded_and_rollback_is_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ComponentLockStore(root)
            snapshot = store.snapshot()
            candidate = component_lock()
            store.update(candidate)
            self.assertTrue(
                store.restore_if_current(snapshot, expected_current=candidate)
            )
            self.assertFalse(store.path.exists())

            store.path.write_bytes(b"x" * 33)
            with (
                patch.object(component_lock_store_module, "_MAX_PERSISTED_BYTES", 32),
                self.assertRaisesRegex(ComponentLockStoreError, "byte limit"),
            ):
                store.read(authorings=candidate.authorings)

    def test_concurrent_uncooperative_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = component_lock()
            store = ComponentLockStore(root)
            store.update(original)
            concurrent_bytes = b"concurrent writer\n"
            replacement = replace(
                original,
                resolver_identity=canonical_identity({"resolver": "replacement"}),
            )

            def change_destination() -> None:
                store.path.write_bytes(concurrent_bytes)

            with self.assertRaisesRegex(ComponentLockStoreError, "changed"):
                store.update(replacement, revalidate=change_destination)
            self.assertEqual(store.path.read_bytes(), concurrent_bytes)
            self.assertFalse(tuple(root.glob(f".{store.path.name}.*")))

    def test_changed_external_authoring_is_reported_as_unverified_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_plan = resolution_plan()
            resolver = ComponentLockResolver()
            before = resolver.resolve(
                before_plan, expected_input_evidence_identity=before_plan.identity
            ).lock
            store = ComponentLockStore(root)
            store.update(before)

            original_root = next(
                item
                for item in before_plan.nodes
                if item.authoring.identity == before_plan.root_authoring_identity
            )
            changed_authoring = replace(
                original_root.authoring, description="changed human intent"
            )
            changed_node = replace(original_root, authoring=changed_authoring)
            after_plan = replace(
                before_plan,
                root_authoring_identity=changed_authoring.identity,
                nodes=tuple(
                    changed_node if item is original_root else item
                    for item in before_plan.nodes
                ),
                requirement_providers=tuple(
                    replace(
                        item,
                        consumer_authoring_identity=changed_authoring.identity,
                    )
                    if item.consumer_authoring_identity
                    == original_root.authoring.identity
                    else item
                    for item in before_plan.requirement_providers
                ),
            )
            after = resolver.resolve(
                after_plan, expected_input_evidence_identity=after_plan.identity
            ).lock

            check = store.check(after, authorings=after.authorings)
            self.assertEqual(check.state, "stale")
            self.assertFalse(check.current_semantically_verified)
            self.assertTrue(
                any("authoring_identity" in item.path for item in check.differences)
            )

    def test_symlink_destination_and_forged_lock_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = component_lock()
            store = ComponentLockStore(root)
            target = root / "outside.json"
            target.write_text("{}", encoding="utf-8")
            try:
                store.path.symlink_to(target)
            except OSError:
                self.skipTest("symbolic links are unavailable")
            with self.assertRaisesRegex(ComponentLockStoreError, "regular file"):
                store.update(candidate)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ComponentLockStore(root)
            store.path.write_text(json.dumps({"schema": "forged"}), encoding="utf-8")
            with self.assertRaisesRegex(ComponentLockStoreError, "forged"):
                store.read(authorings=component_lock().authorings)


if __name__ == "__main__":
    unittest.main()

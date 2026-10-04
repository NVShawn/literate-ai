"""Atomic Component lock filesystem adapter tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.component_locks import (
    ComponentLockStore,
    ComponentLockStoreError,
)
from literate_ai.contracts.identity import canonical_identity
from tests.support.fixtures_test_component_lock_contracts import component_lock


class ComponentLockStoreTests(unittest.TestCase):
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

"""Reading missing matrix lock state must never initialize its storage."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai._filesystem import UnsafeFilesystemPathError, require_safe_directory
from literate_ai.adapters.component_locks import (
    ComponentLockStore,
    ComponentLockStoreError,
)
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
    ComponentResolutionAuditStoreError,
)
from tests.support.fixtures_test_component_lock_contracts import component_lock
from tests.support.fixtures_test_component_resolution_audit_store import (
    audit,
    create_windows_junction,
)


class MatrixLockReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.component = self.root / "component"
        self.component.mkdir()
        self.cell = self.root / "cell"
        self.enterContext(
            patch.dict(os.environ, {"LITAI_MATRIX_CELL_ROOT": str(self.cell)})
        )

    def stores(self):
        lock = component_lock()
        return (
            (
                ComponentLockStore(self.component),
                lock,
                {"authorings": lock.authorings},
                ComponentLockStoreError,
            ),
            (
                ComponentResolutionAuditStore(self.component, "host"),
                audit(),
                {},
                ComponentResolutionAuditStoreError,
            ),
        )

    def test_checks_snapshots_and_required_reads_do_not_create_missing_storage(self):
        for store, candidate, kwargs, error in self.stores():
            with self.subTest(store=type(store).__name__):
                self.assertEqual(store.check(candidate, **kwargs).state, "missing")
                self.assertIsNone(store.snapshot().content)
                with self.assertRaises(error):
                    store.read(**kwargs)
                self.assertFalse(self.cell.exists())

    def test_updates_still_create_only_matrix_artifacts_and_checks_are_inert(self):
        for store, candidate, kwargs, _error in self.stores():
            with self.subTest(store=type(store).__name__):
                self.assertTrue(store.update(candidate))
                before = {
                    p.relative_to(self.cell): p.read_bytes()
                    for p in self.cell.rglob("*")
                    if p.is_file()
                }
                self.assertTrue(store.check(candidate, **kwargs).current)
                self.assertEqual(store.read(**kwargs).identity, candidate.identity)
                after = {
                    p.relative_to(self.cell): p.read_bytes()
                    for p in self.cell.rglob("*")
                    if p.is_file()
                }
                self.assertEqual(before, after)
        self.assertEqual(list(self.component.iterdir()), [])

    def test_missing_suffix_does_not_permit_file_ancestors(self):
        missing = self.cell / "nested"
        with self.assertRaises(UnsafeFilesystemPathError):
            require_safe_directory(missing)
        require_safe_directory(missing, allow_missing=True)
        self.assertFalse(self.cell.exists())
        with self.assertRaises(UnsafeFilesystemPathError):
            require_safe_directory(missing / ".." / "outside", allow_missing=True)
        self.cell.write_text("not a directory")
        with self.assertRaises(UnsafeFilesystemPathError):
            require_safe_directory(missing, allow_missing=True)

    def test_rechecks_reject_storage_ancestor_links_created_after_construction(self):
        stores = self.stores()
        self.cell.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        for store, candidate, kwargs, error in stores:
            alias = store.storage_root.parent
            if os.name == "nt":
                create_windows_junction(alias, outside)
            else:
                alias.symlink_to(outside, target_is_directory=True)
            with self.subTest(store=type(store).__name__), self.assertRaises(error):
                store.check(candidate, **kwargs)
        self.assertEqual(list(outside.iterdir()), [])

"""Target-specific resolution-audit filesystem adapter tests."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import literate_ai.adapters.component_resolution_audits as audit_store_module
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
    ComponentResolutionAuditStoreError,
)
from literate_ai.contracts.component_locking import ComponentResolutionAudit
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from tests.unit.test_component_lock_contracts import component_lock


def audit(label: str = "catalog") -> ComponentResolutionAudit:
    lock = component_lock()
    return ComponentResolutionAudit(
        component_lock_identity=lock.identity,
        catalog_identity=canonical_identity({"catalog": label}),
        resolver_identity=lock.resolver_identity,
        candidates=(),
    )


def create_windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode(errors="replace"))


def temporary_files(store: ComponentResolutionAuditStore) -> tuple[Path, ...]:
    return tuple(
        item
        for item in store.root.glob(f".{store.path.name}.*")
        if item != store.write_lock_path
    )


class ComponentResolutionAuditStoreTests(unittest.TestCase):
    def test_target_scoped_update_is_canonical_idempotent_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate = audit()
            host = ComponentResolutionAuditStore(root, "host")
            windows = ComponentResolutionAuditStore(root, "windows-x86_64")

            self.assertNotEqual(host.path, windows.path)
            self.assertTrue(host.update(candidate))
            self.assertFalse(host.update(candidate))
            self.assertEqual(
                host.path.read_bytes(),
                canonical_json_bytes(candidate.to_dict()) + b"\n",
            )
            self.assertEqual(host.read(), candidate)
            self.assertTrue(host.check(candidate).current)
            self.assertEqual(windows.check(candidate).state, "missing")

    def test_stale_check_is_separate_from_selected_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            selected_lock = root / "component.lock.json"
            selected_lock.write_bytes(b"selected-lock-sentinel\n")
            before = audit()
            after = replace(
                before, catalog_identity=canonical_identity({"catalog": "new"})
            )
            store = ComponentResolutionAuditStore(root, "host")
            store.update(before)

            check = store.check(after)
            self.assertEqual(check.state, "stale")
            self.assertEqual(check.current_identity, before.identity.uri)
            self.assertEqual(selected_lock.read_bytes(), b"selected-lock-sentinel\n")

    def test_noncanonical_and_forged_documents_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate = audit()
            store = ComponentResolutionAuditStore(root, "host")
            store.path.write_text(
                json.dumps(candidate.to_dict(), indent=2), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ComponentResolutionAuditStoreError, "canonical"
            ):
                store.read()

            store.path.write_text(json.dumps({"schema": "forged"}), encoding="utf-8")
            with self.assertRaisesRegex(ComponentResolutionAuditStoreError, "forged"):
                store.check(candidate)

    def test_failed_revalidation_preserves_original_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ComponentResolutionAuditStore(Path(directory).resolve(), "host")
            before = audit()
            after = audit("replacement")
            store.update(before)
            original = store.path.read_bytes()

            def fail() -> None:
                raise RuntimeError("injected stale inputs")

            with self.assertRaisesRegex(RuntimeError, "stale inputs"):
                store.update(after, revalidate=fail)
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(temporary_files(store), ())

    def test_persisted_read_is_bounded_and_rollback_is_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ComponentResolutionAuditStore(Path(directory).resolve(), "host")
            snapshot = store.snapshot()
            candidate = audit()
            store.update(candidate)
            self.assertTrue(
                store.restore_if_current(snapshot, expected_current=candidate)
            )
            self.assertFalse(store.path.exists())

            store.path.write_bytes(b"x" * 33)
            with (
                patch.object(audit_store_module, "_MAX_PERSISTED_BYTES", 32),
                self.assertRaisesRegex(
                    ComponentResolutionAuditStoreError, "byte limit"
                ),
            ):
                store.read()

    def test_uncooperative_concurrent_change_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ComponentResolutionAuditStore(Path(directory).resolve(), "host")
            store.update(audit())
            concurrent = b"uncooperative writer\n"

            def change() -> None:
                store.path.write_bytes(concurrent)

            with self.assertRaisesRegex(ComponentResolutionAuditStoreError, "changed"):
                store.update(audit("replacement"), revalidate=change)
            self.assertEqual(store.path.read_bytes(), concurrent)
            self.assertEqual(temporary_files(store), ())

    def test_symlink_destination_fails_without_mutating_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = ComponentResolutionAuditStore(root, "host")
            outside = root / "outside.json"
            outside.write_bytes(b"outside-sentinel\n")
            try:
                store.path.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"host cannot create file symlinks: {error}")

            with self.assertRaisesRegex(
                ComponentResolutionAuditStoreError, "regular file"
            ):
                store.update(audit())
            self.assertEqual(outside.read_bytes(), b"outside-sentinel\n")

    def test_invalid_target_and_unsafe_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for target in ("../host", "Host", "host/name", ""):
                with self.subTest(target=target):
                    with self.assertRaisesRegex(ValueError, "portable"):
                        ComponentResolutionAuditStore(root, target)

            actual = root / "actual"
            actual.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(actual, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"host cannot create directory symlinks: {error}")
            with self.assertRaisesRegex(
                ComponentResolutionAuditStoreError, "non-reparse"
            ):
                ComponentResolutionAuditStore(linked, "host")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_windows_junction_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            actual = root / "actual"
            actual.mkdir()
            junction = root / "junction"
            create_windows_junction(junction, actual)
            try:
                with self.assertRaisesRegex(
                    ComponentResolutionAuditStoreError, "non-reparse"
                ):
                    ComponentResolutionAuditStore(junction, "host")
            finally:
                junction.rmdir()


if __name__ == "__main__":
    unittest.main()

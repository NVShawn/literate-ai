"""SDK byte custody rejects corrupt content and unsafe or replayed snapshots."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.native_sdk_custody import (
    capture_native_sdk,
    materialize_native_sdk,
)
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.native_sdks import NativeSdkSnapshot
from literate_ai.storage.cas import BlobIntegrityError, BlobNotFoundError, FileSystemCAS
from tests.unit.test_schema_catalog import SchemaCatalog


class NativeSdkCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sdk-custody-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        (self.source / "python/vendor/lib").mkdir(parents=True)
        (self.source / "python/vendor/__init__.py").write_text(
            "# original SDK fixture\n"
        )
        self.library = self.source / "python/vendor/lib/native.bin"
        self.library.write_bytes(b"opaque native candidate, not an admitted ABI")
        self.store = FileSystemCAS(self.root / "store")
        self.parent = self.root / "materializations"
        self.parent.mkdir()
        self.arguments = dict(
            store=self.store,
            source_lock_identity=canonical_identity("source"),
            recipe_identity=canonical_identity("recipe"),
            target_identity=canonical_identity("target"),
            license_identity=canonical_identity("license"),
            import_surface=LibraryImportSurface(
                "python",
                "vendor",
                (
                    LibraryCapabilityImport(
                        "vendor.api",
                        canonical_identity("interface"),
                        "vendor",
                        ("call",),
                    ),
                ),
            ),
            import_root="python",
            native_libraries=("python/vendor/lib/native.bin",),
        )

    def capture(self) -> NativeSdkSnapshot:
        return capture_native_sdk(self.source, **self.arguments)

    def test_schema_roundtrip_and_unknown_fields_fail_closed(self) -> None:
        snapshot = self.capture()
        schemas = SchemaCatalog()
        document = json.loads(json.dumps(snapshot.to_dict()))
        schemas.validate(snapshot.SCHEMA, document)
        self.assertEqual(NativeSdkSnapshot.from_dict(document), snapshot)
        document["execution_admitted"] = True
        with self.assertRaises(ValueError):
            NativeSdkSnapshot.from_dict(document)
        with self.assertRaises(AssertionError):
            schemas.validate(snapshot.SCHEMA, document)

    def test_replayed_bindings_fail_before_materialization(self) -> None:
        snapshot = self.capture()
        for field in (
            "source_lock_identity",
            "recipe_identity",
            "target_identity",
            "license_identity",
        ):
            with self.subTest(field=field):
                changed = dataclasses.replace(
                    snapshot, **{field: canonical_identity("other")}
                )
                with self.assertRaisesRegex(ValueError, "requested exact identity"):
                    materialize_native_sdk(
                        changed,
                        expected_identity=snapshot.identity,
                        store=self.store,
                        parent=self.parent,
                    )
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_corrupt_or_missing_blobs_fail_without_a_partial_directory(self) -> None:
        snapshot = self.capture()
        for missing in (False, True):
            path = self.store.path_for(snapshot.files[-1].blob)
            path.chmod(0o600)
            if missing:
                path.unlink()
            else:
                path.write_bytes(b"corrupt")
            with self.assertRaises(
                BlobNotFoundError if missing else BlobIntegrityError
            ):
                materialize_native_sdk(
                    snapshot,
                    expected_identity=snapshot.identity,
                    store=self.store,
                    parent=self.parent,
                )
            self.assertEqual(list(self.parent.iterdir()), [])

    def test_copy_corruption_is_rejected_and_unrelated_files_survive(self) -> None:
        snapshot = self.capture()
        unrelated = self.parent / "keep.txt"
        unrelated.write_text("keep")
        with mock.patch.object(
            self.store,
            "copy_to",
            side_effect=lambda _blob, target: target.write_bytes(b"corrupt"),
        ):
            with self.assertRaisesRegex(ValueError, "changed during materialization"):
                materialize_native_sdk(
                    snapshot,
                    expected_identity=snapshot.identity,
                    store=self.store,
                    parent=self.parent,
                )
        self.assertEqual(list(self.parent.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_text(), "keep")

    def test_capture_rejects_symlinks_and_concurrent_mutation(self) -> None:
        link = self.source / "linked"
        try:
            link.symlink_to(self.library)
        except OSError:
            self.skipTest("host does not permit test symlinks")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            self.capture()
        link.unlink()
        original = self.store.put_file

        def mutate(path, **kwargs):
            result = original(path, **kwargs)
            self.library.write_bytes(self.library.read_bytes() + b"changed")
            return result

        with mock.patch.object(self.store, "put_file", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed during capture"):
                self.capture()

    def test_unsafe_aliases_missing_native_files_and_empty_imports_fail(self) -> None:
        snapshot = self.capture()
        for name in ("../escape", "python/VENDOR/__init__.py", "python/vendor"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                added = dataclasses.replace(snapshot.files[-1], path=name)
                dataclasses.replace(
                    snapshot,
                    files=tuple(
                        sorted((*snapshot.files, added), key=lambda item: item.path)
                    ),
                )
        with self.assertRaises(ValueError):
            dataclasses.replace(snapshot, native_libraries=("missing.bin",))
        with self.assertRaises(ValueError):
            dataclasses.replace(snapshot, import_root="missing")

    def test_materializations_are_distinct_and_preserve_exact_bytes(self) -> None:
        snapshot = self.capture()
        roots = [
            materialize_native_sdk(
                snapshot,
                expected_identity=snapshot.identity,
                store=self.store,
                parent=self.parent,
            )
            for _ in range(2)
        ]
        self.assertNotEqual(*roots)
        for root in roots:
            self.assertEqual(
                {
                    p.relative_to(root).as_posix()
                    for p in root.rglob("*")
                    if p.is_file()
                },
                {f.path for f in snapshot.files},
            )
            for item in snapshot.files:
                self.assertEqual(
                    (root / item.path).read_bytes(), self.store.get_bytes(item.blob)
                )

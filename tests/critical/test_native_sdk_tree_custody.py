"""Real filesystem SDK alias custody, retirement, drift and cleanup checks."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import native_sdk_tree_custody as custody
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    HashAlgorithm,
    canonical_identity,
)
from literate_ai.contracts.native_sdk_tree_products import NativeSdkTreeBuildLayout
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.storage.cas import FileSystemCAS


@unittest.skipIf(os.name == "nt", "Symbolic SDK tree custody requires POSIX")
class NativeSdkTreeCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sdk-tree-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        (self.source / "bin").mkdir(parents=True)
        (self.source / "include").mkdir()
        (self.source / "bin/clang").write_bytes(b"compiler bytes")
        (self.source / "bin/clang").chmod(0o755)
        os.link(self.source / "bin/clang", self.source / "bin/cc")
        (self.source / "include/header.h").write_bytes(b"header bytes")
        (self.source / "headers").symlink_to("include", target_is_directory=True)
        (self.source / "bin/public").symlink_to("clang")
        self.store = FileSystemCAS(self.root / "store")
        self.parent = self.root / "placements"
        self.parent.mkdir()
        self.limits = RetainedProjectLimits(100, 1024, 4096)

    def capture(self, **kwargs):
        return custody.capture_native_sdk_tree(
            self.source, store=self.store, limits=self.limits, **kwargs
        )

    def place(self, tree, **kwargs):
        return custody.materialize_native_sdk_tree(
            tree,
            expected_identity=tree.identity,
            store=self.store,
            limits=self.limits,
            parent=self.parent,
            **kwargs,
        )

    def test_links_survive_producer_retirement_and_relocation(self) -> None:
        tree = self.capture()
        shutil.rmtree(self.source)
        placed = self.place(tree)
        self.assertEqual(os.readlink(placed / "headers"), "include")
        self.assertEqual(os.readlink(placed / "bin/public"), "clang")
        self.assertEqual((placed / "headers/header.h").read_bytes(), b"header bytes")
        self.assertEqual((placed / "bin/public").read_bytes(), b"compiler bytes")
        self.assertEqual(
            (placed / "bin/clang").stat().st_ino, (placed / "bin/cc").stat().st_ino
        )
        self.assertEqual(tree.resolve("bin/clang"), "bin/cc")
        self.assertEqual(
            custody.capture_native_sdk_tree(
                placed, store=self.store, limits=self.limits
            ),
            tree,
        )

    def test_external_hard_link_group_refuses(self) -> None:
        os.link(self.source / "include/header.h", self.root / "outside")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.capture()
        self.assertEqual((self.root / "outside").read_bytes(), b"header bytes")

    def test_symbolic_escape_refuses_without_reading_target_bytes(self) -> None:
        (self.root / "outside").write_bytes(b"outside bytes")
        (self.source / "escape").symlink_to("../outside")
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.capture()
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_new_top_level_member_during_capture_refuses(self) -> None:
        original = custody._stream
        changed = False

        def mutate(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                changed = True
                (self.source / "new-member").write_bytes(b"unreviewed")
            return result

        with mock.patch.object(custody, "_stream", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.capture()

    def test_hard_link_replacement_with_identical_bytes_refuses(self) -> None:
        original = custody._stream
        changed = False

        def mutate(*args, **kwargs):
            nonlocal changed
            result = original(*args, **kwargs)
            if not changed:
                changed = True
                path = self.source / "bin/clang"
                path.unlink()
                path.write_bytes(b"compiler bytes")
                path.chmod(0o755)
            return result

        with mock.patch.object(custody, "_stream", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.capture()

    def test_placement_failure_removes_owned_staging(self) -> None:
        tree = self.capture()
        with mock.patch.object(
            custody.os, "symlink", side_effect=OSError("link refused")
        ):
            with self.assertRaisesRegex(OSError, "link refused"):
                self.place(tree)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_case_sensitive_placement_checks_actual_filesystem(self) -> None:
        tree = self.capture(case_sensitive=True)
        probe = self.root / "CaseProbe"
        probe.write_bytes(b"")
        supported = not (self.root / "caseprobe").exists()
        probe.unlink()
        if supported:
            placed = self.place(tree)
            self.assertTrue(placed.is_dir())
            self.assertEqual(
                custody.capture_native_sdk_tree(
                    placed, store=self.store, limits=self.limits, case_sensitive=True
                ),
                tree,
            )
        else:
            with self.assertRaisesRegex(ValueError, "case-sensitive"):
                self.place(tree)
            self.assertEqual(list(self.parent.iterdir()), [])

    def test_limits_and_replayed_identity_refuse(self) -> None:
        with self.assertRaises(ValueError):
            custody.capture_native_sdk_tree(
                self.source,
                store=self.store,
                limits=RetainedProjectLimits(2, 1024, 4096),
            )
        tree = self.capture()
        with self.assertRaisesRegex(ValueError, "exact identity"):
            custody.materialize_native_sdk_tree(
                tree,
                expected_identity=canonical_identity("other"),
                store=self.store,
                limits=self.limits,
                parent=self.parent,
            )
        with self.assertRaises(ValueError):
            custody.capture_native_sdk_tree(
                self.source,
                store=self.store,
                limits=dataclasses.replace(self.limits, max_total_bytes=1),
            )
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_placement_budget_refuses_before_payload_copy(self) -> None:
        tree = self.capture()
        with mock.patch.object(
            self.store,
            "copy_to",
            side_effect=AssertionError("copied before budget refusal"),
        ):
            with self.assertRaisesRegex(ValueError, "bounds"):
                custody.materialize_native_sdk_tree(
                    tree,
                    expected_identity=tree.identity,
                    store=self.store,
                    limits=dataclasses.replace(self.limits, max_total_bytes=1),
                    parent=self.parent,
                )
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_capture_never_writes_a_store_inside_its_source(self) -> None:
        store = FileSystemCAS(self.source / "store")
        before = sorted(
            str(path.relative_to(self.source)) for path in self.source.rglob("*")
        )
        with self.assertRaisesRegex(ValueError, "outside"):
            custody.capture_native_sdk_tree(
                self.source, store=store, limits=self.limits
            )
        self.assertEqual(
            sorted(
                str(path.relative_to(self.source)) for path in self.source.rglob("*")
            ),
            before,
        )

    def snapshot_layout(self):
        license_bytes = b"fixture license"
        (self.source / "LICENSE").write_bytes(license_bytes)
        surface = LibraryImportSurface(
            "c",
            "fixture",
            (
                LibraryCapabilityImport(
                    "fixture.api",
                    canonical_identity("interface"),
                    "header.h",
                    ("fixture_version",),
                ),
            ),
        )
        return NativeSdkTreeBuildLayout(
            "out/sdk",
            "headers",
            surface,
            ("bin/clang",),
            "LICENSE",
            "LICENSE",
            ContentIdentity(
                HashAlgorithm.SHA256, hashlib.sha256(license_bytes).hexdigest()
            ),
            "freebsd",
            "x86_64",
            self.limits,
        )

    def test_explicit_layout_snapshot_capture_and_relocation(self) -> None:
        layout = self.snapshot_layout()
        snapshot = custody.capture_native_sdk_tree_snapshot(
            self.source,
            layout=layout,
            store=self.store,
            source_lock_identity=canonical_identity("source"),
            recipe_identity=canonical_identity("recipe"),
            target_identity=canonical_identity("target"),
        )
        self.assertEqual(snapshot.file_at("bin/clang").path, "bin/cc")
        shutil.rmtree(self.source)
        placed = custody.materialize_native_sdk_tree_snapshot(
            snapshot,
            expected_identity=snapshot.identity,
            store=self.store,
            limits=self.limits,
            parent=self.parent,
        )
        self.assertEqual((placed / "LICENSE").read_bytes(), b"fixture license")
        self.assertEqual((placed / "bin/public").read_bytes(), b"compiler bytes")
        with self.assertRaisesRegex(ValueError, "exact identity"):
            custody.materialize_native_sdk_tree_snapshot(
                snapshot,
                expected_identity=canonical_identity("replayed"),
                store=self.store,
                limits=self.limits,
                parent=self.parent,
            )

    def test_changed_output_license_refuses_snapshot_capture(self) -> None:
        layout = self.snapshot_layout()
        (self.source / "LICENSE").write_bytes(b"substituted license")
        with self.assertRaisesRegex(ValueError, "selected license"):
            custody.capture_native_sdk_tree_snapshot(
                self.source,
                layout=layout,
                store=self.store,
                source_lock_identity=canonical_identity("source"),
                recipe_identity=canonical_identity("recipe"),
                target_identity=canonical_identity("target"),
            )

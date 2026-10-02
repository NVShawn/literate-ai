"""Retained inputs are bounded binary snapshots, not generated-source evidence."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_inputs as inputs
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
)


class RetainedProjectInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "empty").mkdir()
        (self.root / "src" / "main.py").write_bytes(b"print('retained')\n")
        (self.root / "assets").mkdir()
        (self.root / "assets" / "image.bin").write_bytes(bytes(range(256)) * 8192)
        self.limits = RetainedProjectLimits(100, 3_000_000, 4_000_000)
        self.roots = {"src": "source", "assets": "asset"}

    def discover(self):
        return inputs.discover_retained_project_inputs(
            self.root, self.roots, self.limits
        )

    def test_binary_capture_roundtrip_and_relocation(self):
        manifest = self.discover()
        self.assertEqual(
            manifest, RetainedProjectManifest.from_dict(manifest.to_dict())
        )
        tree = inputs.capture_retained_project_inputs(
            self.root, manifest, self.base / "snapshot"
        )
        inputs.verify_retained_project_inputs(tree, manifest)
        self.assertEqual(manifest.identity, self.discover().identity)
        for member in manifest.members:
            self.assertEqual(
                hashlib.sha256((tree / member.path).read_bytes()).hexdigest(),
                member.sha256,
            )
            self.assertEqual((tree / member.path).stat().st_mode & 0o222, 0)
            self.assertNotEqual(
                (tree / member.path).stat().st_ino,
                (tree.parent / "blobs" / member.sha256).stat().st_ino,
            )
        (tree / "src" / "main.py").chmod(0o644)
        (tree / "src" / "main.py").write_bytes(b"mutated")
        with self.assertRaises(ValueError):
            inputs.verify_retained_project_inputs(tree, manifest)
        inputs.verify_retained_project_inputs(self.root, manifest)

    def test_extra_missing_mode_and_bytes_rejected(self):
        for mutation in ("extra", "directory", "missing", "mode", "bytes"):
            with self.subTest(mutation=mutation):
                if mutation == "mode" and os.name == "nt":
                    continue  # Windows chmod does not set executable bits.
                manifest = self.discover()
                path = self.root / "src" / "main.py"
                original = path.read_bytes()
                if mutation == "extra":
                    extra = self.root / "src" / "extra"
                    extra.write_bytes(b"x")
                elif mutation == "directory":
                    extra = self.root / "src" / "newdir"
                    extra.mkdir()
                elif mutation == "missing":
                    path.unlink()
                elif mutation == "mode":
                    path.chmod(0o755)
                else:
                    path.write_bytes(b"x" * len(original))
                with self.assertRaises((ValueError, OSError)):
                    inputs.verify_retained_project_inputs(self.root, manifest)
                if mutation == "extra":
                    extra.unlink()
                elif mutation == "directory":
                    extra.rmdir()
                path.write_bytes(original)
                path.chmod(0o644)

    def test_bounds_include_directories_and_binary_bytes(self):
        for limits in (
            RetainedProjectLimits(4, 3_000_000, 4_000_000),
            RetainedProjectLimits(100, 100, 4_000_000),
            RetainedProjectLimits(100, 3_000_000, 100),
        ):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                inputs.discover_retained_project_inputs(self.root, self.roots, limits)
        for value in (0, -1, True, 1.5, 2**63):
            with self.assertRaises(ValueError):
                RetainedProjectLimits(value, 10, 100)

    def test_alias_and_unsafe_paths_rejected(self):
        for path in ("../secret", "C:secret", "src/CON", "src/a.", "src\\a"):
            with self.subTest(path=path), self.assertRaises((ValueError, TypeError)):
                RetainedProjectMember(path, 0, 0o644, "0" * 64, "source")
        # A case-insensitive filesystem cannot create this alias; wire validation
        # must reject it on every platform nevertheless.
        manifest = RetainedProjectMember("src/a", 0, 0o644, "0" * 64, "source")
        alias = RetainedProjectMember("SRC/A", 0, 0o644, "0" * 64, "source")
        with self.assertRaises(ValueError):
            RetainedProjectManifest(
                ("src",),
                tuple(sorted((manifest, alias), key=lambda m: m.path)),
                self.limits,
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "Host has no symlink support")
    def test_links_and_redirected_parents_rejected(self):
        (self.root / "src" / "link").symlink_to(self.root / "assets" / "image.bin")
        with self.assertRaises(ValueError):
            self.discover()
        (self.root / "src" / "link").unlink()
        redirected = self.base / "redirected"
        redirected.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            inputs.discover_retained_project_inputs(redirected, self.roots, self.limits)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Host has no FIFO support")
    def test_special_file_rejected_without_blocking(self):
        os.mkfifo(self.root / "src" / "pipe")
        with self.assertRaises(ValueError):
            self.discover()

    def test_no_destination_reuse_and_no_source_nested_capture(self):
        manifest = self.discover()
        destination = self.base / "existing"
        destination.mkdir()
        marker = destination / "unrelated"
        marker.write_bytes(b"keep")
        for target in (destination, self.root / "snapshot"):
            with self.assertRaises(ValueError):
                inputs.capture_retained_project_inputs(self.root, manifest, target)
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_mutation_during_capture_never_publishes_snapshot(self):
        manifest = self.discover()
        original = inputs._stream
        changed = False

        def mutate(root, name, limit, output=None):
            nonlocal changed
            result = original(root, name, limit, output)
            if output is not None and not changed:
                changed = True
                (self.root / "src" / "main.py").write_bytes(b"changed")
            return result

        with patch.object(inputs, "_stream", side_effect=mutate):
            with self.assertRaises(ValueError):
                inputs.capture_retained_project_inputs(
                    self.root, manifest, self.base / "snapshot"
                )
        self.assertFalse((self.base / "snapshot").exists())
        self.assertFalse(list(self.base.glob(".retained-*")))

    def test_reads_are_bounded_even_for_large_binary_assets(self):
        original = os.fdopen
        observed = []

        class Guard:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                self.assert_bounded(size)
                return self.stream.read(size)

            def assert_bounded(self, size):
                if not 0 < size <= 1024 * 1024:
                    raise AssertionError(f"Unbounded retained read: {size}")
                observed.append(size)

        with patch.object(
            inputs.os, "fdopen", side_effect=lambda *a, **kw: Guard(original(*a, **kw))
        ):
            self.discover()
        self.assertGreater(len(observed), 4)

    def test_growth_during_stream_is_rejected(self):
        original = os.fdopen
        path = self.root / "src" / "main.py"

        class GrowingReader:
            def __init__(self, stream):
                self.stream = stream
                self.changed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def read(self, size):
                data = self.stream.read(size)
                if not self.changed:
                    with path.open("ab") as output:
                        output.write(b"extra")
                    self.changed = True
                return data

        with patch.object(
            inputs.os,
            "fdopen",
            side_effect=lambda *a, **kw: GrowingReader(original(*a, **kw)),
        ):
            with self.assertRaises(ValueError):
                inputs._stream(self.root, "src/main.py", path.stat().st_size)

    def test_directory_inventory_must_be_complete_and_non_aliasing(self):
        manifest = self.discover().to_dict()
        manifest["directories"].remove("src")
        with self.assertRaises(ValueError):
            RetainedProjectManifest.from_dict(manifest)
        manifest = self.discover().to_dict()
        manifest["directories"].append("SRC")
        manifest["directories"].sort()
        with self.assertRaises(ValueError):
            RetainedProjectManifest.from_dict(manifest)

    def test_manifest_fields_limits_and_roles_bind_identity(self):
        manifest = self.discover()
        changed = manifest.to_dict()
        changed["members"][0]["role"] = "sdk"
        self.assertNotEqual(
            manifest.identity, RetainedProjectManifest.from_dict(changed).identity
        )
        changed = manifest.to_dict()
        changed["limits"]["max_total_bytes"] += 1
        self.assertNotEqual(
            manifest.identity, RetainedProjectManifest.from_dict(changed).identity
        )
        changed["extra"] = "unreviewed"
        with self.assertRaises(ValueError):
            RetainedProjectManifest.from_dict(changed)


if __name__ == "__main__":
    unittest.main()

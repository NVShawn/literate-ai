"""Observed native hashes, loader aliases and current physical custody agree."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.retained_native_file_custody import native_file_digest
from literate_ai.adapters.retained_native_files import capture_retained_native_files


class RetainedNativeFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nf-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "native"
        self.directory.mkdir()
        self.file = self.directory / "library"
        self.file.write_bytes(b"observed native file")
        self.component = {
            "bom-ref": "native",
            "type": "file",
            "hashes": [
                {
                    "alg": "SHA-256",
                    "content": hashlib.sha256(self.file.read_bytes()).hexdigest(),
                }
            ],
            "properties": [{"name": "literate-ai:elf-path", "value": str(self.file)}],
        }

    def capture(self, components=None, **limits):
        return capture_retained_native_files(
            HostDependencyObservation(tuple(components or [self.component]), ()),
            **limits,
        )

    def test_observed_hashes_and_snapshot_identity_are_bound(self):
        capture = self.capture()
        self.assertEqual(capture.identity, self.capture().identity)
        self.assertEqual(capture.unmaterialized_components, ())
        old = capture.observation_content
        self.component["name"] = "later mutation"
        self.assertEqual(capture.observation_content, old)
        self.assertNotEqual(
            capture.observation_identity, self.capture().observation_identity
        )

    def test_file_changed_since_observation_refuses_initial_capture(self):
        self.file.write_bytes(b"different bytes")
        with self.assertRaisesRegex(ValueError, "observed-file-changed"):
            self.capture()

    def test_same_byte_replacement_and_later_mutation_refuse(self):
        capture = self.capture()
        backup = self.directory / "old"
        self.file.rename(backup)
        self.file.write_bytes(backup.read_bytes())
        backup.unlink()
        with self.assertRaises(ValueError):
            capture.require_unchanged()
        capture = self.capture()
        self.file.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            capture.require_unchanged()

    def test_parent_replacement_preserving_file_refuses(self):
        capture = self.capture()
        moved = self.root / "old-directory"
        self.directory.rename(moved)
        self.directory.mkdir()
        (moved / "library").rename(self.file)
        moved.rmdir()
        with self.assertRaises(ValueError):
            capture.require_unchanged()

    def test_macho_alias_is_bound_and_same_target_link_replacement_refuses(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.file)
        except OSError:
            self.skipTest("host does not permit symbolic links")
        self.component["properties"] = [
            {"name": "literate-ai:macho-resolved-path", "value": str(self.file)},
            {"name": "literate-ai:macho-path", "value": str(alias)},
            {
                "name": "literate-ai:macho-symlink",
                "value": json.dumps({"path": str(alias), "target": os.readlink(alias)}),
            },
        ]
        capture = self.capture()
        capture.require_unchanged()
        alias.rename(self.root / "old-alias")
        alias.symlink_to(self.file)
        with self.assertRaises(ValueError):
            capture.require_unchanged()
        capture = self.capture()
        other = self.directory / "other"
        other.write_bytes(self.file.read_bytes())
        alias.unlink()
        alias.symlink_to(other)
        with self.assertRaises(ValueError):
            capture.require_unchanged()
        with self.assertRaisesRegex(ValueError, "observed-loader-changed"):
            self.capture()

    def test_unmaterialized_components_are_explicit(self):
        shared = {
            "bom-ref": "shared-cache",
            "properties": [
                {
                    "name": "literate-ai:macho-path",
                    "value": "/usr/lib/shared-cache-image",
                },
                {"name": "literate-ai:macho-uuid:arm64", "value": "observed-uuid"},
            ],
        }
        capture = self.capture([self.component, shared])
        self.assertEqual(capture.unmaterialized_components, ("shared-cache",))
        self.assertEqual(capture.closure.total_bytes, len(self.file.read_bytes()))

    def test_missing_ambiguous_hashes_and_paths_refuse(self):
        for changes in (
            {"hashes": []},
            {"hashes": self.component["hashes"] * 2},
            {"properties": self.component["properties"] * 2},
            {"properties": [{"name": "literate-ai:elf-path", "value": "relative"}]},
            {"properties": None},
            {"hashes": None},
        ):
            component = copy.deepcopy(self.component)
            component.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.capture([component])

    def test_entry_and_file_budgets(self):
        for limits in (
            {"maximum_entries": 1},
            {"maximum_file_bytes": 1},
            {"maximum_entries": True},
        ):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.capture(**limits)

    def test_duplicate_file_references_share_bytes_but_distinct_files_share_budget(
        self,
    ):
        second = copy.deepcopy(self.component)
        second["bom-ref"] = "second"
        size = self.file.stat().st_size
        capture = self.capture([self.component, second])
        self.assertEqual(capture.closure.total_bytes, size)
        other = self.directory / "other"
        other.write_bytes(self.file.read_bytes())
        second["properties"][0]["value"] = str(other)
        with self.assertRaises(ValueError):
            self.capture(
                [self.component, second],
                maximum_file_bytes=size,
                maximum_total_bytes=2 * size - 1,
            )

    def test_image_above_old_limit_uses_bounded_reads_and_retains_only_digests(self):
        size = 128 * 1024 * 1024 + 1
        with self.file.open("wb") as stream:
            stream.truncate(size)
        digest = hashlib.sha256()
        block = bytes(1024 * 1024)
        for _ in range(128):
            digest.update(block)
        digest.update(b"\0")
        self.component["hashes"][0]["content"] = digest.hexdigest()
        original = os.read
        reads = []

        def bounded(descriptor, count):
            self.assertLessEqual(count, len(block))
            self.assertGreater(count, 0)
            reads.append(count)
            return original(descriptor, count)

        with patch("os.read", side_effect=bounded):
            capture = self.capture()
        self.assertEqual(capture.closure.total_bytes, size)
        self.assertGreater(len(reads), 128)
        self.assertEqual(capture.closure.entries[0].digest, digest.hexdigest())
        self.assertFalse(hasattr(capture.closure.entries[0], "content"))

    def test_exact_file_budget_passes_and_oversize_refuses_before_read(self):
        size = self.file.stat().st_size
        self.capture(maximum_file_bytes=size, maximum_total_bytes=size)
        with (
            patch("os.read") as read,
            self.assertRaisesRegex(ValueError, "file-size-limit"),
        ):
            self.capture(maximum_file_bytes=size - 1)
        read.assert_not_called()

    def test_change_during_streaming_refuses_even_when_size_is_unchanged(self):
        original = os.read
        changed = False

        def mutate(descriptor, count):
            nonlocal changed
            chunk = original(descriptor, count)
            if chunk and not changed:
                changed = True
                self.file.write_bytes(b"X" * len(chunk))
            return chunk

        with (
            patch("os.read", side_effect=mutate),
            self.assertRaisesRegex(ValueError, "custody-changed"),
        ):
            native_file_digest(self.file, 1024)

    def test_linux_recheck_does_not_depend_on_same_size_metadata_change(self):
        original = os.read
        changed = False

        def mutate(descriptor, count):
            nonlocal changed
            chunk = original(descriptor, count)
            if chunk and not changed:
                changed = True
                self.file.write_bytes(b"X" * len(chunk))
            return chunk

        module = "literate_ai.adapters.retained_native_file_custody"
        with (
            patch("os.read", side_effect=mutate),
            patch(f"{module}.sys.platform", "linux"),
            patch(f"{module}._signature", return_value=("same",)),
            patch(f"{module}._stat_binding_identity", return_value=("same",)),
            self.assertRaisesRegex(ValueError, "custody-changed"),
        ):
            native_file_digest(self.file, 1024)

    def test_windows_recheck_does_not_depend_on_same_size_metadata_change(self):
        original = os.read
        changed = False

        def mutate(descriptor, count):
            nonlocal changed
            chunk = original(descriptor, count)
            if chunk and not changed:
                changed = True
                self.file.write_bytes(b"X" * len(chunk))
            return chunk

        module = "literate_ai.adapters.retained_native_file_custody"
        with (
            patch("os.read", side_effect=mutate),
            patch(f"{module}._requires_content_recheck", return_value=True),
            patch(f"{module}._signature", return_value=("same",)),
            patch(f"{module}._stat_binding_identity", return_value=("same",)),
            self.assertRaisesRegex(ValueError, "custody-changed"),
        ):
            native_file_digest(self.file, 1024)

    def test_empty_native_file_digest_has_finite_eof_check(self):
        self.file.write_bytes(b"")
        self.assertEqual(
            native_file_digest(self.file, 0), (hashlib.sha256(b"").hexdigest(), 0)
        )

    def test_symlinked_file_or_parent_cannot_be_captured(self):
        alias = self.root / "link"
        try:
            alias.symlink_to(self.file)
        except OSError:
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            native_file_digest(alias, 1024)
        alias.unlink()
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            native_file_digest(alias / "library", 1024)

    def test_growth_during_read_cannot_escape_the_byte_budget(self):
        original = os.read
        changed = False
        size = self.file.stat().st_size

        def grow(descriptor, count):
            nonlocal changed
            chunk = original(descriptor, count)
            if chunk and not changed:
                changed = True
                with self.file.open("ab") as stream:
                    stream.write(b"beyond budget")
            return chunk

        with (
            patch("os.read", side_effect=grow),
            self.assertRaisesRegex(ValueError, "file-size-limit"),
        ):
            native_file_digest(self.file, size)

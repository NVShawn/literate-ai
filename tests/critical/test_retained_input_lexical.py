"""Lexical reuse never replaces fresh native input observations."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_tree_v2 as trees
from literate_ai.adapters import retained_project_inputs as inputs
from literate_ai.contracts.retained_cargo_custody import RetainedCargoInputBudget


@unittest.skipIf(os.name == "nt", "POSIX access-local lexical acceleration")
class RetainedInputLexicalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lexical-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.directory = self.root / "nested"
        self.directory.mkdir(parents=True)
        self.file = self.directory / "file"
        self.file.write_bytes(b"original payload")
        self.budget = RetainedCargoInputBudget(1024, 2048, 1024, 1024**2)

    def capture(self, **kwargs):
        return trees.capture_retained_cargo_tree_v2(
            self.root, budget=self.budget, **kwargs
        )

    def test_repeated_capture_work_keeps_all_filesystem_and_stream_calls(self):
        for index in range(128):
            folder = self.root / f"group-{index // 8}"
            folder.mkdir(exist_ok=True)
            (folder / f"file-{index}").write_bytes(str(index).encode())
        actual_access = inputs._directory_access

        @contextmanager
        def no_lexical_cache(root, **_kwargs):
            with actual_access(root) as access:
                self.assertIsNotNone(access)
                with patch.object(access, "set_lexical_budget", lambda _value: None):
                    yield access

        observations = []
        for uncached in (True, False):
            with ExitStack() as stack:
                if uncached:
                    stack.enter_context(
                        patch.object(trees, "_directory_access", no_lexical_cache)
                    )
                native = {
                    name: stack.enter_context(
                        patch.object(inputs.os, name, wraps=getattr(inputs.os, name))
                    )
                    for name in ("stat", "lstat", "fstat", "open", "close")
                }
                stream = stack.enter_context(
                    patch.object(trees, "_stream", wraps=trees._stream)
                )
                lexical = stack.enter_context(
                    patch.object(
                        inputs,
                        "canonical_relative_posix_paths",
                        wraps=inputs.canonical_relative_posix_paths,
                    )
                )
                tree = self.capture()
                observations.append(
                    (
                        tree,
                        {k: v.call_count for k, v in native.items()},
                        stream.call_count,
                        lexical.call_count,
                    )
                )
        before, after = observations
        self.assertEqual(before[0].identity, after[0].identity)
        self.assertEqual(before[0]._physical, after[0]._physical)
        self.assertEqual(before[1:3], after[1:3])
        self.assertGreater(before[3], after[3] * 3)
        self.assertEqual(after[2], 129)

    def test_byte_cap_shrink_invalid_names_and_new_access(self):
        with inputs._directory_access(self.root, lexical_cache_bytes=2048) as access:
            self.assertIsNotNone(access)
            for name in ("first", "second", "third", "fourth"):
                access.relative_parts(name)
                self.assertLessEqual(access._lexical_bytes, 2048)
            self.assertLess(len(access._lexical), 4)
            access.set_lexical_budget(0)
            self.assertEqual((len(access._lexical), access._lexical_bytes), (0, 0))
            with self.assertRaises(ValueError):
                access.relative_parts("../outside")
            self.assertFalse(access._lexical)
            access.set_lexical_budget(2048)
            access.relative_parts("missing")
            with self.assertRaises(FileNotFoundError):
                access.metadata(self.root / "missing")
        self.assertFalse(access._lexical)
        with patch.object(
            inputs,
            "canonical_relative_posix_paths",
            wraps=inputs.canonical_relative_posix_paths,
        ) as lexical:
            with inputs._directory_access(self.root, lexical_cache_bytes=2048) as fresh:
                fresh.relative_parts("missing")
            self.assertEqual(lexical.call_count, 1)

    def test_entry_cap_and_optional_large_name_fallback_release_allocated_capacity(
        self,
    ):
        with inputs._directory_access(self.root, lexical_cache_bytes=1024**3) as access:
            self.assertEqual(access._lexical_limit, 128 * 1024**2)
            empty_size = sys.getsizeof(access._lexical)
            for index in range(65537):
                access.relative_parts(f"file-{index:05}")
            self.assertEqual(len(access._lexical), 65536)
            self.assertNotIn("file-00000", access._lexical)
            access.set_lexical_budget(0)
            self.assertEqual(sys.getsizeof(access._lexical), empty_size)
            access.set_lexical_budget(1000)
            self.assertEqual(access.relative_parts("a" * 5000), ("a" * 5000,))
            self.assertFalse(access._lexical)

    def test_unavailable_descriptor_cache_preserves_public_identity(self):
        @contextmanager
        def unavailable(_root, **_kwargs):
            yield None

        current = self.capture()
        with patch.object(trees, "_directory_access", unavailable):
            fallback = self.capture()
        self.assertEqual(current.identity, fallback.identity)
        self.assertEqual(current._physical, fallback._physical)

    def test_empty_root_keeps_exact_minimum_metadata_allowance(self):
        root = self.base / "empty"
        root.mkdir()
        tree = trees.capture_retained_cargo_tree_v2(
            root, budget=self.budget, maximum_metadata_bytes=16 * 1024**2
        )
        self.assertEqual(
            (tree.file_count, tree.entry_count, tree.payload_bytes), (0, 0, 0)
        )
        tree.require_unchanged()

    def test_exception_closes_descriptors_and_drops_pure_cache(self):
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with inputs._directory_access(
                self.root, lexical_cache_bytes=4096
            ) as access:
                access.metadata(self.file)
                descriptor = access._absolute[-1][1]
                self.assertTrue(access._lexical)
                raise RuntimeError("injected")
        self.assertEqual((len(access._lexical), access._lexical_bytes), (0, 0))
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_metadata_budget_evicts_without_rejecting_an_otherwise_valid_tree(self):
        tiny = self.base / "tiny"
        tiny.mkdir()
        (tiny / "f").write_bytes(b"x")
        # Existing graph reservation: baseline + top name + one ordinary entry.
        maximum = 16 * 1024**2 + 128 + 32 + 2048 + 32 * len(b'"f"')
        actual = inputs._DirectoryAccess.set_lexical_budget
        reservations = []

        def observe(access, value):
            actual(access, value)
            reservations.append((value, access._lexical_bytes))
            self.assertLessEqual(access._lexical_bytes, value)

        with patch.object(inputs._DirectoryAccess, "set_lexical_budget", observe):
            tree = trees.capture_retained_cargo_tree_v2(
                tiny, budget=self.budget, maximum_metadata_bytes=maximum
            )
        self.assertEqual(tree.file_count, 1)
        self.assertIn((0, 0), reservations)
        with self.assertRaisesRegex(ValueError, "metadata-limit"):
            trees.capture_retained_cargo_tree_v2(
                tiny, budget=self.budget, maximum_metadata_bytes=maximum - 1
            )

    def test_cached_names_do_not_hide_parent_or_root_replacement(self):
        for replace_root in (False, True):
            root = self.base / ("root-case" if replace_root else "parent-case")
            folder = root / "nested"
            folder.mkdir(parents=True)
            (folder / "file").write_bytes(b"same")
            with self.subTest(replace_root=replace_root), self.assertRaises(ValueError):
                with inputs._directory_access(root, lexical_cache_bytes=4096) as access:
                    access.metadata(folder / "file")
                    selected = root if replace_root else folder
                    selected.rename(self.base / (selected.name + "-saved"))
                    selected.mkdir()
                    access.metadata(folder / "file")

    def test_cached_names_do_not_hide_ancestor_symlink(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "file").write_bytes(b"original payload")
        with self.assertRaises((ValueError, OSError)):
            with inputs._directory_access(
                self.root, lexical_cache_bytes=4096
            ) as access:
                access.metadata(self.file)
                self.directory.rename(self.base / "saved")
                self.directory.symlink_to(outside, target_is_directory=True)
                access.metadata(self.file)

    def test_content_mode_and_same_byte_replacement_still_refuse(self):
        for mutation in ("content", "mode", "replacement", "write-and-restore"):
            with self.subTest(mutation=mutation):
                self.file.chmod(0o600)
                self.file.write_bytes(b"original payload")
                tree = self.capture()
                if mutation == "content":
                    self.file.write_bytes(b"changed")
                elif mutation == "mode":
                    self.file.chmod(0o400)
                elif mutation == "replacement":
                    replacement = self.base / "replacement"
                    replacement.write_bytes(self.file.read_bytes())
                    replacement.chmod(0o600)
                    replacement.replace(self.file)
                else:
                    original_times = self.file.stat()
                    self.file.write_bytes(b"changed")
                    self.file.write_bytes(b"original payload")
                    os.utime(
                        self.file,
                        ns=(original_times.st_atime_ns, original_times.st_mtime_ns),
                    )
                with self.assertRaisesRegex(ValueError, "custody-changed"):
                    tree.require_unchanged()

    def test_replacement_during_stream_is_not_hidden_by_warmed_lexical_cache(self):
        original = trees._stream
        replaced = False

        def replace_after_read(root, name, *args, **kwargs):
            nonlocal replaced
            value = original(root, name, *args, **kwargs)
            if not replaced:
                replacement = self.base / "other"
                replacement.write_bytes(self.file.read_bytes())
                replacement.replace(self.file)
                replaced = True
            return value

        with patch.object(trees, "_stream", replace_after_read):
            with self.assertRaisesRegex(ValueError, "physical-custody-changed"):
                self.capture()
        self.assertTrue(replaced)

    def test_namespace_link_and_hardlink_drift_still_refuse(self):
        tree = self.capture()
        extra = self.root / "extra"
        extra.write_bytes(b"new entry")
        with self.assertRaisesRegex(ValueError, "custody-changed"):
            tree.require_unchanged()
        extra.unlink()
        alias = self.root / "alias"
        os.link(self.file, alias)
        with self.assertRaisesRegex(ValueError, "custody-changed"):
            tree.require_unchanged()
        alias.unlink()
        self.file.unlink()
        self.file.symlink_to("../../foreign")
        with self.assertRaises(ValueError):
            tree.require_unchanged()


if __name__ == "__main__":
    unittest.main()

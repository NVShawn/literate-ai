"""Current filesystem names remain authoritative when directory handles are reused."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_inputs as inputs
from literate_ai.contracts.retained_project import RetainedProjectLimits


@unittest.skipIf(os.name == "nt", "POSIX descriptor traversal")
class RetainedInputWalkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "source"
        self.directory = self.root / "shared" / "include" / "nested"
        self.directory.mkdir(parents=True)
        for index in range(100):
            (self.directory / f"file-{index:03}.h").write_bytes(str(index).encode())
        self.limits = RetainedProjectLimits(500, 1024, 16384)
        self.manifest = inputs.discover_retained_project_inputs(
            self.root, {"shared": "source"}, self.limits
        )

    def test_shared_prefix_open_cost_is_bounded_without_changing_the_manifest(self):
        @contextmanager
        def legacy(_root):
            yield None

        original = os.open
        with patch.object(inputs.os, "open", wraps=original) as observed:
            inputs.verify_retained_project_inputs(self.root, self.manifest)
        optimized = observed.call_count
        with (
            patch.object(inputs, "_directory_access", legacy),
            patch.object(inputs.os, "open", wraps=original) as observed,
        ):
            inputs.verify_retained_project_inputs(self.root, self.manifest)
        self.assertLess(optimized * 4, observed.call_count)
        self.assertEqual(
            inputs.discover_retained_project_inputs(
                self.root, {"shared": "source"}, self.limits
            ),
            self.manifest,
        )

    def _replace_during_open(self, replace):
        original = inputs._open_file
        changed = False

        def opening(path, access=None):
            nonlocal changed
            descriptor = original(path, access)
            if path.name == "file-000.h" and not changed:
                changed = True
                replace()
            return descriptor

        with patch.object(inputs, "_open_file", side_effect=opening):
            with self.assertRaises((ValueError, OSError)):
                inputs.verify_retained_project_inputs(self.root, self.manifest)
        self.assertTrue(changed)

    def test_discovery_and_capture_reuse_prefixes_without_changing_inputs(self):
        @contextmanager
        def legacy(_root):
            yield None

        original = os.open
        for operation in ("discover", "capture"):
            counts = []
            for baseline in (False, True):
                destination = self.base / f"{operation}-{baseline}"
                with patch.object(inputs.os, "open", wraps=original) as observed:
                    with (
                        patch.object(inputs, "_directory_access", legacy)
                        if baseline
                        else nullcontext()
                    ):
                        if operation == "discover":
                            manifest = inputs.discover_retained_project_inputs(
                                self.root, {"shared": "source"}, self.limits
                            )
                            self.assertEqual(manifest, self.manifest)
                        else:
                            tree = inputs.capture_retained_project_inputs(
                                self.root, self.manifest, destination
                            )
                    counts.append(observed.call_count)
                if operation == "capture":
                    inputs.verify_retained_project_inputs(tree, self.manifest)
            with self.subTest(operation=operation):
                self.assertLess(counts[0] * 4, counts[1])

    def test_capture_refuses_replaced_cached_parent_before_publication(self):
        original = inputs._stream
        changed = False
        destination = self.base / "snapshot"

        def stream(root, name, limit, output=None, **kwargs):
            nonlocal changed
            result = original(root, name, limit, output, **kwargs)
            if output is not None and name.endswith("file-099.h") and not changed:
                changed = True
                saved = self.base / "saved-copy-input"
                self.directory.rename(saved)
                shutil.copytree(saved, self.directory)
            return result

        with patch.object(inputs, "_stream", side_effect=stream):
            with self.assertRaises((ValueError, OSError)):
                inputs.capture_retained_project_inputs(
                    self.root, self.manifest, destination
                )
        self.assertTrue(changed)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.base.glob(".retained-*")), [])

    def test_cached_parent_replaced_by_link_cannot_hide_behind_its_old_handle(self):
        def replace():
            saved = self.base / "saved"
            self.directory.rename(saved)
            self.directory.symlink_to(saved, target_is_directory=True)

        self._replace_during_open(replace)

    def test_replaced_root_is_refused_even_when_it_contains_the_same_bytes(self):
        def replace():
            saved = self.base / "saved-root"
            self.root.rename(saved)
            shutil.copytree(saved, self.root)

        self._replace_during_open(replace)

    def test_extra_entry_during_hashing_and_later_byte_changes_still_refuse(self):
        original = inputs._stream
        changed = False

        def stream(root, name, limit, output=None, **kwargs):
            nonlocal changed
            result = original(root, name, limit, output, **kwargs)
            if not changed:
                changed = True
                (self.directory / "extra.h").write_bytes(b"new")
            return result

        with patch.object(inputs, "_stream", side_effect=stream):
            with self.assertRaises(ValueError):
                inputs.verify_retained_project_inputs(self.root, self.manifest)
        (self.directory / "extra.h").unlink()
        inputs.verify_retained_project_inputs(self.root, self.manifest)
        (self.directory / "file-000.h").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            inputs.verify_retained_project_inputs(self.root, self.manifest)

    def test_descriptor_budget_and_exception_cleanup(self):
        for index in range(48):
            directory = self.root / f"branch-{index}"
            directory.mkdir()
            (directory / "file").write_bytes(b"x")
        opened = set()
        owned = set()
        peak = 0
        original_open, original_close = os.open, os.close

        def opening(*args, **kwargs):
            nonlocal peak
            descriptor = original_open(*args, **kwargs)
            owned.add(descriptor)
            opened.add(descriptor)
            peak = max(peak, len(owned))
            return descriptor

        def closing(descriptor):
            original_close(descriptor)
            owned.discard(descriptor)

        with (
            patch.object(inputs.os, "open", side_effect=opening),
            patch.object(inputs.os, "close", side_effect=closing),
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            with inputs._directory_access(self.root) as access:
                self.assertIsNotNone(access)
                for index in range(48):
                    access.metadata(self.root / f"branch-{index}" / "file")
                access.require_current()
                raise RuntimeError("stop")
        self.assertLessEqual(peak, 32)
        self.assertFalse(owned)
        for descriptor in opened:
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_initial_descriptor_exhaustion_falls_back_without_leaking(self):
        with patch.object(
            inputs._DirectoryAccess,
            "_open_directory",
            side_effect=OSError(errno.EMFILE, "injected descriptor limit"),
        ):
            inputs.verify_retained_project_inputs(self.root, self.manifest)

    def test_directory_replacement_after_last_access_refuses_at_transaction_end(self):
        with self.assertRaises(ValueError):
            with inputs._directory_access(self.root) as access:
                self.assertIsNotNone(access)
                access.metadata(self.directory / "file-000.h")
                self.directory.rename(self.base / "saved-last-directory")
                self.directory.mkdir()

    def test_cached_directory_handles_do_not_preserve_revoked_read_permissions(self):
        self.addCleanup(self.directory.chmod, 0o755)
        with self.assertRaises(ValueError):
            with inputs._directory_access(self.root) as access:
                self.assertIsNotNone(access)
                access.metadata(self.directory / "file-000.h")
                self.directory.chmod(0o111)

    def test_lexical_prefix_and_cached_names_cannot_escape_the_root(self):
        sibling = self.base / "source-other"
        sibling.mkdir()
        (sibling / "file").write_bytes(b"outside")
        with inputs._directory_access(self.root) as access:
            self.assertIsNotNone(access)
            access.metadata(self.directory / "file-000.h")
            for path in (sibling / "file", self.root / ".." / "source-other" / "file"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    access.metadata(path)
            access.metadata(self.directory / "file-000.h")

    def test_low_real_descriptor_limit_preserves_the_legacy_path(self):
        program = """import importlib.util,resource,sys
from pathlib import Path
from literate_ai.contracts.retained_project import RetainedProjectLimits
spec=importlib.util.spec_from_file_location('retained_input_under_test',sys.argv[2])
inputs=importlib.util.module_from_spec(spec);spec.loader.exec_module(inputs)
resource.setrlimit(resource.RLIMIT_NOFILE,(24,24))
manifest=inputs.discover_retained_project_inputs(Path(sys.argv[1]),{'shared':'source'},RetainedProjectLimits(500,1024,16384))
assert len(manifest.members)==100
"""
        result = subprocess.run(
            [sys.executable, "-c", program, str(self.root), inputs.__file__],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

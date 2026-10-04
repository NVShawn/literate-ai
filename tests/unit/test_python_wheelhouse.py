from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packaging.markers import default_environment

from literate_ai.adapters.dependencies.python_lock import parse_python_wheel_lock
from literate_ai.adapters.dependencies.python_wheelhouse import stage_python_wheels
from literate_ai.adapters.dependencies.types import DependencyObservationError
from tests.support.fixtures_test_python_wheel_lock import document, wheel_record


class PythonWheelhouseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lock = parse_python_wheel_lock(json.dumps(document()))
        self.sources = {
            "example": io.BytesIO(wheel_record(dependencies=("helper>=1",))[1]),
            "helper": io.BytesIO(wheel_record("helper")[1]),
        }

    def stage(self, **overrides):
        options = {
            "environment": default_environment(),
            "tags": ("py3-none-any",),
            "temporary_root": self.root,
        }
        return stage_python_wheels(self.lock, self.sources, **{**options, **overrides})

    def test_copies_do_not_retain_mutable_input_and_cleanup_on_exit(self):
        with self.stage() as staged:
            path = staged.directory
            self.sources["example"].seek(0)
            self.sources["example"].write(b"corrupted input")
            staged.revalidate()
            self.assertEqual(len(staged.payloads), 2)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertFalse(path.exists())
        self.assertFalse(self.sources["example"].closed)
        with self.assertRaises(DependencyObservationError):
            staged.revalidate()

    def test_changed_or_extra_staged_bytes_rejected(self):
        for action in ("overwrite", "extra", "remove"):
            with self.subTest(action=action), self.stage() as staged:
                path = staged.directory / self.lock.packages[0].filename
                if action == "overwrite":
                    path.write_bytes(b"modified cache")
                elif action == "extra":
                    (staged.directory / "unlocked.whl").write_bytes(b"extra")
                else:
                    path.unlink()
                with self.assertRaises(DependencyObservationError):
                    staged.revalidate()

    def test_symlink_replacement_rejected(self):
        with self.stage() as staged:
            path = staged.directory / self.lock.packages[0].filename
            external = self.root / "external.whl"
            external.write_bytes(path.read_bytes())
            path.unlink()
            try:
                path.symlink_to(external)
            except OSError:
                self.skipTest("Host does not permit creation of test symlinks")
            with self.assertRaises(DependencyObservationError):
                staged.revalidate()

    def test_hardlink_alias_rejected(self):
        with self.stage() as staged:
            path = staged.directory / self.lock.packages[0].filename
            alias = self.root / "alias.whl"
            try:
                os.link(path, alias)
            except OSError:
                self.skipTest("Host does not permit creation of test hardlinks")
            with self.assertRaises(DependencyObservationError):
                staged.revalidate()

    def test_failed_hash_or_closed_input_cleans_partial_copies(self):
        for action in ("corrupt", "closed"):
            with self.subTest(action=action):
                self.sources["helper"] = io.BytesIO(b"wrong bytes")
                if action == "closed":
                    self.sources["helper"].close()
                with self.assertRaises(DependencyObservationError), self.stage():
                    self.fail("Invalid input must not yield a wheelhouse")
                self.assertEqual(list(self.root.iterdir()), [])

    def test_size_limits_clean_partial_copies(self):
        for limit in ("_MAX_WHEEL", "_MAX_BUNDLE"):
            with (
                self.subTest(limit=limit),
                patch(
                    "literate_ai.adapters.dependencies.python_wheelhouse." + limit, 8
                ),
                self.assertRaises(DependencyObservationError),
                self.stage(),
            ):
                self.fail("Oversized copy must not yield")
            self.assertEqual(list(self.root.iterdir()), [])

    def test_target_and_inventory_rejected_before_copy(self):
        with (
            self.assertRaises(DependencyObservationError),
            self.stage(tags=("different",)),
        ):
            self.fail("Wrong target must not yield")
        self.sources.pop("helper")
        with self.assertRaises(DependencyObservationError), self.stage():
            self.fail("Incomplete inventory must not yield")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_consumer_exception_still_cleans_wheelhouse(self):
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            with self.stage() as staged:
                path = staged.directory
                raise RuntimeError("consumer failed")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

"""Generated runtime data has recursive byte and physical-directory custody."""

import os
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.retained_cargo_build_outputs import (
    capture_retained_cargo_build_outputs,
)


class CargoBuildOutputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bo-")
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name).resolve()
        self.root = self.output / "pkg" / "out"
        self.root.mkdir(parents=True)
        self.nested = self.root / "nested"
        self.nested.mkdir()
        self.file = self.nested / "data"
        self.file.write_bytes(b"generated data")
        self.records = [
            {
                "reason": "build-script-executed",
                "package_id": "pkg",
                "out_dir": str(self.root),
            }
        ]
        self.metadata = {"packages": [{"id": "pkg"}]}

    def capture(self, records=None, **limits):
        return capture_retained_cargo_build_outputs(
            self.records if records is None else records,
            self.metadata,
            output_root=self.output,
            **limits,
        )

    def test_recursive_capture_and_stable_identity_including_empty_directories(self):
        capture = self.capture(self.records * 2)
        self.assertEqual(capture.closure.total_bytes, len(b"generated data"))
        self.assertEqual(capture.identity, self.capture().identity)
        (self.nested / "empty").mkdir()
        with self.assertRaises(ValueError):
            capture.require_unchanged()
        self.assertNotEqual(capture.identity, self.capture().identity)

    def test_nested_byte_change_refuses(self):
        capture = self.capture()
        self.file.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            capture.require_unchanged()

    def test_same_bytes_replacement_and_removed_files_refuse(self):
        capture = self.capture()
        self.file.rename(self.nested / "old")
        self.file.write_bytes(b"generated data")
        (self.nested / "old").unlink()
        with self.assertRaises(ValueError):
            capture.require_unchanged()
        capture = self.capture()
        self.file.unlink()
        with self.assertRaises(ValueError):
            capture.require_unchanged()

    def test_parent_replacement_preserving_output_tree_refuses(self):
        capture = self.capture()
        parent = self.root.parent
        moved = self.output / "saved"
        parent.rename(moved)
        parent.mkdir()
        (moved / "out").rename(self.root)
        moved.rmdir()
        with self.assertRaises(ValueError):
            capture.require_unchanged()

    def test_links_and_hardlinks_refuse(self):
        alias = self.nested / "alias"
        try:
            alias.symlink_to(self.file)
        except OSError:
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            self.capture()
        alias.unlink()
        os.link(self.file, alias)
        with self.assertRaises(ValueError):
            self.capture()

    def test_unknown_missing_relative_escape_and_overlap_refuse(self):
        for patch in (
            {"package_id": "foreign"},
            {"package_id": []},
            {"out_dir": None},
            {"out_dir": "relative"},
            {"out_dir": str(self.output.parent)},
            {"out_dir": str(self.output)},
            {"out_dir": str(self.root / "..")},
            {"out_dir": str(self.output / "missing")},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.capture([{**self.records[0], **patch}])
        with self.assertRaisesRegex(ValueError, "overlap"):
            self.capture(
                self.records + [{**self.records[0], "out_dir": str(self.nested)}]
            )

    def test_global_entry_and_byte_limits(self):
        # fresh root + package parent + OUT_DIR + nested directory + data file
        self.capture(maximum_entries=5)
        for limit in (1, 4):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.capture(maximum_entries=limit)
        for limits in (
            {"maximum_file_bytes": 2},
            {"maximum_file_bytes": 12, "maximum_total_bytes": 12},
            {"maximum_entries": True},
        ):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.capture(**limits)

    def test_no_build_scripts_is_an_empty_stable_capture(self):
        capture = self.capture([])
        capture.require_unchanged()
        self.assertEqual(capture.trees, ())

    def test_limits_are_shared_across_output_trees(self):
        other = self.output / "second" / "out"
        other.mkdir(parents=True)
        (other / "data").write_bytes(b"generated data")
        records = self.records + [{**self.records[0], "out_dir": str(other)}]
        self.capture(records, maximum_entries=8)
        with self.assertRaises(ValueError):
            self.capture(records, maximum_entries=7)
        with self.assertRaises(ValueError):
            self.capture(records, maximum_file_bytes=16, maximum_total_bytes=25)
        with self.assertRaisesRegex(ValueError, "root-limit"):
            self.capture(
                [
                    {**self.records[0], "out_dir": str(self.output / f"out-{i}")}
                    for i in range(129)
                ]
            )

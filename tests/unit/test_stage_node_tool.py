"""Disposable Node-tool projection tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.stage_node_tool import StagingError, stage_node_tool


class StageNodeToolTests(unittest.TestCase):
    def test_projects_authored_files_but_not_existing_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tools" / "example"
            (source / "scripts").mkdir(parents=True)
            (source / "node_modules" / "dependency").mkdir(parents=True)
            (source / "package.json").write_text("{}\n", encoding="utf-8")
            (source / "scripts" / "check.mjs").write_text(
                "export {};\n", encoding="utf-8"
            )
            (source / "node_modules" / "dependency" / "index.js").write_text(
                "throw new Error();\n", encoding="utf-8"
            )
            object_root = root / "_build"
            destination = object_root / "tools" / "example"

            stage_node_tool(source, destination, object_root)

            self.assertTrue((destination / "package.json").is_file())
            self.assertTrue((destination / "scripts" / "check.mjs").is_file())
            self.assertFalse((destination / "node_modules").exists())

    def test_replaces_a_prior_disposable_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "package.json").write_text('{"version":1}\n', encoding="utf-8")
            object_root = root / "_build"
            destination = object_root / "tools" / "example"
            destination.mkdir(parents=True)
            (destination / "stale.txt").write_text("stale", encoding="utf-8")

            stage_node_tool(source, destination, object_root)

            self.assertFalse((destination / "stale.txt").exists())
            self.assertEqual(
                (destination / "package.json").read_text(encoding="utf-8"),
                '{"version":1}\n',
            )

    def test_rejects_a_destination_outside_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "package.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(StagingError, "child of OBJ_DIR"):
                stage_node_tool(source, root / "tools", root / "_build")


if __name__ == "__main__":
    unittest.main()

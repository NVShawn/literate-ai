"""Plugin bundle copies checked-in skills without duplicating them in git."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from literate_ai.version import DISTRIBUTION_VERSION
from scripts.build_plugin_bundle import build_plugin_bundle, build_release_plugins

REPO = Path(__file__).resolve().parents[2]


class PluginBundleTests(unittest.TestCase):
    def test_release_archives_are_reproducible_and_preserve_canonical_skill(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_release_plugins(repository=REPO, output=root / "first")
            second = build_release_plugins(repository=REPO, output=root / "second")
            self.assertEqual(first["manifest"], second["manifest"])
            self.assertEqual(len(first["artifacts"]), 2)
            for left, right in zip(
                first["artifacts"], second["artifacts"], strict=True
            ):
                self.assertEqual(
                    Path(left["path"]).read_bytes(), Path(right["path"]).read_bytes()
                )
                provider = left["role"].removesuffix("-plugin")
                with zipfile.ZipFile(left["path"]) as archive:
                    manifest = json.loads(
                        archive.read(f"literate-ai/.{provider}-plugin/plugin.json")
                    )
                    self.assertEqual(manifest["version"], DISTRIBUTION_VERSION)
                    self.assertEqual(
                        archive.read("literate-ai/skills/literate-ai/SKILL.md"),
                        (REPO / "SKILL.md").read_bytes(),
                    )
                    self.assertNotIn("mcpServers", manifest)
                    self.assertFalse(
                        any(".." in Path(name).parts for name in archive.namelist())
                    )

    def test_bundle_skill_bytes_match_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "literate-ai"
            result = build_plugin_bundle(repository=REPO, output=output)
            self.assertEqual(result["version"], DISTRIBUTION_VERSION)
            catalog = REPO / "skills" / "agent"
            bundled = output / "skills" / "agent"
            catalog_files = sorted(
                path.relative_to(catalog)
                for path in catalog.rglob("*")
                if path.is_file()
            )
            bundled_files = sorted(
                path.relative_to(bundled)
                for path in bundled.rglob("*")
                if path.is_file()
            )
            self.assertEqual(catalog_files, bundled_files)
            for relative in catalog_files:
                self.assertEqual(
                    (catalog / relative).read_bytes(), (bundled / relative).read_bytes()
                )
            self.assertEqual(
                (REPO / "SKILL.md").read_bytes(), (output / "SKILL.md").read_bytes()
            )
            self.assertFalse((REPO / "skills" / "agent" / ".plugin-copy").exists())

"""Cargo adoption is not displaced by incidental Python/C++ helper sources."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.harness_inventory import source_language_markers
from literate_ai.adapters.project_initialization import (
    detect_repo_flavors,
    detected_language_flavors,
    plan_convert,
)


class RustAdoptionDetectionTests(unittest.TestCase):
    def test_public_plan_keeps_cargo_primary_and_reports_helper_languages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "Cargo.toml": '[package]\nname="fixture"\nversion="0.1.0"\n',
                "src/main.rs": "fn main() {}\n",
                "package.json": '{"private":true}\n',
                "pnpm-workspace.yaml": "packages:\n  - web/*\n",
                "web/helper.js": "throw new Error('never execute discovery');\n",
                "tools/helper.py": (
                    "raise RuntimeError('never execute during discovery')\n"
                ),
                "vendor/helper.cpp": "#error never compile during discovery\n",
            }
            for name, content in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            before = {str(p.relative_to(root)) for p in root.rglob("*")}
            self.assertEqual(
                source_language_markers(root), {"rust", "cpp", "python", "javascript"}
            )
            self.assertEqual(
                detected_language_flavors(root),
                [
                    "flavor://literate-ai/lang-rust",
                    "flavor://literate-ai/lang-cpp",
                    "flavor://literate-ai/lang-python",
                    "flavor://literate-ai/lang-javascript",
                ],
            )
            selected = detect_repo_flavors(root)
            self.assertIn("flavor://literate-ai/lang-rust", selected)
            self.assertIn("flavor://literate-ai/build-cargo", selected)
            self.assertIn("flavor://literate-ai/package-cargo", selected)
            self.assertNotIn("flavor://literate-ai/package-pip", selected)
            plan = plan_convert(root, default_branch="main")
            self.assertEqual(plan["languages"], ["cpp", "javascript", "python", "rust"])
            self.assertEqual(
                plan["proposed_flavors"][0], "flavor://literate-ai/lang-rust"
            )
            self.assertIn(
                "flavor://literate-ai/package-cargo", plan["proposed_flavors"]
            )
            self.assertIn(
                "flavor://literate-ai/lang-javascript", plan["proposed_flavors"]
            )
            self.assertFalse(plan["writes"])
            self.assertEqual(
                {str(p.relative_to(root)) for p in root.rglob("*")}, before
            )
            for name, content in files.items():
                self.assertEqual((root / name).read_text(encoding="utf-8"), content)
            for omitted in ("package.json", "web/helper.js"):
                with self.subTest(omitted=omitted):
                    (root / omitted).unlink()
                    before = {
                        str(p.relative_to(root)): p.read_bytes()
                        for p in root.rglob("*")
                        if p.is_file()
                    }
                    variant = plan_convert(root, default_branch="main")
                    self.assertEqual(variant["languages"], plan["languages"])
                    self.assertIn(
                        "flavor://literate-ai/lang-javascript",
                        variant["proposed_flavors"],
                    )
                    self.assertFalse(variant["writes"])
                    self.assertEqual(
                        {
                            str(p.relative_to(root)): p.read_bytes()
                            for p in root.rglob("*")
                            if p.is_file()
                        },
                        before,
                    )
                    (root / omitted).write_text(files[omitted], encoding="utf-8")

    def test_root_cmake_preserves_existing_primary_language_and_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CMakeLists.txt").write_text("project(fixture)\n", encoding="utf-8")
            (root / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
            (root / "package.json").write_text("{}\n", encoding="utf-8")
            (root / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            languages = detected_language_flavors(root)
            self.assertEqual(languages[0], "flavor://literate-ai/lang-cpp")
            self.assertIn("flavor://literate-ai/lang-rust", languages)
            self.assertIn("flavor://literate-ai/lang-javascript", languages)
            selected = detect_repo_flavors(root)
            self.assertIn("flavor://literate-ai/build-cmake", selected)
            self.assertNotIn("flavor://literate-ai/build-cargo", selected)
            self.assertNotIn("flavor://literate-ai/package-cargo", selected)

    def test_rust_inventory_ignores_generated_and_hidden_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("_build", ".cache"):
                (root / name).mkdir()
                (root / name / "main.rs").write_text("", encoding="utf-8")
                (root / name / "helper.js").write_text("", encoding="utf-8")
            self.assertEqual(source_language_markers(root), set())
            self.assertEqual(detected_language_flavors(root), [])

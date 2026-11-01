"""Explicit external JS dependencies work in the real adoption sandbox."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support.fixtures_test_project_cli import invoke


@unittest.skipUnless(
    os.name != "nt" and shutil.which("node") and shutil.which("make"),
    "POSIX Make and native Node required",
)
class AdoptionNodeDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts/helper.mjs").write_text(
            "import { value } from '@fixture/retained-helper';\nconsole.log(value);\n"
        )
        (self.root / "package.json").write_text(
            json.dumps({"name": "retained-fixture", "private": True, "type": "module"})
        )
        (self.root / "test_dep.py").write_text(
            "import subprocess, unittest\n"
            "class DependencyTest(unittest.TestCase):\n"
            "    def test_node_dependency(self):\n"
            "        p = subprocess.run(['node', 'scripts/helper.mjs'],\n"
            "                           capture_output=True, text=True)\n"
            "        self.assertEqual(p.returncode, 0, p.stderr)\n"
            "        self.assertEqual(p.stdout.strip(), '7')\n"
        )
        python = shlex.quote(sys.executable)
        (self.root / "Makefile").write_text(
            f"build:\n\t{python} -m py_compile test_dep.py\n"
            f"test:\n\t{python} -m unittest -q test_dep\n"
            f"package:\n\t{python} -c 'import zipfile; "
            'z=zipfile.ZipFile("package.zip", "w"); '
            'z.write("scripts/helper.mjs"); z.close()'
            "'\n"
        )
        (self.root / ".gitignore").write_text(
            "node_modules/\n__pycache__/\npackage.zip\n"
        )
        for args in (
            ("init", "-q"),
            ("add", "."),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "baseline",
            ),
        ):
            subprocess.run(
                ("git", "-C", str(self.root), *args),
                check=True,
                capture_output=True,
                timeout=30,
            )
        modules = self.root / "node_modules"
        package = modules / "@fixture/retained-helper"
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "@fixture/retained-helper",
                    "version": "1.0.0",
                    "type": "module",
                    "exports": "./index.js",
                }
            )
        )
        (package / "index.js").write_text("export const value = 7;\n")
        self.external = self.base / "dependency-cache/node_modules"
        shutil.copytree(modules, self.external)
        self.source = (self.root / "scripts/helper.mjs").read_bytes()

    def adopt(self, *options):
        return invoke(
            "onboard",
            "adopt",
            str(self.root),
            "--source-intelligence-provider",
            "none",
            "--apply",
            "--acknowledge",
            "--run-baseline",
            *options,
        )

    def test_checkout_passes_but_missing_sandbox_binding_fails_and_rolls_back(self):
        checkout = subprocess.run(
            ("node", "scripts/helper.mjs"),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual((checkout.returncode, checkout.stdout.strip()), (0, "7"))
        status, result = self.adopt()
        self.assertEqual(status, 2, result)
        self.assertEqual(result["error"]["code"], "project.convert_legacy_gate_failed")
        self.assertIn("ERR_MODULE_NOT_FOUND", result["error"]["message"])
        self.assertEqual((self.root / "scripts/helper.mjs").read_bytes(), self.source)
        self.assertTrue(
            (self.root / "node_modules/@fixture/retained-helper/index.js").is_file()
        )
        self.assertFalse((self.root / "literate.project.json").exists())

    def test_explicit_external_binding_passes_real_import_and_keeps_portable_evidence(
        self,
    ):
        before = {
            p.relative_to(self.external).as_posix(): p.read_bytes()
            for p in self.external.rglob("*")
            if p.is_file()
        }
        status, result = self.adopt(
            "--harness-workspace-link", f"node_modules={self.external}"
        )
        self.assertEqual(status, 0, result)
        implementation = self.root / "components/legacy-project-wrapper/implementation"
        self.assertEqual(
            (implementation / "scripts/helper.mjs").read_bytes(), self.source
        )
        inventory_bytes = (self.root / ".literate/harness-inventory.json").read_bytes()
        inventory = json.loads(inventory_bytes)
        self.assertEqual(inventory["workspace_links"]["destinations"], ["node_modules"])
        self.assertNotIn(str(self.external).encode(), inventory_bytes)
        after = {
            p.relative_to(self.external).as_posix(): p.read_bytes()
            for p in self.external.rglob("*")
            if p.is_file()
        }
        self.assertEqual(after, before)
        self.assertTrue(
            (
                implementation / "node_modules/@fixture/retained-helper/index.js"
            ).is_file()
        )


if __name__ == "__main__":
    unittest.main()

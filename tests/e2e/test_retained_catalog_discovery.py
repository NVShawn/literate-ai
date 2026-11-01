"""Retained dependency files are not Component authoring or lock authority."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.component_lock_planning import (
    component_catalog_manifest_paths,
)
from literate_ai.project_authority_graph import project_authority_graph
from tests.support.fixtures_test_project_cli import invoke


class RetainedCatalogDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "adopted"
        status, envelope = invoke(
            "init",
            str(self.root),
            "--flavor",
            "python",
            "--flavor",
            "macos",
            "--flavor",
            "bazel",
        )
        self.assertEqual(status, 0, envelope)
        self.component = self.root / "samples" / "hello-component"

    def test_real_validation_and_graph_do_not_enter_retained_dependency_tree(self):
        status, before = invoke("project", "validate", str(self.root))
        self.assertEqual(status, 0, before)
        graph_before = project_authority_graph(self.root).to_dict()
        implementation = self.component / "implementation"
        for relative, content in {
            "node_modules/extend/component.json": '{"name":"third-party-component-io"}',
            "node_modules/another/component.md": "Unrelated third-party Markdown\n",
            "target/fixture/component.lock.json": "not a framework lock\n",
        }.items():
            path = implementation / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        scandir = os.scandir

        def guarded_scandir(path):
            if isinstance(path, (str, Path)) and Path(path).is_relative_to(
                implementation
            ):
                raise AssertionError("retained implementation was enumerated")
            return scandir(path)

        with mock.patch("os.scandir", side_effect=guarded_scandir):
            status, after = invoke("project", "validate", str(self.root))
            self.assertEqual(status, 0, after)
            self.assertEqual(project_authority_graph(self.root).to_dict(), graph_before)
        self.assertEqual(after["result"]["components"], before["result"]["components"])

    def test_real_legacy_only_authoring_still_requires_migration(self):
        # An implementation-named *catalog group* is not a retained boundary.
        path = self.root / "components" / "implementation" / "team" / "component.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        status, envelope = invoke("project", "validate", str(self.root))
        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"], "project.component_migration_required"
        )

    def test_shared_discovery_keeps_nested_components_and_implementation_groups(self):
        root = self.root / "components"
        expected = []
        for relative in (
            "team/nested/component.md",
            "implementation/team/component.md",
            "team/nested/subcomponent/component.md",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Authored Component\n", encoding="utf-8")
            expected.append(path)
        hidden = root / "team/nested/implementation/third-party/component.md"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("Retained source\n", encoding="utf-8")
        self.assertEqual(
            component_catalog_manifest_paths((root,), boundary=self.root),
            tuple(sorted(expected)),
        )

    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX Make fixture")
    def test_public_adopt_preserves_untracked_dependency_manifest_and_validates(self):
        legacy = self.root.parent / "legacy"
        legacy.mkdir()
        (legacy / "app.py").write_text("def value():\n    return 7\n", encoding="utf-8")
        (legacy / "test_app.py").write_text(
            "import unittest\nfrom app import value\n"
            "class ValueTests(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(value(), 7)\n",
            encoding="utf-8",
        )
        python = shlex.quote(sys.executable)
        (legacy / "Makefile").write_text(
            f"build:\n\t{python} -m py_compile app.py\n"
            f"test:\n\t{python} -m unittest discover -p 'test_*.py'\n"
            f"package:\n\t{python} -c 'import zipfile; "
            'z=zipfile.ZipFile("package.zip", "w"); z.write("app.py"); z.close()'
            "'\n",
            encoding="utf-8",
        )
        (legacy / ".gitignore").write_text(
            "node_modules/\n__pycache__/\npackage.zip\n", encoding="utf-8"
        )
        for arguments in (
            ("init", "--quiet"),
            ("add", "."),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--quiet",
                "-m",
                "baseline",
            ),
        ):
            subprocess.run(
                ("git", "-C", str(legacy), *arguments),
                check=True,
                capture_output=True,
                timeout=30,
            )
        dependency = legacy / "node_modules" / "extend" / "component.json"
        dependency.parent.mkdir(parents=True)
        dependency.write_bytes(b'{"name":"third-party-component-io"}\n')
        args = (
            "onboard",
            "adopt",
            str(legacy),
            "--source-intelligence-provider",
            "none",
        )
        status, plan = invoke(*args)
        self.assertEqual(status, 0, plan)
        status, adopted = invoke(*args, "--apply", "--acknowledge", "--run-baseline")
        self.assertEqual(status, 0, adopted)
        retained = legacy / "components" / "legacy-project-wrapper" / "implementation"
        self.assertEqual(
            (retained / "node_modules/extend/component.json").read_bytes(),
            b'{"name":"third-party-component-io"}\n',
        )
        status, validated = invoke("project", "validate", str(legacy))
        self.assertEqual(status, 0, validated)


if __name__ == "__main__":
    unittest.main()

"""Repository authored/generated boundary tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_repository_layout import LayoutError, validate_repository_layout


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
    )


class RepositoryLayoutTests(unittest.TestCase):
    def test_accepts_conventional_root_metadata_and_src_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "--quiet")
            (root / ".gitignore").write_text("_build/\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            package = root / "src" / "example"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")

            validate_repository_layout(root)

    def test_rejects_a_root_python_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "--quiet")
            (root / "helper.py").write_text("value = 1\n", encoding="utf-8")

            with self.assertRaisesRegex(LayoutError, "root importable Python"):
                validate_repository_layout(root)

    def test_rejects_root_bytecode_even_when_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "--quiet")
            (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "helper.pyc").write_bytes(b"derived")

            with self.assertRaisesRegex(LayoutError, "root __pycache__"):
                validate_repository_layout(root)

    def test_rejects_node_modules_even_after_a_deletion_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _git(root, "init", "--quiet")
            _git(root, "config", "user.name", "Layout Test")
            _git(root, "config", "user.email", "layout@example.invalid")
            dependency = root / "tools" / "example" / "node_modules" / "dep.js"
            dependency.parent.mkdir(parents=True)
            dependency.write_text("export {};\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "--quiet", "-m", "bad dependency tree")
            dependency.unlink()
            _git(root, "add", "-u")
            _git(root, "commit", "--quiet", "-m", "delete dependency tree")

            with self.assertRaisesRegex(LayoutError, "reachable Git history"):
                validate_repository_layout(root)


if __name__ == "__main__":
    unittest.main()

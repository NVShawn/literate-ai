"""Git worktree and submodule boundaries for legacy conversion."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.project_initialization import (
    plan_convert,
)
from tests.support.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(root: Path, branch: str = "main") -> None:
    root.mkdir()
    _git(root, "init", "--quiet", "-b", branch)
    _git(root, "config", "user.email", "test@example.test")
    _git(root, "config", "user.name", "Test")
    (root / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    _git(root, "add", "Makefile")
    _git(root, "commit", "--quiet", "-m", "initial")


class ConversionGitBoundaryTests(unittest.TestCase):
    def test_current_upstream_precedes_remote_head_in_absorbed_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            _repository(source, "master")
            _git(source, "branch", "dev/next/111")
            remote = root / "remote.git"
            subprocess.run(
                ("git", "clone", "--bare", str(source), str(remote)),
                check=True,
                capture_output=True,
                text=True,
            )
            superproject = root / "superproject"
            _repository(superproject)
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(superproject),
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(remote),
                    "absorbed",
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            _git(superproject, "commit", "--quiet", "-am", "add submodule")
            absorbed = superproject / "absorbed"
            _git(
                absorbed,
                "checkout",
                "--quiet",
                "-b",
                "dev/next/111",
                "--track",
                "origin/dev/next/111",
            )

            plan = plan_convert(absorbed)

            self.assertTrue((absorbed / ".git").is_file())
            self.assertEqual(
                plan["repository_default_branch"],
                {"branch": "dev/next/111", "source": "current-upstream"},
            )

    def test_initialized_nested_submodule_is_relocated_with_legacy_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child"
            _repository(child)
            parent = root / "parent"
            _repository(parent)
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(parent),
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(child),
                    "source/extensions/nested",
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            _git(parent, "commit", "--quiet", "-am", "add submodule")
            plan = plan_convert(parent, default_branch="main")

            self.assertEqual(plan["readiness"], "ready")
            self.assertEqual(
                plan["submodules"],
                [{"path": "source/extensions/nested", "state": "initialized"}],
            )
            self.assertIn(
                "repository.git-submodule",
                {item["detector_id"] for item in plan["findings"]},
            )
            self.assertEqual(plan["gitlink_blockers"], [])
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None
            ).initialize(
                parent,
                convert=True,
                default_branch="main",
            )
            status = _git(parent, "status", "--porcelain=v1").stdout
            self.assertIn("components/legacy-project-wrapper/implementation/", status)
            relocated = next(
                (parent / "components/legacy-project-wrapper/implementation").glob(
                    "source/extensions/nested/.git"
                )
            )
            self.assertTrue(relocated.is_file())
            self.assertEqual(
                Path(
                    _git(
                        relocated.parent, "rev-parse", "--show-toplevel"
                    ).stdout.strip()
                ).resolve(),
                relocated.parent.resolve(),
            )


if __name__ == "__main__":
    unittest.main()

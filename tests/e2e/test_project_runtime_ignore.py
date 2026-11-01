"""Real lifecycle lock files stay outside Git authority after init and update."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.lifecycle_lock import (
    project_lifecycle_lock,
    project_lifecycle_lock_path,
)
from literate_ai.adapters.project_update_apply import apply_project_update
from literate_ai.adapters.project_updates import FilesystemProjectUpdateAdapter
from literate_ai.adapters.update_merge import UpdateBases
from tests.support.fixtures_test_project_update_adapter import _origin
from tests.support.root_parent_adapter import RootParentProjectInitializationAdapter


class ProjectRuntimeIgnoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.origin = _origin("a", "1.4.0")
        RootParentProjectInitializationAdapter(
            standard_binding_provider=lambda: None,
            initialization_origin_provider=lambda: self.origin,
        ).initialize(self.root, empty=True, source_intelligence_provider="none")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)

    def assert_runtime_files_ignored(self):
        lock = project_lifecycle_lock_path(self.root)
        owner = lock.with_name(lock.name + ".owner.json")
        with project_lifecycle_lock(self.root, operation="test-ignore"):
            self.assertTrue(lock.is_file())
            self.assertTrue(owner.is_file())
            for path in (lock, owner):
                result = subprocess.run(
                    ["git", "check-ignore", "--quiet", str(path)],
                    cwd=self.root,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, path.name)
        status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                ".litai-locks",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(status.stdout, "")
        unrelated = self.root / "ordinary-input.txt"
        unrelated.write_text("authored input", encoding="utf-8")
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(unrelated)],
            cwd=self.root,
            check=False,
        )
        self.assertEqual(result.returncode, 1)

    def test_initialized_project_ignores_actual_lifecycle_lock_and_owner(self):
        self.assert_runtime_files_ignored()

    def test_legacy_update_adds_rule_and_preserves_local_ignore_overlay(self):
        target = self.root / ".gitignore"
        upstream = target.read_bytes()
        legacy = upstream.replace(b"/.litai-locks/\n", b"")
        self.assertNotEqual(legacy, upstream)
        overlay = b"\n# Local policy\nprivate-local-output/\n"
        target.write_bytes(legacy + overlay)
        bases = UpdateBases(self.root)
        bases.advance("framework", ".gitignore", legacy)
        bases.write()
        with (
            patch(
                "literate_ai.adapters.project_updates._upstream_template",
                return_value={".gitignore": upstream},
            ),
            patch(
                "literate_ai.adapters.project_update_apply._upstream_template",
                return_value={".gitignore": upstream},
            ),
        ):
            adapter = FilesystemProjectUpdateAdapter(
                origin_provider=lambda: self.origin
            )
            plan = adapter.plan(self.root)
            applied = apply_project_update(plan, self.root)
        self.assertIn(".gitignore", applied.merged)
        self.assertEqual(target.read_bytes(), upstream + overlay)
        self.assert_runtime_files_ignored()

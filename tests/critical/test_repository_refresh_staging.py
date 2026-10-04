"""Owned staging preserves live files and refuses collisions or altered custody."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.adapters import repository_refresh_staging as staging
from literate_ai.adapters.repository_file_custody import (
    prepare_worktree_changes,
    require_worktree_changes_unchanged,
)
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_files import PreparedRefreshFiles
from literate_ai.adapters.repository_refresh_index import tree_index_bytes
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support.fixtures_test_repository_orchestration import snapshot
from tests.support.fixtures_test_repository_tree import tree


class RefreshStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.gitdir = self.root / "git"
        self.gitdir.mkdir()
        child = self.root / "child"
        child.mkdir()
        (child / "source").write_bytes(b"before\r\n")
        before = tree({"source": ("100644", b"before\n")})
        after = tree(
            {"source": ("100755", b"after\n"), "link": ("120000", b"../../outside")}
        )
        node = child.stat()
        plan = prepare_worktree_changes(
            child, (node.st_dev, node.st_ino, node.st_mode), before, after
        )
        observation = SimpleNamespace(
            root=child,
            index=SimpleNamespace(content=tree_index_bytes(before)),
            head=SimpleNamespace(content=b"1" * 40 + b"\n"),
        )
        node = self.gitdir.stat()
        root_git = SimpleNamespace(
            git_directory=self.gitdir,
            git_node=(node.st_dev, node.st_ino, node.st_mode),
            commit="3" * 40,
            index=SimpleNamespace(
                content=tree_index_bytes(tree({"app": ("160000", b"")}))
            ),
            head=SimpleNamespace(content=b"ref: refs/heads/main\n"),
        )
        files = Mock(spec=PreparedRefreshFiles)
        files.plans = (("app", plan),)
        files.target_modes = (("app", "source-transition"),)
        files.identity = "sha256:" + "a" * 64
        files.require_current.side_effect = lambda: require_worktree_changes_unchanged(
            plan
        )
        files._owner = SimpleNamespace(
            _prepared=SimpleNamespace(
                refresh=SimpleNamespace(
                    root_git=root_git,
                    children=(observation,),
                    manifest=SimpleNamespace(content=b"{}\n"),
                    repository_lock=SimpleNamespace(content=None),
                    authority=SimpleNamespace(
                        request=RepositoryRefreshRequest(
                            (RepositoryRefreshTarget("app", after.commit),)
                        )
                    ),
                )
            )
        )
        self.files = files
        self.path = self.gitdir / staging.STAGING_DIRECTORY

    def test_abrupt_process_exit_retains_stage_and_blocks_new_owner(self):
        crash_root = self.root / "crash"
        crash_root.mkdir()
        script = """
import os, sys
from types import SimpleNamespace
from unittest.mock import patch
from tests.critical import test_repository_refresh_staging as fixture_module
from literate_ai.adapters.repository_refresh_staging import stage_refresh_files
fixture = fixture_module.RefreshStagingTests()
temporary = SimpleNamespace(name=sys.argv[1], cleanup=lambda: None)
with patch.object(
    fixture_module.tempfile, 'TemporaryDirectory', return_value=temporary
):
    fixture.setUp()
with stage_refresh_files(fixture.files):
    os._exit(0)
"""
        result = subprocess.run(
            (sys.executable, "-c", script, str(crash_root)),
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        retained = crash_root / "git" / staging.STAGING_DIRECTORY
        self.assertTrue((retained / "owner").is_file())
        self.assertTrue((retained / "manifest.json").is_file())
        root_git = self.files._owner._prepared.refresh.root_git
        root_git.git_directory = retained.parent
        node = retained.parent.stat()
        root_git.git_node = (node.st_dev, node.st_ino, node.st_mode)
        before = snapshot(crash_root)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with staging.stage_refresh_files(self.files):
                self.fail("crashed staging must block a new owner")
        self.assertEqual(caught.exception.code, "orchestration.refresh_stage_busy")
        self.assertEqual(snapshot(crash_root), before)

    def test_mutation_then_exception_retains_physical_before_state(self):
        with self.assertRaisesRegex(
            RuntimeError, "fixture partial logical write"
        ) as caught:
            with staging.stage_refresh_files(self.files) as stage:
                stage.arm_recovery()
                (self.root / "child/source").write_bytes(b"partial application\n")
                raise RuntimeError("fixture partial logical write")
        self.assertIsInstance(caught.exception.__cause__, OrchestrationInventoryError)
        self.assertEqual(
            caught.exception.__cause__.code,
            "orchestration.refresh_stage_recovery_required",
        )
        self.assertEqual(
            (self.root / "child/source").read_bytes(), b"partial application\n"
        )
        contents = [p.read_bytes() for p in stage.path.iterdir()]
        self.assertIn(b"before\r\n", contents)
        self.assertTrue((stage.path / "manifest.json").is_file())
        self.assertTrue((stage.path / "application.json").is_file())

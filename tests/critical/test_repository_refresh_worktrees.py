"""Registered-worktree effects must stay within reviewed checkout ownership."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters import repository_refresh_worktrees as worktrees
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh import _observe_git
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RefreshRegisteredWorktreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.app = self.base / "app"
        self.app.mkdir()
        git(self.app, "init", "-q", "-b", "main")
        git(self.app, "config", "user.name", "Test")
        git(self.app, "config", "user.email", "test@example.test")
        (self.app / "source").write_bytes(b"source\n")
        git(self.app, "add", "source")
        git(self.app, "commit", "-q", "-m", "initial")
        self.other = self.base / "other"

    def prepare(self):
        observed = _observe_git(self.app, clean=False)
        children = (observed,)
        targets = [RepositoryRefreshTarget("app", "4" * 40)]
        return SimpleNamespace(
            root_git=SimpleNamespace(
                root=self.base,
                common_directory=self.base / ".rootgit",
                commit=observed.commit,
                symbolic_reference=None,
            ),
            children=children,
            repository=SimpleNamespace(root=self.base),
            authority=SimpleNamespace(request=RepositoryRefreshRequest(tuple(targets))),
            worktrees=worktrees.observe_refresh_worktrees(children),
        )

    def require(self):
        before = snapshot(self.base)
        try:
            value = self.prepare()
            worktrees.require_refresh_worktree_ownership(value)
            return value
        finally:
            self.assertEqual(snapshot(self.base), before)

    def test_same_branch_and_symbolic_alias_outside_selection_refuse(self):
        git(self.app, "worktree", "add", "--force", str(self.other), "main")
        for alias in (False, True):
            if alias:
                git(self.other, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
                git(self.other, "symbolic-ref", "HEAD", "refs/heads/alias")
            with (
                self.subTest(alias=alias),
                self.assertRaises(OrchestrationInventoryError) as caught,
            ):
                self.require()
            self.assertEqual(
                caught.exception.code, "orchestration.refresh_external_worktree"
            )

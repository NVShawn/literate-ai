"""Registered-worktree effects must stay within reviewed checkout ownership."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters import repository_refresh_worktrees as worktrees
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh import _observe_git
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RegisteredWorktreeProtocolTests(unittest.TestCase):
    def test_nul_protocol_preserves_spaces_and_newlines_without_path_quoting(self):
        path = Path.cwd() / "space and\nnewline"
        raw = (
            b"worktree "
            + str(path).encode()
            + b"\0HEAD "
            + b"a" * 40
            + b"\0branch refs/heads/main\0locked reason\nmore\0\0"
        )
        parsed = worktrees.parse_refresh_worktrees(raw)
        self.assertEqual(parsed[0].path, path)
        self.assertEqual(parsed[0].commit, "a" * 40)
        self.assertTrue(parsed[0].locked)

    def test_bare_unborn_detached_and_prunable_rows(self):
        root = str(Path.cwd()).encode()
        for suffix, bare, commit in (
            (b"bare", True, None),
            (b"HEAD " + b"0" * 40 + b"\0branch refs/heads/unborn", False, None),
            (b"HEAD " + b"a" * 64 + b"\0detached\0prunable missing", False, "a" * 64),
        ):
            value = worktrees.parse_refresh_worktrees(
                b"worktree " + root + b"\0" + suffix + b"\0\0"
            )[0]
            self.assertEqual(value.bare, bare)
            self.assertEqual(value.commit, commit)

    def test_unknown_duplicate_ambiguous_or_truncated_fields_refuse(self):
        prefix = b"worktree " + str(Path.cwd()).encode() + b"\0"
        valid = prefix + b"HEAD " + b"a" * 40 + b"\0detached\0\0"
        for raw in (
            b"",
            valid[:-1],
            valid + valid,
            prefix + b"bare true\0\0",
            prefix + b"bare\0unknown\0\0",
            prefix + b"bare\0HEAD " + b"a" * 40 + b"\0\0",
            prefix + b"HEAD " + b"a" * 40 + b"\0detached\0branch refs/heads/main\0\0",
            prefix + b"HEAD " + b"0" * 40 + b"\0detached\0\0",
            prefix + b"HEAD " + b"a" * 40 + b"\0HEAD " + b"a" * 40 + b"\0detached\0\0",
            b"worktree relative\0bare\0\0",
        ):
            with (
                self.subTest(raw=raw),
                self.assertRaises((OrchestrationInventoryError, ValueError)),
            ):
                worktrees.parse_refresh_worktrees(raw)
        with (
            patch.object(worktrees, "_MAX_LIST_BYTES", 1),
            self.assertRaises(OrchestrationInventoryError),
        ):
            worktrees.parse_refresh_worktrees(valid)
        with (
            patch.object(worktrees, "_MAX_WORKTREES", 0),
            self.assertRaises(OrchestrationInventoryError),
        ):
            worktrees.parse_refresh_worktrees(valid)


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

    def add(self, *arguments):
        git(self.app, "worktree", "add", *arguments, str(self.other), "HEAD")

    def prepare(self, *, selected_other=False, noop=False):
        observed = _observe_git(self.app, clean=False)
        children = (observed,)
        targets = [
            RepositoryRefreshTarget("app", observed.commit if noop else "4" * 40)
        ]
        if selected_other:
            children += (_observe_git(self.other, clean=False),)
            targets.append(RepositoryRefreshTarget("other", "4" * 40))
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

    def require(self, **options):
        before = snapshot(self.base)
        try:
            value = self.prepare(**options)
            worktrees.require_refresh_worktree_ownership(value)
            return value
        finally:
            self.assertEqual(snapshot(self.base), before)

    def test_unrelated_dirty_worktree_is_not_entered_or_changed(self):
        self.add("-b", "unrelated")
        (self.other / "source").write_bytes(b"unrelated uncommitted work\n")
        observed = self.require()
        self.assertEqual(len(observed.worktrees), 1)
        self.assertEqual(len(observed.worktrees[0].worktrees), 2)

    def absorb(self):
        git(self.base, "init", "-q", "-b", "main")
        git(self.base, "config", "-f", ".gitmodules", "submodule.app.path", "app")
        git(self.base, "config", "-f", ".gitmodules", "submodule.app.url", "./source")
        git(self.base, "add", ".gitmodules", "app")
        git(self.base, "submodule", "absorbgitdirs", "app")
        self.assertTrue((self.app / ".git").is_file())

    def test_absorbed_primary_attached_and_detached_are_owned(self):
        self.absorb()
        prepared = self.require()
        observed = prepared.children[0]
        self.assertEqual(observed.git_directory, observed.common_directory)
        rows = worktrees._checkout_registrations(
            prepared.worktrees[0], prepared.children
        )
        self.assertEqual(rows[0].path, self.app)
        git(self.app, "switch", "--detach")
        self.require()

    def test_absorbed_primary_still_refuses_external_shared_branch(self):
        self.absorb()
        self.test_same_branch_and_symbolic_alias_outside_selection_refuse()

    def test_absorbed_primary_allows_unrelated_dirty_peer(self):
        self.absorb()
        self.test_unrelated_dirty_worktree_is_not_entered_or_changed()

    def test_absorbed_registration_does_not_admit_unknown_or_ambiguous_roots(self):
        self.absorb()
        prepared = self.prepare()
        registry = prepared.worktrees[0]
        observed = prepared.children[0]
        row = registry.worktrees[0]
        # Exercise the administrative spelling even on Git versions that
        # already list the actual primary worktree path.
        registry = replace(
            registry, worktrees=(replace(row, path=observed.common_directory),)
        )
        for observations in (
            (observed, replace(observed, root=self.other)),
            (observed,),
        ):
            candidate = registry
            if len(observations) == 1:
                candidate = replace(
                    registry,
                    worktrees=(*registry.worktrees, replace(row, path=self.app)),
                )
            with self.assertRaises(OrchestrationInventoryError):
                worktrees._checkout_registrations(candidate, observations)
        unknown = replace(registry, worktrees=(replace(row, path=self.other),))
        prepared.worktrees = (unknown,)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            worktrees.require_refresh_worktree_ownership(prepared)
        self.assertEqual(
            caught.exception.code, "orchestration.refresh_worktrees_invalid"
        )

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

    def test_all_shared_checkouts_selected_share_one_registry(self):
        git(self.app, "worktree", "add", "--force", str(self.other), "main")
        prepared = self.require(selected_other=True)
        self.assertEqual(len(prepared.worktrees), 1)

    def test_detached_and_unborn_foreign_worktrees_are_unaffected(self):
        self.add("--detach")
        self.require()
        git(self.other, "symbolic-ref", "HEAD", "refs/heads/unborn")
        prepared = self.require()
        row = next(
            item for item in prepared.worktrees[0].worktrees if item.path == self.other
        )
        self.assertIsNone(row.commit)
        self.assertEqual(row.reference, "refs/heads/unborn")

    def test_active_foreign_operation_refuses_even_when_head_is_detached(self):
        self.add("--detach")
        metadata = _observe_git(self.other, clean=False).git_directory
        (metadata / "rebase-merge").mkdir()
        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.require()
        self.assertEqual(caught.exception.code, "orchestration.refresh_worktree_busy")
        self.require(noop=True)

    def test_locked_missing_worktree_is_not_mistaken_for_available(self):
        self.add("-b", "unrelated")
        git(self.app, "worktree", "lock", str(self.other))
        self.other.rename(self.base / "retained")
        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.require()
        self.assertEqual(caught.exception.code, "orchestration.refresh_worktree_busy")

    def test_registration_addition_and_same_byte_head_replacement_change_custody(self):
        before = self.prepare().worktrees
        self.add("-b", "unrelated")
        after = self.prepare().worktrees
        self.assertNotEqual(before, after)
        metadata = _observe_git(self.other, clean=False).git_directory
        original = metadata / "HEAD"
        replacement = metadata / "replacement"
        replacement.write_bytes(original.read_bytes())
        replacement.replace(original)
        replaced = self.prepare().worktrees
        self.assertNotEqual(after, replaced)
        git(self.app, "worktree", "remove", str(self.other))
        self.assertNotEqual(replaced, self.prepare().worktrees)

    def test_corrupt_foreign_head_refuses_without_repair(self):
        self.add("--detach")
        metadata = _observe_git(self.other, clean=False).git_directory
        (metadata / "HEAD").write_bytes(b"ref: refs/heads/bad.lock\n")
        with self.assertRaises(OrchestrationInventoryError):
            self.require()

    def test_noop_and_private_ref_updates_do_not_affect_foreign_checkout(self):
        git(self.app, "worktree", "add", "--force", str(self.other), "main")
        self.require(noop=True)
        commit = git(self.app, "rev-parse", "HEAD").decode().strip()
        git(self.app, "update-ref", "refs/worktree/private", commit)
        git(self.app, "symbolic-ref", "HEAD", "refs/worktree/private")
        self.require()

    def test_bare_main_registration_is_not_an_independent_checkout(self):
        bare = self.base / "bare.git"
        git(self.base, "clone", "--bare", str(self.app), str(bare))
        git(bare, "worktree", "add", str(self.other), "main")
        observed = _observe_git(self.other, clean=False)
        registry = worktrees.observe_refresh_worktrees((observed,))
        self.assertTrue(any(item.bare for item in registry[0].worktrees))
        prepared = self.prepare()
        prepared.children = (observed,)
        prepared.authority = SimpleNamespace(
            request=RepositoryRefreshRequest(
                (RepositoryRefreshTarget("other", "4" * 40),)
            )
        )
        prepared.worktrees = registry
        before = snapshot(self.base)
        worktrees.require_refresh_worktree_ownership(prepared)
        self.assertEqual(snapshot(self.base), before)

    def test_malformed_registration_and_aggregate_bounds_refuse_without_repair(self):
        self.add("--detach")
        observation = _observe_git(self.app, clean=False)
        for field in ("_MAX_TOTAL_WORKTREES", "_MAX_TOTAL_BYTES"):
            with (
                patch.object(worktrees, field, 1),
                self.assertRaises(OrchestrationInventoryError),
            ):
                worktrees.observe_refresh_worktrees((observation,))
        directory = observation.common_directory / "worktrees"
        (directory / "foreign-entry").write_bytes(b"not a registration\n")
        before = snapshot(self.base)
        with self.assertRaises(UnsafeFilesystemPathError):
            worktrees.observe_refresh_worktrees((observation,))
        self.assertEqual(snapshot(self.base), before)

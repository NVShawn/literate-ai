"""Acknowledged real-Git reservations without source/pin application."""

from __future__ import annotations

import subprocess
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters import repository_refresh as refresh
from literate_ai.adapters import repository_refresh_reservations as reservations
from literate_ai.adapters.repository_locks import RepositoryLockStore
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_metadata import (
    refresh_metadata_transitions,
)
from tests.support import fixtures_test_repository_refresh_inputs as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RefreshReservationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RepositoryRefreshInputTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base

    def prepare(self):
        return self.fixture.prepare()

    def acquire(self, prepared, **kwargs):
        return reservations.reserve_refresh_git_inputs(
            prepared,
            expected_custody_identity=kwargs.pop(
                "expected_custody_identity",
                reservations.refresh_custody_identity(prepared),
            ),
            acknowledge=kwargs.pop("acknowledge", True),
            **kwargs,
        )

    def test_live_owner_revalidates_while_default_readers_refuse_and_inputs_survive(
        self,
    ):
        prepared = self.prepare()
        before = snapshot(self.base)
        with self.acquire(prepared) as owner:
            refresh.require_repository_refresh_inputs_unchanged(
                prepared, reservations=owner
            )
            with self.assertRaises(OrchestrationInventoryError):
                refresh.require_repository_refresh_inputs_unchanged(prepared)
            owner.verify_all()
        self.assertEqual(snapshot(self.base), before)
        refresh.require_repository_refresh_inputs_unchanged(prepared)
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(
                prepared, reservations=owner
            )

    def test_absent_false_integer_ack_and_stale_identity_do_not_create_markers(self):
        prepared = self.prepare()
        before = snapshot(self.base)
        for options in (
            {"acknowledge": False},
            {"acknowledge": 1},
            {"acknowledge": None},
            {"expected_custody_identity": "sha256:" + "0" * 64},
        ):
            with (
                self.subTest(options=options),
                self.assertRaises(OrchestrationInventoryError),
            ):
                with self.acquire(prepared, **options):
                    self.fail("unreviewed reservation")
        self.assertEqual(snapshot(self.base), before)

    def test_actual_git_index_head_and_branch_writers_are_blocked(self):
        child = self.root / "app"
        git(child, "checkout", "-q", "-b", "attached")
        prepared = self.prepare()
        before = snapshot(self.base)
        with self.acquire(prepared):
            for root in (self.root, child):
                for arguments in (
                    ("add", "source.txt"),
                    ("checkout", "--detach", "HEAD"),
                    ("update-ref", "HEAD", prepared.root_git.commit),
                ):
                    with (
                        self.subTest(root=root, arguments=arguments),
                        self.assertRaises(subprocess.CalledProcessError),
                    ):
                        git(root, *arguments)
        self.assertEqual(snapshot(self.base), before)

    def test_repository_lock_writer_is_reserved(self):
        prepared = self.prepare()
        before = snapshot(self.base)
        with self.acquire(prepared):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                RepositoryLockStore(self.root).update(prepared.repository)
            self.assertEqual(caught.exception.code, "orchestration.lock_busy")
        self.assertEqual(snapshot(self.base), before)

    def test_missing_log_parents_normalize_only_for_live_owned_reservations(self):
        child = self.root / "app"
        git(child, "checkout", "-q", "-b", "nested/topic")
        initial = self.prepare()
        logs = initial.children[0].git_directory / "logs"
        logs.rename(logs.with_name("retained-logs"))
        prepared = self.prepare()
        self.assertTrue(prepared.reflogs)
        self.assertTrue(all(item.before.content is None for item in prepared.reflogs))
        before = snapshot(self.base)
        with self.acquire(prepared) as owner:
            refresh.require_repository_refresh_inputs_unchanged(
                prepared, reservations=owner
            )
            for item in prepared.reflogs:
                self.assertTrue(
                    owner.owns(
                        item.before.path.with_name(item.before.path.name + ".lock")
                    )
                )
                self.assertTrue(owner.owns_directory(item.before.path.parent))
                self.assertFalse(item.before.path.exists())
        self.assertFalse(logs.exists())
        self.assertEqual(snapshot(self.base), before)

    def test_disabled_missing_logs_do_not_acquire_writers_or_create_parents(self):
        child = self.root / "app"
        git(child, "config", "core.logAllRefUpdates", "false")
        initial = self.prepare()
        logs = initial.children[0].git_directory / "logs"
        logs.rename(logs.with_name("retained-logs"))
        prepared = self.prepare()
        before = snapshot(self.base)
        with self.acquire(prepared) as owner:
            self.assertFalse(logs.exists())
            refresh.require_repository_refresh_inputs_unchanged(
                prepared, reservations=owner
            )
            self.assertTrue(all(not item.append for item in prepared.reflogs))
        self.assertEqual(snapshot(self.base), before)

    def test_packed_symbolic_ref_gets_a_reserved_loose_writer_path(self):
        git(self.root, "checkout", "-b", "nested/topic")
        git(self.root, "pack-refs", "--all", "--prune")
        prepared = self.prepare()
        self.assertEqual(
            prepared.root_git.symbolic_reference, "refs/heads/nested/topic"
        )
        before = snapshot(self.base)
        with self.acquire(prepared):
            self.assertTrue(
                (prepared.root_git.common_directory / "packed-refs.lock").is_file()
            )
            self.assertTrue(
                (
                    prepared.root_git.common_directory / "refs/heads/nested/topic.lock"
                ).is_file()
            )
            with self.assertRaises(subprocess.CalledProcessError):
                git(self.root, "update-ref", "HEAD", prepared.root_git.commit)
        self.assertEqual(snapshot(self.base), before)

    def test_all_symbolic_hops_and_packed_ref_writer_are_reserved(self):
        git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/alias")
        prepared = self.prepare()
        before = snapshot(self.base)
        with self.acquire(prepared) as owner:
            for name in ("HEAD", "refs/heads/alias", "refs/heads/main", "packed-refs"):
                self.assertTrue(
                    owner.owns(prepared.root_git.git_directory / (name + ".lock"))
                )
            for arguments in (
                ("symbolic-ref", "refs/heads/alias", "refs/heads/main"),
                ("pack-refs", "--all", "--prune"),
            ):
                with (
                    self.subTest(arguments=arguments),
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    git(self.root, *arguments)
        self.assertEqual(snapshot(self.base), before)

    def test_changed_inputs_before_acquisition_refuse_without_cleanup_of_foreign_data(
        self,
    ):
        prepared = self.prepare()
        foreign = self.root / "app/foreign"
        foreign.write_bytes(b"concurrent")
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire(prepared):
                self.fail("stale inputs")
        self.assertEqual(snapshot(self.base), before)

    def test_failure_after_acquisition_releases_only_owned_markers(self):
        prepared = self.prepare()
        before = snapshot(self.base)
        with patch.object(
            reservations,
            "require_repository_refresh_inputs_unchanged",
            side_effect=(
                None,
                OrchestrationInventoryError("fixture_changed", "fixture"),
            ),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire(prepared):
                    self.fail("late drift")
        self.assertEqual(snapshot(self.base), before)

    def test_local_custody_identity_binds_exact_nodes_and_request(self):
        prepared = self.prepare()
        first = reservations.refresh_custody_identity(prepared)
        self.assertEqual(first, reservations.refresh_custody_identity(self.prepare()))
        changed = replace(prepared, metadata_node=(1, 2, 3))
        self.assertNotEqual(first, reservations.refresh_custody_identity(changed))
        changed = replace(
            prepared, manifest=replace(prepared.manifest, content=b"changed")
        )
        self.assertNotEqual(first, reservations.refresh_custody_identity(changed))

    def test_local_custody_identity_binds_oversized_os_node_identifiers(self):
        prepared = self.prepare()
        child = prepared.children[0]
        oversized = replace(
            prepared,
            children=(
                replace(child, boundary=(2**63, 2, 3)),
                *prepared.children[1:],
            ),
        )
        changed = replace(
            prepared,
            children=(
                replace(child, boundary=(2**63 + 1, 2, 3)),
                *prepared.children[1:],
            ),
        )

        identity = reservations.refresh_custody_identity(oversized)

        self.assertTrue(identity.startswith("sha256:"))
        self.assertNotEqual(identity, reservations.refresh_custody_identity(changed))

    def test_registration_added_after_review_refuses_without_removing_it(self):
        prepared = self.prepare()
        foreign = self.base / "foreign-app"
        git(self.root / "app", "worktree", "add", "--detach", str(foreign), "HEAD")
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire(prepared):
                self.fail("registration drift must invalidate earlier approval")
        self.assertEqual(snapshot(self.base), before)
        self.assertTrue(foreign.is_dir())

    def test_shared_external_branch_refuses_metadata_preparation_without_writes(self):
        child = self.root / "app"
        git(child, "checkout", "-q", "-b", "shared")
        git(
            child,
            "worktree",
            "add",
            "--force",
            str(self.base / "foreign-app"),
            "shared",
        )
        prepared = self.prepare()
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            refresh_metadata_transitions(prepared)
        self.assertEqual(
            caught.exception.code, "orchestration.refresh_external_worktree"
        )
        self.assertEqual(snapshot(self.base), before)

    def test_shared_common_directory_deduplicates_the_same_branch_writer(self):
        original = self.base / "shared-app"
        (self.root / "app").rename(original)
        (self.root / "lib").rename(self.base / "original-lib")
        git(original, "checkout", "-b", "shared")
        for name in ("app", "lib"):
            git(original, "worktree", "add", "--force", str(self.root / name), "shared")
        prepared = self.prepare()
        left, right = prepared.children
        self.assertNotEqual(left.git_directory, right.git_directory)
        self.assertEqual(left.common_directory, right.common_directory)
        self.assertEqual(left.symbolic_reference, right.symbolic_reference)
        before = snapshot(self.base)
        with self.acquire(prepared) as owner:
            self.assertTrue(
                owner.owns(left.common_directory / "refs/heads/shared.lock")
            )
            refresh.require_repository_refresh_inputs_unchanged(
                prepared, reservations=owner
            )
            with self.assertRaises(subprocess.CalledProcessError):
                git(original, "update-ref", "HEAD", left.commit)
        self.assertEqual(snapshot(self.base), before)

    def test_same_commit_symbolic_chain_change_invalidates_custody(self):
        git(self.root, "branch", "same-commit")
        git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
        git(self.root, "symbolic-ref", "HEAD", "refs/heads/alias")
        prepared = self.prepare()
        self.assertEqual(prepared.root_git.symbolic_reference, "refs/heads/main")
        git(self.root, "symbolic-ref", "refs/heads/alias", "refs/heads/same-commit")
        self.assertEqual(
            (self.root / ".git/HEAD").read_bytes(), prepared.root_git.head.content
        )
        self.assertEqual(
            git(self.root, "rev-parse", "HEAD").decode().strip(),
            prepared.root_git.commit,
        )
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire(prepared):
                self.fail("resolved symbolic reference changed")
        self.assertEqual(snapshot(self.base), before)

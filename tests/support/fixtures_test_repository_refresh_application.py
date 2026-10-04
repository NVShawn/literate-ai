from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_repository_refresh_application``."""

import errno

import hashlib

import json

import os

import stat

import unittest

from pathlib import Path

from types import SimpleNamespace

from unittest.mock import patch

from literate_ai.adapters import repository_refresh_application as application

from literate_ai.adapters import repository_refresh_objects as objects_adapter

from literate_ai.adapters import repository_refresh_ownership as ownership

from literate_ai.adapters import repository_refresh_staging as staging

from literate_ai.adapters._write_reservations import WriteReservationSet

from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError

from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy

from literate_ai.projects import parse_project_configuration

from tests.support import fixtures_test_refresh_file_custody as fixtures

from tests.support.fixtures_test_repository_orchestration import git, snapshot

class RepositoryRefreshApplicationTests(unittest.TestCase):
    def setUp(self):
        harness = fixtures.RefreshFileCustodyTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.harness = harness
        self.source = harness.fixture
        self.child = self.source.child

    def publish(self):
        (self.child / "source.txt").write_bytes(b"prospective\n")
        (self.child / "tool").write_bytes(b"#!/bin/sh\nexit 0\n")
        if os.name != "nt":
            (self.child / "tool").chmod(0o755)
            os.symlink("source.txt", self.child / "link")
        git(
            self.child,
            "add",
            "source.txt",
            "tool",
            *(("link",) if os.name != "nt" else ()),
        )
        git(self.child, "update-index", "--chmod=+x", "tool")
        git(self.child, "commit", "-q", "-m", "prospective tree")
        target = git(self.child, "rev-parse", "HEAD").decode().strip()
        git(
            self.child,
            "push",
            self.source.remote.as_uri(),
            "HEAD:refs/heads/main",
        )
        git(self.child, "checkout", "-q", self.source.pin)
        return target

    def publish_nested_child(self):
        nested = self.child / "nested"
        git(
            self.source.base,
            "clone",
            "-q",
            "--no-hardlinks",
            str(self.source.root / "lib"),
            str(nested),
        )
        git(nested, "config", "user.name", "Test")
        git(nested, "config", "user.email", "test@example.test")
        previous = git(nested, "rev-parse", "HEAD").decode().strip()
        git(nested, "commit", "--allow-empty", "-q", "-m", "nested target")
        nested_target = git(nested, "rev-parse", "HEAD").decode().strip()
        (self.child / ".gitmodules").write_bytes(
            b'[submodule "nested"]\npath = nested\nurl = ../nested.git\n'
        )
        git(self.child, "add", ".gitmodules")
        git(
            self.child,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            nested_target,
            "nested",
        )
        git(self.child, "commit", "-q", "-m", "publish nested child")
        target = git(self.child, "rev-parse", "HEAD").decode().strip()
        git(
            self.child,
            "push",
            self.source.remote.as_uri(),
            "HEAD:refs/heads/main",
        )
        git(self.child, "config", "submodule.nested.ignore", "all")
        return target, nested, previous

    def prepared(self, target):
        return self.source.prepare(self.source.inputs(target))

    def stage(self, owned):
        files = owned.prepare_filesystem_changes()
        objects = files.prepare_objects()
        return objects.stage(metadata=True)

    def test_absorbed_attached_refresh_keeps_symbolic_head_and_detached_peer(self):
        target = self.publish()
        git(self.child, "switch", "-c", "attached-refresh", self.source.pin)
        git(self.source.root, "submodule", "absorbgitdirs", "app")
        peer = self.source.base / "detached-peer"
        git(self.child, "worktree", "add", "--detach", str(peer), self.source.pin)
        peer_source = (peer / "source.txt").read_bytes()
        prepared = self.prepared(target)
        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                result = application.apply_repository_refresh(stage)
                self.assertEqual(result.state, "committed")
        self.assertEqual(
            git(self.child, "symbolic-ref", "HEAD").strip(),
            b"refs/heads/attached-refresh",
        )
        self.assertEqual(git(self.child, "rev-parse", "HEAD").decode().strip(), target)
        self.assertEqual(
            git(self.source.root, "rev-parse", ":app").decode().strip(), target
        )
        self.assertEqual((self.child / "source.txt").read_bytes(), b"prospective\n")
        self.assertEqual(
            git(peer, "rev-parse", "HEAD").decode().strip(), self.source.pin
        )
        self.assertEqual((peer / "source.txt").read_bytes(), peer_source)
        self.assertEqual(git(peer, "status", "--porcelain"), b"")

    def test_windows_stage_directory_retries_delete_pending_owner(self):
        stage = self.source.root / ".git" / "delete-pending-stage"
        stage.mkdir()
        parent_node = staging._key(stage.parent)
        node = staging._key(stage)
        original_rmdir = Path.rmdir
        attempts = 0

        def transient_rmdir(path):
            nonlocal attempts
            if path == stage:
                attempts += 1
                if attempts == 1:
                    raise OSError(errno.ENOTEMPTY, "fixture delete-pending marker")
            return original_rmdir(path)

        with (
            patch.object(staging, "_is_windows", return_value=True),
            patch.object(staging.time, "sleep"),
            patch.object(Path, "rmdir", transient_rmdir),
        ):
            staging._remove_owned_empty_directory(
                stage, parent_node=parent_node, node=node
            )

        self.assertEqual(attempts, 2)
        self.assertFalse(stage.exists())

    def test_windows_closes_retained_descriptor_before_discard(self):
        path = self.source.root / "discard-custody.txt"
        path.write_bytes(b"discard custody\n")
        owned = application._remove_owned(path, application._observe(path))
        self.assertIsNotNone(owned.descriptor)

        with patch.object(application, "_is_windows", return_value=True):
            application._discard_owned(owned)

        self.assertIsNone(owned.descriptor)
        self.assertFalse(owned.path.exists())

    def test_windows_closes_retained_descriptor_before_restore(self):
        path = self.source.root / "restore-custody.txt"
        content = b"restore custody\n"
        path.write_bytes(content)
        owned = application._remove_owned(path, application._observe(path))
        self.assertIsNotNone(owned.descriptor)

        with patch.object(application, "_is_windows", return_value=True):
            application._restore_owned(owned, path)

        self.assertIsNone(owned.descriptor)
        self.assertEqual(path.read_bytes(), content)
        self.assertFalse(owned.path.exists())

    def test_terminal_deferral_failure_is_not_masked_by_directory_cleanup(self):
        target = self.publish()
        prepared = self.prepared(target)
        stage_path = None

        with self.harness.acquire(prepared) as owned:
            with (
                patch.object(
                    staging.StagedRefresh,
                    "defer_terminal_cleanup",
                    side_effect=OrchestrationInventoryError(
                        "fixture_terminal_deferral",
                        "fixture terminal deferral failure",
                    ),
                ),
                self.assertRaises(OrchestrationInventoryError) as caught,
            ):
                with self.stage(owned) as stage:
                    stage_path = stage.path
                    application.apply_repository_refresh(stage)

        self.assertEqual(
            caught.exception.code, "orchestration.fixture_terminal_deferral"
        )
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )

    def test_stage_body_failure_is_not_masked_by_directory_cleanup(self):
        stage = self.source.root / ".git" / staging.STAGING_DIRECTORY
        marker = stage / "retained"
        fixture_error = ValueError("fixture stage body failure")
        root_git = SimpleNamespace(
            git_directory=stage.parent,
            git_node=staging._key(stage.parent),
        )

        with self.assertRaises(ValueError) as caught:
            with staging._exclusive_directory(root_git):
                marker.write_bytes(b"retain cleanup evidence\n")
                raise fixture_error

        self.assertIs(caught.exception, fixture_error)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(marker.is_file())

    def test_armed_stage_preserves_body_error_before_terminal_proof(self):
        prepared = self.prepared(self.source.pin)
        fixture_error = ValueError("fixture armed stage body failure")
        stage_path = None

        with self.harness.acquire(prepared) as owned:
            with self.assertRaises(ValueError) as caught:
                with self.stage(owned) as stage:
                    stage_path = stage.path
                    stage.arm_recovery()
                    raise fixture_error

        self.assertIs(caught.exception, fixture_error)
        self.assertIsInstance(caught.exception.__cause__, OrchestrationInventoryError)
        self.assertEqual(
            caught.exception.__cause__.code,
            "orchestration.refresh_stage_recovery_required",
        )
        self.assertTrue(stage_path.is_dir())

    def test_complete_commit_is_manifest_last_and_cleans_terminal_journal(self):
        target = self.publish()
        prepared = self.prepared(target)
        unrelated = git(self.source.root, "ls-files", "--stage", "source.txt")
        writes = []
        terminal = []
        preterminal = []
        materialize = application._materialize
        record_terminal = staging.StagedRefresh.record_terminal

        def observed_write(path, state, **kwargs):
            materialize(path, state, **kwargs)
            writes.append(path)

        def observed_terminal(stage, state):
            record_terminal(stage, state)
            terminal.append(
                json.loads((stage.path / "application.json").read_bytes())["state"]
            )

        def observed_preterminal(live):
            self.assertEqual(live.state, "applying")
            self.assertEqual(
                live.stage._files.target_modes, (("app", "source-transition"),)
            )
            preterminal.append(live)

        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                authority = application.prepare_refresh_application(stage)
                self.assertTrue(authority.to_dict()["apply_supported"])
                self.assertFalse(
                    prepared.refresh.authority.to_dict()["apply_supported"]
                )
                stage_path = stage.path
                with (
                    patch.object(application, "_materialize", observed_write),
                    patch.object(
                        staging.StagedRefresh,
                        "record_terminal",
                        observed_terminal,
                    ),
                    patch.object(
                        application,
                        "_before_terminal_journal",
                        observed_preterminal,
                    ),
                ):
                    result = application.apply_repository_refresh(stage)
                self.assertEqual(result.state, "committed")
                self.assertTrue(result.to_dict()["writes"])
                self.assertEqual(terminal, ["committed"])
                self.assertEqual(preterminal, [stage._application])
                self.assertEqual(writes[-1], self.source.root / "literate.project.json")
        self.assertFalse(stage_path.exists())
        self.assertEqual(git(self.child, "rev-parse", "HEAD").decode().strip(), target)
        self.assertEqual((self.child / "source.txt").read_bytes(), b"prospective\n")
        if os.name != "nt":
            self.assertEqual(os.readlink(self.child / "link"), "source.txt")
            self.assertTrue(stat.S_IMODE((self.child / "tool").stat().st_mode) & 0o111)
        manifest = parse_project_configuration(
            (self.source.root / "literate.project.json").read_bytes()
        )
        pin = next(
            item
            for item in manifest.repository_orchestration.repositories
            if item.path == "app"
        )
        self.assertEqual(pin.commit, target)
        self.assertEqual(
            git(self.source.root, "ls-files", "--stage", "app").decode().split()[1],
            target,
        )
        self.assertEqual(
            git(self.source.root, "ls-files", "--stage", "source.txt"), unrelated
        )

    def test_exact_head_oversized_child_updates_only_root_authority(self):
        target = self.harness.publish("oversized.bin", b"x" * (16 * 1024 * 1024 + 1))
        git(self.child, "checkout", "-q", target)
        prepared = self.prepared(target)
        child_before = snapshot(self.child)
        with (
            patch.object(
                ownership,
                "capture_published_repository_tree",
                side_effect=AssertionError("root-only child must not capture a tree"),
            ),
            patch.object(
                objects_adapter,
                "capture_published_repository_pack",
                side_effect=AssertionError("root-only child must not capture a pack"),
            ),
            self.harness.acquire(prepared) as owned,
        ):
            files = owned.prepare_filesystem_changes(
                tree_policy=RepositoryTreeCapturePolicy(
                    maximum_entries=1,
                    maximum_metadata_bytes=1,
                    maximum_blob_bytes=1,
                    maximum_total_blob_bytes=1,
                )
            )
            self.assertEqual(files.target_modes, (("app", "root-pin-only"),))
            self.assertEqual(files.plans, ())
            captured = files.prepare_objects()
            self.assertEqual(captured.packs, ())
            with captured.stage(metadata=True) as stage:
                self.assertFalse(
                    any(entry.repository == "app" for entry in stage.entries)
                )
                result = application.apply_repository_refresh(stage)
        self.assertFalse(result.source_writes)
        self.assertTrue(result.filesystem_writes)
        self.assertEqual(snapshot(self.child), child_before)
        self.assertEqual(
            git(self.source.root, "ls-files", "--stage", "app").decode().split()[1],
            target,
        )
        manifest = parse_project_configuration(
            (self.source.root / "literate.project.json").read_bytes()
        )
        self.assertEqual(
            next(
                item.commit
                for item in manifest.repository_orchestration.repositories
                if item.path == "app"
            ),
            target,
        )

    def test_exact_head_child_drift_before_stage_and_terminal_commit_refuses(self):
        target = self.publish()
        git(self.child, "checkout", "-q", target)
        prepared = self.prepared(target)
        with self.assertRaises(OrchestrationInventoryError):
            with self.harness.acquire(prepared) as owned:
                (self.child / "foreign-before-stage").write_bytes(b"foreign\n")
                owned.prepare_filesystem_changes()
        (self.child / "foreign-before-stage").unlink()

        prepared = self.prepared(target)
        root_index = prepared.refresh.root_git.index.content
        manifest = prepared.refresh.manifest.content
        renew = ownership.OwnedRefreshPublication.renew_application_publication

        def drift(owner, live):
            (self.child / "foreign-before-commit").write_bytes(b"foreign\n")
            return renew(owner, live)

        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                with (
                    patch.object(
                        ownership.OwnedRefreshPublication,
                        "renew_application_publication",
                        drift,
                    ),
                    self.assertRaises(OrchestrationInventoryError),
                ):
                    application.apply_repository_refresh(stage)
        self.assertEqual(prepared.refresh.root_git.index.path.read_bytes(), root_index)
        self.assertEqual(prepared.refresh.manifest.path.read_bytes(), manifest)
        self.assertEqual(
            (self.child / "foreign-before-commit").read_bytes(), b"foreign\n"
        )

    def test_final_prejournal_validation_preserves_root_only_foreign_bytes(self):
        target = self.publish()
        git(self.child, "checkout", "-q", target)
        prepared = self.prepared(target)
        observed = next(
            child for child in prepared.refresh.children if child.root == self.child
        )
        root_index = prepared.refresh.root_git.index.content
        manifest = prepared.refresh.manifest.content
        foreign_head = (self.source.pin + "\n").encode("ascii")
        foreign_index = observed.index.content + b"foreign-index-race"
        foreign_worktree = b"foreign-worktree-race\n"
        terminal = []
        record_terminal = staging.StagedRefresh.record_terminal

        def race(live):
            self.assertEqual(live.state, "applying")
            observed.head.path.write_bytes(foreign_head)
            observed.index.path.write_bytes(foreign_index)
            (self.child / "source.txt").write_bytes(foreign_worktree)

        def observed_terminal(stage, state):
            terminal.append(state)
            return record_terminal(stage, state)

        with self.assertRaises(OrchestrationInventoryError):
            with self.harness.acquire(prepared) as owned:
                with self.stage(owned) as stage:
                    with (
                        patch.object(application, "_before_terminal_journal", race),
                        patch.object(
                            staging.StagedRefresh,
                            "record_terminal",
                            observed_terminal,
                        ),
                    ):
                        application.apply_repository_refresh(stage)

        self.assertEqual(terminal, ["rolled_back"])
        self.assertEqual(prepared.refresh.root_git.index.path.read_bytes(), root_index)
        self.assertEqual(prepared.refresh.manifest.path.read_bytes(), manifest)
        self.assertEqual(observed.head.path.read_bytes(), foreign_head)
        self.assertEqual(observed.index.path.read_bytes(), foreign_index)
        self.assertEqual((self.child / "source.txt").read_bytes(), foreign_worktree)

    def test_nested_root_only_prejournal_race_preserves_foreign_bytes(self):
        target, nested, previous = self.publish_nested_child()
        prepared = self.prepared(target)
        self.assertEqual(prepared.target_modes, (("app", "root-pin-only"),))
        observed = next(
            child for child in prepared.refresh.children if child.root == nested
        )
        root_index = prepared.refresh.root_git.index.content
        manifest = prepared.refresh.manifest.content
        foreign_head = (previous + "\n").encode("ascii")
        foreign_index = observed.index.content + b"nested-index-race"
        foreign_worktree = b"nested-worktree-race\n"
        terminal = []
        record_terminal = staging.StagedRefresh.record_terminal

        def race(live):
            self.assertEqual(live.state, "applying")
            observed.head.path.write_bytes(foreign_head)
            observed.index.path.write_bytes(foreign_index)
            (nested / "source.txt").write_bytes(foreign_worktree)

        def observed_terminal(stage, state):
            terminal.append(state)
            return record_terminal(stage, state)

        with self.assertRaises(OrchestrationInventoryError):
            with self.harness.acquire(prepared) as owned:
                files = owned.prepare_filesystem_changes()
                self.assertEqual(files.plans, ())
                captured = files.prepare_objects()
                self.assertEqual(captured.packs, ())
                with captured.stage(metadata=True) as stage:
                    self.assertFalse(
                        any(
                            entry.repository == "app"
                            or entry.repository.startswith("app/")
                            for entry in stage.entries
                        )
                    )
                    with (
                        patch.object(application, "_before_terminal_journal", race),
                        patch.object(
                            staging.StagedRefresh,
                            "record_terminal",
                            observed_terminal,
                        ),
                    ):
                        application.apply_repository_refresh(stage)

        self.assertEqual(terminal, ["rolled_back"])
        self.assertEqual(prepared.refresh.root_git.index.path.read_bytes(), root_index)
        self.assertEqual(prepared.refresh.manifest.path.read_bytes(), manifest)
        self.assertEqual(observed.head.path.read_bytes(), foreign_head)
        self.assertEqual(observed.index.path.read_bytes(), foreign_index)
        self.assertEqual((nested / "source.txt").read_bytes(), foreign_worktree)

    def test_exact_head_publication_drift_before_terminal_commit_rolls_back_root(self):
        target = self.publish()
        git(self.child, "checkout", "-q", target)
        prepared = self.prepared(target)
        root_index = prepared.refresh.root_git.index.content
        manifest = prepared.refresh.manifest.content
        renew = ownership.OwnedRefreshPublication.renew_application_publication
        changed = False

        def drift(owner, live):
            nonlocal changed
            if not changed:
                changed = True
                git(
                    self.source.remote,
                    "update-ref",
                    "refs/tags/concurrent-proof",
                    target,
                )
            return renew(owner, live)

        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                with (
                    patch.object(
                        ownership.OwnedRefreshPublication,
                        "renew_application_publication",
                        drift,
                    ),
                    self.assertRaises(OrchestrationInventoryError) as caught,
                ):
                    application.apply_repository_refresh(stage)
        self.assertEqual(
            caught.exception.code, "orchestration.refresh_publication_changed"
        )
        self.assertEqual(prepared.refresh.root_git.index.path.read_bytes(), root_index)
        self.assertEqual(prepared.refresh.manifest.path.read_bytes(), manifest)

    def test_failure_rolls_back_and_keeps_additive_verified_objects(self):
        target = self.publish()
        prepared = self.prepared(target)
        original_source = (self.child / "source.txt").read_bytes()
        original_head = git(self.child, "rev-parse", "HEAD").decode().strip()
        original_manifest = (self.source.root / "literate.project.json").read_bytes()
        original = application._materialize
        terminal = []
        failed = False

        def fail_after_child_index(path, state, **kwargs):
            nonlocal failed
            original(path, state, **kwargs)
            if path == prepared.refresh.children[0].index.path and not failed:
                failed = True
                raise RuntimeError("fixture application failure")

        record_terminal = staging.StagedRefresh.record_terminal

        def observed_terminal(stage, state):
            record_terminal(stage, state)
            terminal.append(
                json.loads((stage.path / "application.json").read_bytes())["state"]
            )

        with self.harness.acquire(prepared) as owned:
            with self.assertRaisesRegex(RuntimeError, "fixture application failure"):
                with self.stage(owned) as stage:
                    packs = tuple(
                        (
                            prepared.refresh.repository.root / repository,
                            captured.objects.pack_id,
                        )
                        for repository, captured in stage._objects.packs
                    )
                    with (
                        patch.object(
                            application, "_materialize", fail_after_child_index
                        ),
                        patch.object(
                            staging.StagedRefresh,
                            "record_terminal",
                            observed_terminal,
                        ),
                    ):
                        application.apply_repository_refresh(stage)
            self.assertEqual(terminal, ["rolled_back"])
        self.assertEqual((self.child / "source.txt").read_bytes(), original_source)
        self.assertEqual(
            git(self.child, "rev-parse", "HEAD").decode().strip(), original_head
        )
        self.assertEqual(
            (self.source.root / "literate.project.json").read_bytes(),
            original_manifest,
        )
        for repository, pack_id in packs:
            common = git(repository, "rev-parse", "--git-common-dir").decode().strip()
            directory = (repository / common).resolve() / "objects/pack"
            self.assertTrue((directory / f"pack-{pack_id}.pack").is_file())
            self.assertTrue((directory / f"pack-{pack_id}.idx").is_file())
        self.assertEqual(
            git(self.child, "cat-file", "-t", target).decode().strip(), "commit"
        )

    def test_foreign_concurrent_edit_is_preserved_and_retains_applying_recovery(self):
        target = self.publish()
        prepared = self.prepared(target)
        original = application._materialize
        foreign = b"foreign concurrent edit\n"
        stage_path = None

        def edit_after_source(path, state, **kwargs):
            original(path, state, **kwargs)
            if path == self.child / "source.txt":
                path.write_bytes(foreign)
                raise RuntimeError("fixture concurrent writer")

        with self.assertRaises(OrchestrationInventoryError):
            with self.harness.acquire(prepared) as owned:
                with self.stage(owned) as stage:
                    stage_path = stage.path
                    with patch.object(application, "_materialize", edit_after_source):
                        application.apply_repository_refresh(stage)
        self.assertEqual((self.child / "source.txt").read_bytes(), foreign)
        self.assertIsNotNone(stage_path)
        self.assertTrue(stage_path.is_dir())
        journal = json.loads((stage_path / "application.json").read_bytes())
        self.assertEqual(journal["state"], "applying")
        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(stage._files, metadata=True):
                self.fail("retained recovery staging must not be adopted")

    def test_unrelated_tracked_edit_and_foreign_addition_block_manifest_commit(self):
        target = self.publish()
        prepared = self.prepared(target)
        foreign_tracked = b"foreign tracked edit\n"
        foreign_added = b"foreign addition\n"
        original_source = (self.child / "source.txt").read_bytes()
        original = application._LiveRefreshApplication.require_worktrees
        injected = False
        stage_path = None

        def inject_before_manifest(selected, *, prospective):
            nonlocal injected
            if prospective and not injected:
                injected = True
                (self.child / ".gitmodules").write_bytes(foreign_tracked)
                (self.child / "foreign-added").write_bytes(foreign_added)
            return original(selected, prospective=prospective)

        with self.assertRaises(OrchestrationInventoryError):
            with self.harness.acquire(prepared) as owned:
                with self.stage(owned) as stage:
                    stage_path = stage.path
                    with patch.object(
                        application._LiveRefreshApplication,
                        "require_worktrees",
                        inject_before_manifest,
                    ):
                        application.apply_repository_refresh(stage)
        self.assertEqual((self.child / ".gitmodules").read_bytes(), foreign_tracked)
        self.assertEqual((self.child / "foreign-added").read_bytes(), foreign_added)
        self.assertEqual((self.child / "source.txt").read_bytes(), original_source)
        self.assertTrue(stage_path.is_dir())
        journal = json.loads((stage_path / "application.json").read_bytes())
        self.assertEqual(journal["state"], "applying")

    @unittest.skipIf(os.name == "nt", "symlink substitution fixture is POSIX-only")
    def test_terminal_object_symlink_substitution_is_not_followed(self):
        target = self.publish()
        prepared = self.prepared(target)
        original = application._apply_live_changes
        substituted = None
        stage_path = None

        def substitute_pack(selected):
            nonlocal substituted
            original(selected)
            record = selected._installed_objects.records[0]
            prefix = record.directory / ("pack-" + record.objects.pack_id)
            pack = prefix.with_suffix(".pack")
            pack.unlink()
            pack.symlink_to(prefix.with_suffix(".idx"))
            substituted = pack

        with self.assertRaises(OrchestrationInventoryError):
            with self.harness.acquire(prepared) as owned:
                with self.stage(owned) as stage:
                    stage_path = stage.path
                    with patch.object(
                        application, "_apply_live_changes", substitute_pack
                    ):
                        application.apply_repository_refresh(stage)
        self.assertTrue(substituted.is_symlink())
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "applying",
        )

    def test_failed_committed_journal_precedes_directory_ownership_transfer(self):
        target = self.publish()
        git(self.child, "checkout", "-q", "-B", "feature/nested", self.source.pin)
        git(self.child, "pack-refs", "--all", "--prune")
        common = Path(git(self.child, "rev-parse", "--git-common-dir").decode().strip())
        common = (self.child / common).resolve()
        new_parent = common / "refs/heads/feature"
        if new_parent.is_dir():
            new_parent.rmdir()
        prepared = self.prepared(target)
        record_terminal = staging.StagedRefresh.record_terminal

        def fail_committed(stage, state):
            if state == "committed":
                raise OrchestrationInventoryError(
                    "fixture_terminal_journal_failure",
                    "fixture terminal journal failure",
                )
            return record_terminal(stage, state)

        with self.harness.acquire(prepared) as owned:
            with self.assertRaises(OrchestrationInventoryError) as caught:
                with self.stage(owned) as stage:
                    self.assertTrue(new_parent.is_dir())
                    with (
                        patch.object(
                            staging.StagedRefresh,
                            "record_terminal",
                            fail_committed,
                        ),
                        patch.object(
                            owned._reservations,
                            "commit_directory_transfer",
                            side_effect=AssertionError(
                                "directory transfer preceded journal"
                            ),
                        ),
                    ):
                        application.apply_repository_refresh(stage)
            self.assertEqual(
                caught.exception.code, "orchestration.fixture_terminal_journal_failure"
            )
        self.assertFalse(new_parent.exists())
        self.assertEqual(
            git(self.child, "rev-parse", "HEAD").decode().strip(), self.source.pin
        )

    def test_post_committed_transfer_failure_retains_committed_journal(self):
        target = self.publish()
        git(self.child, "checkout", "-q", "-B", "feature/nested", self.source.pin)
        git(self.child, "pack-refs", "--all", "--prune")
        common = Path(git(self.child, "rev-parse", "--git-common-dir").decode().strip())
        common = (self.child / common).resolve()
        new_parent = common / "refs/heads/feature"
        if new_parent.is_dir():
            new_parent.rmdir()
        prepared = self.prepared(target)
        stage_path = None
        with self.assertRaises(ExceptionGroup) as caught:
            with self.harness.acquire(prepared) as owned:
                with self.stage(owned) as stage:
                    stage_path = stage.path
                    with patch.object(
                        owned._reservations,
                        "commit_directory_transfer",
                        side_effect=OrchestrationInventoryError(
                            "fixture_post_commit_transfer",
                            "fixture post-commit transfer failure",
                        ),
                    ):
                        application.apply_repository_refresh(stage)
        group = caught.exception
        self.assertEqual(
            group.message,
            "repository refresh body and outer-reservation release both failed",
        )
        self.assertEqual(
            tuple(error.code for error in group.exceptions),
            (
                "orchestration.fixture_post_commit_transfer",
                "orchestration.reservation_cleanup_incomplete",
            ),
        )
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )
        self.assertEqual(git(self.child, "rev-parse", "HEAD").decode().strip(), target)
        manifest = parse_project_configuration(
            (self.source.root / "literate.project.json").read_bytes()
        )
        self.assertEqual(
            next(
                item
                for item in manifest.repository_orchestration.repositories
                if item.path == "app"
            ).commit,
            target,
        )

    def test_stage_marker_release_failure_reports_committed_cleanup_retained(self):
        target = self.publish()
        prepared = self.prepared(target)
        original_release = WriteReservationSet._release
        stage_marker = None
        stage_path = None

        def fail_after_stage_marker_release(selected):
            original_release(selected)
            if selected is stage_marker:
                raise OrchestrationInventoryError(
                    "reservation_cleanup_incomplete",
                    "fixture stage marker release failure",
                )

        with self.harness.acquire(prepared) as owned:
            with patch.object(
                WriteReservationSet,
                "_release",
                fail_after_stage_marker_release,
            ):
                with self.stage(owned) as stage:
                    stage_marker = stage._marker
                    stage_path = stage.path
                    result = application.apply_repository_refresh(stage)
        self.assertEqual(result.state, "committed")
        self.assertEqual(
            result.cleanup_retained,
            ("terminal-refresh-staging", "stage-marker-artifacts"),
        )
        self.assertEqual(
            result.to_dict()["cleanup_retained"],
            ["terminal-refresh-staging", "stage-marker-artifacts"],
        )
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )

    def test_outer_reservation_release_failure_retains_terminal_stage(self):
        target = self.publish()
        prepared = self.prepared(target)
        original_release = WriteReservationSet._release
        outer_reservations = None
        stage_path = None

        def fail_after_outer_release(selected):
            original_release(selected)
            if selected is outer_reservations:
                raise OrchestrationInventoryError(
                    "reservation_cleanup_incomplete",
                    "fixture outer reservation release failure",
                )

        with patch.object(
            WriteReservationSet,
            "_release",
            fail_after_outer_release,
        ):
            with self.harness.acquire(prepared) as owned:
                outer_reservations = owned._reservations
                with self.stage(owned) as stage:
                    stage_path = stage.path
                    result = application.apply_repository_refresh(stage)
        self.assertEqual(result.state, "committed")
        self.assertEqual(
            result.cleanup_retained,
            ("terminal-refresh-staging", "refresh-reservation-artifacts"),
        )
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )

    def test_unexpected_stage_marker_exception_propagates_with_evidence(self):
        target = self.publish()
        prepared = self.prepared(target)
        original_release = WriteReservationSet._release
        stage_marker = None
        stage_path = None

        def fail_after_stage_marker_release(selected):
            original_release(selected)
            if selected is stage_marker:
                raise RuntimeError("unexpected stage marker failure")

        with self.assertRaisesRegex(RuntimeError, "unexpected stage marker"):
            with self.harness.acquire(prepared) as owned:
                with patch.object(
                    WriteReservationSet,
                    "_release",
                    fail_after_stage_marker_release,
                ):
                    with self.stage(owned) as stage:
                        stage_marker = stage._marker
                        stage_path = stage.path
                        application.apply_repository_refresh(stage)
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )

    def test_unexpected_outer_release_base_exception_propagates_with_evidence(self):
        target = self.publish()
        prepared = self.prepared(target)
        original_release = WriteReservationSet._release
        outer_reservations = None
        stage_path = None

        def fail_after_outer_release(selected):
            original_release(selected)
            if selected is outer_reservations:
                raise KeyboardInterrupt("unexpected outer release interruption")

        with self.assertRaisesRegex(KeyboardInterrupt, "unexpected outer release"):
            with patch.object(
                WriteReservationSet,
                "_release",
                fail_after_outer_release,
            ):
                with self.harness.acquire(prepared) as owned:
                    outer_reservations = owned._reservations
                    with self.stage(owned) as stage:
                        stage_path = stage.path
                        application.apply_repository_refresh(stage)
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )

    def test_body_and_stage_marker_release_failures_are_grouped_in_order(self):
        target = self.publish()
        prepared = self.prepared(target)
        original_release = WriteReservationSet._release
        stage_marker = None
        stage_path = None

        def fail_after_stage_marker_release(selected):
            original_release(selected)
            if selected is stage_marker:
                raise OrchestrationInventoryError(
                    "reservation_cleanup_incomplete",
                    "fixture stage marker release failure",
                )

        with self.assertRaises(ExceptionGroup) as caught:
            with self.harness.acquire(prepared) as owned:
                with patch.object(
                    WriteReservationSet,
                    "_release",
                    fail_after_stage_marker_release,
                ):
                    with self.stage(owned) as stage:
                        stage_marker = stage._marker
                        stage_path = stage.path
                        application.apply_repository_refresh(stage)
                        raise ValueError("fixture committed body failure")
        group = caught.exception
        self.assertEqual(
            group.message,
            "repository refresh body and stage-marker release both failed",
        )
        self.assertEqual(
            tuple(type(error) for error in group.exceptions),
            (ValueError, OrchestrationInventoryError),
        )
        self.assertEqual(str(group.exceptions[0]), "fixture committed body failure")
        self.assertEqual(
            group.exceptions[1].code,
            "orchestration.reservation_cleanup_incomplete",
        )
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )

    def test_base_body_and_outer_release_failures_are_grouped_in_order(self):
        target = self.publish()
        prepared = self.prepared(target)
        original_release = WriteReservationSet._release
        outer_reservations = None
        stage_path = None

        def fail_after_outer_release(selected):
            original_release(selected)
            if selected is outer_reservations:
                raise RuntimeError("fixture outer reservation release failure")

        with self.assertRaises(BaseExceptionGroup) as caught:
            with patch.object(
                WriteReservationSet,
                "_release",
                fail_after_outer_release,
            ):
                with self.harness.acquire(prepared) as owned:
                    outer_reservations = owned._reservations
                    with self.stage(owned) as stage:
                        stage_path = stage.path
                        application.apply_repository_refresh(stage)
                    raise KeyboardInterrupt("fixture committed body interruption")
        group = caught.exception
        self.assertNotIsInstance(group, ExceptionGroup)
        self.assertEqual(
            group.message,
            "repository refresh body and outer-reservation release both failed",
        )
        self.assertEqual(
            tuple(type(error) for error in group.exceptions),
            (KeyboardInterrupt, RuntimeError),
        )
        self.assertEqual(
            str(group.exceptions[0]), "fixture committed body interruption"
        )
        self.assertEqual(
            str(group.exceptions[1]),
            "fixture outer reservation release failure",
        )
        self.assertTrue(stage_path.is_dir())
        self.assertEqual(
            json.loads((stage_path / "application.json").read_bytes())["state"],
            "committed",
        )

    def test_hardlink_preflight_failure_preserves_exact_before_state(self):
        target = self.publish()
        prepared = self.prepared(target)
        before = snapshot(self.source.base)
        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                with (
                    patch.object(
                        application.os,
                        "link",
                        side_effect=OSError("hardlinks unavailable"),
                    ),
                    self.assertRaises(OrchestrationInventoryError) as caught,
                ):
                    application.apply_repository_refresh(stage)
        self.assertEqual(
            caught.exception.code,
            "orchestration.refresh_application_hardlink_unsupported",
        )
        self.assertEqual(snapshot(self.source.base), before)

    def test_mocked_windows_preflight_refuses_prospective_symlink(self):
        if os.name == "nt":
            self.skipTest("fixture publication creates its symlink only on POSIX")
        target = self.publish()
        prepared = self.prepared(target)
        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                with (
                    patch.object(application, "_is_windows", return_value=True),
                    self.assertRaises(OrchestrationInventoryError) as caught,
                ):
                    application.prepare_refresh_application(stage)
        self.assertEqual(
            caught.exception.code,
            "orchestration.refresh_application_platform_unsupported",
        )

    def test_mocked_windows_preflight_refuses_directory_transition(self):
        directory = self.child / "new-directory"
        directory.mkdir()
        (directory / "source.txt").write_bytes(b"prospective directory\n")
        git(self.child, "add", "new-directory/source.txt")
        git(self.child, "commit", "-q", "-m", "prospective directory")
        target = git(self.child, "rev-parse", "HEAD").decode().strip()
        git(
            self.child,
            "push",
            self.source.remote.as_uri(),
            "HEAD:refs/heads/main",
        )
        git(self.child, "checkout", "-q", self.source.pin)
        prepared = self.prepared(target)
        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                with (
                    patch.object(application, "_is_windows", return_value=True),
                    self.assertRaises(OrchestrationInventoryError) as caught,
                ):
                    application.prepare_refresh_application(stage)
        self.assertEqual(
            caught.exception.code,
            "orchestration.refresh_application_platform_unsupported",
        )

    def test_hydrated_lfs_payload_remains_exact_during_unrelated_apply(self):
        payload = b"hydrated binary asset\x00\xff"
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{hashlib.sha256(payload).hexdigest()}\n"
            f"size {len(payload)}\n"
        ).encode()
        asset = self.child / "asset.dat"
        asset.write_bytes(pointer)
        (self.child / ".gitattributes").write_text(
            "asset.dat filter=lfs -text\n", encoding="utf-8"
        )
        git(self.child, "add", "asset.dat", ".gitattributes")
        git(self.child, "commit", "-q", "-m", "LFS baseline")
        self.source.pin = git(self.child, "rev-parse", "HEAD").decode().strip()
        (self.child / "source.txt").write_bytes(b"unrelated prospective\n")
        git(self.child, "add", "source.txt")
        git(self.child, "commit", "-q", "-m", "prospective change")
        target = git(self.child, "rev-parse", "HEAD").decode().strip()
        # This fixture intentionally supplies the indexed pointer without an LFS
        # object store. Ambient Git LFS pre-push hooks are outside the refresh
        # authority and must not execute while publishing the synthetic fixture.
        git(
            self.child,
            "push",
            "--no-verify",
            self.source.remote.as_uri(),
            "HEAD:refs/heads/main",
        )
        git(self.child, "checkout", "-q", self.source.pin)
        asset.write_bytes(payload)
        prepared = self.prepared(target)
        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                result = application.apply_repository_refresh(stage)
        self.assertEqual(result.state, "committed")
        self.assertEqual(asset.read_bytes(), payload)

    def test_noop_preserves_every_live_file_and_does_not_install_a_redundant_pack(self):
        prepared = self.prepared(self.source.pin)
        before = snapshot(self.source.base)
        materialize = application._materialize
        with self.harness.acquire(prepared) as owned:
            with self.stage(owned) as stage:
                with patch.object(
                    application,
                    "_materialize",
                    side_effect=AssertionError("no-op live write"),
                ):
                    result = application.apply_repository_refresh(stage)
                self.assertEqual(result.state, "committed")
                document = result.to_dict()
                self.assertFalse(document["authority_changed"])
                self.assertFalse(document["filesystem_writes"])
                self.assertFalse(document["writes"])
        self.assertEqual(snapshot(self.source.base), before)
        self.assertIs(application._materialize, materialize)

if __name__ == "__main__":
    unittest.main()


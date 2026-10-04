"""Real-Git qualification for live refresh commit and conservative rollback."""

from __future__ import annotations

import json
import os
import stat
import unittest
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_application as application
from literate_ai.adapters import repository_refresh_staging as staging
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
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

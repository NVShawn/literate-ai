"""Owned staging preserves live files and refuses collisions or altered custody."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
from tests.support import fixtures_test_refresh_file_custody as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot
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

    def test_staging_is_bounded_inert_and_cleaned_after_success_or_body_failure(self):
        before = snapshot(self.root)
        for fail in (False, True):
            try:
                with staging.stage_refresh_files(self.files) as stage:
                    stage.require_current()
                    manifest = json.loads((stage.path / "manifest.json").read_bytes())
                    self.assertEqual(manifest["physical_custody"], self.files.identity)
                    for record, entry in zip(
                        manifest["entries"], stage.entries, strict=True
                    ):
                        path = stage.path / record["file"]
                        self.assertFalse(path.is_symlink())
                        self.assertEqual(path.read_bytes(), entry.content)
                    self.assertTrue(
                        any(entry.content == b"before\r\n" for entry in stage.entries)
                    )
                    if fail:
                        raise RuntimeError("body failure")
            except RuntimeError as error:
                self.assertTrue(fail)
                self.assertEqual(str(error), "body failure")
            self.assertFalse(self.path.exists())
            self.assertEqual(snapshot(self.root), before)
            with self.assertRaises(OrchestrationInventoryError):
                stage.require_current()

    def test_existing_empty_or_crash_left_staging_is_never_adopted(self):
        self.path.mkdir()
        for populated in (False, True):
            if populated:
                (self.path / "owner").write_bytes(b"crashed owner")
            before = snapshot(self.root)
            with self.assertRaises(OrchestrationInventoryError) as caught:
                with staging.stage_refresh_files(self.files):
                    self.fail("must refuse existing staging")
            self.assertEqual(caught.exception.code, "orchestration.refresh_stage_busy")
            self.assertTrue(self.path.is_dir())
            self.assertEqual(snapshot(self.root), before)

    def test_foreign_entry_or_same_byte_replacement_preserves_staging(self):
        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(self.files) as stage:
                foreign = stage.path / "foreign"
                foreign.write_bytes(b"private")
                with self.assertRaises(OrchestrationInventoryError) as caught:
                    stage.require_current()
                self.assertEqual(
                    caught.exception.code, "orchestration.refresh_stage_changed"
                )
        self.assertEqual(foreign.read_bytes(), b"private")
        self.assertTrue((stage.path / "manifest.json").exists())
        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(self.files):
                self.fail("must not reuse retained staging")

    def test_same_byte_inode_replacement_is_not_cleaned(self):
        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(self.files) as stage:
                target = stage.path / "00000"
                replacement = stage.path / "replacement"
                replacement.write_bytes(target.read_bytes())
                os.replace(replacement, target)
        self.assertEqual(target.read_bytes(), b"{}\n")
        self.assertTrue((stage.path / "manifest.json").exists())

    def test_failed_payload_write_removes_only_owned_partial_bytes(self):
        write = staging.os.write
        calls = []

        def broken(descriptor, content):
            if bytes(content) == b"{}\n":
                calls.append(True)
                return write(descriptor, content[:1])
            if calls and bytes(content) == b"}\n":
                raise OSError("fixture disk failure")
            return write(descriptor, content)

        before = snapshot(self.root)
        with patch.object(staging.os, "write", broken):
            with self.assertRaises(OrchestrationInventoryError):
                with staging.stage_refresh_files(self.files):
                    self.fail("must refuse failed write")
        self.assertEqual(snapshot(self.root), before)
        self.assertFalse(self.path.exists())

    def test_bounds_and_stale_custody_refuse_before_directory_creation(self):
        with patch.object(staging, "_MAX_TOTAL", 1):
            with self.assertRaises(OrchestrationInventoryError):
                with staging.stage_refresh_files(self.files):
                    self.fail("must refuse over budget")
        self.assertFalse(self.path.exists())

    def test_expired_custody_refuses_before_directory_creation(self):
        self.files.require_current.side_effect = RuntimeError("expired")
        with self.assertRaises(RuntimeError):
            with staging.stage_refresh_files(self.files):
                self.fail("must refuse expired inputs")
        self.assertFalse(self.path.exists())

    def test_metadata_selection_requires_an_explicit_boolean(self):
        for value in (1, None, "true"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                with staging.stage_refresh_files(self.files, metadata=value):
                    self.fail("untyped metadata selection")
        self.assertFalse(self.path.exists())

    def test_abrupt_process_exit_retains_stage_and_blocks_new_owner(self):
        crash_root = self.root / "crash"
        crash_root.mkdir()
        script = """
import os, sys
from types import SimpleNamespace
from unittest.mock import patch
from tests.unit import test_repository_refresh_staging as fixture_module
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

    def test_armed_normal_exit_retains_every_before_file_and_refuses_reuse(self):
        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(self.files) as stage:
                stage.arm_recovery()
                before = {
                    p.name: p.read_bytes()
                    for p in stage.path.iterdir()
                    if p.name != "owner"
                }
                self.assertEqual(
                    json.loads(before["application.json"])["state"], "applying"
                )
                with self.assertRaises(OrchestrationInventoryError):
                    stage.arm_recovery()
        self.assertEqual({p.name: p.read_bytes() for p in stage.path.iterdir()}, before)
        with self.assertRaises(OrchestrationInventoryError):
            stage.require_current()
        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(self.files):
                self.fail("retained recovery state must not be reused")

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

    def test_partial_application_marker_write_retains_payloads(self):
        write = staging.os.write
        armed = False

        def short_write(fd, data):
            nonlocal armed
            if not armed and b"refresh-application@1" in bytes(data):
                armed = True
                return write(fd, data[:10])
            if armed:
                raise OSError("fixture journal disk error")
            return write(fd, data)

        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(self.files) as stage:
                with patch.object(staging.os, "write", short_write):
                    stage.arm_recovery()
        self.assertEqual(len((stage.path / "application.json").read_bytes()), 10)
        self.assertIn(b"before\r\n", [p.read_bytes() for p in stage.path.iterdir()])

    def test_expired_input_cannot_arm_retention(self):
        with staging.stage_refresh_files(self.files) as stage:
            previous = self.files.require_current.side_effect
            self.files.require_current.side_effect = RuntimeError("expired custody")
            with self.assertRaisesRegex(RuntimeError, "expired custody"):
                stage.arm_recovery()
            self.files.require_current.side_effect = previous
            self.assertFalse((stage.path / "application.json").exists())
        self.assertFalse(self.path.exists())

    def test_application_journal_limits_refuse_before_retention(self):
        with staging.stage_refresh_files(self.files) as stage:
            with patch.object(staging, "_MAX_TOTAL", 1):
                with self.assertRaises(OrchestrationInventoryError) as caught:
                    stage.arm_recovery()
                self.assertEqual(
                    caught.exception.code, "orchestration.refresh_stage_limit"
                )
            self.assertFalse((stage.path / "application.json").exists())
        self.assertFalse(self.path.exists())

    def test_application_sync_failure_retains_before_state(self):
        with self.assertRaises(OrchestrationInventoryError):
            with staging.stage_refresh_files(self.files) as stage:
                with patch.object(
                    staging.os, "fsync", side_effect=OSError("sync failed")
                ):
                    stage.arm_recovery()
        self.assertTrue((stage.path / "application.json").is_file())
        self.assertIn(b"before\r\n", [p.read_bytes() for p in stage.path.iterdir()])


class NativeRefreshStagingTests(unittest.TestCase):
    def test_staging_under_live_reservations_preserves_all_original_bytes(self):
        fixture = fixtures.RefreshFileCustodyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.fixture
        target = fixture.publish("source.txt", b"prospective\n")
        prepared = source.prepare(source.inputs(target))
        before = snapshot(source.base)
        with fixture.acquire(prepared) as owned:
            files = owned.prepare_filesystem_changes()
            with files.stage(metadata=True) as stage:
                stage.require_current()
                document = json.loads((stage.path / "manifest.json").read_bytes())
                metadata = document["metadata"]
                self.assertEqual(metadata, [item.to_dict() for item in stage.metadata])
                self.assertEqual(
                    document["metadata_directories"],
                    [item.to_dict() for item in prepared.refresh.metadata_directories],
                )
                logs = [item for item in stage.metadata if item.kind == "reflog"]
                self.assertTrue(logs)
                for log in logs:
                    self.assertEqual(log.before.path.read_bytes(), log.before.content)
                    self.assertTrue(log.prospective.startswith(log.before.content))
                    self.assertTrue(
                        log.prospective.endswith(b"\tliterate-ai refresh\n")
                    )
                    self.assertTrue(
                        any(
                            entry.role == "metadata-before-reflog"
                            and entry.repository == log.repository
                            and entry.path == log.name
                            and entry.content == log.before.content
                            for entry in stage.entries
                        )
                    )
                manifest = next(
                    item for item in stage.metadata if item.kind == "manifest"
                )
                self.assertEqual(manifest.to_dict()["operation"], "replace")
                prospective = json.loads(manifest.prospective)
                pins = prospective["repository_orchestration"]["repositories"]
                self.assertEqual(
                    next(pin["commit"] for pin in pins if pin["path"] == "app"), target
                )
                self.assertEqual(
                    (source.root / "literate.project.json").read_bytes(),
                    manifest.before.content,
                )
                for index, entry in enumerate(stage.entries):
                    if entry.role != "prospective-index":
                        continue
                    root = source.root if entry.repository == "." else source.child
                    with patch.dict(
                        os.environ,
                        {
                            "GIT_INDEX_FILE": str(stage.path / f"{index:05d}"),
                            "GIT_OPTIONAL_LOCKS": "0",
                        },
                    ):
                        listing = git(root, "ls-files", "--stage")
                    self.assertTrue(listing)
                self.assertEqual(
                    (source.child / "source.txt").read_bytes(), b"original\n"
                )
            files.require_current()
        self.assertEqual(snapshot(source.base), before)

"""Real-Git refresh custody without child commands, publication or mutation."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import sys
import unittest
from unittest.mock import patch

from literate_ai.adapters import repository_lfs
from literate_ai.adapters import repository_refresh as refresh
from literate_ai.adapters.repository_locks import RepositoryLockStore
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support import fixtures_test_repository_lock_planning as lock_fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RepositoryRefreshInputTests(unittest.TestCase):
    def setUp(self):
        fixture = lock_fixtures.RepositoryLockPlanningTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base
        for name in ("app", "lib"):
            child = self.root / name
            git(self.base, "clone", "-q", "--no-hardlinks", str(self.root), str(child))
            git(child, "checkout", "-q", fixture.fixture.pin)
            git(child, "config", "user.name", "Test")
            git(child, "config", "user.email", "test@example.test")
        self.request = RepositoryRefreshRequest(
            (RepositoryRefreshTarget("app", "4" * 40),)
        )

    def prepare(self):
        return refresh.prepare_repository_refresh(self.root, self.request)

    def assert_refuses_without_writes(self, suffix):
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "orchestration." + suffix)
        self.assertEqual(snapshot(self.base), before)

    def command_sentinel(self, directory, name, marker):
        if os.name == "nt":
            executable = directory / (name + ".cmd")
            executable.write_text(
                f'@echo executed>"{marker}"\r\n@exit /b 1\r\n',
                encoding="utf-8",
            )
        else:
            executable = directory / name
            executable.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_bytes(b'executed')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
        return executable

    def test_clean_observation_and_revalidation_preserve_all_bytes_and_mtimes(self):
        before = snapshot(self.base)
        prepared = self.prepare()
        refresh.require_repository_refresh_inputs_unchanged(prepared)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(len(prepared.children), 2)
        self.assertEqual(prepared.authority.previous, self.fixture.binding)
        self.assertIsNone(prepared.repository_lock.content)
        # An arbitrary exact target is intent, not evidence that the object exists.
        self.assertEqual(prepared.authority.to_dict()["publication"], "not-checked")
        self.assertFalse(prepared.authority.to_dict()["apply_supported"])

    def test_dirty_tracked_staged_untracked_and_deleted_child_files_refuse(self):
        source = self.root / "app/source.txt"
        original = source.read_bytes()
        for kind in ("tracked", "staged", "untracked", "deleted"):
            with self.subTest(kind=kind):
                if kind == "untracked":
                    (source.parent / "extra.txt").write_bytes(b"untracked")
                elif kind == "deleted":
                    source.unlink()
                else:
                    source.write_bytes(b"changed\n")
                    if kind == "staged":
                        git(source.parent, "add", "source.txt")
                self.assert_refuses_without_writes("refresh_child_dirty")
                if kind == "untracked":
                    (source.parent / "extra.txt").unlink()
                source.write_bytes(original)
                git(source.parent, "add", "source.txt")

    def test_configured_untracked_suppression_does_not_hide_dirt(self):
        child = self.root / "app"
        git(child, "config", "status.showUntrackedFiles", "no")
        (child / "extra.txt").write_bytes(b"foreign")
        self.assert_refuses_without_writes("refresh_child_dirty")

    def test_hydrated_lfs_is_preserved_and_revalidated_without_filters(self):
        child = self.root / "app"
        payload = b"binary asset\x00\xff"
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{hashlib.sha256(payload).hexdigest()}\n"
            f"size {len(payload)}\n"
        ).encode()
        asset = child / "asset.dat"
        asset.write_bytes(pointer)
        (child / ".gitattributes").write_text("asset.dat filter=lfs -text\n")
        git(child, "add", "asset.dat", ".gitattributes")
        git(child, "commit", "-qm", "LFS asset")
        asset.write_bytes(payload)
        before = snapshot(self.base)
        prepared = self.prepare()
        refresh.require_repository_refresh_inputs_unchanged(prepared)
        self.assertEqual(snapshot(self.base), before)
        asset.write_bytes(bytes((payload[0] ^ 1,)) + payload[1:])
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(prepared)
        self.assert_refuses_without_writes("refresh_child_dirty")
        asset.write_bytes(payload + b"wrong size")
        self.assert_refuses_without_writes("refresh_child_dirty")
        asset.unlink()
        self.assert_refuses_without_writes("refresh_child_dirty")
        asset.mkdir()
        self.assert_refuses_without_writes("refresh_child_dirty")
        asset.rmdir()
        asset.write_bytes(pointer)
        pointer_prepared = self.prepare()
        self.assertEqual(pointer_prepared.children[0].hydrated_lfs, ())

    def test_malformed_indexed_lfs_pointer_cannot_admit_payload(self):
        child = self.root / "app"
        asset = child / "asset.dat"
        asset.write_bytes(b"not an LFS pointer\n")
        (child / ".gitattributes").write_text("asset.dat filter=lfs -text\n")
        git(child, "add", "asset.dat", ".gitattributes")
        git(child, "commit", "-qm", "Malformed LFS pointer")
        asset.write_bytes(b"hydrated-looking payload")
        self.assert_refuses_without_writes("refresh_child_dirty")

    def test_canonical_lfs_filters_are_inert_and_exact_configuration_is_bound(self):
        child = self.root / "app"
        payload = b"installed LFS payload\x00\xff"
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{hashlib.sha256(payload).hexdigest()}\n"
            f"size {len(payload)}\n"
        ).encode()
        asset = child / "asset.dat"
        asset.write_bytes(pointer)
        (child / ".gitattributes").write_text("asset.dat filter=lfs -text\n")
        git(child, "add", "asset.dat", ".gitattributes")
        git(child, "commit", "-qm", "LFS pointer")

        lfs_sentinel = child / "lfs-filter-ran"
        cat_sentinel = child / "cat-filter-ran"
        binary = self.base / "adversarial-filter"
        binary.mkdir()
        self.command_sentinel(binary, "git-lfs", lfs_sentinel)
        self.command_sentinel(binary, "cat", cat_sentinel)
        git(child, "config", "filter.lfs.clean", "git-lfs clean -- %f")
        git(child, "config", "filter.lfs.process", "git-lfs filter-process")
        git(child, "config", "filter.lfs.required", "true")
        global_configuration = self.base / "global.gitconfig"
        global_configuration.write_text(
            '[filter "lfs"]\n'
            "\tclean = git-lfs clean -- %f\n"
            "\tprocess = git-lfs filter-process\n"
            "\trequired = true\n",
            encoding="utf-8",
        )
        environment = {
            "GIT_CONFIG_GLOBAL": str(global_configuration),
            "PATH": str(binary) + os.pathsep + os.environ.get("PATH", ""),
        }
        with patch.dict(os.environ, environment):
            pointer_prepared = self.prepare()
            self.assertEqual(pointer_prepared.children[0].hydrated_lfs, ())
            asset.write_bytes(payload)
            hydrated = self.prepare()
            self.assertEqual(
                tuple(item.path for item in hydrated.children[0].hydrated_lfs),
                ("asset.dat",),
            )
            refresh.require_repository_refresh_inputs_unchanged(hydrated)
            git(child, "config", "filter.lfs.required", "false")
            with self.assertRaises(OrchestrationInventoryError):
                refresh.require_repository_refresh_inputs_unchanged(hydrated)
        self.assertFalse(lfs_sentinel.exists())
        self.assertFalse(cat_sentinel.exists())

    def test_hydrated_lfs_identity_drift_is_not_admitted_as_false_observation(self):
        child = self.root / "app"
        payload = b"identity-bound asset"
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{hashlib.sha256(payload).hexdigest()}\n"
            f"size {len(payload)}\n"
        ).encode()
        asset = child / "asset.dat"
        asset.write_bytes(pointer)
        (child / ".gitattributes").write_text("asset.dat filter=lfs -text\n")
        git(child, "add", "asset.dat", ".gitattributes")
        git(child, "commit", "-qm", "LFS identity")
        asset.write_bytes(payload)
        entry = b" M asset.dat"
        with patch.object(repository_lfs, "_identity_matches", return_value=False):
            self.assertIsNone(repository_lfs.observe_hydrated_lfs_entry(child, entry))
            self.assertFalse(repository_lfs.hydrated_lfs_entry(child, entry))
            self.assert_refuses_without_writes("refresh_child_dirty")

    def test_lfs_match_does_not_excuse_staged_or_executable_changes(self):
        child = self.root / "app"
        payload = b"binary asset"
        asset = child / "asset.dat"
        asset.write_bytes(
            (
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{hashlib.sha256(payload).hexdigest()}\n"
                f"size {len(payload)}\n"
            ).encode("ascii")
        )
        (child / ".gitattributes").write_text("asset.dat filter=lfs -text\n")
        git(child, "add", "asset.dat", ".gitattributes")
        git(child, "commit", "-qm", "LFS asset")
        asset.write_bytes(payload)
        if os.name != "nt":
            asset.chmod(0o755)
            self.assert_refuses_without_writes("refresh_child_dirty")
            asset.chmod(0o644)
        (child / "source.txt").write_bytes(b"staged work\n")
        git(child, "add", "source.txt")
        self.assert_refuses_without_writes("refresh_child_dirty")

    def test_assume_unchanged_and_skip_worktree_cannot_hide_modified_files(self):
        child = self.root / "app"
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                git(child, "update-index", "--" + flag, "source.txt")
                self.assert_refuses_without_writes("refresh_hidden_changes")
                git(child, "update-index", "--no-" + flag, "source.txt")

    def test_configured_content_filters_refuse_before_status_execution(self):
        child = self.root / "app"
        call = refresh._git
        for field in ("clean", "process"):
            with self.subTest(field=field):
                git(child, "config", "filter.fixture." + field, "must-not-run")

                def guard(root, *arguments):
                    if root == child and "status" in arguments:
                        self.fail("status must not run with a configured filter")
                    return call(root, *arguments)

                with patch.object(refresh, "_git", side_effect=guard):
                    self.assert_refuses_without_writes("refresh_filter_unsupported")
                git(child, "config", "--unset", "filter.fixture." + field)

    def test_noncanonical_lfs_clean_and_process_filters_refuse(self):
        child = self.root / "app"
        for field, value in (
            ("clean", "git-lfs clean --"),
            ("process", "git-lfs filter-process --unexpected"),
        ):
            with self.subTest(field=field):
                git(child, "config", "filter.lfs." + field, value)
                self.assert_refuses_without_writes("refresh_filter_unsupported")
                git(child, "config", "--unset", "filter.lfs." + field)

    def test_filter_section_and_variable_case_follow_git_semantics(self):
        raw = (
            b"FILTER.lfs.CLEAN\ngit-lfs clean -- %f\0"
            b"filter.lfs.ProCeSs\ngit-lfs filter-process\0"
        )
        with patch.object(refresh, "_git", return_value=raw):
            self.assertEqual(
                refresh._configuration(self.root / "app", clean=True),
                "sha256:" + hashlib.sha256(raw).hexdigest(),
            )

    def test_mixed_case_lfs_subsection_refuses_before_its_sentinel_can_run(self):
        child = self.root / "app"
        (child / ".gitattributes").write_text("source.txt filter=LFS\n")
        git(child, "add", ".gitattributes")
        git(child, "commit", "-qm", "Mixed-case filter fixture")
        (child / "source.txt").write_bytes(b"changed fixture\n")
        sentinel = child / "mixed-lfs-filter-ran"
        binary = self.base / "mixed-case-filter"
        binary.mkdir()
        self.command_sentinel(binary, "git-lfs", sentinel)
        git(child, "config", "filter.LFS.clean", "git-lfs clean -- %f")
        git(child, "config", "filter.LFS.process", "git-lfs filter-process")
        git(child, "config", "filter.LFS.required", "true")
        with patch.dict(
            os.environ,
            {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")},
        ):
            self.assert_refuses_without_writes("refresh_filter_unsupported")
        self.assertFalse(sentinel.exists())

    def test_real_child_clean_filter_cannot_execute_during_inspection(self):
        child = self.root / "app"
        (child / ".gitattributes").write_bytes(b"source.txt filter=fixture\n")
        (child / "filter.py").write_bytes(
            b"import pathlib, sys\n"
            b"pathlib.Path('filter-ran').write_bytes(b'observed')\n"
            b"sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        )
        git(child, "add", ".gitattributes", "filter.py")
        git(child, "commit", "-q", "-m", "filter fixture")
        git(
            child,
            "config",
            "filter.fixture.clean",
            shlex.quote(sys.executable) + " filter.py",
        )
        (child / "source.txt").write_bytes(b"changed fixture\n")
        self.assert_refuses_without_writes("refresh_filter_unsupported")
        self.assertFalse((child / "filter-ran").exists())

    def test_missing_child_refuses_without_initializing_it(self):
        child = self.root / "lib"
        child.rename(self.base / "retained-lib")
        self.assert_refuses_without_writes("refresh_child_missing")

    def test_exact_index_and_manifest_bytes_are_bound_even_without_semantic_drift(self):
        initial = self.prepare()
        manifest = self.root / "literate.project.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(initial)
        initial = self.prepare()
        (self.root / "unrelated.txt").write_bytes(b"root-owned work")
        git(self.root, "add", "unrelated.txt")
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(initial)

    def test_lock_creation_and_same_content_replacement_invalidate_custody(self):
        initial = self.prepare()
        store = RepositoryLockStore(self.root)
        store.update(initial.repository)
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(initial)
        initial = self.prepare()
        replacement = store.path.with_name("replacement.json")
        replacement.write_bytes(store.path.read_bytes())
        os.replace(replacement, store.path)
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(initial)

    def test_malformed_repository_lock_refuses(self):
        RepositoryLockStore(self.root).path.write_bytes(b"not a lock")
        self.assert_refuses_without_writes("lock_invalid")

    def test_root_and_child_index_writer_markers_are_preserved(self):
        for root in (self.root, self.root / "app"):
            with self.subTest(root=root.name):
                marker = root / ".git/index.lock"
                marker.write_bytes(b"foreign writer")
                self.assert_refuses_without_writes("refresh_busy")
                marker.unlink()
        marker = RepositoryLockStore(self.root).writer_path
        marker.write_bytes(b"foreign writer")
        self.assert_refuses_without_writes("refresh_busy")

    def test_in_progress_git_operations_refuse_even_with_clean_source(self):
        for name in ("HEAD.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge"):
            with self.subTest(name=name):
                marker = self.root / "app/.git" / name
                if name == "rebase-merge":
                    marker.mkdir()
                else:
                    marker.write_bytes(b"foreign Git operation")
                self.assert_refuses_without_writes("refresh_busy")
                if marker.is_dir():
                    marker.rmdir()
                else:
                    marker.unlink()

    def test_clean_child_head_and_branch_changes_invalidate_observation(self):
        initial = self.prepare()
        child = self.root / "app"
        git(child, "checkout", "-q", "-b", "other")
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(initial)
        initial = self.prepare()
        git(child, "commit", "-q", "--allow-empty", "-m", "new revision")
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(initial)

    def test_reobservation_rejects_late_foreign_changes_without_repairing_them(self):
        observe = refresh._observe_checked
        calls = 0
        foreign = self.root / "app/foreign.txt"

        def racing(root, request, reservations=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                foreign.write_bytes(b"do not undo")
            return observe(root, request, reservations)

        with patch.object(refresh, "_observe_checked", side_effect=racing):
            with self.assertRaises(OrchestrationInventoryError):
                self.prepare()
        self.assertEqual(foreign.read_bytes(), b"do not undo")

    def test_absorbed_child_git_directory_is_observed_without_writes(self):
        git(self.root, "submodule", "absorbgitdirs", "app")
        self.assertTrue((self.root / "app/.git").is_file())
        before = snapshot(self.base)
        prepared = self.prepare()
        self.assertTrue(prepared.children[0].git_directory.is_relative_to(self.root))
        self.assertEqual(snapshot(self.base), before)

    def test_linked_child_worktree_is_observed_without_writes(self):
        child = self.root / "app"
        original = self.base / "original-app"
        child.rename(original)
        git(original, "worktree", "add", "--detach", str(child), "HEAD")
        before = snapshot(self.base)
        prepared = self.prepare()
        self.assertNotEqual(
            prepared.children[0].git_directory, prepared.children[0].common_directory
        )
        self.assertEqual(snapshot(self.base), before)

    def test_split_indexes_refuse_before_git_can_change_shared_index_timestamps(self):
        for root in (self.root / "app", self.root):
            with self.subTest(root=root.name):
                git(root, "update-index", "--split-index")
                self.assert_refuses_without_writes("split_index_unsupported")
                git(root, "update-index", "--no-split-index")

    def test_untyped_inputs_refuse(self):
        with self.assertRaises(TypeError):
            refresh.prepare_repository_refresh(self.root, self.request.to_dict())
        with self.assertRaises(TypeError):
            refresh.require_repository_refresh_inputs_unchanged({})

    def nested_child(self):
        app = self.root / "app"
        nested = app / "nested"
        git(
            self.base,
            "clone",
            "-q",
            "--no-hardlinks",
            str(self.root / "lib"),
            str(nested),
        )
        pin = git(nested, "rev-parse", "HEAD").decode().strip()
        (app / ".gitmodules").write_bytes(
            b'[submodule "nested"]\npath = nested\nurl = ../nested.git\n'
        )
        git(app, "add", ".gitmodules")
        git(app, "update-index", "--add", "--cacheinfo", "160000", pin, "nested")
        git(app, "commit", "-q", "-m", "nested child")
        git(app, "config", "submodule.nested.ignore", "all")
        return nested

    def test_nested_children_are_checked_despite_parent_ignore_configuration(self):
        nested = self.nested_child()
        before = snapshot(self.base)
        prepared = self.prepare()
        self.assertEqual(len(prepared.children), 3)
        self.assertEqual(snapshot(self.base), before)
        (nested / "foreign.txt").write_bytes(b"nested foreign work")
        self.assert_refuses_without_writes("refresh_child_dirty")

    def test_uninitialized_nested_child_remains_outside_refresh_boundary(self):
        nested = self.nested_child()
        shutil.rmtree(nested)
        self.assert_refuses_without_writes("refresh_child_missing")

    def test_nested_filters_are_rejected_without_recursive_status_execution(self):
        nested = self.nested_child()
        git(nested, "config", "filter.nested.clean", "must-not-run")
        self.assert_refuses_without_writes("refresh_filter_unsupported")

    def test_nested_gitlink_head_drift_refuses(self):
        nested = self.nested_child()
        git(
            nested,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "unbound nested revision",
        )
        self.assert_refuses_without_writes("refresh_child_dirty")

    def test_root_replacement_with_identical_files_is_not_original_custody(self):
        initial = self.prepare()
        original = self.base / "original-super"
        self.root.rename(original)
        shutil.copytree(original, self.root)
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError):
            refresh.require_repository_refresh_inputs_unchanged(initial)
        self.assertEqual(snapshot(self.base), before)

    def test_indirect_manifest_is_refused_without_repair(self):
        manifest = self.root / "literate.project.json"
        original = self.base / "manifest.json"
        manifest.rename(original)
        try:
            manifest.symlink_to(original)
        except OSError:
            self.skipTest("host does not permit symlink fixture creation")
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError):
            self.prepare()
        self.assertEqual(snapshot(self.base), before)

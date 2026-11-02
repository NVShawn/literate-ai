"""Real Git ownership discovery preserves every checkout and refuses drift."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import project_worktree_discovery as discovery
from literate_ai.adapters import repository_orchestration as orchestration
from literate_ai.adapters.project_worktrees import ProjectWorktreeError
from literate_ai.cli import main
from tests.support.symlinks import skip_unless_symlinks_followable


def _rewrite_control_file(path, content):
    # Git for Windows hides .git pointer files. Opening an existing hidden file
    # with truncation can fail; r+b preserves its native attributes and inode,
    # which is also required by the in-place custody-drift control below.
    with path.open("r+b") as stream:
        stream.write(content)
        stream.truncate()


class WorktreeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "project"
        self.initialize(self.root)

    def git(self, root, *arguments):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
        )
        return subprocess.run(
            (
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                *arguments,
            ),
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout

    def initialize(self, path):
        path.mkdir()
        self.git(path, "init", "-q")
        (path / "source").write_bytes(b"original source\n")
        self.git(path, "add", "source")
        self.git(path, "commit", "-qm", "initial")

    def linked(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
        return path

    def entries(self, report):
        return {entry.path: entry for entry in report.entries}

    def test_ownership_uses_git_identity_and_never_a_directory_name(self):
        legacy = self.linked(self.parent / "task with spaces")
        managed = self.linked(self.root / ".worktrees" / "managed")
        independent = self.parent / "project-lookalike"
        self.initialize(independent)
        ordinary = self.parent / "project-old"
        ordinary.mkdir()
        (self.parent / "notes").write_bytes(b"operator notes")
        bare = self.parent / "bare-clone"
        self.git(self.parent, "clone", "--bare", "-q", str(self.root), str(bare))
        report = discovery.discover_project_worktrees(self.root)
        entries = self.entries(report)
        for path, classification in (
            (self.root, "canonical-checkout"),
            (legacy, "legacy-peer-worktree"),
            (managed, "project-worktree"),
            (independent, "independent-repository"),
            (ordinary, "ordinary-peer-directory"),
            (self.parent / "notes", "ordinary-peer-file"),
            (bare, "independent-bare-repository"),
        ):
            with self.subTest(path=path.name):
                self.assertEqual(entries[path].classification, classification)
                self.assertTrue(path.exists())
        self.assertEqual(entries[legacy].common_directory, self.root / ".git")
        self.assertEqual(entries[independent].common_directory, independent / ".git")
        self.assertFalse(report.to_dict()["audit_complete"])
        self.assertFalse(report.to_dict()["retirement_authorized"])
        self.assertTrue(report.identity.uri.startswith("sha256:"))

    def test_discovery_does_not_create_roots_or_change_dirty_ignored_or_index_bytes(
        self,
    ):
        (self.root / ".gitignore").write_text("ignored\n")
        (self.root / "ignored").write_bytes(b"retained ignored evidence")
        (self.root / "source").write_bytes(b"staged source")
        self.git(self.root, "add", "source")
        (self.root / "source").write_bytes(b"working source")
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        index = hashlib.sha256((self.root / ".git/index").read_bytes()).digest()
        first = discovery.discover_project_worktrees(self.root)
        second = discovery.discover_project_worktrees(self.root)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        )
        self.assertEqual(
            index, hashlib.sha256((self.root / ".git/index").read_bytes()).digest()
        )
        self.assertFalse((self.root / ".worktrees").exists())

    def test_external_placement_and_locked_detached_state_are_preserved(self):
        outside = self.parent / "operator-trees" / "owned"
        self.linked(outside)
        self.git(
            self.root, "worktree", "lock", str(outside), "--reason", "retained evidence"
        )
        entry = self.entries(discovery.discover_project_worktrees(self.root))[outside]
        self.assertEqual(entry.classification, "registered-external-worktree")
        self.assertTrue(entry.locked)
        self.assertIsNone(entry.registered_reference)
        self.assertTrue(outside.exists())

    def test_missing_registration_target_remains_reported_without_pruning(self):
        absent = self.linked(self.parent / "missing")
        shutil.rmtree(absent)
        before = self.git(self.root, "worktree", "list", "--porcelain", "-z")
        entry = self.entries(discovery.discover_project_worktrees(self.root))[absent]
        self.assertEqual(entry.classification, "missing-registered-checkout")
        self.assertTrue(entry.prunable)
        self.assertEqual(
            before, self.git(self.root, "worktree", "list", "--porcelain", "-z")
        )

    def test_unregistered_shared_git_directory_is_not_admitted_as_a_worktree(self):
        linked = self.linked(self.parent / "registered")
        ghost = self.parent / "unregistered"
        ghost.mkdir()
        (ghost / ".git").write_bytes((linked / ".git").read_bytes())
        entry = self.entries(discovery.discover_project_worktrees(self.root))[ghost]
        self.assertEqual(entry.classification, "unregistered-shared-repository")
        self.assertFalse(entry.registered)

    def test_contradictory_registration_does_not_establish_ownership(self):
        linked = self.linked(self.parent / "registered")
        foreign = self.parent / "foreign"
        self.initialize(foreign)
        _rewrite_control_file(linked / ".git", f"gitdir: {foreign / '.git'}\n".encode())
        entry = self.entries(discovery.discover_project_worktrees(self.root))[linked]
        self.assertEqual(entry.classification, "contradictory-registration")

    def test_same_common_directory_pointer_swap_is_contradictory(self):
        first = self.linked(self.parent / "first")
        second = self.linked(self.parent / "second")
        _rewrite_control_file(first / ".git", (second / ".git").read_bytes())
        entries = self.entries(discovery.discover_project_worktrees(self.root))
        self.assertEqual(entries[first].classification, "contradictory-registration")
        self.assertEqual(entries[second].classification, "legacy-peer-worktree")
        wire = entries[first].to_dict()
        self.assertIn("registered_commit", wire)
        self.assertNotIn("commit", wire)

    def test_custody_byte_bound_inside_common_is_not_an_inspection_fallback(self):
        original = discovery._Observation
        for with_peer in (False, True):
            if with_peer:
                self.initialize(self.parent / "independent")
            observed = []

            class TrackingObservation(original):
                def __init__(self, instances=observed):
                    super().__init__()
                    instances.append(self)

            with mock.patch.object(discovery, "_Observation", TrackingObservation):
                discovery.discover_project_worktrees(self.root)
            limit = observed[0].path_bytes - 1
            with (
                self.subTest(with_peer=with_peer),
                mock.patch.object(discovery, "_MAX_PATH_BYTES", limit),
            ):
                with self.assertRaisesRegex(
                    ProjectWorktreeError, "custody paths.*byte bound"
                ):
                    discovery.discover_project_worktrees(self.root)

    def test_link_like_peer_is_reported_without_following_it(self):
        skip_unless_symlinks_followable(self, directory=self.parent)
        alias = self.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        entry = self.entries(discovery.discover_project_worktrees(self.root))[alias]
        self.assertEqual(entry.classification, "link-like-peer")
        self.assertIsNone(entry.common_directory)

    def test_peer_membership_change_refuses_an_incomplete_observation(self):
        collect = discovery._peer_paths
        calls = 0

        def changing(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.parent / "arrived").write_bytes(b"new peer")
            return collect(*args)

        with mock.patch.object(discovery, "_peer_paths", side_effect=changing):
            with self.assertRaisesRegex(ProjectWorktreeError, "membership changed"):
                discovery.discover_project_worktrees(self.root)

    def test_in_place_git_marker_change_refuses_stale_custody(self):
        linked = self.linked(self.parent / "registered")
        revalidate = discovery._Observation.revalidate

        def changing(observed):
            marker = linked / ".git"
            _rewrite_control_file(marker, marker.read_bytes() + b"\n")
            return revalidate(observed)

        with mock.patch.object(discovery._Observation, "revalidate", changing):
            with self.assertRaisesRegex(ProjectWorktreeError, "custody changed"):
                discovery.discover_project_worktrees(self.root)

    def test_per_worktree_common_directory_change_refuses_stale_custody(self):
        linked = self.linked(self.parent / "registered")
        administration = Path(
            self.git(linked, "rev-parse", "--absolute-git-dir").decode().strip()
        )
        revalidate = discovery._Observation.revalidate

        def changing(observed):
            marker = administration / "commondir"
            _rewrite_control_file(marker, marker.read_bytes() + b"\n")
            return revalidate(observed)

        with mock.patch.object(discovery._Observation, "revalidate", changing):
            with self.assertRaisesRegex(ProjectWorktreeError, "custody changed"):
                discovery.discover_project_worktrees(self.root)

    def test_changed_registered_head_refuses_the_old_observation(self):
        resolve = discovery.resolve_project_worktree_location
        calls = 0

        def changing(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.git(self.root, "commit", "--allow-empty", "-qm", "concurrent")
            return resolve(*args, **kwargs)

        with mock.patch.object(
            discovery, "resolve_project_worktree_location", side_effect=changing
        ):
            with self.assertRaisesRegex(ProjectWorktreeError, "registration changed"):
                discovery.discover_project_worktrees(self.root)

    def test_bounds_and_global_deadline_refuse_without_removal(self):
        for bound in (True, 0, 4097, 1.5):
            with self.subTest(bound=bound), self.assertRaises(ProjectWorktreeError):
                discovery.discover_project_worktrees(
                    self.root, maximum_peer_entries=bound
                )
        (self.parent / "extra").mkdir()
        with self.assertRaisesRegex(ProjectWorktreeError, "entry bound"):
            discovery.discover_project_worktrees(self.root, maximum_peer_entries=1)
        with mock.patch.object(discovery, "_MAX_PATH_BYTES", 1):
            with self.assertRaisesRegex(ProjectWorktreeError, "byte bound"):
                discovery.discover_project_worktrees(self.root)
        with mock.patch.object(discovery.time, "monotonic", side_effect=(0, 61)):
            with self.assertRaisesRegex(ProjectWorktreeError, "time bound"):
                discovery.discover_project_worktrees(self.root)
        self.assertTrue(self.root.exists())

    def test_public_cli_reports_discovery_without_retirement_authority(self):
        output, errors = io.StringIO(), io.StringIO()
        status = main(
            ["--json", "project", "worktree", "discover", str(self.root)],
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(status, 0, errors.getvalue())
        result = json.loads(output.getvalue())["result"]
        self.assertEqual(result["schema"], "literate-ai/project-worktree-discovery@1")
        self.assertFalse(result["retirement_authorized"])

    def test_shared_git_timeout_can_only_be_tightened(self):
        for timeout in (True, 0, 61, float("nan")):
            with (
                self.subTest(timeout=timeout),
                mock.patch.object(orchestration, "run_bounded_process") as run,
            ):
                with self.assertRaises(orchestration.OrchestrationInventoryError):
                    orchestration._git(self.root, "status", timeout_seconds=timeout)
                run.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "native POSIX FIFO replacement")
    def test_marker_replaced_by_fifo_refuses_without_blocking(self):
        marker = self.parent / "custody-pointer"
        marker.write_text("target\n")
        source = r"""
import os, sys
from pathlib import Path
from unittest.mock import patch
from literate_ai.adapters.project_worktree_discovery import _Observation
from literate_ai.adapters.project_worktrees import ProjectWorktreeError
marker = Path(sys.argv[1])
original = os.open
def replaced(path, flags, *args, **kwargs):
    if Path(path) == marker:
        marker.unlink()
        os.mkfifo(marker)
    return original(path, flags, *args, **kwargs)
try:
    with patch.object(os, "open", replaced):
        _Observation().pointer(marker)
except ProjectWorktreeError:
    print("refused")
else:
    raise AssertionError("FIFO replacement supplied pointer custody")
"""
        completed = subprocess.run(
            (sys.executable, "-c", source, str(marker)),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "refused")


if __name__ == "__main__":
    unittest.main()

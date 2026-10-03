"""Physical permission admission and subsequent drift during a no-op refresh."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_application as application
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy
from tests.unit.test_repository_orchestration import git, repository
from tests.unit.test_repository_tree import tree


@unittest.skipIf(os.name == "nt", "POSIX physical permissions")
class RefreshNoopModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.write_bytes(b"source\n")
        self.source.chmod(0o664)
        (self.root / "directory").mkdir(mode=0o775)
        (self.root / "directory" / "child").write_bytes(b"child\n")
        self.snapshot = tree(
            {
                "source": ("100644", b"source\n"),
                "directory/child": ("100644", b"child\n"),
            }
        )
        node = self.root.stat()
        self.plan = SimpleNamespace(
            root=self.root,
            root_node=(node.st_dev, node.st_ino, node.st_mode),
            previous=self.snapshot,
            prospective=self.snapshot,
            changes=(),
            directories=(),
            policy=RepositoryTreeCapturePolicy(),
        )
        observation = SimpleNamespace(root=self.root, hydrated_lfs=())
        self.app = SimpleNamespace(
            owner=SimpleNamespace(
                _prepared=SimpleNamespace(
                    refresh=SimpleNamespace(children=(observation,))
                )
            ),
            _retained_custody=(),
            _noop_states={},
        )

    def check(self, prospective=False):
        application._require_worktree_state(
            self.app, self.plan, prospective=prospective
        )

    def test_group_writable_noop_retains_exact_permissions(self):
        self.check()
        self.check(prospective=True)
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o664)

    def test_permission_drift_after_admission_is_rejected(self):
        self.check()
        self.source.chmod(0o644)
        with self.assertRaises(OrchestrationInventoryError):
            self.check(prospective=True)

    def test_directory_permission_drift_after_admission_is_rejected(self):
        self.check()
        (self.root / "directory").chmod(0o700)
        with self.assertRaises(OrchestrationInventoryError):
            self.check(prospective=True)

    def test_wrong_executable_intent_is_rejected_on_entry(self):
        self.source.chmod(0o775)
        with self.assertRaises(OrchestrationInventoryError):
            self.check()

    def test_wrong_content_is_rejected_on_entry(self):
        self.source.write_bytes(b"foreign\n")
        with self.assertRaises(OrchestrationInventoryError):
            self.check()


@unittest.skipIf(os.name == "nt", "POSIX physical permissions")
class RefreshPhysicalModeIntegrationTests(unittest.TestCase):
    def test_refresh_accepts_admitted_group_writable_permissions(self):
        from tests.unit import test_repository_refresh_application as fixtures

        fixture = fixtures.RepositoryRefreshApplicationTests()

        def initial_repository(root):
            repository(root)
            (root / "unchanged.txt").write_bytes(b"unchanged\n")
            git(root, "add", "unchanged.txt")
            git(root, "commit", "-q", "-m", "initial unchanged member")

        with patch(
            "tests.unit.test_orchestration_planning.repository",
            side_effect=initial_repository,
        ):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        unchanged = fixture.child / "unchanged.txt"
        unchanged_content = unchanged.read_bytes()
        target = fixture.publish()
        (fixture.child / "source.txt").chmod(0o664)
        unchanged.chmod(0o664)
        prepared = fixture.prepared(target)
        with fixture.harness.acquire(prepared) as owned:
            with fixture.stage(owned) as stage:
                result = application.apply_repository_refresh(stage)
                self.assertEqual(result.state, "committed")
        self.assertEqual(stat.S_IMODE(unchanged.stat().st_mode), 0o664)
        self.assertEqual(unchanged.read_bytes(), unchanged_content)
        self.assertEqual((fixture.child / "source.txt").read_bytes(), b"prospective\n")
        self.assertEqual(
            stat.S_IMODE((fixture.child / "source.txt").stat().st_mode), 0o644
        )

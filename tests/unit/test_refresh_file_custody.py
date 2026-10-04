"""Native physical preflight remains bound to live reviewed refresh ownership."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters import repository_refresh_files as files_adapter
from literate_ai.adapters import repository_refresh_ownership as ownership
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_files import PreparedRefreshFiles
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy
from tests.support import fixtures_test_repository_refresh_publication as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot
from tests.support.fixtures_test_repository_tree import tree


class RefreshFileCustodyTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RefreshPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def acquire(self, prepared):
        return ownership.reserve_published_refresh(
            prepared,
            expected_custody_identity=ownership.refresh_publication_custody_identity(
                prepared
            ),
            acknowledge=True,
        )

    def publish(self, path, content):
        fixture = self.fixture
        (fixture.child / path).write_bytes(content)
        git(fixture.child, "add", path)
        git(fixture.child, "commit", "-q", "-m", "prospective change")
        target = git(fixture.child, "rev-parse", "HEAD").decode().strip()
        git(fixture.child, "push", fixture.remote.as_uri(), "HEAD:refs/heads/main")
        git(fixture.child, "checkout", "-q", fixture.pin)
        return target

    def test_physical_crlf_custody_is_nonmutating_and_expires_with_ownership(self):
        fixture = self.fixture
        # The physical before-tree belongs to observed HEAD, not the root pin.
        declared_pin = fixture.pin
        git(fixture.child, "commit", "--allow-empty", "-q", "-m", "local before")
        fixture.pin = git(fixture.child, "rev-parse", "HEAD").decode().strip()
        target = self.publish("source.txt", b"new\n")
        git(fixture.child, "config", "core.autocrlf", "true")
        (fixture.child / "source.txt").write_bytes(b"original\r\n")
        # Establish a clean CRLF checkout before read-only custody begins.
        git(fixture.child, "-c", "core.autocrlf=true", "add", "source.txt")
        prepared = fixture.prepare(fixture.inputs(target))
        before = snapshot(fixture.base)
        with self.acquire(prepared) as owned:
            files = owned.prepare_filesystem_changes()
            self.assertEqual(tuple(path for path, _ in files.plans), ("app",))
            plan = files.plans[0][1]
            self.assertEqual(plan.previous.commit, fixture.pin)
            self.assertNotEqual(plan.previous.commit, declared_pin)
            self.assertEqual(plan.prospective.commit, target)
            self.assertEqual(plan.nodes[0].content, b"original\r\n")
            self.assertEqual(files.identity, files.identity)
            files.require_current()
        self.assertEqual(snapshot(fixture.base), before)
        with patch.object(
            ownership,
            "capture_published_repository_tree",
            side_effect=AssertionError("transport"),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                files.require_current()
        with self.assertRaises(TypeError):
            PreparedRefreshFiles(None, owned, files.plans)

    def test_ignored_incoming_collision_is_preserved(self):
        fixture = self.fixture
        target = self.publish("incoming", b"published")
        (fixture.child / ".git/info/exclude").write_bytes(b"incoming\n")
        foreign = fixture.child / "incoming"
        foreign.write_bytes(b"private ignored content")
        prepared = fixture.prepare(fixture.inputs(target))
        before = snapshot(fixture.base)
        with self.acquire(prepared) as owned:
            with self.assertRaises(OrchestrationInventoryError) as caught:
                owned.prepare_filesystem_changes()
            self.assertEqual(
                caught.exception.code, "orchestration.refresh_files_collision"
            )
            owned.require_inputs_unchanged()
        self.assertEqual(foreign.read_bytes(), b"private ignored content")
        self.assertEqual(snapshot(fixture.base), before)


class RefreshFileBudgetTests(unittest.TestCase):
    def test_aggregate_old_objects_and_physical_records_refuse_partial_results(self):
        previous = tree({"source": ("100644", b"x")})
        prospective = tree({"source": ("100644", b"y")})
        root = Path.cwd()
        owner = Mock(spec=ownership.OwnedRefreshPublication)
        owner._prepared = SimpleNamespace(
            target_modes=(
                ("a", "source-transition"),
                ("b", "source-transition"),
            ),
            refresh=SimpleNamespace(
                repository=SimpleNamespace(root=root),
                children=tuple(
                    SimpleNamespace(
                        root=root / name, root_node=(1, 2, 3), commit=previous.commit
                    )
                    for name in ("a", "b")
                ),
            ),
        )
        owner.capture_prospective_trees.return_value = tuple(
            (name, SimpleNamespace(tree=prospective)) for name in ("a", "b")
        )
        # Deliberately over-return at the lower boundary: the owner must still
        # enforce the aggregate, not assume each child's runner did so.
        cases = (
            (
                RepositoryTreeCapturePolicy(maximum_entries=1),
                0,
                0,
                "refresh_before_limit",
            ),
            (
                RepositoryTreeCapturePolicy(maximum_total_blob_bytes=1),
                0,
                0,
                "refresh_before_limit",
            ),
            (
                RepositoryTreeCapturePolicy(
                    maximum_metadata_bytes=previous.metadata_size_bound * 2 - 1
                ),
                0,
                0,
                "refresh_before_limit",
            ),
            (
                RepositoryTreeCapturePolicy(maximum_total_blob_bytes=3),
                2,
                0,
                "refresh_files_limit",
            ),
            (
                RepositoryTreeCapturePolicy(maximum_entries=3),
                0,
                2,
                "refresh_files_limit",
            ),
        )
        for policy, byte_count, member_count, suffix in cases:
            with self.subTest(suffix=suffix, policy=policy):
                plan = SimpleNamespace(
                    physical_bytes=byte_count,
                    directories=(SimpleNamespace(members=(None,) * member_count),),
                )
                with (
                    patch.object(
                        files_adapter,
                        "capture_local_repository_tree",
                        return_value=previous,
                    ),
                    patch.object(
                        files_adapter, "prepare_worktree_changes", return_value=plan
                    ),
                    self.assertRaises(OrchestrationInventoryError) as caught,
                ):
                    files_adapter.prepare_refresh_files(owner, policy=policy)
                self.assertEqual(caught.exception.code, "orchestration." + suffix)
        self.assertTrue(owner.require_inputs_unchanged.called)

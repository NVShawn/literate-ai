"""Reviewed refresh planning and refusal contracts over real Git repositories."""

from __future__ import annotations

import unittest

from literate_ai.adapters import repository_refresh_planning as planning
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_lineage import RepositoryFetchDeadlinePolicy
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support import fixtures_test_refresh_file_custody as fixtures
from tests.support.fixtures_test_repository_orchestration import snapshot


class RepositoryRefreshPlanningTests(unittest.TestCase):
    def setUp(self):
        harness = fixtures.RefreshFileCustodyTests()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.harness = harness
        self.source = harness.fixture
        self.policy = RepositoryFetchDeadlinePolicy(120, 60, 10)

    def request(self, commit):
        return RepositoryRefreshRequest((RepositoryRefreshTarget("app", commit),))

    def assert_refuses(self, suffix, action):
        with self.assertRaises(OrchestrationInventoryError) as caught:
            action()
        self.assertEqual(caught.exception.code, "orchestration." + suffix)

    def test_unpublished_and_dirty_children_refuse_without_root_writes(self):
        unpublished = self.source.advance(publish=False)
        before = snapshot(self.source.base)
        self.assert_refuses(
            "publication_unpublished",
            lambda: planning.plan_repository_refresh(
                self.source.root,
                self.request(unpublished),
                deadline_policy=self.policy,
            ),
        )
        self.assertEqual(snapshot(self.source.base), before)
        (self.source.child / "source.txt").write_bytes(b"foreign dirty work\n")
        before = snapshot(self.source.base)
        self.assert_refuses(
            "refresh_child_dirty",
            lambda: planning.plan_repository_refresh(
                self.source.root,
                self.request(self.source.pin),
                deadline_policy=self.policy,
            ),
        )
        self.assertEqual(snapshot(self.source.base), before)


if __name__ == "__main__":
    unittest.main()

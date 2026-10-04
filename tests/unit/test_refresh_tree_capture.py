"""Prospective source capture bound to live root ownership and reviewed proofs."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_ownership as ownership
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from tests.support import fixtures_test_repository_refresh_publication as fixtures
from tests.support.fixtures_test_repository_orchestration import snapshot


class RefreshTreeCaptureTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RefreshPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base

    def acquire(self, prepared):
        return ownership.reserve_published_refresh(
            prepared,
            expected_custody_identity=ownership.refresh_publication_custody_identity(
                prepared
            ),
            acknowledge=True,
        )

    def test_capture_detects_and_preserves_foreign_source_changes(self):
        prepared = self.fixture.prepare()
        before = snapshot(self.base)
        foreign = self.root / "app/foreign"
        capture = ownership.capture_published_repository_tree

        def drift(*args, **options):
            result = capture(*args, **options)
            foreign.write_bytes(b"concurrent owner")
            return result

        with patch.object(ownership, "capture_published_repository_tree", drift):
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire(prepared) as owned:
                    owned.capture_prospective_trees()
        after = snapshot(self.base)
        self.assertEqual(
            after.pop(foreign.relative_to(self.base).as_posix())[0], b"concurrent owner"
        )
        self.assertEqual(after, before)

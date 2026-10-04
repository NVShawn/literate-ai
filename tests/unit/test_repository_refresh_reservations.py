"""Acknowledged real-Git reservations without source/pin application."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_reservations as reservations
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
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

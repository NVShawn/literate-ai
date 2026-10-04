"""Real publication renewal under exact, acknowledged multi-root ownership."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from literate_ai.adapters import repository_refresh as custody
from literate_ai.adapters import repository_refresh_ownership as ownership
from literate_ai.adapters import repository_refresh_publication as publication
from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
)
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from tests.support import fixtures_test_repository_refresh_publication as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RefreshPublicationOwnershipTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RefreshPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base

    def acquire(self, prepared, **options):
        return ownership.reserve_published_refresh(
            prepared,
            expected_custody_identity=options.pop(
                "expected_custody_identity",
                ownership.refresh_publication_custody_identity(prepared),
            ),
            acknowledge=options.pop("acknowledge", True),
            **options,
        )

    def test_real_proofs_are_renewed_only_under_all_writer_locks(self):
        commit = self.fixture.advance(publish=True)
        prepared = self.fixture.prepare(self.fixture.inputs(commit))
        before = snapshot(self.base)
        verify = publication.verify_repository_publication
        calls = []

        def while_owned(endpoint, target, **options):
            calls.append((endpoint, target))
            self.assertEqual(endpoint, self.fixture.remote.as_uri())
            self.assertEqual(target, commit)
            for observed in (prepared.refresh.root_git, *prepared.refresh.children):
                self.assertTrue((observed.git_directory / "index.lock").is_file())
                self.assertTrue(
                    (observed.root / ".literate.project.json.write.lock").is_file()
                )
                with self.assertRaises(ProjectLifecycleLockError):
                    with project_lifecycle_lock(observed.root, operation="fixture"):
                        self.fail("lifecycle writer entered during remote proof")
                with self.assertRaises(subprocess.CalledProcessError):
                    git(observed.root, "add", "source.txt")
            return verify(endpoint, target, **options)

        with patch.object(publication, "verify_repository_publication", while_owned):
            with self.acquire(prepared) as owned:
                owned.require_inputs_unchanged()
                self.assertEqual(len(calls), 1)
                with self.assertRaises(OrchestrationInventoryError):
                    custody.require_repository_refresh_inputs_unchanged(
                        prepared.refresh
                    )
                owned.renew_publication()
                self.assertEqual(len(calls), 2)
        self.assertEqual(snapshot(self.base), before)
        with patch.object(
            publication,
            "verify_repository_publication",
            side_effect=AssertionError("stale handle transported"),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                owned.renew_publication()
            with self.assertRaises(OrchestrationInventoryError):
                owned.require_inputs_unchanged()

    def test_remote_advertisement_drift_rejects_and_releases_all_local_markers(self):
        prepared = self.fixture.prepare()
        git(self.fixture.remote, "update-ref", "refs/tags/new-proof", self.fixture.pin)
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError) as caught:
            with self.acquire(prepared):
                self.fail("changed remote proof")
        self.assertEqual(
            caught.exception.code, "orchestration.refresh_publication_changed"
        )
        self.assertEqual(snapshot(self.base), before)

    def test_foreign_child_write_during_transport_is_detected_and_preserved(self):
        prepared = self.fixture.prepare()
        foreign = self.root / "app/foreign.txt"
        before = snapshot(self.base)
        verify = publication.verify_repository_publication

        def drift(*args, **kwargs):
            proof = verify(*args, **kwargs)
            foreign.write_bytes(b"foreign owner")
            return proof

        with patch.object(publication, "verify_repository_publication", drift):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                with self.acquire(prepared):
                    self.fail("foreign drift")
        self.assertEqual(caught.exception.code, "orchestration.refresh_child_dirty")
        after = snapshot(self.base)
        self.assertEqual(
            after.pop(foreign.relative_to(self.base).as_posix())[0], b"foreign owner"
        )
        self.assertEqual(after, before)

    def test_saved_document_cannot_construct_a_live_handle(self):
        with self.assertRaises(TypeError):
            ownership.OwnedRefreshPublication({"owned": True}, None, None)

"""Real publication renewal under exact, acknowledged multi-root ownership."""

from __future__ import annotations

import hashlib
import subprocess
import unittest
from dataclasses import replace
from pathlib import PurePosixPath
from unittest.mock import patch

from literate_ai.adapters import repository_refresh as custody
from literate_ai.adapters import repository_refresh_ownership as ownership
from literate_ai.adapters import repository_refresh_publication as publication
from literate_ai.adapters.lifecycle_lock import (
    ProjectLifecycleLockError,
    project_lifecycle_lock,
)
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_lineage import RepositoryFetchDeadlinePolicy
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
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

    def test_root_pin_only_ancestry_uses_path_components_not_prefixes(self):
        modes = (
            ("app", "root-pin-only"),
            ("lib", "source-transition"),
        )
        self.assertTrue(
            ownership._within_root_pin_only_target(PurePosixPath("app"), modes)
        )
        self.assertTrue(
            ownership._within_root_pin_only_target(
                PurePosixPath("app/nested/deeper"), modes
            )
        )
        for sibling in ("app2", "application/nested", "lib/app"):
            with self.subTest(sibling=sibling):
                self.assertFalse(
                    ownership._within_root_pin_only_target(
                        PurePosixPath(sibling), modes
                    )
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

    def test_acknowledgement_binds_local_nodes_endpoints_policy_and_proofs(self):
        prepared = self.fixture.prepare()
        identity = ownership.refresh_publication_custody_identity(prepared)
        changed = (
            replace(
                prepared, refresh=replace(prepared.refresh, metadata_node=(1, 2, 3))
            ),
            replace(prepared, endpoints=(("app", "https://example.test/changed.git"),)),
            replace(prepared, deadline_policy=RepositoryFetchDeadlinePolicy(90, 30, 5)),
            replace(
                prepared,
                observations=(replace(prepared.observations[0], commit="4" * 40),),
            ),
        )
        before = snapshot(self.base)
        with patch.object(
            publication,
            "verify_repository_publication",
            side_effect=AssertionError("unapproved transport"),
        ):
            for value in changed:
                self.assertNotEqual(
                    identity, ownership.refresh_publication_custody_identity(value)
                )
                with self.assertRaises(OrchestrationInventoryError):
                    with self.acquire(value, expected_custody_identity=identity):
                        self.fail("stale approval")
            for value in (False, None, 1):
                with self.assertRaises(OrchestrationInventoryError):
                    with self.acquire(prepared, acknowledge=value):
                        self.fail("absent acknowledgement")
        self.assertEqual(snapshot(self.base), before)

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

    def test_root_endpoint_drift_refuses_before_renewal_or_lock_creation(self):
        prepared = self.fixture.prepare()
        git(
            self.root, "config", "remote.origin.url", "https://example.test/changed.git"
        )
        before = snapshot(self.base)
        with patch.object(
            publication,
            "verify_repository_publication",
            side_effect=AssertionError("stale root transported"),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire(prepared):
                    self.fail("stale root")
        self.assertEqual(snapshot(self.base), before)

    def test_transport_failure_releases_owned_additions_without_yielding(self):
        prepared = self.fixture.prepare()
        before = snapshot(self.base)
        with patch.object(
            publication,
            "verify_repository_publication",
            side_effect=OrchestrationInventoryError(
                "publication_unavailable", "fixture"
            ),
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                with self.acquire(prepared):
                    self.fail("failed proof")
        self.assertEqual(caught.exception.code, "orchestration.publication_unavailable")
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

    def test_changed_marker_during_transport_is_not_treated_as_live_ownership(self):
        prepared = self.fixture.prepare()
        marker = prepared.refresh.root_git.git_directory / "index.lock"
        before = snapshot(self.base)

        def lose_owner(*args, **kwargs):
            marker.write_bytes(b"foreign marker")
            return prepared.observations[0]

        with patch.object(publication, "verify_repository_publication", lose_owner):
            with self.assertRaises(OrchestrationInventoryError):
                with self.acquire(prepared):
                    self.fail("lost marker")
        after = snapshot(self.base)
        self.assertEqual(
            after.pop(marker.relative_to(self.base).as_posix())[0], b"foreign marker"
        )
        self.assertEqual(after, before)

    def test_body_failure_releases_reservations_and_preserves_the_original_error(self):
        prepared = self.fixture.prepare()
        before = snapshot(self.base)
        with self.assertRaisesRegex(RuntimeError, "fixture body"):
            with self.acquire(prepared):
                raise RuntimeError("fixture body")
        self.assertEqual(snapshot(self.base), before)

    def test_body_source_drift_is_detected_at_context_exit_without_undoing_it(self):
        prepared = self.fixture.prepare()
        foreign = self.root / "lib/source.txt"
        before = snapshot(self.base)
        with self.assertRaises(OrchestrationInventoryError):
            with self.acquire(prepared):
                foreign.write_bytes(b"do not undo")
        after = snapshot(self.base)
        key = foreign.relative_to(self.base).as_posix()
        self.assertEqual(after.pop(key)[0], b"do not undo")
        before.pop(key)
        self.assertEqual(after, before)

    def test_saved_document_cannot_construct_a_live_handle(self):
        with self.assertRaises(TypeError):
            ownership.OwnedRefreshPublication({"owned": True}, None, None)

    def test_every_selected_child_proof_is_renewed_in_canonical_order(self):
        remote = self.base / "lib.git"
        git(
            self.base,
            "clone",
            "--bare",
            "--no-hardlinks",
            str(self.root / "lib"),
            str(remote),
        )
        git(
            self.root,
            "config",
            "--file",
            ".gitmodules",
            "submodule.lib.url",
            remote.as_uri(),
        )
        git(self.root, "add", ".gitmodules")
        fixture = self.fixture.fixture.fixture
        binding = replace(
            fixture.binding,
            gitmodules_identity="sha256:"
            + hashlib.sha256((self.root / ".gitmodules").read_bytes()).hexdigest(),
            repositories=tuple(
                replace(pin, url=remote.as_uri()) if pin.path == "lib" else pin
                for pin in fixture.binding.repositories
            ),
        )
        fixture.materialize(binding)
        inputs = custody.prepare_repository_refresh(
            self.root,
            RepositoryRefreshRequest(
                tuple(
                    RepositoryRefreshTarget(path, self.fixture.pin)
                    for path in ("lib", "app")
                )
            ),
        )
        prepared = self.fixture.prepare(inputs)
        before = snapshot(self.base)
        calls = []
        verify = publication.verify_repository_publication

        def record(endpoint, commit, **options):
            calls.append((endpoint, commit))
            return verify(endpoint, commit, **options)

        with patch.object(publication, "verify_repository_publication", record):
            with self.acquire(prepared):
                self.assertEqual(
                    calls,
                    [
                        (self.fixture.remote.as_uri(), self.fixture.pin),
                        (remote.as_uri(), self.fixture.pin),
                    ],
                )
        self.assertEqual(snapshot(self.base), before)

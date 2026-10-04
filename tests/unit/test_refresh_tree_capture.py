"""Prospective source capture bound to live root ownership and reviewed proofs."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters import repository_refresh as custody
from literate_ai.adapters import repository_refresh_ownership as ownership
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy
from tests.support import fixtures_test_repository_refresh_publication as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


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

    def prepare_two(self):
        git(
            self.root,
            "config",
            "--file",
            ".gitmodules",
            "submodule.lib.url",
            "../app.git",
        )
        git(self.root, "add", ".gitmodules")
        fixture = self.fixture.fixture.fixture
        binding = replace(
            fixture.binding,
            gitmodules_identity="sha256:"
            + hashlib.sha256((self.root / ".gitmodules").read_bytes()).hexdigest(),
            repositories=tuple(
                replace(pin, url="../app.git") if pin.path == "lib" else pin
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
        return self.fixture.prepare(inputs)

    def test_complete_selected_trees_require_live_ownership(
        self,
    ):
        prepared = self.prepare_two()
        before = snapshot(self.base)
        calls = []
        capture = ownership.capture_published_repository_tree

        def guarded(endpoint, commit, **options):
            self.assertTrue(
                (prepared.refresh.root_git.git_directory / "index.lock").is_file()
            )
            calls.append((endpoint, commit))
            return capture(endpoint, commit, **options)

        with patch.object(ownership, "capture_published_repository_tree", guarded):
            with self.acquire(prepared) as owned:
                captured = owned.capture_prospective_trees()
                self.assertEqual(tuple(path for path, _ in captured), ("app", "lib"))
                self.assertEqual(
                    calls, [(self.fixture.remote.as_uri(), self.fixture.pin)] * 2
                )
                for (_, result), proof in zip(
                    captured, prepared.observations, strict=True
                ):
                    self.assertEqual(result.publication, proof)
                    self.assertEqual(result.tree.entries[0].content, b"original\n")
        self.assertEqual(snapshot(self.base), before)
        with patch.object(
            ownership,
            "capture_published_repository_tree",
            side_effect=AssertionError("stale transport"),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                owned.capture_prospective_trees()

    def test_aggregate_bounds_cover_all_selected_children(self):
        prepared = self.prepare_two()
        baseline = ownership.capture_published_repository_tree(
            self.fixture.remote.as_uri(),
            self.fixture.pin,
            deadline_policy=self.fixture.policy,
        )
        before = snapshot(self.base)
        with self.acquire(prepared) as owned:
            for policy in (
                RepositoryTreeCapturePolicy(maximum_entries=1),
                RepositoryTreeCapturePolicy(maximum_total_blob_bytes=17),
                RepositoryTreeCapturePolicy(
                    maximum_metadata_bytes=baseline.tree.metadata_size_bound * 2 - 1
                ),
            ):
                with self.assertRaises(OrchestrationInventoryError):
                    owned.capture_prospective_trees(tree_policy=policy)
                owned.require_inputs_unchanged()
        self.assertEqual(snapshot(self.base), before)

    def test_mixed_exact_head_selection_captures_only_source_transition(self):
        self.prepare_two()
        target = self.fixture.advance(publish=True)
        git(self.root / "app", "checkout", "-q", self.fixture.pin)
        git(
            self.root / "lib",
            "fetch",
            self.fixture.remote.as_uri(),
            "refs/heads/main",
        )
        git(self.root / "lib", "checkout", "-q", target)
        inputs = custody.prepare_repository_refresh(
            self.root,
            RepositoryRefreshRequest(
                tuple(RepositoryRefreshTarget(path, target) for path in ("app", "lib"))
            ),
        )
        prepared = self.fixture.prepare(inputs)
        self.assertEqual(
            prepared.target_modes,
            (
                ("app", "source-transition"),
                ("lib", "root-pin-only"),
            ),
        )
        calls = []
        capture = ownership.capture_published_repository_tree

        def counted(endpoint, commit, **options):
            calls.append((endpoint, commit))
            return capture(endpoint, commit, **options)

        with (
            patch.object(
                ownership, "capture_published_repository_tree", side_effect=counted
            ),
            self.acquire(prepared) as owned,
        ):
            files = owned.prepare_filesystem_changes()
            self.assertEqual(tuple(path for path, _plan in files.plans), ("app",))
            captured = files.prepare_objects()
            self.assertEqual(tuple(path for path, _pack in captured.packs), ("app",))
        self.assertEqual(calls, [(self.fixture.remote.as_uri(), target)])

    def test_changed_publication_after_lock_acquisition_refuses_tree_capture(self):
        prepared = self.fixture.prepare()
        before = snapshot(self.root)
        with self.acquire(prepared) as owned:
            git(self.fixture.remote, "update-ref", "refs/tags/drift", self.fixture.pin)
            with self.assertRaises(OrchestrationInventoryError) as caught:
                owned.capture_prospective_trees()
            self.assertEqual(
                caught.exception.code, "orchestration.refresh_publication_changed"
            )
        self.assertEqual(snapshot(self.root), before)

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

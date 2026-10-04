"""Root object selection rechecks publication/tree equality and aggregate bounds."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters import repository_refresh_objects as objects
from literate_ai.adapters._repository_pack_capture import RepositoryPackPolicy
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_files import PreparedRefreshFiles
from tests.support import fixtures_test_published_repository_pack as fixtures


class RefreshObjectCaptureTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.PublishedRepositoryPackTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.captured = fixture.capture()
        files = Mock(spec=PreparedRefreshFiles)
        files.identity = "sha256:" + "a" * 64
        files.target_modes = (
            ("a", "source-transition"),
            ("b", "source-transition"),
        )
        files.plans = tuple(
            (name, SimpleNamespace(prospective=self.captured.tree))
            for name in ("a", "b")
        )
        files._owner = SimpleNamespace(
            _prepared=SimpleNamespace(
                endpoints=(
                    ("a", fixture.fixture.endpoint),
                    ("b", fixture.fixture.endpoint),
                ),
                observations=(self.captured.publication,) * 2,
                deadline_policy=fixture.fixture.policy,
                refresh=None,
            )
        )
        self.files = files
        self.protected = patch.object(objects, "_protected_roots", return_value=())
        self.protected.start()
        self.addCleanup(self.protected.stop)

    def test_complete_selection_binds_exact_proofs_and_physical_identity(self):
        with patch.object(
            objects, "capture_published_repository_pack", return_value=self.captured
        ):
            prepared = objects.prepare_refresh_objects(self.files)
        self.assertEqual(tuple(path for path, _ in prepared.packs), ("a", "b"))
        self.assertEqual(prepared.identity, prepared.identity)
        prepared.require_current()
        self.files.require_current.side_effect = RuntimeError("expired")
        with self.assertRaises(RuntimeError):
            prepared.require_current()
        with self.assertRaises(TypeError):
            objects.PreparedRefreshObjects(None, self.files, prepared.packs)

    def test_root_pin_only_targets_export_no_object_packs(self):
        self.files.target_modes = (
            ("a", "root-pin-only"),
            ("b", "root-pin-only"),
        )
        self.files.plans = ()
        with patch.object(
            objects,
            "capture_published_repository_pack",
            side_effect=AssertionError("root-only targets must not export packs"),
        ):
            prepared = objects.prepare_refresh_objects(self.files)
        self.assertEqual(prepared.packs, ())
        self.assertEqual(prepared.identity, prepared.identity)

    def test_aggregate_pack_index_and_count_limits_reject_partial_result(self):
        pack = self.captured.objects
        for policy in (
            RepositoryPackPolicy(maximum_pack_bytes=len(pack.pack) * 2 - 1),
            RepositoryPackPolicy(maximum_index_bytes=len(pack.index) * 2 - 1),
            RepositoryPackPolicy(maximum_objects=pack.object_count * 2 - 1),
        ):
            with (
                self.subTest(policy=policy),
                patch.object(
                    objects,
                    "capture_published_repository_pack",
                    return_value=self.captured,
                ),
            ):
                with self.assertRaises(OrchestrationInventoryError) as caught:
                    objects.prepare_refresh_objects(self.files, pack_policy=policy)
                self.assertEqual(
                    caught.exception.code, "orchestration.refresh_objects_limit"
                )

    def test_publication_drift_is_not_admitted_as_object_custody(self):
        changed = replace(
            self.captured,
            publication=replace(
                self.captured.publication, advertisement_identity="sha256:" + "b" * 64
            ),
        )
        with patch.object(
            objects, "capture_published_repository_pack", return_value=changed
        ):
            with self.assertRaises(OrchestrationInventoryError) as caught:
                objects.prepare_refresh_objects(self.files)
        self.assertEqual(caught.exception.code, "orchestration.refresh_objects_changed")

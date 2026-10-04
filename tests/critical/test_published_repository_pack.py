"""Exact published history packs must work in an independent empty repository."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.adapters import repository_publication as publication
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from tests.support import fixtures_test_repository_publication as fixtures
from tests.support.fixtures_test_repository_orchestration import git


class PublishedRepositoryPackTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RepositoryPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def capture(self, **options):
        fixture = self.fixture
        return publication.capture_published_repository_pack(
            fixture.endpoint,
            fixture.latest,
            deadline_policy=fixture.policy,
            protected_roots=(fixture.source, fixture.remote),
            **options,
        )

    def test_corrupted_pack_index_and_bundle_prerequisites_are_rejected(self):
        captured = self.capture()
        for field in ("pack", "index"):
            original = getattr(captured.objects, field)
            corrupted = original[:-1] + bytes([original[-1] ^ 1])
            with (
                self.subTest(field=field),
                self.assertRaises(OrchestrationInventoryError),
            ):
                replace(
                    captured.objects,
                    **{field: corrupted},
                )

        def thin(command, **options):
            result = publication.run_bounded_process(command, **options)
            if "bundle" in command:
                return replace(
                    result,
                    stdout=result.stdout.replace(
                        b"\n\nPACK", b"\n-prerequisite\n\nPACK", 1
                    ),
                )
            return result

        with self.assertRaises(OrchestrationInventoryError):
            self.capture(process_runner=thin)

    def test_remote_drift_after_export_refuses_the_pack(self):
        fixture = self.fixture

        def drift(command, **options):
            result = publication.run_bounded_process(command, **options)
            if "index-pack" in command:
                git(fixture.remote, "update-ref", "refs/tags/drift", fixture.latest)
            return result

        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.capture(process_runner=drift)
        self.assertEqual(
            caught.exception.code, "orchestration.publication_refs_changed"
        )

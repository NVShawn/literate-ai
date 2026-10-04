from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_published_repository_pack``."""

import unittest
from dataclasses import replace

from literate_ai.adapters import repository_publication as publication
from literate_ai.adapters._repository_pack_capture import RepositoryPackPolicy
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from tests.support import fixtures_test_repository_publication as fixtures
from tests.support.fixtures_test_repository_orchestration import (
    git,
    repository,
    snapshot,
)


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

    def test_complete_parent_history_is_readable_without_borrowed_objects(self):
        fixture = self.fixture
        before = snapshot(fixture.base)
        commands = []

        def runner(command, **options):
            commands.append(command)
            return publication.run_bounded_process(command, **options)

        result = self.capture(process_runner=runner)
        self.assertEqual(snapshot(fixture.base), before)
        self.assertEqual(result.publication, fixture.verify(fixture.latest))
        fetches = [command for command in commands if "fetch" in command]
        self.assertEqual(len(fetches), 2)
        self.assertIn("--depth=1", fetches[0])
        self.assertIn("--unshallow", fetches[1])
        self.assertTrue(
            all(
                "+refs/heads/main:refs/litai-publication/heads/main" in command
                for command in fetches
            )
        )
        self.assertTrue(
            all(not any("*" in argument for argument in command) for command in fetches)
        )
        empty = fixture.base / "empty.git"
        git(fixture.base, "init", "--bare", "--template=", str(empty))
        prefix = empty / "objects/pack" / ("pack-" + result.objects.pack_id)
        prefix.with_suffix(".pack").write_bytes(result.objects.pack)
        prefix.with_suffix(".idx").write_bytes(result.objects.index)
        self.assertEqual(
            git(empty, "rev-list", "--count", fixture.latest).strip(), b"2"
        )
        self.assertEqual(
            git(empty, "show", fixture.latest + ":source.txt"), b"original\n"
        )
        git(empty, "fsck", "--full", "--no-reflogs", fixture.latest)
        self.assertEqual(git(empty, "for-each-ref"), b"")

    def test_sha256_pack_is_self_contained(self):
        fixture = self.fixture
        root = fixture.base / "sha256"
        repository(root, sha256=True)
        commit = git(root, "rev-parse", "HEAD").decode().strip()
        captured = publication.capture_published_repository_pack(
            root.as_uri(), commit, deadline_policy=fixture.policy
        )
        self.assertEqual(len(captured.objects.pack_id), 64)
        empty = fixture.base / "empty256"
        git(
            fixture.base,
            "init",
            "--bare",
            "--template=",
            "--object-format=sha256",
            str(empty),
        )
        prefix = empty / "objects/pack" / ("pack-" + captured.objects.pack_id)
        prefix.with_suffix(".pack").write_bytes(captured.objects.pack)
        prefix.with_suffix(".idx").write_bytes(captured.objects.index)
        git(empty, "fsck", "--full", "--no-reflogs", commit)

    def test_pack_index_and_object_count_bounds_refuse(self):
        before = snapshot(self.fixture.base)
        for policy in (
            RepositoryPackPolicy(maximum_pack_bytes=1),
            RepositoryPackPolicy(maximum_index_bytes=1),
            RepositoryPackPolicy(maximum_objects=1),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                self.capture(pack_policy=policy)
        self.assertEqual(snapshot(self.fixture.base), before)

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

    def test_independent_gitlink_objects_are_not_required_or_acquired(self):
        fixture = self.fixture
        git(
            fixture.source,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            "4" * 40,
            "nested",
        )
        git(fixture.source, "commit", "-q", "-m", "independent child")
        fixture.latest = git(fixture.source, "rev-parse", "HEAD").decode().strip()
        git(fixture.source, "push", fixture.endpoint, "HEAD:refs/heads/main")
        before = snapshot(fixture.base)
        captured = self.capture()
        self.assertEqual(snapshot(fixture.base), before)
        child = next(entry for entry in captured.tree.entries if entry.path == "nested")
        self.assertEqual(child.object_id, "4" * 40)
        self.assertEqual(child.mode, "160000")

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

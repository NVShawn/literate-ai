"""Published tree capture in disposable stores without checkout or execution."""

from __future__ import annotations

import subprocess
import unittest
from dataclasses import replace

from literate_ai.adapters import repository_publication as publication
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from tests.support import fixtures_test_repository_publication as fixtures
from tests.support.fixtures_test_repository_orchestration import snapshot


class PublishedRepositoryTreeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.RepositoryPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.source, self.remote, self.base = (
            fixture.source,
            fixture.remote,
            fixture.base,
        )

    def capture(self, commit=None, **options):
        return publication.capture_published_repository_tree(
            self.fixture.endpoint,
            commit or self.fixture.first,
            deadline_policy=self.fixture.policy,
            protected_roots=(self.source, self.remote),
            **options,
        )

    def blob(self, content, *, kind="blob"):
        return (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.source),
                    "hash-object",
                    "-t",
                    kind,
                    "-w",
                    "--stdin",
                ],
                input=content,
                capture_output=True,
                check=True,
                timeout=20,
            )
            .stdout.decode()
            .strip()
        )

    def test_unpublished_objects_are_rejected_before_tree_capture(self):
        commit = self.fixture.advance()
        calls = []

        def record(command, **options):
            calls.append(command)
            return publication.run_bounded_process(command, **options)

        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.capture(commit, process_runner=record)
        self.assertEqual(caught.exception.code, "orchestration.publication_unpublished")
        self.assertFalse(any("ls-tree" in command for command in calls))

    def test_blob_corruption_and_truncated_listing_are_rejected(self):
        before = snapshot(self.base)
        for variant in ("blob", "listing", "commit", "oid"):

            def corrupt(command, variant=variant, **options):
                self.assertNotIn("--filters", command)
                result = publication.run_bounded_process(command, **options)
                if variant == "blob" and "cat-file" in command and "blob" in command:
                    return replace(result, stdout=b"x" * len(result.stdout))
                if (
                    variant == "commit"
                    and "cat-file" in command
                    and "commit" in command
                ):
                    return replace(result, stdout=result.stdout + b"altered")
                if "ls-tree" in command:
                    if variant == "listing":
                        return replace(result, stdout=result.stdout[:-1])
                    if variant == "oid":
                        words = result.stdout.split(b" ")
                        return replace(
                            result,
                            stdout=result.stdout.replace(words[2], b"--filters", 1),
                        )
                return result

            with (
                self.subTest(variant=variant),
                self.assertRaises(OrchestrationInventoryError),
            ):
                self.capture(process_runner=corrupt)
        self.assertEqual(snapshot(self.base), before)

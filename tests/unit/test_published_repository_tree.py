"""Published tree capture in disposable stores without checkout or execution."""

from __future__ import annotations

import subprocess
import unittest
from dataclasses import replace

from literate_ai.adapters import repository_publication as publication
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy
from tests.unit import test_repository_publication as fixtures
from tests.unit.test_repository_orchestration import git, snapshot


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

    def test_capture_preserves_raw_modes_links_attributes_and_all_repository_bytes(
        self,
    ):
        (self.source / "dir").mkdir()
        (self.source / "dir/file").write_bytes(b"raw\0binary\xff")
        (self.source / "dir.c").write_bytes(b"do not execute")
        (self.source / ".gitattributes").write_bytes(
            b"* export-ignore\n* filter=untrusted\n"
        )
        git(self.source, "add", ".")
        git(self.source, "update-index", "--chmod=+x", "dir.c")
        git(
            self.source,
            "update-index",
            "--add",
            "--cacheinfo",
            "120000",
            self.blob(b"../../foreign"),
            "link",
        )
        git(
            self.source,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            "4" * 40,
            "child",
        )
        git(self.source, "commit", "-q", "-m", "raw tree")
        commit = git(self.source, "rev-parse", "HEAD").decode().strip()
        git(self.source, "push", self.fixture.endpoint, "HEAD:refs/heads/main")
        before = snapshot(self.base)
        stores = set()

        def guarded(command, **options):
            self.assertFalse(
                {
                    "checkout",
                    "archive",
                    "--filters",
                    "--textconv",
                    "submodule",
                }.intersection(command)
            )
            self.assertNotIn(options["cwd"], (self.source, self.remote))
            stores.add(options["cwd"])
            return publication.run_bounded_process(command, **options)

        captured = self.capture(commit, process_runner=guarded)
        entries = {entry.path: entry for entry in captured.tree.entries}
        self.assertEqual(entries["dir/file"].content, b"raw\0binary\xff")
        self.assertEqual(entries["dir.c"].mode, "100755")
        self.assertEqual(entries["link"].content, b"../../foreign")
        self.assertEqual(entries["child"].mode, "160000")
        self.assertEqual(entries["child"].object_id, "4" * 40)
        self.assertIn(".gitattributes", entries)
        self.assertEqual(
            captured.tree.tree_id,
            git(self.source, "rev-parse", commit + "^{tree}").decode().strip(),
        )
        self.assertEqual(captured.publication, self.fixture.verify(commit))
        self.assertEqual(snapshot(self.base), before)
        self.assertTrue(all(not path.exists() for path in stores))

    def test_sha256_capture_recomputes_all_object_hashes(self):
        from tests.unit.test_repository_orchestration import repository

        root = self.base / "sha256"
        repository(root, sha256=True)
        commit = git(root, "rev-parse", "HEAD").decode().strip()
        result = publication.capture_published_repository_tree(
            root.as_uri(), commit, deadline_policy=self.fixture.policy
        )
        self.assertEqual(len(result.tree.tree_id), 64)
        self.assertEqual(result.tree.entries[0].content, b"original\n")

    def test_entry_blob_and_aggregate_bounds_refuse_without_repository_writes(self):
        (self.source / "copy.txt").write_bytes(b"original\n")
        git(self.source, "add", "copy.txt")
        git(self.source, "commit", "-q", "-m", "duplicate blob")
        commit = git(self.source, "rev-parse", "HEAD").decode().strip()
        git(self.source, "push", self.fixture.endpoint, "HEAD:refs/heads/main")
        before = snapshot(self.base)
        for policy in (
            RepositoryTreeCapturePolicy(maximum_entries=1),
            RepositoryTreeCapturePolicy(maximum_blob_bytes=1),
            RepositoryTreeCapturePolicy(maximum_total_blob_bytes=17),
            RepositoryTreeCapturePolicy(maximum_metadata_bytes=10),
        ):
            with self.assertRaises(OrchestrationInventoryError):
                self.capture(commit, tree_policy=policy)
        self.assertEqual(snapshot(self.base), before)

    def test_empty_directory_objects_are_included_in_complete_tree_capture(self):
        empty = self.blob(b"", kind="tree")
        root = self.blob(b"40000 empty\0" + bytes.fromhex(empty), kind="tree")
        raw = (
            "tree " + root + "\nauthor Test <test@example.test> 1 +0000\n"
            "committer Test <test@example.test> 1 +0000\n\nempty directory\n"
        ).encode()
        commit = self.blob(raw, kind="commit")
        git(self.source, "push", self.fixture.endpoint, commit + ":refs/heads/empty")
        before = snapshot(self.base)
        captured = self.capture(commit)
        self.assertEqual(len(captured.tree.entries), 1)
        self.assertEqual(captured.tree.entries[0].mode, "040000")
        self.assertEqual(captured.tree.entries[0].object_id, empty)
        self.assertEqual(captured.tree.tree_id, root)
        self.assertEqual(snapshot(self.base), before)

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

    def test_remote_drift_during_tree_capture_refuses(self):
        before = snapshot(self.source)

        def drift(command, **options):
            result = publication.run_bounded_process(command, **options)
            if "ls-tree" in command:
                git(self.remote, "update-ref", "refs/tags/drift", self.fixture.first)
            return result

        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.capture(process_runner=drift)
        self.assertEqual(
            caught.exception.code, "orchestration.publication_refs_changed"
        )
        self.assertEqual(snapshot(self.source), before)

    def test_tree_capture_consumes_the_shared_transport_deadline(self):
        elapsed = 0
        stores = set()

        def slow(command, **options):
            nonlocal elapsed
            stores.add(options["cwd"])
            result = publication.run_bounded_process(command, **options)
            if "ls-tree" in command:
                elapsed = 121
            return result

        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.capture(process_runner=slow, clock=lambda: elapsed)
        self.assertEqual(caught.exception.code, "orchestration.publication_timeout")
        self.assertTrue(all(not path.exists() for path in stores))

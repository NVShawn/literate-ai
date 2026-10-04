"""Real remote publication proof, independent of local checkout object custody."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_publication as publication
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_lineage import RepositoryFetchDeadlinePolicy
from tests.support.fixtures_test_repository_orchestration import (
    git,
    repository,
    snapshot,
)


class RepositoryPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pub-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        repository(self.source)
        self.first = git(self.source, "rev-parse", "HEAD").decode().strip()
        self.latest = self.advance()
        self.remote = self.base / "remote.git"
        git(
            self.base,
            "clone",
            "--bare",
            "--no-hardlinks",
            str(self.source),
            str(self.remote),
        )
        # A push starts a separate receive-pack process; client -c options do
        # not prevent its background maintenance from racing byte snapshots.
        git(self.remote, "config", "receive.autogc", "false")
        self.endpoint = self.remote.as_uri()
        self.policy = RepositoryFetchDeadlinePolicy(120, 60, 10)

    def advance(self):
        git(self.source, "commit", "--allow-empty", "-q", "-m", "advance")
        return git(self.source, "rev-parse", "HEAD").decode().strip()

    def verify(self, commit=None, **kwargs):
        return publication.verify_repository_publication(
            self.endpoint,
            commit or self.first,
            deadline_policy=self.policy,
            **kwargs,
        )

    def assert_refuses(self, suffix, operation):
        with self.assertRaises(OrchestrationInventoryError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "orchestration.publication_" + suffix)
        return caught.exception

    def test_older_commit_is_published_and_all_checkout_remote_bytes_are_preserved(
        self,
    ):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return publication.run_bounded_process(command, **kwargs)

        before = snapshot(self.base)
        proof = self.verify(process_runner=runner)
        self.assertEqual(proof.commit, self.first)
        self.assertEqual(proof.reference, "refs/heads/main")
        self.assertEqual(proof.reference_object, self.latest)
        self.assertEqual(proof.reference_commit, self.latest)
        fetch = next(command for command in commands if "fetch" in command)
        self.assertIn("+refs/*:refs/litai-publication/*", fetch)
        self.assertNotIn("--depth=1", fetch)
        self.assertTrue(any("merge-base" in command for command in commands))
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.verify(), proof)
        self.assertEqual(self.verify().identity, proof.identity)
        with self.assertRaises(FrozenInstanceError):
            proof.commit = self.latest

    def test_unpublished_child_commit_and_server_dangling_object_refuse(self):
        private = self.advance()
        self.assert_refuses("unpublished", lambda: self.verify(private))
        git(self.source, "push", self.endpoint, private + ":refs/heads/temporary")
        git(self.remote, "update-ref", "-d", "refs/heads/temporary")
        # The server has the object, but no advertised reference reaches it.
        self.assertEqual(git(self.remote, "cat-file", "-t", private), b"commit\n")
        before = snapshot(self.base)
        self.assert_refuses("unpublished", lambda: self.verify(private))
        self.assertEqual(snapshot(self.base), before)

    def test_forged_advertisement_does_not_override_the_fetched_ref_snapshot(self):
        repositories = []

        def runner(command, **kwargs):
            if kwargs["cwd"] not in repositories:
                repositories.append(kwargs["cwd"])
            result = publication.run_bounded_process(command, **kwargs)
            if "ls-remote" in command:
                return BoundedProcessResult(
                    0,
                    result.stdout.replace(self.latest.encode(), self.first.encode()),
                    b"",
                )
            return result

        self.assert_refuses("refs_changed", lambda: self.verify(process_runner=runner))
        self.assertEqual(len(repositories), 1)

    def test_remote_move_during_fetch_and_after_ancestry_refuses(self):
        for stage in ("fetch", "merge-base"):
            with self.subTest(stage=stage):
                advanced = self.advance()
                changed = False

                def runner(command, stage=stage, advanced=advanced, **kwargs):
                    nonlocal changed
                    result = publication.run_bounded_process(command, **kwargs)
                    if stage in command and not changed:
                        changed = True
                        git(
                            self.source,
                            "push",
                            self.endpoint,
                            advanced + ":refs/heads/main",
                        )
                    return result

                self.assert_refuses(
                    "refs_changed", lambda: self.verify(process_runner=runner)
                )
                self.assertTrue(changed)

    def test_inherited_git_controls_are_removed_and_commands_use_only_owned_storage(
        self,
    ):
        controls = {
            "GIT_DIR": str(self.remote),
            "GIT_WORK_TREE": str(self.source),
            "GIT_INDEX_FILE": str(self.source / "foreign-index"),
            "GIT_OBJECT_DIRECTORY": str(self.remote / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.remote / "objects"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.fixture",
            "GIT_CONFIG_VALUE_0": "must-not-run",
            "GIT_SSH_COMMAND": "must-not-run",
            "GIT_CONFIG_GLOBAL": str(self.base / "must-not-read"),
        }
        visited = []

        def runner(command, **kwargs):
            visited.append(kwargs["cwd"])
            self.assertFalse(kwargs["cwd"].is_relative_to(self.base))
            environment = kwargs["environment"]
            for key, value in controls.items():
                self.assertNotEqual(environment.get(key), value)
            self.assertEqual(environment["LITAI_PUBLICATION_FIXTURE"], "preserved")
            self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(environment["GIT_NO_REPLACE_OBJECTS"], "1")
            if "fetch" in command:
                self.assertNotIn(self.first, command)
                self.assertIn("--no-recurse-submodules", command)
                self.assertIn("--no-write-fetch-head", command)
                self.assertFalse(any(part.startswith("--depth") for part in command))
            return publication.run_bounded_process(command, **kwargs)

        before = snapshot(self.base)
        with patch.dict(
            os.environ, {**controls, "LITAI_PUBLICATION_FIXTURE": "preserved"}
        ):
            self.verify(process_runner=runner)
            for key, value in controls.items():
                self.assertEqual(os.environ[key], value)
        self.assertEqual(snapshot(self.base), before)
        self.assertTrue(visited)
        self.assertTrue(all(not path.exists() for path in visited))

    def test_nonzero_git_result_never_exposes_remote_diagnostics(self):
        error = self.assert_refuses(
            "git_failed",
            lambda: self.verify(
                process_runner=lambda *args, **kwargs: BoundedProcessResult(
                    128, b"secret", b"secret"
                )
            ),
        )
        self.assertNotIn("secret", str(error))


if __name__ == "__main__":
    unittest.main()

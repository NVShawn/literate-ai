from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_repository_publication``."""

import os
import tempfile
import traceback
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_publication as publication
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.python import BuildError
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

    def test_fixture_receiver_disables_automatic_post_receive_maintenance(self):
        # Client-side -c options do not establish the receiver's local policy.
        self.assertEqual(
            git(self.remote, "config", "--local", "--bool", "--get", "receive.autogc"),
            b"false\n",
        )

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

    def test_exact_branch_tip_fetches_only_its_depth_one_fully_qualified_ref(self):
        git(self.source, "tag", "same-tip", self.latest)
        git(self.source, "push", self.endpoint, "refs/tags/same-tip")
        git(self.remote, "update-ref", "refs/pull/1/head", self.latest)
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return publication.run_bounded_process(command, **kwargs)

        proof = self.verify(self.latest, process_runner=runner)
        self.assertEqual(proof.reference, "refs/heads/main")
        self.assertEqual(proof.reference_object, self.latest)
        self.assertEqual(proof.reference_commit, self.latest)
        fetches = [command for command in commands if "fetch" in command]
        self.assertEqual(len(fetches), 1)
        fetch = fetches[0]
        self.assertIn("--depth=1", fetch)
        self.assertIn("--no-tags", fetch)
        self.assertIn("+refs/heads/main:refs/litai-publication/heads/main", fetch)
        self.assertFalse(any("*" in argument for argument in fetch))
        self.assertFalse(any("merge-base" in command for command in commands))

    def test_exact_tip_tree_capture_stays_on_one_shallow_branch_ref(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return publication.run_bounded_process(command, **kwargs)

        captured = publication.capture_published_repository_tree(
            self.endpoint,
            self.latest,
            deadline_policy=self.policy,
            process_runner=runner,
        )
        self.assertEqual(captured.publication.commit, self.latest)
        self.assertEqual(captured.tree.commit, self.latest)
        fetches = [command for command in commands if "fetch" in command]
        self.assertEqual(len(fetches), 1)
        self.assertIn("--depth=1", fetches[0])
        self.assertFalse(any("*" in argument for argument in fetches[0]))

    def test_shallow_git_failure_falls_back_fresh_with_remaining_budget(self):
        reviewed_policy = RepositoryFetchDeadlinePolicy(120, 100, 90)
        git(
            self.remote,
            "update-ref",
            "refs/changes/00/competing",
            self.latest,
        )
        direct = publication.verify_repository_publication(
            self.endpoint,
            self.latest,
            deadline_policy=reviewed_policy,
        )
        self.assertEqual(direct.reference, "refs/heads/main")
        repositories = []
        fetches = []
        failed = False
        elapsed = 0
        fallback_bounds = []

        def clock():
            return elapsed

        def runner(command, **kwargs):
            nonlocal elapsed, failed
            if kwargs["cwd"] not in repositories:
                if repositories:
                    self.assertFalse(repositories[-1].exists())
                repositories.append(kwargs["cwd"])
            if "fetch" in command:
                fetches.append(command)
                if "--depth=1" in command and not failed:
                    failed = True
                    elapsed = 80
                    return BoundedProcessResult(128, b"secret", b"secret")
            if failed:
                fallback_bounds.append(
                    (
                        kwargs["timeout_seconds"],
                        kwargs["inactivity_timeout_seconds"],
                        kwargs["environment"]["GIT_SSH_COMMAND"],
                    )
                )
            return publication.run_bounded_process(command, **kwargs)

        proof = publication.verify_repository_publication(
            self.endpoint,
            self.latest,
            deadline_policy=reviewed_policy,
            process_runner=runner,
            clock=clock,
        )
        self.assertEqual(proof.commit, self.latest)
        self.assertEqual(proof, direct)
        self.assertEqual(proof.identity, direct.identity)
        self.assertEqual(proof.deadline_policy_identity, reviewed_policy.identity.uri)
        self.assertEqual(len(repositories), 2)
        self.assertTrue(all(not path.exists() for path in repositories))
        self.assertEqual(len(fetches), 2)
        self.assertIn("--depth=1", fetches[0])
        self.assertFalse(any("*" in argument for argument in fetches[0]))
        self.assertNotIn("--depth=1", fetches[1])
        self.assertIn("+refs/*:refs/litai-publication/*", fetches[1])
        self.assertTrue(fallback_bounds)
        self.assertTrue(all(timeout <= 40 for timeout, _, _ in fallback_bounds))
        self.assertTrue(all(inactivity <= 40 for _, inactivity, _ in fallback_bounds))
        self.assertTrue(
            all("ConnectTimeout=40" in ssh for _, _, ssh in fallback_bounds)
        )

    def test_shallow_timeout_types_retry_without_full_fetch_fallback(self):
        for code, suffix in (
            ("git_timeout", "timeout"),
            ("git_no_progress_timeout", "no_progress_timeout"),
        ):
            with self.subTest(code=code):
                repositories = []
                fetches = []

                def runner(
                    command,
                    code=code,
                    fetches=fetches,
                    repositories=repositories,
                    **kwargs,
                ):
                    if kwargs["cwd"] not in repositories:
                        repositories.append(kwargs["cwd"])
                    if "fetch" in command:
                        fetches.append(command)
                        raise BuildError(code, "secret")
                    return publication.run_bounded_process(command, **kwargs)

                error = self.assert_refuses(
                    suffix,
                    lambda: self.verify(
                        self.latest,
                        process_runner=runner,
                        clock=lambda: 0,
                    ),
                )
                self.assertNotIn("secret", "".join(traceback.format_exception(error)))
                self.assertEqual(len(repositories), publication._MAXIMUM_ATTEMPTS)
                self.assertTrue(all(not path.exists() for path in repositories))
                self.assertEqual(len(fetches), publication._MAXIMUM_ATTEMPTS)
                self.assertTrue(all("--depth=1" in command for command in fetches))
                self.assertTrue(
                    all(
                        not any("*" in argument for argument in command)
                        for command in fetches
                    )
                )

    def test_only_direct_branch_tips_select_the_shallow_fast_path(self):
        candidates = (
            ("HEAD", self.latest),
            ("refs/tags/v1", self.latest),
            ("refs/tags/v1^{}", self.latest),
            ("refs/pull/1/head", self.latest),
            ("refs/litai-hidden/branch", self.latest),
        )
        self.assertIsNone(
            publication._exact_advertised_branch_tip(candidates, self.latest)
        )
        self.assertEqual(
            publication._exact_advertised_branch_tip(
                (
                    *candidates,
                    ("refs/heads/z", self.latest),
                    ("refs/heads/a", self.latest),
                ),
                self.latest,
            ),
            ("refs/heads/a", self.latest, self.latest),
        )

    def test_lightweight_tag_tip_retains_full_ref_tag_proof(self):
        git(self.source, "tag", "tip", self.latest)
        git(self.source, "push", self.endpoint, "refs/tags/tip")
        git(self.remote, "update-ref", "-d", "refs/heads/main")
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            return publication.run_bounded_process(command, **kwargs)

        proof = self.verify(self.latest, process_runner=runner)
        self.assertEqual(proof.reference, "refs/tags/tip")
        fetch = next(command for command in commands if "fetch" in command)
        self.assertIn("+refs/*:refs/litai-publication/*", fetch)
        self.assertNotIn("--depth=1", fetch)

    def test_exact_tip_rejects_mutable_advertisements(self):
        advanced = self.advance()
        changed = False

        def runner(command, **kwargs):
            nonlocal changed
            result = publication.run_bounded_process(command, **kwargs)
            if "fetch" in command and "--depth=1" in command and not changed:
                changed = True
                git(
                    self.source,
                    "push",
                    self.endpoint,
                    advanced + ":refs/heads/main",
                )
            return result

        self.assert_refuses(
            "refs_changed",
            lambda: self.verify(self.latest, process_runner=runner),
        )
        self.assertTrue(changed)

    def test_exact_tip_rejects_missing_or_wrong_fetched_witness_ref(self):
        for replacement in (b"", None):
            with self.subTest(replacement=replacement):
                repositories = []
                fetches = []

                def runner(
                    command,
                    fetches=fetches,
                    replacement=replacement,
                    repositories=repositories,
                    **kwargs,
                ):
                    if kwargs["cwd"] not in repositories:
                        repositories.append(kwargs["cwd"])
                    result = publication.run_bounded_process(command, **kwargs)
                    if "fetch" in command:
                        fetches.append(command)
                    if "for-each-ref" in command:
                        return BoundedProcessResult(
                            0,
                            result.stdout.replace(
                                self.latest.encode(), self.first.encode()
                            )
                            if replacement is None
                            else replacement,
                            b"",
                        )
                    return result

                self.assert_refuses(
                    "refs_changed",
                    lambda: self.verify(
                        self.latest,
                        process_runner=runner,
                    ),
                )
                self.assertEqual(len(repositories), 1)
                self.assertEqual(len(fetches), 1)
                self.assertIn("--depth=1", fetches[0])
                self.assertFalse(any("*" in argument for argument in fetches[0]))

    def test_tip_and_annotated_tag_only_publication(self):
        self.assertEqual(self.verify(self.latest).commit, self.latest)
        git(self.source, "tag", "-a", "v1", "-m", "published tag", self.latest)
        git(self.source, "push", self.endpoint, "refs/tags/v1")
        git(self.remote, "update-ref", "-d", "refs/heads/main")
        before = snapshot(self.base)
        proof = self.verify()
        self.assertEqual(proof.reference, "refs/tags/v1")
        self.assertNotEqual(proof.reference_object, self.latest)
        self.assertEqual(proof.reference_commit, self.latest)
        self.assertEqual(snapshot(self.base), before)

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

    def test_shallow_child_does_not_limit_remote_ancestry_proof(self):
        child = self.base / "shallow"
        git(self.base, "clone", "--depth=1", self.endpoint, str(child))
        self.assertEqual(git(child, "rev-parse", "--is-shallow-repository"), b"true\n")
        before = snapshot(self.base)
        self.assertEqual(self.verify().commit, self.first)
        self.assertEqual(snapshot(self.base), before)

    def test_extra_fetched_object_still_requires_an_advertised_ancestry_witness(self):
        private = self.advance()
        git(self.source, "push", self.endpoint, private + ":refs/heads/temporary")
        git(self.remote, "update-ref", "-d", "refs/heads/temporary")
        traversed = []

        def runner(command, **kwargs):
            result = publication.run_bounded_process(command, **kwargs)
            if "fetch" in command:
                # Simulate extra objects delivered in a server pack. Availability
                # must not bypass the separately verified advertised ancestry.
                git(
                    kwargs["cwd"],
                    "fetch",
                    "--no-write-fetch-head",
                    self.endpoint,
                    private,
                )
                self.assertEqual(
                    git(kwargs["cwd"], "cat-file", "-t", private), b"commit\n"
                )
            if "merge-base" in command:
                traversed.append(result.returncode)
            return result

        self.assert_refuses(
            "unpublished", lambda: self.verify(private, process_runner=runner)
        )
        self.assertEqual(traversed, [1])

    def test_absolute_local_endpoint_and_distinct_endpoint_identity(self):
        first = self.verify()
        second = publication.verify_repository_publication(
            str(self.remote), self.first, deadline_policy=self.policy
        )
        self.assertEqual(first.commit, second.commit)
        self.assertNotEqual(first.repository_identity, second.repository_identity)
        self.assertNotEqual(first.identity, second.identity)

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

    def test_blob_tag_is_not_a_commit_witness_or_valid_target(self):
        blob = git(self.source, "rev-parse", "HEAD:source.txt").decode().strip()
        git(self.source, "tag", "blob", blob)
        git(self.source, "push", self.endpoint, "refs/tags/blob")
        self.assert_refuses("unpublished", lambda: self.verify(blob))
        git(self.remote, "update-ref", "-d", "refs/heads/main")
        self.assert_refuses("unpublished", self.verify)

    def test_empty_remote_refuses_without_fetch(self):
        git(self.remote, "update-ref", "-d", "refs/heads/main")
        repositories = []

        def runner(command, **kwargs):
            if kwargs["cwd"] not in repositories:
                repositories.append(kwargs["cwd"])
            self.assertNotIn("fetch", command)
            return publication.run_bounded_process(command, **kwargs)

        self.assert_refuses("unpublished", lambda: self.verify(process_runner=runner))
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

    def test_fetched_ref_mismatch_refuses_even_if_advertisements_match(self):
        def runner(command, **kwargs):
            result = publication.run_bounded_process(command, **kwargs)
            if "for-each-ref" in command:
                return BoundedProcessResult(
                    0,
                    result.stdout.replace(self.latest.encode(), self.first.encode()),
                    b"",
                )
            return result

        self.assert_refuses("refs_changed", lambda: self.verify(process_runner=runner))

    def test_sha256_history_is_proved_in_a_matching_fresh_object_database(self):
        source = self.base / "sha256"
        repository(source, sha256=True)
        commit = git(source, "rev-parse", "HEAD").decode().strip()
        before = snapshot(self.base)
        proof = publication.verify_repository_publication(
            source.as_uri(), commit, deadline_policy=self.policy
        )
        self.assertEqual(len(proof.commit), 64)
        self.assertEqual(snapshot(self.base), before)

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

    def test_transport_errors_are_bounded_sanitized_and_cleanup_owned_storage(self):
        secret = "fixture-secret-must-not-appear"
        for code, suffix in (
            ("git_timeout", "timeout"),
            ("git_no_progress_timeout", "no_progress_timeout"),
            ("git_output_stream", "transport_failed"),
        ):
            with self.subTest(code=code):
                visited = []

                def runner(command, visited=visited, code=code, **kwargs):
                    visited.append(kwargs["cwd"])
                    self.assertGreater(kwargs["timeout_seconds"], 0)
                    self.assertLessEqual(kwargs["timeout_seconds"], 120)
                    self.assertLessEqual(kwargs["inactivity_timeout_seconds"], 60)
                    self.assertEqual(
                        kwargs["stdout_limit_bytes"], publication._STREAM_BYTES
                    )
                    raise BuildError(code, secret)

                error = self.assert_refuses(
                    suffix, lambda: self.verify(process_runner=runner)
                )
                self.assertNotIn(secret, "".join(traceback.format_exception(error)))
                self.assertTrue(all(not path.exists() for path in visited))

    def test_deadline_rounding_never_increases_the_reviewed_process_budget(self):
        now = 399.99999999999994
        # Deterministically recreate the rounding seen on hosted Windows.
        self.assertGreater((now + 120) - now, 120)
        observed = []

        def runner(command, **kwargs):
            observed.append(kwargs["timeout_seconds"])
            self.assertLessEqual(kwargs["timeout_seconds"], 120)
            raise BuildError("git_output_stream", "fixture transport failure")

        self.assert_refuses(
            "transport_failed",
            lambda: self.verify(process_runner=runner, clock=lambda: now),
        )
        self.assertTrue(observed)

    def test_semantic_failure_remains_primary_when_proof_cleanup_also_fails(self):
        calls = []
        cleanup_calls = []
        original_cleanup = publication.tempfile.TemporaryDirectory.cleanup

        def cleanup(temporary):
            original_cleanup(temporary)
            cleanup_calls.append(temporary.name)
            raise OSError("cleanup-secret")

        def runner(command, **kwargs):
            calls.append(command)
            if "ls-remote" in command:
                return BoundedProcessResult(0, b"malformed\n", b"")
            return BoundedProcessResult(0, b"", b"")

        with patch.object(
            publication.tempfile.TemporaryDirectory,
            "cleanup",
            autospec=True,
            side_effect=cleanup,
        ):
            error = self.assert_refuses(
                "refs_invalid",
                lambda: self.verify(process_runner=runner, clock=lambda: 0),
            )
        self.assertIsInstance(error.__cause__, publication._PublicationCleanupFailure)
        self.assertEqual(
            error.__cause__.code, "orchestration.publication_transport_failed"
        )
        self.assertEqual(len(cleanup_calls), 1)
        self.assertTrue(all(not Path(path).exists() for path in cleanup_calls))
        self.assertEqual(sum("ls-remote" in command for command in calls), 1)
        self.assertNotIn("cleanup-secret", "".join(traceback.format_exception(error)))

    def test_transient_failure_with_cleanup_failure_retries_by_primary_type(self):
        calls = []
        cleanup_calls = []
        original_cleanup = publication.tempfile.TemporaryDirectory.cleanup

        def cleanup(temporary):
            original_cleanup(temporary)
            cleanup_calls.append(temporary.name)
            raise OSError("cleanup-secret")

        def runner(command, **kwargs):
            calls.append(command)
            return BoundedProcessResult(128, b"git-secret", b"git-secret")

        with patch.object(
            publication.tempfile.TemporaryDirectory,
            "cleanup",
            autospec=True,
            side_effect=cleanup,
        ):
            error = self.assert_refuses(
                "git_failed",
                lambda: self.verify(process_runner=runner, clock=lambda: 0),
            )
        self.assertIsInstance(error.__cause__, publication._PublicationCleanupFailure)
        self.assertEqual(
            error.__cause__.code, "orchestration.publication_transport_failed"
        )
        self.assertEqual(len(calls), publication._MAXIMUM_ATTEMPTS)
        self.assertEqual(len(cleanup_calls), publication._MAXIMUM_ATTEMPTS)
        self.assertTrue(all(not Path(path).exists() for path in cleanup_calls))
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn("cleanup-secret", rendered)
        self.assertNotIn("git-secret", rendered)

    def test_successful_proof_with_cleanup_failure_remains_transport_failure(self):
        cleanup_calls = []
        original_cleanup = publication.tempfile.TemporaryDirectory.cleanup

        def cleanup(temporary):
            original_cleanup(temporary)
            cleanup_calls.append(temporary.name)
            raise OSError("cleanup-secret")

        with patch.object(
            publication.tempfile.TemporaryDirectory,
            "cleanup",
            autospec=True,
            side_effect=cleanup,
        ):
            error = self.assert_refuses("transport_failed", self.verify)
        self.assertIsInstance(error, publication._PublicationCleanupFailure)
        self.assertIsNone(error.__cause__)
        self.assertEqual(len(cleanup_calls), publication._MAXIMUM_ATTEMPTS)
        self.assertTrue(all(not Path(path).exists() for path in cleanup_calls))
        self.assertNotIn("cleanup-secret", "".join(traceback.format_exception(error)))

    def test_transient_failure_retries_verify_tree_and_pack_in_fresh_storage(self):
        def retry_fixture(transient_code, failure_command):
            state = {"elapsed": 0, "failed": False}
            repositories = []
            second_attempt_bounds = []

            def clock():
                return state["elapsed"]

            def runner(command, **kwargs):
                repository = kwargs["cwd"]
                if repository not in repositories:
                    repositories.append(repository)
                if not state["failed"] and (
                    failure_command is None or failure_command in command
                ):
                    state["failed"] = True
                    state["elapsed"] = 80
                    if transient_code is not None:
                        raise BuildError(transient_code, "secret")
                    return BoundedProcessResult(128, b"secret", b"secret")
                if state["failed"]:
                    second_attempt_bounds.append(
                        (
                            kwargs["timeout_seconds"],
                            kwargs["inactivity_timeout_seconds"],
                            kwargs["environment"]["GIT_SSH_COMMAND"],
                        )
                    )
                return publication.run_bounded_process(command, **kwargs)

            return clock, runner, repositories, second_attempt_bounds

        operations = (
            (publication.verify_repository_publication, None, None),
            (
                publication.capture_published_repository_tree,
                "git_output_stream",
                "ls-tree",
            ),
            (
                publication.capture_published_repository_pack,
                "git_timeout",
                "bundle",
            ),
        )
        for operation, transient_code, failure_command in operations:
            with self.subTest(operation=operation.__name__):
                before = snapshot(self.base)
                reviewed_policy = RepositoryFetchDeadlinePolicy(120, 100, 90)
                clock, runner, repositories, second_attempt_bounds = retry_fixture(
                    transient_code, failure_command
                )

                captured = operation(
                    self.endpoint,
                    self.first,
                    deadline_policy=reviewed_policy,
                    process_runner=runner,
                    clock=clock,
                )
                proof = (
                    captured
                    if isinstance(
                        captured, publication.RepositoryPublicationObservation
                    )
                    else captured.publication
                )
                self.assertEqual(proof.commit, self.first)
                self.assertEqual(
                    proof.deadline_policy_identity, reviewed_policy.identity.uri
                )
                self.assertEqual(len(repositories), 2)
                self.assertNotEqual(*repositories)
                self.assertTrue(all(not path.exists() for path in repositories))
                self.assertTrue(second_attempt_bounds)
                self.assertTrue(
                    all(timeout <= 40 for timeout, _, _ in second_attempt_bounds)
                )
                self.assertTrue(
                    all(inactivity <= 40 for _, inactivity, _ in second_attempt_bounds)
                )
                self.assertTrue(
                    all(
                        "ConnectTimeout=40" in ssh
                        for _, _, ssh in second_attempt_bounds
                    )
                )
                self.assertEqual(snapshot(self.base), before)

    def test_semantic_invalid_advertisement_is_not_retried(self):
        repositories = []

        def runner(command, **kwargs):
            if kwargs["cwd"] not in repositories:
                repositories.append(kwargs["cwd"])
            if "ls-remote" in command:
                return BoundedProcessResult(0, b"malformed\n", b"")
            return BoundedProcessResult(0, b"", b"")

        self.assert_refuses(
            "refs_invalid",
            lambda: self.verify(process_runner=runner, clock=lambda: 0),
        )
        self.assertEqual(len(repositories), 1)

    def test_transient_retry_cap_returns_the_last_typed_failure(self):
        failures = iter(
            (
                BuildError("git_timeout", "first secret"),
                None,
                BuildError("git_no_progress_timeout", "last secret"),
            )
        )
        repositories = []

        def runner(command, **kwargs):
            if kwargs["cwd"] not in repositories:
                repositories.append(kwargs["cwd"])
            failure = next(failures)
            if failure is not None:
                raise failure
            return BoundedProcessResult(128, b"middle secret", b"middle secret")

        error = self.assert_refuses(
            "no_progress_timeout",
            lambda: self.verify(process_runner=runner, clock=lambda: 0),
        )
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn("first secret", rendered)
        self.assertNotIn("middle secret", rendered)
        self.assertNotIn("last secret", rendered)
        self.assertEqual(len(repositories), publication._MAXIMUM_ATTEMPTS)
        self.assertTrue(all(not path.exists() for path in repositories))

    def test_total_budget_exhaustion_returns_last_failure_without_new_attempt(self):
        elapsed = 0
        calls = []

        def clock():
            return elapsed

        def runner(command, **kwargs):
            nonlocal elapsed
            calls.append(command)
            elapsed = 120
            return BoundedProcessResult(128, b"secret", b"secret")

        self.assert_refuses(
            "git_failed",
            lambda: self.verify(process_runner=runner, clock=clock),
        )
        self.assertEqual(len(calls), 1)

    def test_nonzero_git_result_never_exposes_remote_diagnostics(self):
        self.assert_refuses(
            "git_failed",
            lambda: self.verify(
                process_runner=lambda *args, **kwargs: BoundedProcessResult(
                    128, b"secret", b"secret"
                )
            ),
        )

    def test_one_total_deadline_is_shared_across_commands_and_attempts(self):
        elapsed = 0
        calls = []

        def runner(command, **kwargs):
            nonlocal elapsed
            calls.append(command)
            elapsed = 121
            return BoundedProcessResult(0, b"", b"")

        self.assert_refuses(
            "timeout",
            lambda: self.verify(process_runner=runner, clock=lambda: elapsed),
        )
        self.assertEqual(len(calls), 1)

    def test_unsafe_and_unresolved_endpoints_refuse_before_process_start(self):
        for endpoint in (
            "../child.git",
            "child.git",
            "C:relative.git",
            "https://user:secret@example.test/repo",
            "ext::command",
            "-option",
            "https://example.test/repo?token=secret",
        ):
            with self.subTest(endpoint=endpoint):
                self.assert_refuses(
                    "endpoint_invalid",
                    lambda endpoint=endpoint: publication.verify_repository_publication(
                        endpoint,
                        self.first,
                        process_runner=lambda *args, **kwargs: self.fail(
                            "must not run"
                        ),
                    ),
                )

    def test_advertisement_parser_refuses_duplicate_malformed_or_unbounded_input(self):
        valid = self.first.encode() + b"\trefs/heads/main\n"
        for raw in (
            valid + valid,
            valid[:-1],
            b"invalid\n",
            valid.replace(b"main", b"../main"),
            b"0" * 40 + b"\trefs/heads/main\n",
        ):
            with self.subTest(raw=raw):
                self.assert_refuses(
                    "refs_invalid", lambda raw=raw: publication._references(raw)
                )
        with patch.object(publication, "_MAXIMUM_REFS", 1):
            self.assert_refuses(
                "refs_invalid",
                lambda: publication._references(
                    valid + valid.replace(b"main", b"other")
                ),
            )

    def test_invalid_commit_and_policy_refuse_before_any_process(self):
        def must_not_run(*args, **kwargs):
            self.fail("invalid input must not start a process")

        for commit in ("HEAD", "0" * 40, "A" * 40, "1" * 41):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                publication.verify_repository_publication(
                    self.endpoint, commit, process_runner=must_not_run
                )
        for policy in (False, 0, {}):
            with self.subTest(policy=policy), self.assertRaises(TypeError):
                publication.verify_repository_publication(
                    self.endpoint,
                    self.first,
                    deadline_policy=policy,
                    process_runner=must_not_run,
                )

    def test_protected_storage_refuses_before_git_without_touching_the_repository(self):
        before = snapshot(self.base)
        with patch.object(
            publication.tempfile, "gettempdir", return_value=str(self.source / ".git")
        ):
            self.assert_refuses(
                "storage_overlap",
                lambda: publication.verify_repository_publication(
                    self.endpoint,
                    self.first,
                    protected_roots=(self.source,),
                    process_runner=lambda *args, **kwargs: self.fail("must not run"),
                ),
            )
        self.assertEqual(snapshot(self.base), before)


if __name__ == "__main__":
    unittest.main()

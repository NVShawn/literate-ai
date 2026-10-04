"""Root-authorized endpoint selection and real remote refresh proof composition."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from literate_ai.adapters import repository_publication as transport
from literate_ai.adapters import repository_refresh as custody
from literate_ai.adapters import repository_refresh_publication as publication
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_lineage import RepositoryFetchDeadlinePolicy
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from tests.support import fixtures_test_repository_refresh_inputs as fixtures
from tests.support.fixtures_test_repository_orchestration import git, snapshot


class RefreshPublicationTests(TestCase):
    def setUp(self):
        fixture = fixtures.RepositoryRefreshInputTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root, self.base = fixture.root, fixture.base
        self.child = self.root / "app"
        self.remote = self.base / "app.git"
        # Git preserves the local prefix spelling and appends relative URL
        # components with '/', including when that prefix uses Windows '\\'.
        self.local_endpoint = str(self.base) + "/app.git"
        git(
            self.base,
            "clone",
            "--bare",
            "--no-hardlinks",
            str(self.child),
            str(self.remote),
        )
        self.pin = git(self.child, "rev-parse", "HEAD").decode().strip()
        git(self.remote, "update-ref", "refs/heads/main", self.pin)
        git(
            self.root, "config", "remote.origin.url", (self.base / "super.git").as_uri()
        )
        self.policy = RepositoryFetchDeadlinePolicy(120, 60, 10)

    def inputs(self, commit=None):
        return custody.prepare_repository_refresh(
            self.root,
            RepositoryRefreshRequest(
                (RepositoryRefreshTarget("app", commit or self.pin),)
            ),
        )

    def prepare(self, inputs=None):
        return publication.prepare_refresh_publication(
            inputs or self.inputs(), deadline_policy=self.policy
        )

    def advance(self, *, publish):
        git(self.child, "commit", "--allow-empty", "-q", "-m", "new child")
        commit = git(self.child, "rev-parse", "HEAD").decode().strip()
        if publish:
            git(self.child, "push", self.remote.as_uri(), "HEAD:refs/heads/main")
        return commit

    def assert_refuses(self, suffix, action):
        with self.assertRaises(OrchestrationInventoryError) as caught:
            action()
        self.assertEqual(caught.exception.code, "orchestration." + suffix)

    def test_published_refresh_binds_exact_root_request_and_preserves_all_input_bytes(
        self,
    ):
        commit = self.advance(publish=True)
        inputs = self.inputs(commit)
        before = snapshot(self.base)
        prepared = self.prepare(inputs)
        self.assertEqual(prepared.refresh, inputs)
        self.assertEqual(prepared.endpoints, (("app", self.remote.as_uri()),))
        self.assertEqual(
            tuple(proof.commit for proof in prepared.observations), (commit,)
        )
        self.assertEqual(prepared.target_modes, (("app", "root-pin-only"),))
        self.assertNotEqual(
            inputs.authority.previous.identity, inputs.authority.prospective.identity
        )
        publication.require_refresh_publication_unchanged(prepared)
        self.assertEqual(self.prepare(inputs).identity, prepared.identity)
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse(inputs.authority.to_dict()["apply_supported"])
        with self.assertRaises(OrchestrationInventoryError):
            replace(
                prepared,
                target_modes=(("app", "source-transition"),),
            )

    def test_noop_intent_still_requires_a_remote_proof(self):
        inputs = self.inputs()
        self.assertEqual(inputs.authority.to_dict()["changes"], [])
        prepared = self.prepare(inputs)
        self.assertEqual(len(prepared.observations), 1)
        self.assertEqual(prepared.observations[0].commit, self.pin)

    def test_unpublished_local_head_is_refused_without_input_writes(self):
        inputs = self.inputs(self.advance(publish=False))
        before = snapshot(self.base)
        self.assert_refuses("publication_unpublished", lambda: self.prepare(inputs))
        self.assertEqual(snapshot(self.base), before)

    def test_child_and_submodule_origin_overrides_do_not_choose_publication_authority(
        self,
    ):
        git(
            self.child,
            "config",
            "remote.origin.url",
            "https://ignored.example/child.git",
        )
        git(
            self.root,
            "config",
            "submodule.app.url",
            "https://ignored.example/override.git",
        )
        verify = publication.verify_repository_publication

        def only_declared_endpoint(endpoint, *args, **kwargs):
            self.assertEqual(endpoint, self.remote.as_uri())
            return verify(endpoint, *args, **kwargs)

        with patch.object(
            publication, "verify_repository_publication", only_declared_endpoint
        ):
            prepared = self.prepare()
        self.assertEqual(prepared.endpoints, (("app", self.remote.as_uri()),))

    def test_relative_urls_follow_git_default_remote_forms_without_network(self):
        cases = (
            (
                "https://example.test/group/super.git",
                "https://example.test/group/app.git",
            ),
            ("git@example.test:group/super.git", "git@example.test:group/app.git"),
            (
                "ssh://git@example.test:2222/group/super.git",
                "ssh://git@example.test:2222/group/app.git",
            ),
            ((self.base / "super.git").as_uri(), self.remote.as_uri()),
            (str(self.base / "super.git"), self.local_endpoint),
            ("../super.git", self.local_endpoint),
        )
        for default, expected in cases:
            with self.subTest(default=default):
                git(self.root, "config", "remote.origin.url", default)
                prepared = self.inputs()
                before = snapshot(self.base)
                self.assertEqual(
                    publication.resolve_refresh_endpoints(prepared),
                    (("app", expected),),
                )
                self.assertEqual(snapshot(self.base), before)

    def test_git_resolved_local_endpoint_proves_publication_without_network(self):
        for default in (str(self.base / "super.git"), "../super.git"):
            with self.subTest(default=default):
                git(self.root, "config", "remote.origin.url", default)
                inputs = self.inputs()
                before = snapshot(self.base)
                prepared = self.prepare(inputs)
                self.assertEqual(prepared.endpoints, (("app", self.local_endpoint),))
                self.assertEqual(prepared.observations[0].commit, self.pin)
                publication.require_refresh_publication_unchanged(prepared)
                self.assertEqual(snapshot(self.base), before)

    def test_tracking_remote_detached_origin_and_local_upstream_selection(self):
        git(self.root, "config", "branch.main.remote", "upstream")
        git(
            self.root,
            "config",
            "remote.upstream.url",
            "https://example.test/group/super.git",
        )
        self.assertEqual(
            publication.resolve_refresh_endpoints(self.inputs()),
            (("app", "https://example.test/group/app.git"),),
        )
        git(self.root, "checkout", "--detach", "HEAD")
        self.assertEqual(
            publication.resolve_refresh_endpoints(self.inputs()),
            (("app", self.remote.as_uri()),),
        )
        git(self.root, "checkout", "main")
        git(self.root, "config", "branch.main.remote", ".")
        self.assertEqual(
            publication.resolve_refresh_endpoints(self.inputs()),
            (("app", self.local_endpoint),),
        )
        git(self.root, "config", "--unset", "branch.main.remote")
        git(self.root, "config", "--unset", "remote.origin.url")
        self.assertEqual(
            publication.resolve_refresh_endpoints(self.inputs()),
            (("app", self.local_endpoint),),
        )

    def test_duplicate_and_credential_bearing_remote_configuration_refuses(self):
        original = (self.base / "super.git").as_uri()
        for key, value in (
            ("remote.origin.url", "https://example.test/other.git"),
            ("branch.main.remote", "upstream"),
        ):
            with self.subTest(key=key):
                if key.startswith("branch"):
                    git(self.root, "config", key, "origin")
                git(self.root, "config", "--add", key, value)
                inputs = self.inputs()
                before = snapshot(self.base)
                self.assert_refuses(
                    "refresh_endpoint_invalid",
                    lambda inputs=inputs: publication.resolve_refresh_endpoints(inputs),
                )
                self.assertEqual(snapshot(self.base), before)
                git(self.root, "config", "--unset-all", key)
                git(self.root, "config", "remote.origin.url", original)
        git(
            self.root,
            "config",
            "remote.origin.url",
            "https://user:fixture-secret@example.test/repo",
        )
        inputs = self.inputs()
        self.assert_refuses(
            "refresh_endpoint_invalid",
            lambda: publication.resolve_refresh_endpoints(inputs),
        )

    def test_root_remote_change_after_preparation_refuses_before_transport(self):
        inputs = self.inputs()
        git(self.root, "config", "remote.origin.url", "https://example.test/other.git")
        with patch.object(
            publication,
            "verify_repository_publication",
            side_effect=AssertionError("transport must not run"),
        ):
            self.assert_refuses("inputs_changed", lambda: self.prepare(inputs))

    def test_default_remote_preserves_legal_unicode_whitespace_in_branch_names(self):
        branch = "topic\u00a0"
        expected = "https://example.test/group/super.git"
        git(self.root, "checkout", "-b", branch)
        git(self.root, "config", "branch." + branch + ".remote", "upstream")
        git(self.root, "config", "remote.upstream.url", expected)
        self.assertEqual(publication._default_remote(self.inputs()), expected)

    def test_child_mutation_during_transport_refuses_and_preserves_foreign_file(self):
        inputs = self.inputs()
        verify = publication.verify_repository_publication
        foreign = self.child / "foreign.txt"

        def mutate(*args, **kwargs):
            proof = verify(*args, **kwargs)
            foreign.write_bytes(b"concurrent owner")
            return proof

        with patch.object(publication, "verify_repository_publication", mutate):
            self.assert_refuses("refresh_child_dirty", lambda: self.prepare(inputs))
        self.assertEqual(foreign.read_bytes(), b"concurrent owner")

    def test_changed_advertisement_invalidates_a_retained_preparation(self):
        prepared = self.prepare()
        git(self.remote, "update-ref", "refs/tags/new-witness", self.pin)
        before = snapshot(self.base)
        self.assert_refuses(
            "refresh_publication_changed",
            lambda: publication.require_refresh_publication_unchanged(prepared),
        )
        self.assertEqual(snapshot(self.base), before)

    def test_wrong_commit_endpoint_or_policy_proof_cannot_be_bound(self):
        prepared = self.prepare()
        proof = prepared.observations[0]
        for change in (
            {"commit": "4" * 40},
            {"repository_identity": "sha256:" + "1" * 64},
            {"deadline_policy_identity": "sha256:" + "2" * 64},
        ):
            with (
                self.subTest(change=change),
                patch.object(
                    publication,
                    "verify_repository_publication",
                    return_value=replace(proof, **change),
                ),
            ):
                self.assert_refuses(
                    "refresh_publication_mismatch",
                    lambda: self.prepare(prepared.refresh),
                )

    def test_temporary_storage_cannot_be_placed_in_root_or_child_git_metadata(self):
        inputs = self.inputs()
        for path in (self.root, self.child / ".git", inputs.root_git.common_directory):
            with (
                self.subTest(path=path),
                patch.object(transport.tempfile, "gettempdir", return_value=str(path)),
            ):
                before = snapshot(self.base)
                self.assert_refuses(
                    "publication_storage_overlap", lambda: self.prepare(inputs)
                )
                self.assertEqual(snapshot(self.base), before)

    def test_object_format_change_refuses_before_endpoint_or_transport_work(self):
        inputs = self.inputs("4" * 64)
        with patch.object(
            publication,
            "_default_remote",
            side_effect=AssertionError("must not resolve"),
        ):
            self.assert_refuses(
                "refresh_object_format_mismatch", lambda: self.prepare(inputs)
            )

    def test_multiple_targets_bind_ordered_complete_proofs_from_declared_urls(self):
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
        fixture = self.fixture.fixture
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
                    RepositoryRefreshTarget(path, self.pin) for path in ("lib", "app")
                )
            ),
        )
        before = snapshot(self.base)
        prepared = self.prepare(inputs)
        self.assertEqual(
            prepared.endpoints,
            (("app", self.remote.as_uri()), ("lib", remote.as_uri())),
        )
        self.assertEqual(len(prepared.observations), 2)
        self.assertEqual(
            tuple(proof.commit for proof in prepared.observations), (self.pin, self.pin)
        )
        self.assertNotEqual(
            prepared.observations[0].repository_identity,
            prepared.observations[1].repository_identity,
        )
        self.assertEqual(snapshot(self.base), before)

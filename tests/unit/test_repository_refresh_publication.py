"""Root-authorized endpoint selection and real remote refresh proof composition."""

from __future__ import annotations

from unittest import TestCase

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

    def test_unpublished_local_head_is_refused_without_input_writes(self):
        inputs = self.inputs(self.advance(publish=False))
        before = snapshot(self.base)
        self.assert_refuses("publication_unpublished", lambda: self.prepare(inputs))
        self.assertEqual(snapshot(self.base), before)

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

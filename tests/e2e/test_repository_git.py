"""Real Git source locking through a local fixture transport, without a build."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.source.repository_git import (
    GitRepositorySourceAcquirer,
    GitRepositorySourceCapturer,
)
from literate_ai.application.repository_sources import (
    RepositorySourceLockResolver,
    RepositorySourceResolver,
)
from tests.support.fixtures_test_repository_sources import (
    _Authorizer,
    _Builder,
    _Cache,
    _Indexer,
    _Planner,
    dependency,
    identity,
)


@unittest.skipUnless(shutil.which("git"), "Git is required")
class RepositoryGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vendor_directory = tempfile.TemporaryDirectory(
            prefix="original source ", dir=self.root
        )
        self.addCleanup(self.vendor_directory.cleanup)
        self.vendor = Path(self.vendor_directory.name)
        self.git("init", "--template=", "-b", "main")
        self.git("config", "user.name", "Source fixture")
        self.git("config", "user.email", "fixture@example.test")
        (self.vendor / "build.txt").write_bytes(b"original native source\n")
        self.commit = self.commit_source()
        self.commands: list[tuple[str, ...]] = []

        def local_transport(command, **kwargs):
            command = tuple(command)
            self.commands.append(command)
            # Only the transport is substituted. Fetch, checkout, capture, hashing,
            # detached revision selection and cleanup all use the actual Git tools.
            command = tuple(
                self.vendor.as_uri() if value == dependency().repository_url else value
                for value in command
            )
            return run_bounded_process(command, **kwargs)

        transport = patch(
            "literate_ai.adapters.source.repository_git.run_bounded_process",
            side_effect=local_transport,
        )
        transport.start()
        self.addCleanup(transport.stop)
        self.acquirer = GitRepositorySourceAcquirer(scratch_root=self.root)
        self.capturer = GitRepositorySourceCapturer()
        self.resolver = RepositorySourceLockResolver(
            acquirer=self.acquirer, capturer=self.capturer
        )

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(self.vendor), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit_source(self) -> str:
        self.git("add", "build.txt")
        self.git("commit", "-m", "fixture source")
        return self.git("rev-parse", "HEAD")

    def admission_resolver(self, events: list[str]) -> RepositorySourceResolver:
        return RepositorySourceResolver(
            acquirer=self.acquirer,
            capturer=self.capturer,
            indexer=_Indexer(events),
            planner=_Planner(events),
            authorizer=_Authorizer(events),
            builder=_Builder(events),
            cache=_Cache(events),
        )

    def test_exact_lock_is_reproducible_and_cleans_temporary_source(self) -> None:
        selected = dependency()
        first = self.resolver.lock(selected)
        self.assertEqual(first, self.resolver.lock(selected))
        self.assertEqual(first.resolved_commit, self.commit)
        self.assertEqual(first.dependency, selected)
        self.assertEqual(list(self.root.glob("litai-source-*")), [])
        self.assertTrue(
            all(Path(command[0]).name.startswith("git") for command in self.commands)
        )

    def test_moved_branch_does_not_retarget_locked_admission(self) -> None:
        first = self.resolver.lock(dependency())
        (self.vendor / "build.txt").write_bytes(b"changed source\n")
        second_commit = self.commit_source()
        self.assertEqual(
            self.resolver.lock(dependency()).resolved_commit, second_commit
        )
        events: list[str] = []
        admitted = self.admission_resolver(events).resolve(
            dependency(),
            expected_lock=first,
            effective_revision=identity("effective"),
            flavor_set=identity("flavors"),
            toolchains=(identity("cmake"),),
        )
        self.assertEqual(admitted.lock, first)
        self.assertIn("build", events)

    def test_ambient_git_directory_and_filter_settings_cannot_change_capture(
        self,
    ) -> None:
        (self.vendor / ".gitattributes").write_text(
            "build.txt filter=fixture\n", encoding="utf-8"
        )
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "filter declaration")
        expected = self.resolver.lock(dependency())
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(self.root / "wrong-repository"),
                "GIT_WORK_TREE": str(self.root / "wrong-tree"),
                "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "filter.fixture.smudge",
                "GIT_CONFIG_VALUE_0": "command-that-must-not-run",
                "GIT_CONFIG_KEY_1": "filter.fixture.required",
                "GIT_CONFIG_VALUE_1": "true",
            },
        ):
            self.assertEqual(self.resolver.lock(dependency()), expected)

"""Real Git custody fixtures for read-only orchestration inventory."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_orchestration as orchestration
from tests.support.fixtures_test_repository_orchestration import (
    git,
    repository,
    snapshot,
)


class RepositoryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "super"
        repository(self.root)
        self.pin = git(self.root, "rev-parse", "HEAD").decode().strip()

    def modules(self, text: str) -> None:
        (self.root / ".gitmodules").write_text(text, encoding="utf-8")
        git(self.root, "add", ".gitmodules")

    def uninitialized(self, *, url: str = "../child.git", branch: str | None = None):
        self.modules(
            '[submodule "child.name"]\n\tpath = children/one\n\turl = '
            + url
            + "\n"
            + ("\tbranch = " + branch + "\n" if branch is not None else "")
        )
        git(
            self.root,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            self.pin,
            "children/one",
        )

    def refusal(self, code: str | None = None):
        before = snapshot(self.root)
        with self.assertRaises(orchestration.OrchestrationInventoryError) as caught:
            orchestration.inspect_gitlink_inventory(self.root)
        if code is not None:
            self.assertEqual(caught.exception.code, "orchestration." + code)
        self.assertEqual(snapshot(self.root), before)
        return caught.exception

    def test_exact_uninitialized_inventory_is_immutable_read_only_and_repeatable(self):
        self.uninitialized(branch=".")
        before = snapshot(self.root)
        result = orchestration.inspect_gitlink_inventory(self.root)
        self.assertEqual(
            result.children,
            (
                orchestration.GitlinkObservation(
                    "child.name",
                    "children/one",
                    "../child.git",
                    self.pin,
                    ".",
                    "uninitialized",
                    None,
                ),
            ),
        )
        self.assertTrue(result.gitmodules_identity.startswith("sha256:"))
        self.assertEqual(
            result.identity, orchestration.inspect_gitlink_inventory(self.root).identity
        )
        self.assertEqual(snapshot(self.root), before)
        with self.assertRaises(FrozenInstanceError):
            result.children[0].commit = "changed"
        wire = result.to_dict()
        wire["children"].clear()
        self.assertEqual(len(result.children), 1)

    def test_credential_bearing_urls_refuse_without_echo(self):
        for url in (
            "https://secret-token@example.test/child.git",
            "ssh://git:secret-token@example.test/child.git",
            "https://example.test/child.git?token=secret-token",
            "ext::secret-token",
        ):
            with self.subTest(url=url):
                self.uninitialized(url=url)
                error = self.refusal("modules_invalid")
                self.assertNotIn("secret-token", str(error))

    def test_ambient_git_repository_and_index_selectors_are_ignored(self):
        self.uninitialized()
        other = self.base / "other"
        repository(other)
        expected = orchestration.inspect_gitlink_inventory(self.root)
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(other / ".git"),
                "GIT_WORK_TREE": str(other),
                "GIT_INDEX_FILE": str(other / ".git/index"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            },
        ):
            self.assertEqual(
                orchestration.inspect_gitlink_inventory(self.root), expected
            )

    def test_symlinked_configuration_and_child_paths_refuse(self):
        self.uninitialized()
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.root / "children").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlink fixture creation")
        self.refusal()
        (self.root / "children").unlink()
        modules = self.root / ".gitmodules"
        original = modules.read_bytes()
        external = outside / "config"
        external.write_bytes(original)
        modules.unlink()
        modules.symlink_to(external)
        self.refusal("modules_invalid")

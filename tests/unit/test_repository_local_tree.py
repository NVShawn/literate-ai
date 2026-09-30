"""Native old-object capture is bounded, isolated and nonmutating."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import repository_local_tree as local
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.contracts.repository_tree import RepositoryTreeCapturePolicy
from tests.unit.test_repository_orchestration import git, repository, snapshot


class RepositoryLocalTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "repo"
        repository(self.root)
        self.commit = git(self.root, "rev-parse", "HEAD").decode().strip()

    def capture(self, commit=None, **options):
        return local.capture_local_repository_tree(
            self.root,
            commit or self.commit,
            policy=RepositoryTreeCapturePolicy(**options),
        )

    def test_exact_objects_ignore_ambient_selectors_and_do_not_write(self):
        before = snapshot(self.root)
        runner = local.run_bounded_process
        calls = []

        def guarded(argv, **options):
            environment = options["environment"]
            self.assertNotIn("GIT_DIR", environment)
            self.assertEqual(environment["GIT_ALLOW_PROTOCOL"], "")
            self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
            self.assertIn("core.hooksPath=" + os.devnull, argv)
            self.assertTrue("cat-file" in argv or "ls-tree" in argv)
            calls.append(argv)
            return runner(argv, **options)

        with patch.dict(os.environ, {"GIT_DIR": "missing", "GIT_CONFIG_COUNT": "99"}):
            with patch.object(local, "run_bounded_process", guarded):
                captured = self.capture()
        self.assertEqual(captured.commit, self.commit)
        self.assertEqual(captured.entries[0].content, b"original\n")
        self.assertTrue(calls)
        self.assertEqual(snapshot(self.root), before)

    def test_missing_object_and_too_small_budget_refuse_without_writes(self):
        before = snapshot(self.root)
        for options in ({"commit": "4" * 40}, {"maximum_blob_bytes": 1}):
            with self.assertRaises(OrchestrationInventoryError):
                self.capture(**options)
        self.assertEqual(snapshot(self.root), before)

    def test_raw_blob_above_inventory_command_limit_is_supported(self):
        content = b"x" * (8 * 1024 * 1024 + 1)
        (self.root / "large").write_bytes(content)
        git(self.root, "add", "large")
        git(self.root, "commit", "-q", "-m", "large object")
        commit = git(self.root, "rev-parse", "HEAD").decode().strip()
        captured = self.capture(commit)
        self.assertEqual(captured.entries[0].content, content)

    def test_unsafe_root_is_a_sanitized_custody_error(self):
        target = self.root
        link = self.root.parent / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlink fixtures")
        self.root = link
        with self.assertRaises(OrchestrationInventoryError) as caught:
            self.capture()
        self.assertEqual(
            caught.exception.code, "orchestration.refresh_before_unavailable"
        )
        self.assertNotIn(str(target), str(caught.exception))

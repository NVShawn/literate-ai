"""Index-format preflight must not let Git mutate shared-index timestamps."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters import repository_orchestration as inventory
from tests.support.fixtures_test_repository_orchestration import (
    git,
    repository,
    snapshot,
)


class RepositoryIndexObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "repository"
        repository(self.root)

    def test_split_index_refusal_preserves_shared_file_bytes_and_mtimes(self):
        for version in (2, 4):
            with self.subTest(version=version):
                git(self.root, "update-index", "--index-version", str(version))
                git(self.root, "update-index", "--split-index")
                before = snapshot(self.root)
                with self.assertRaises(inventory.OrchestrationInventoryError) as caught:
                    inventory.inspect_gitlink_inventory(self.root)
                self.assertEqual(
                    caught.exception.code, "orchestration.split_index_unsupported"
                )
                self.assertEqual(snapshot(self.root), before)
                git(self.root, "update-index", "--no-split-index")

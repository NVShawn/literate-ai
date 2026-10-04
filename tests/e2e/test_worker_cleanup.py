"""Cleanup investigation is bounded, private, and strictly read-only."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.application.worker_cleanup import (
    CleanupRoot,
    CleanupScanPolicy,
    investigate_cleanup_candidates,
)


class WorkerCleanupInvestigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cleanup-investigation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def policy(self, **changes):
        values = {
            "roots": (
                CleanupRoot(
                    "task-cache",
                    self.root,
                    "task-owned",
                    "re-download from the pinned package source",
                    (".active",),
                    (".complete",),
                    ("cleanup-tool", "{target}"),
                ),
            ),
            "deadline_ms": 1000,
            "maximum_entries": 100,
            "maximum_depth": 4,
            "minimum_candidate_bytes": 1,
        }
        values.update(changes)
        return CleanupScanPolicy(**values)

    def test_links_are_not_followed_into_unrelated_content(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        (outside / "large").write_bytes(b"z" * 4096)
        self.addCleanup(lambda: (outside / "large").unlink(missing_ok=True))
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symbolic links")

        result = investigate_cleanup_candidates(self.policy(minimum_candidate_bytes=0))

        self.assertEqual(result.skipped_links, 1)
        self.assertEqual(result.candidates, ())


if __name__ == "__main__":
    unittest.main()

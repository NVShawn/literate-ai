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

    def test_measures_top_level_candidates_without_paths_or_deletion(self):
        inactive = self.root / "old-cache"
        inactive.mkdir()
        (inactive / "payload").write_bytes(b"x" * 32)
        (inactive / ".complete").write_text("done")
        active = self.root / "active-cache"
        active.mkdir()
        (active / ".active").write_text("owned")
        (active / "payload").write_bytes(b"y" * 16)
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

        result = investigate_cleanup_candidates(self.policy()).to_dict()

        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["deletion_authorized"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(result["candidates"][0]["root"], "task-cache")
        self.assertEqual(result["candidates"][0]["active_use"], "inactive")
        self.assertNotIn(str(self.root), str(result))
        self.assertEqual(result["candidates"][1]["active_use"], "active")
        self.assertEqual(
            before,
            {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )

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

    def test_entry_and_time_budgets_return_partial_evidence(self):
        candidate = self.root / "many"
        candidate.mkdir()
        for index in range(8):
            (candidate / str(index)).write_bytes(b"x")
        ticks = iter((0.0, 0.0, 0.0, 2.0, 2.0, 2.0))

        result = investigate_cleanup_candidates(
            self.policy(deadline_ms=1000), monotonic=lambda: next(ticks, 2.0)
        )

        self.assertEqual(result.status, "bounded-partial")
        self.assertLess(result.scanned_entries, 9)

    def test_linked_ancestor_refuses_before_candidate_traversal(self):
        real = self.root / "real"
        cache = real / "cache"
        candidate = cache / "candidate"
        candidate.mkdir(parents=True)
        (candidate / "payload").write_bytes(b"x" * 32)
        linked = self.root / "linked"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symbolic links")
        root = CleanupRoot(
            "linked-cache",
            linked / "cache",
            "task-owned",
            "rebuild",
            (),
            (),
            ("cleanup-tool", "{target}"),
        )

        result = investigate_cleanup_candidates(
            CleanupScanPolicy((root,), 1000, 100, 4, 0)
        )

        self.assertEqual(result.status, "bounded-partial")
        self.assertEqual(result.candidates, ())


if __name__ == "__main__":
    unittest.main()

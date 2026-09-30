from __future__ import annotations

import unittest

from literate_ai.application.roadmap_work import (
    RoadmapWorkError,
    RoadmapWorkRecord,
    close_work,
    record_work,
)


class RoadmapWorkTests(unittest.TestCase):
    def record(self) -> RoadmapWorkRecord:
        return RoadmapWorkRecord(
            "CORE-123",
            "Make authority executable",
            "P0",
            "framework core",
            "Move deterministic work behind Python.",
            "Use one typed connector.",
            ("none",),
            ("Implement connector",),
            ("Contract test passes",),
        )

    def test_record_is_canonical_and_duplicate_safe(self) -> None:
        content = "# Active work\n"
        updated = record_work(content, self.record())
        self.assertIn("### [ ] CORE-123 — Make authority executable", updated)
        self.assertIn("  - [ ] Implement connector", updated)
        with self.assertRaises(RoadmapWorkError) as caught:
            record_work(updated, self.record())
        self.assertEqual(caught.exception.code, "roadmap_work.duplicate")

    def test_close_requires_complete_checklists_and_is_idempotent(self) -> None:
        content = record_work("# Active work\n", self.record())
        with self.assertRaises(RoadmapWorkError) as caught:
            close_work(content, "CORE-123")
        self.assertEqual(caught.exception.code, "roadmap_work.incomplete")
        completed = content.replace("  - [ ]", "  - [x]")
        closed = close_work(completed, "CORE-123")
        self.assertIn("### [x] CORE-123", closed)
        self.assertEqual(close_work(closed, "CORE-123"), closed)


if __name__ == "__main__":
    unittest.main()

"""Display-only update conflict diffs and optional coding-agent reviews."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.project_update_conflicts import (
    CONFLICT_DIFF_SCHEMA,
    conflict_diff_document,
    enrich_conflict_files,
)
from literate_ai.adapters.project_update_reviews import (
    CONFLICT_REVIEW_SCHEMA,
    ConflictReviewError,
    review_update_conflicts,
)


class ConflictDiffTests(unittest.TestCase):
    def test_conflict_markers_are_display_only(self) -> None:
        document = conflict_diff_document(
            "flavors/javascript/flavor.md",
            ours=b"service.node-utilization-tracker\n",
            base=b"sample.portable-app\n",
            theirs=b"sample.portable-app-v2\n",
        )
        self.assertEqual(document["schema"], CONFLICT_DIFF_SCHEMA)
        self.assertIn("<<<<<<< ours", document["unified_diff"])
        self.assertIn("||||||| base", document["unified_diff"])
        self.assertIn(">>>>>>> theirs", document["unified_diff"])
        self.assertIn("service.node-utilization-tracker", document["ours"])
        enriched = enrich_conflict_files(
            [
                {
                    "path": "flavors/javascript/flavor.md",
                    "classification": "conflict",
                }
            ],
            [document],
        )
        self.assertEqual(enriched[0]["unified_diff"], document["unified_diff"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "flavors" / "javascript" / "flavor.md"
            self.assertFalse(marker.exists())


class ConflictReviewTests(unittest.TestCase):
    def test_review_is_plan_only_and_keep_local(self) -> None:
        runner = SimpleNamespace(
            run_json_task=lambda prompt, model=None: SimpleNamespace(
                response={
                    "decision": "keep-local",
                    "rationale": "local Flavor overlay still matches the slot",
                    "merged_text": None,
                }
            )
        )
        reviews = review_update_conflicts(
            [
                {
                    "path": "flavors/javascript/flavor.md",
                    "ours": "local\n",
                    "theirs": "upstream\n",
                    "unified_diff": "diff",
                }
            ],
            task_runner=runner,
        )
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["schema"], CONFLICT_REVIEW_SCHEMA)
        self.assertEqual(reviews[0]["decision"], "keep-local")
        self.assertFalse(reviews[0]["applied"])

    def test_unknown_decision_fails_closed(self) -> None:
        runner = SimpleNamespace(
            run_json_task=lambda prompt, model=None: SimpleNamespace(
                response={"decision": "overwrite-everything", "rationale": "no"}
            )
        )
        with self.assertRaises(ConflictReviewError) as raised:
            review_update_conflicts(
                [{"path": "a.md", "ours": "x", "theirs": "y"}],
                task_runner=runner,
            )
        self.assertEqual(
            raised.exception.code, "project.update_conflict_review_invalid"
        )


if __name__ == "__main__":
    unittest.main()

"""Update-derived work items must be advisory, stable, and additive.

The queue is project authority. These tests pin the three properties that make writing
to it safe: only real upstream movement produces items, ids are stable so re-running
adds nothing, and existing content is never rewritten.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.project_update_work_items import (
    DEFAULT_QUEUE_PATH,
    ProjectUpdateWorkItemError,
    record_work_items,
)
from literate_ai.application.project_update_work_items import (
    project_update_work_items,
)
from literate_ai.contracts.identity import ContentIdentity, HashAlgorithm
from literate_ai.contracts.project_initialization import (
    ProjectInitializationOrigin,
)
from literate_ai.contracts.project_updates import (
    ProjectUpdateClassification,
    ProjectUpdateFile,
    ProjectUpdatePlan,
)


def identity(seed: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, f"{abs(hash(seed)):064x}"[:64])


def origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        repository_url="https://example.invalid/literate-ai.git",
        git_revision="0" * 40,
        distribution_name="literate-ai",
        distribution_version="0.2.0",
    )


def plan(*files: tuple[str, ProjectUpdateClassification]) -> ProjectUpdatePlan:
    return ProjectUpdatePlan(
        project_identity=identity("project"),
        baseline_identity=identity("baseline"),
        previous_origin=origin(),
        upstream_origin=origin(),
        files=tuple(
            ProjectUpdateFile(
                path=path,
                classification=classification,
                baseline_identity=identity(path + "b"),
                local_identity=identity(path + "l"),
                upstream_identity=identity(path + "u"),
            )
            for path, classification in sorted(files)
        ),
    )


class WorkItemProjectionTests(unittest.TestCase):
    def test_settled_classifications_produce_nothing(self) -> None:
        settled = plan(
            ("a.md", ProjectUpdateClassification.UNCHANGED),
            ("b.md", ProjectUpdateClassification.ALREADY_CURRENT),
            ("c.md", ProjectUpdateClassification.LOCAL_ONLY),
            ("d.md", ProjectUpdateClassification.PRESERVED_DYNAMIC),
        )
        self.assertEqual(project_update_work_items(settled), ())

    def test_each_moving_classification_produces_an_item(self) -> None:
        items = project_update_work_items(
            plan(
                ("x.md", ProjectUpdateClassification.CONFLICT),
                ("y.md", ProjectUpdateClassification.UPSTREAM_ONLY),
                ("z.md", ProjectUpdateClassification.UPSTREAM_ADDED),
            )
        )
        self.assertEqual(
            {item.classification for item in items},
            {
                ProjectUpdateClassification.CONFLICT,
                ProjectUpdateClassification.UPSTREAM_ONLY,
                ProjectUpdateClassification.UPSTREAM_ADDED,
            },
        )

    def test_conflicts_are_itemized_but_the_rest_are_grouped(self) -> None:
        items = project_update_work_items(
            plan(
                ("c1.md", ProjectUpdateClassification.CONFLICT),
                ("c2.md", ProjectUpdateClassification.CONFLICT),
                ("u1.md", ProjectUpdateClassification.UPSTREAM_ONLY),
                ("u2.md", ProjectUpdateClassification.UPSTREAM_ONLY),
            )
        )
        conflicts = [
            i for i in items if i.classification is ProjectUpdateClassification.CONFLICT
        ]
        grouped = [
            i
            for i in items
            if i.classification is ProjectUpdateClassification.UPSTREAM_ONLY
        ]
        self.assertEqual(len(conflicts), 2)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].paths, ("u1.md", "u2.md"))

    def test_identifiers_are_stable_and_content_addressed(self) -> None:
        first = project_update_work_items(
            plan(("x.md", ProjectUpdateClassification.CONFLICT))
        )
        again = project_update_work_items(
            plan(("x.md", ProjectUpdateClassification.CONFLICT))
        )
        other = project_update_work_items(
            plan(("y.md", ProjectUpdateClassification.CONFLICT))
        )
        self.assertEqual(first[0].item_id, again[0].item_id)
        self.assertNotEqual(first[0].item_id, other[0].item_id)


class RecordWorkItemsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.queue = self.root.joinpath(*Path(DEFAULT_QUEUE_PATH).parts)
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.existing = "# Active work\n\n## P0\n\n### [ ] ONBOARD-001 — keep me\n"
        self.queue.write_text(self.existing, encoding="utf-8")
        self.items = project_update_work_items(
            plan(("x.md", ProjectUpdateClassification.CONFLICT))
        )

    def test_recording_appends_and_preserves_existing_content(self) -> None:
        recorded = record_work_items(self.root, self.items)
        body = self.queue.read_text(encoding="utf-8")
        self.assertEqual(recorded.recorded, (self.items[0].item_id,))
        self.assertIn("ONBOARD-001 — keep me", body)
        self.assertIn(self.items[0].item_id, body)
        self.assertTrue(body.startswith(self.existing))

    def test_recording_twice_adds_nothing(self) -> None:
        record_work_items(self.root, self.items)
        first = self.queue.read_text(encoding="utf-8")
        recorded = record_work_items(self.root, self.items)
        self.assertEqual(recorded.recorded, ())
        self.assertEqual(recorded.already_present, (self.items[0].item_id,))
        self.assertEqual(self.queue.read_text(encoding="utf-8"), first)

    def test_a_checked_off_item_is_not_reintroduced(self) -> None:
        record_work_items(self.root, self.items)
        body = self.queue.read_text(encoding="utf-8").replace(
            f"### [ ] {self.items[0].item_id}", f"### [x] {self.items[0].item_id}"
        )
        self.queue.write_text(body, encoding="utf-8")
        recorded = record_work_items(self.root, self.items)
        self.assertEqual(recorded.recorded, ())
        self.assertEqual(self.queue.read_text(encoding="utf-8"), body)

    def test_a_missing_queue_fails_closed(self) -> None:
        self.queue.unlink()
        with self.assertRaises(ProjectUpdateWorkItemError) as raised:
            record_work_items(self.root, self.items)
        self.assertEqual(raised.exception.code, "project_update.queue_missing")

    def test_nothing_to_record_leaves_the_queue_untouched(self) -> None:
        recorded = record_work_items(self.root, ())
        self.assertEqual(recorded.recorded, ())
        self.assertEqual(self.queue.read_text(encoding="utf-8"), self.existing)


if __name__ == "__main__":
    unittest.main()

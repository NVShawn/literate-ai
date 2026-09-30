"""Side-effect-free OVA shadow projection and comparison coverage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from literate_ai.compatibility.ova import (
    LifecycleState,
    OvaShadowComparator,
    ProjectionKind,
    ShadowStatus,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ova"
REPOSITORY = Path(__file__).resolve().parents[2]


class OvaShadowComparatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.comparator = OvaShadowComparator()

    def test_valid_matrix_projects_every_neutral_record_family(self) -> None:
        inventory = self.comparator.inventory(FIXTURES / "valid", label="valid")

        self.assertEqual(
            {item.kind for item in inventory.projections},
            {
                ProjectionKind.COMPONENT,
                ProjectionKind.SETTINGS,
                ProjectionKind.CACHE,
                ProjectionKind.SOURCE_PACKAGE,
                ProjectionKind.OBJECT_PACKAGE,
                ProjectionKind.PROVENANCE,
                ProjectionKind.PUBLICATION_SETTINGS,
                ProjectionKind.PUBLICATION_RECORD,
            },
        )
        component = next(
            item
            for item in inventory.projections
            if item.kind is ProjectionKind.COMPONENT
        )
        self.assertEqual(component.identity, "sample.compatibility")
        self.assertEqual(
            component.value["component"]["identity"], "sample.compatibility"
        )
        self.assertTrue(component.semantic_digest.startswith("sha256:"))
        with self.assertRaises(TypeError):
            component.value["component"]["identity"] = "changed"

    def test_exact_report_is_stable_and_source_paths_are_relative(self) -> None:
        first = self.comparator.compare(FIXTURES / "valid", FIXTURES / "valid")
        second = self.comparator.compare(FIXTURES / "valid", FIXTURES / "valid")

        self.assertTrue(first.exact)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertTrue(
            all(item.status is ShadowStatus.EXACT for item in first.comparisons)
        )
        encoded = json.dumps(first.to_dict(), sort_keys=True)
        self.assertNotIn(str(FIXTURES), encoded)

    def test_drift_and_missing_are_distinct_and_have_stable_diagnostics(self) -> None:
        report = self.comparator.compare(
            FIXTURES / "valid" / "cache.json",
            FIXTURES / "drifted" / "cache.json",
        )

        self.assertFalse(report.exact)
        self.assertEqual(len(report.comparisons), 1)
        comparison = report.comparisons[0]
        self.assertEqual(comparison.status, ShadowStatus.DRIFTED)
        self.assertNotEqual(comparison.baseline_digest, comparison.candidate_digest)
        self.assertIn(
            "ova.lifecycle.drift", {item.code for item in comparison.diagnostics}
        )

        missing = self.comparator.compare(FIXTURES / "valid", FIXTURES / "invalid")
        missing_entries = [
            item for item in missing.comparisons if item.status is ShadowStatus.MISSING
        ]
        self.assertTrue(missing_entries)
        self.assertTrue(
            all(
                {diagnostic.code for diagnostic in item.diagnostics}
                == {"ova.shadow.missing"}
                for item in missing_entries
            )
        )

    def test_invalid_and_interrupted_fixture_matrix_retains_reader_state(self) -> None:
        invalid = self.comparator.compare(FIXTURES / "invalid", FIXTURES / "invalid")
        interrupted = self.comparator.compare(
            FIXTURES / "interrupted", FIXTURES / "interrupted"
        )

        self.assertEqual(
            {item.status for item in invalid.comparisons}, {ShadowStatus.INVALID}
        )
        self.assertEqual(
            {item.status for item in interrupted.comparisons},
            {ShadowStatus.INTERRUPTED},
        )
        unreadable = next(
            item
            for item in interrupted.baseline.projections
            if item.source_artifact == "truncated-cache.json"
        )
        self.assertEqual(unreadable.lifecycle_state, LifecycleState.INTERRUPTED)
        self.assertEqual(unreadable.value["component_identity"], "unknown")

    def test_executable_report_is_json_and_does_not_modify_inputs(self) -> None:
        fixture_files = tuple(sorted((FIXTURES / "valid").iterdir()))
        before = {
            path: (path.stat().st_mtime_ns, path.read_bytes()) for path in fixture_files
        }
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "literate_ai.compatibility.ova",
                "--baseline",
                str(FIXTURES / "valid"),
                "--candidate",
                str(FIXTURES / "valid"),
            ],
            cwd=REPOSITORY,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["exact"])
        after = {
            path: (path.stat().st_mtime_ns, path.read_bytes()) for path in fixture_files
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

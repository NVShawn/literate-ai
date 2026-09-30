"""Exact OpenSpec snapshot parsing and drift checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.specifications import OpenSpecError, OpenSpecProvider

SPEC = """## ADDED Requirements

### Requirement: Exact behavior

The Component SHALL retain exact source evidence.

#### Scenario: Evidence changes

- **WHEN** the source identity changes
- **THEN** the old evidence is rejected
- **AND** no output is accepted
"""


class OpenSpecProviderTests(unittest.TestCase):
    def test_loads_full_content_and_normalized_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "openspec/specs/example/spec.md"
            path.parent.mkdir(parents=True)
            path.write_text(SPEC, encoding="utf-8")
            loaded = OpenSpecProvider().load(
                root,
                ["openspec/specs/example/spec.md"],
                baseline_id="baseline",
            )
            self.assertEqual(len(loaded.specification_set.artifacts), 1)
            requirement = loaded.specification_set.requirements[0]
            self.assertIn("SHALL retain", requirement.statement)
            self.assertEqual(
                requirement.scenarios[0].when, ("the source identity changes",)
            )
            self.assertEqual(len(requirement.scenarios[0].then), 2)

    def test_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "spec.md"
            path.write_text(SPEC, encoding="utf-8")
            loaded = OpenSpecProvider().load(root, ["spec.md"])
            path.write_text(SPEC + "\nchanged\n", encoding="utf-8")
            with self.assertRaisesRegex(OpenSpecError, "changed during operation"):
                loaded.require_unchanged(root)

    def test_paths_and_incomplete_scenarios_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(OpenSpecError):
                OpenSpecProvider().load(root, ["../outside.md"])
            path = root / "bad.md"
            path.write_text(
                "### Requirement: Bad\n\nSHALL fail.\n\n"
                "#### Scenario: Missing outcome\n\n- **WHEN** it runs\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OpenSpecError, "requires WHEN and THEN"):
                OpenSpecProvider().load(root, ["bad.md"])

    def test_artifact_count_and_byte_budgets_fail_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = SPEC.encode("utf-8")
            (root / "first.md").write_bytes(content)
            (root / "second.md").write_bytes(content)
            with self.assertRaisesRegex(OpenSpecError, "Too many"):
                OpenSpecProvider(maximum_artifacts=1).load(
                    root, ["first.md", "second.md"]
                )
            with self.assertRaisesRegex(OpenSpecError, "size limit"):
                OpenSpecProvider(maximum_artifact_bytes=16).load(root, ["first.md"])
            with self.assertRaisesRegex(OpenSpecError, "total size"):
                OpenSpecProvider(
                    maximum_artifact_bytes=len(content),
                    maximum_total_bytes=len(content),
                ).load(root, ["first.md", "second.md"])


if __name__ == "__main__":
    unittest.main()

"""Real instructional-demo failures remain public onboarding regressions."""

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.harness_test_discovery import observe_test_collection
from literate_ai.adapters.project_initialization import initialize_project
from literate_ai.contracts import RepositoryParentSelection


class CourseDemoRegressions(unittest.TestCase):
    def test_starter_oracle_matches_the_scaffolded_greeting_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "starter"
            initialize_project(root, parent_selection=RepositoryParentSelection.root())
            spec = (root / "samples/hello-component/component.md").read_text()
            self.assertIn("exactly `greeting` and", spec)
            oracle = json.loads(
                (root / "verification/acceptance/hello-component.json").read_text()
            )
            self.assertGreaterEqual(len(oracle["cases"]), 2)
            for case in oracle["cases"]:
                name = case["arguments"][0]["name"]
                self.assertEqual(
                    case["expected_result"],
                    {"greeting": f"Hello, {name}!", "name": name},
                )

    def test_ctest_summaries_retain_exact_counts(self):
        for output, state, total, failed in (
            ("100% tests passed out of 1", "nonempty", 1, 0),
            ("100% tests passed, 0 tests failed out of 7", "nonempty", 7, 0),
            ("50% tests passed, 2 tests failed out of 4", "nonempty", 4, 2),
            ("100% tests passed out of 0", "empty", 0, 0),
            ("50% tests passed out of 3", "unreported", None, None),
            ("100% tests passed out of 1.5", "unreported", None, None),
        ):
            with self.subTest(output=output):
                result = observe_test_collection("test", output.encode(), b"")
                self.assertEqual(result["state"], state)
                self.assertEqual(result["total"], total)
                self.assertEqual(result["failed"], failed)

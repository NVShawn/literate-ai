"""Real instructional-demo failures remain public onboarding regressions."""

import json
import tempfile
import unittest
from pathlib import Path

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

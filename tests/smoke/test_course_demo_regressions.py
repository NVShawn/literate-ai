"""Real instructional-demo failures remain public onboarding regressions."""

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.project_initialization import (
    _starter_acceptance_cases,
    initialize_project,
)
from literate_ai.contracts import RepositoryParentSelection

REPO = Path(__file__).resolve().parents[2]


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

    def test_derived_starter_oracle_matches_the_repository_greeting_card(self):
        # `init --from` the framework repository inherits its richer sample, so the
        # seeded oracle must match that contract and the sample's own harness oracle.
        harness = REPO / "samples/_harness/hello-component/acceptance"
        execution = json.loads((harness / "execution.json").read_text())
        expected = json.loads((harness / "oracle.json").read_text())
        cases = _starter_acceptance_cases(
            (REPO / "samples/hello-component/component.md").read_text()
        )
        self.assertEqual(
            [(case["case_id"], case["arguments"]) for case in cases],
            [(item["case_id"], item["arguments"]) for item in execution["invocations"]],
        )
        self.assertEqual(
            [(case["case_id"], case["expected_result"]) for case in cases],
            [
                (item["case_id"], item["expected_result"])
                for item in expected["oracle_results"]
            ],
        )

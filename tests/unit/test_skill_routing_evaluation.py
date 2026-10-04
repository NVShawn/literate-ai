"""Tests for the SKILL-ROUTING-001 behavioral skill-routing evaluation harness."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path

from literate_ai.cli.dispatch import main
from literate_ai.projects import discover_project
from literate_ai.skill_routing_evaluation import (
    evaluate_skill_routing,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_CORPUS = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "skill_routing"
    / "literate_ai_catalog_corpus.json"
)


class RealCatalogEvaluationTests(unittest.TestCase):
    """Validate the harness against this repository's own skill catalog."""

    def test_seed_corpus_mostly_routes_correctly_with_one_known_ambiguity(self) -> None:
        project = discover_project(REPO_ROOT)
        report = evaluate_skill_routing(project, SEED_CORPUS)
        summary = report.summary
        total = sum(summary.values())
        self.assertGreaterEqual(summary["correct"], total - 3)
        outcomes = {outcome.case_id: outcome for outcome in report.outcomes}
        # A never-fires case must never silently claim a winner.
        self.assertEqual(outcomes["unrelated-prose"].classification, "correct")
        self.assertIsNone(outcomes["unrelated-prose"].winner)


class SkillsEvaluateCliTests(unittest.TestCase):
    def test_cli_end_to_end(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = main(
            (
                "--json",
                "skills",
                "evaluate",
                str(SEED_CORPUS),
                "--project",
                str(REPO_ROOT),
            ),
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(status, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["result"]["schema"], "literate-ai/skill-routing-evaluation@1"
        )


if __name__ == "__main__":
    unittest.main()

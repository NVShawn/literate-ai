"""Research summaries require complete paired evidence and task-level uncertainty."""

import copy
import unittest

from scripts.summarize_research_trials import SCHEMA, summarize


class ResearchTrialTests(unittest.TestCase):
    def fixture(self):
        rows = []
        for task in ("create", "adopt"):
            for variant in ("plain-cli", "literate-ai"):
                rows.append(
                    {
                        "task": task,
                        "variant": variant,
                        "trial": 0,
                        "model": "fixture-model",
                        "environment": "fixture-host",
                        "specification_identity": "spec-fixture",
                        "oracle_identity": "oracle-fixture",
                        "passed": variant == "literate-ai" or task == "create",
                        "elapsed_ms": 10,
                        "tokens": 20,
                        "cost_microusd": 30,
                        "human_interventions": 0,
                    }
                )
        return {"schema": SCHEMA, "trials": rows}

    def test_matched_trials_report_effect_and_real_totals(self):
        result = summarize(self.fixture(), baseline="plain-cli")
        treatment = next(
            row for row in result["comparisons"] if row["variant"] == "literate-ai"
        )
        self.assertEqual(treatment["paired_task_pass_rate_difference"], 0.5)
        self.assertEqual(treatment["total_tokens"], 40)
        self.assertTrue(result["small_sample"])
        self.assertEqual(result["uncertainty_unit"], "task")

    def test_empty_missing_duplicate_and_incomparable_trials_fail(self):
        fixture = self.fixture()
        cases = [[], fixture["trials"][:-1], fixture["trials"] + [fixture["trials"][0]]]
        incomparable = copy.deepcopy(fixture["trials"])
        incomparable[0]["oracle_identity"] = "different-oracle"
        cases.append(incomparable)
        for rows in cases:
            with self.assertRaises(ValueError):
                summarize({"schema": SCHEMA, "trials": rows}, baseline="plain-cli")

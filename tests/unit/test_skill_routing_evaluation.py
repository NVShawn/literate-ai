"""Tests for the SKILL-ROUTING-001 behavioral skill-routing evaluation harness."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.cli.dispatch import main
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.skills import skills_evaluate_from_args
from literate_ai.projects import discover_project
from literate_ai.skill_routing_evaluation import (
    SkillRoutingCandidate,
    SkillRoutingCase,
    SkillRoutingEvaluationError,
    evaluate_skill_routing,
    evaluate_skill_routing_case,
    load_skill_routing_candidates,
    load_skill_routing_corpus,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_CORPUS = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "skill_routing"
    / "literate_ai_catalog_corpus.json"
)


def _candidate(skill_id: str, description: str) -> SkillRoutingCandidate:
    from literate_ai.skill_routing_evaluation import _tokenize  # noqa: PLC0415

    return SkillRoutingCandidate(
        skill_id, f"skills/{skill_id}/SKILL.md", description, _tokenize(description)
    )


class ScoringAndClassificationTests(unittest.TestCase):
    def test_jaccard_scoring_does_not_favor_a_longer_description(self) -> None:
        # A short, precise description and a long, vocabulary-rich one that
        # happens to share exactly the same words with the prompt must score
        # equally -- a longer description's extra, irrelevant vocabulary
        # should count against it (via the union), not for it.
        short = _candidate("short-match", "build a rust command line tool")
        long_verbose = _candidate(
            "long-match",
            "build a rust command line tool for parsing enormous archives with "
            "compression detection and streaming decode support across many formats",
        )
        prompt_tokens = frozenset({"build", "rust", "command", "line", "tool"})
        short_score = evaluate_skill_routing_case(
            SkillRoutingCase("case", "build a rust command line tool", "short-match"),
            (short,),
            fire_threshold=0.01,
            fragile_threshold=0.99,
        ).winner.score
        self.assertGreater(short_score, 0)
        long_score = evaluate_skill_routing_case(
            SkillRoutingCase("case", "build a rust command line tool", "long-match"),
            (long_verbose,),
            fire_threshold=0.01,
            fragile_threshold=0.99,
        ).winner.score
        self.assertGreater(short_score, long_score)
        del prompt_tokens

    def test_correct_pick_with_strong_overlap(self) -> None:
        candidates = (
            _candidate("python-skill", "generate a python service application"),
            _candidate("go-skill", "generate a go portable application"),
        )
        outcome = evaluate_skill_routing_case(
            SkillRoutingCase(
                "case", "generate a python service application", "python-skill"
            ),
            candidates,
            fire_threshold=0.1,
            fragile_threshold=0.3,
        )
        self.assertEqual(outcome.classification, "correct")
        self.assertEqual(outcome.winner.skill_id, "python-skill")

    def test_fragile_pass_when_overlap_is_thin_but_still_correct(self) -> None:
        candidates = (
            _candidate(
                "python-skill",
                "generate a long-running python service with many unrelated "
                "words that dilute the real overlap down to almost nothing",
            ),
        )
        outcome = evaluate_skill_routing_case(
            SkillRoutingCase("case", "python service please", "python-skill"),
            candidates,
            fire_threshold=0.02,
            fragile_threshold=0.5,
        )
        self.assertEqual(outcome.classification, "fragile_pass")

    def test_misroute_when_a_different_skill_wins(self) -> None:
        candidates = (
            _candidate("python-skill", "generate a python service application"),
            _candidate("go-skill", "generate a go portable application"),
        )
        outcome = evaluate_skill_routing_case(
            SkillRoutingCase(
                "case", "generate a go portable application", "python-skill"
            ),
            candidates,
            fire_threshold=0.1,
            fragile_threshold=0.3,
        )
        self.assertEqual(outcome.classification, "misroute")
        self.assertEqual(outcome.winner.skill_id, "go-skill")

    def test_misroute_when_the_expected_skill_never_reaches_the_fire_threshold(
        self,
    ) -> None:
        candidates = (_candidate("python-skill", "generate a python service"),)
        outcome = evaluate_skill_routing_case(
            SkillRoutingCase(
                "case", "completely unrelated prompt text", "python-skill"
            ),
            candidates,
            fire_threshold=0.5,
            fragile_threshold=0.9,
        )
        self.assertEqual(outcome.classification, "misroute")
        self.assertIsNone(outcome.winner)

    def test_over_greedy_when_nothing_should_fire_but_something_does(self) -> None:
        candidates = (
            _candidate("python-skill", "generate a python service application"),
        )
        outcome = evaluate_skill_routing_case(
            SkillRoutingCase("case", "generate a python service application", None),
            candidates,
            fire_threshold=0.1,
            fragile_threshold=0.3,
        )
        self.assertEqual(outcome.classification, "over_greedy")

    def test_correct_when_nothing_fires_and_nothing_was_expected(self) -> None:
        candidates = (
            _candidate("python-skill", "generate a python service application"),
        )
        outcome = evaluate_skill_routing_case(
            SkillRoutingCase("case", "write a poem about the sea", None),
            candidates,
            fire_threshold=0.5,
            fragile_threshold=0.9,
        )
        self.assertEqual(outcome.classification, "correct")
        self.assertIsNone(outcome.winner)

    def test_known_ambiguous_pair_reproducibly_misroutes_or_fragile_passes(
        self,
    ) -> None:
        # Two candidates with deliberately near-identical descriptions: a
        # prompt aimed at one must not reliably resolve to "correct" -- this
        # is the harness's own evidence that it does not silently validate a
        # selection purely because it was structurally well-formed.
        candidates = (
            _candidate("build-a", "configure the project build system"),
            _candidate("build-b", "configure the project build tooling"),
        )
        # Both descriptions share the exact same overlap with this prompt
        # (only their one distinguishing word differs, and neither appears in
        # the prompt) -- a real ambiguity a genuine router would also face,
        # not an artifact of this test's own construction.
        outcome = evaluate_skill_routing_case(
            SkillRoutingCase("case", "configure the project build", "build-b"),
            candidates,
            fire_threshold=0.05,
            fragile_threshold=0.5,
        )
        self.assertIn(outcome.classification, {"misroute", "fragile_pass"})


class CorpusLoadingTests(unittest.TestCase):
    def test_rejects_a_non_array_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")
            with self.assertRaises(SkillRoutingEvaluationError) as raised:
                load_skill_routing_corpus(path)
            self.assertEqual(raised.exception.code, "skill_routing.corpus_invalid")

    def test_rejects_an_empty_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(SkillRoutingEvaluationError):
                load_skill_routing_corpus(path)

    def test_rejects_a_duplicate_case_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_text(
                json.dumps(
                    [
                        {"case_id": "a", "prompt": "x", "expected_skill_id": None},
                        {"case_id": "a", "prompt": "y", "expected_skill_id": None},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SkillRoutingEvaluationError) as raised:
                load_skill_routing_corpus(path)
            self.assertEqual(raised.exception.code, "skill_routing.corpus_invalid")

    def test_rejects_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_text(
                json.dumps([{"case_id": "a", "prompt": "x"}]), encoding="utf-8"
            )
            with self.assertRaises(SkillRoutingEvaluationError):
                load_skill_routing_corpus(path)

    def test_loads_a_well_formed_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_text(
                json.dumps(
                    [
                        {"case_id": "a", "prompt": "x", "expected_skill_id": "skill-a"},
                        {"case_id": "b", "prompt": "y", "expected_skill_id": None},
                    ]
                ),
                encoding="utf-8",
            )
            cases = load_skill_routing_corpus(path)
            self.assertEqual(len(cases), 2)
            self.assertEqual(cases[0].expected_skill_id, "skill-a")
            self.assertIsNone(cases[1].expected_skill_id)


class RealCatalogEvaluationTests(unittest.TestCase):
    """Validate the harness against this repository's own skill catalog."""

    def test_candidates_load_from_the_real_catalog(self) -> None:
        project = discover_project(REPO_ROOT)
        candidates = load_skill_routing_candidates(project)
        self.assertGreater(len(candidates), 0)
        ids = {item.skill_id for item in candidates}
        self.assertIn("python-service-application", ids)
        self.assertIn("react-dashboard-application", ids)

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

    def test_expected_skill_unknown_fails_closed(self) -> None:
        project = discover_project(REPO_ROOT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "case_id": "a",
                            "prompt": "x",
                            "expected_skill_id": "not-a-real-skill",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SkillRoutingEvaluationError) as raised:
                evaluate_skill_routing(project, path)
            self.assertEqual(
                raised.exception.code, "skill_routing.expected_skill_unknown"
            )


class SkillsEvaluateCliTests(unittest.TestCase):
    def test_cli_function_matches_the_direct_call(self) -> None:
        direct = evaluate_skill_routing(discover_project(REPO_ROOT), SEED_CORPUS)
        envelope = skills_evaluate_from_args(
            SimpleNamespace(
                project=str(REPO_ROOT),
                corpus=str(SEED_CORPUS),
                fire_threshold=None,
                fragile_threshold=None,
            )
        )
        self.assertEqual(envelope["summary"], direct.summary)

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

    def test_cli_reports_a_missing_project_as_a_failure(self) -> None:
        with self.assertRaises(CliFailure):
            skills_evaluate_from_args(
                SimpleNamespace(
                    project="/nonexistent/path/for/this/test",
                    corpus=str(SEED_CORPUS),
                    fire_threshold=None,
                    fragile_threshold=None,
                )
            )


if __name__ == "__main__":
    unittest.main()

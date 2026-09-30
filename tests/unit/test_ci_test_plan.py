"""Public CI shard/impact plan: detect frameworks, never silently no-op."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.ci_test_plan import CiTestPlanError, plan_ci_tests
from tests.unit.test_project_cli import REPO_ROOT, invoke

CATALOG = REPO_ROOT / "skills" / "agent" / "ci-test-plan"
TEMPLATE = (
    REPO_ROOT
    / "src"
    / "literate_ai"
    / "project_template"
    / "skills"
    / "agent"
    / "ci-test-plan"
)
_PLAIN_TEXT_ENVIRONMENT = {
    key: value
    for key, value in os.environ.items()
    if key not in {"FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE"}
} | {"NO_COLOR": "1", "TERM": "dumb"}


class CiTestPlanTests(unittest.TestCase):
    def test_pytest_project_gets_pytest_split_and_fail_closed_impact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
                encoding="utf-8",
            )
            plan = plan_ci_tests(root, mode="compose")

        self.assertEqual(plan["schema"], "literate-ai/ci-test-plan@1")
        self.assertFalse(plan["silent_noop"])
        self.assertEqual(plan["frameworks"][0]["framework_id"], "pytest")
        self.assertEqual(plan["frameworks"][0]["shard"]["status"], "available")
        self.assertEqual(
            plan["frameworks"][0]["shard"]["mechanism"],
            "pytest-split --splits/--group",
        )
        self.assertTrue(plan["fail_closed_to_full_suite"])
        self.assertEqual(plan["selection"], "shard_full_suite")
        self.assertEqual(plan["frameworks"][0]["impact"]["selection"], "full_suite")

    def test_unverified_map_bytes_stay_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\n", encoding="utf-8"
            )
            (root / ".testmondata").write_bytes(b"map")
            plan = plan_ci_tests(root, mode="compose")

        self.assertTrue(plan["fail_closed_to_full_suite"])
        self.assertEqual(plan["selection"], "shard_full_suite")
        self.assertEqual(plan["impact_map_reason"], "map_invalid")
        self.assertEqual(plan["frameworks"][0]["impact"]["selection"], "full_suite")

    def test_jest_and_go_are_detected_without_assuming_pytest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                json.dumps({"devDependencies": {"jest": "29.0.0"}}),
                encoding="utf-8",
            )
            (root / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
            plan = plan_ci_tests(root, mode="shard")

        ids = [item["framework_id"] for item in plan["frameworks"]]
        self.assertEqual(ids, ["jest", "go-test"])
        self.assertNotIn("pytest", ids)
        jest = plan["frameworks"][0]
        self.assertEqual(jest["shard"]["mechanism"], "jest --shard=i/n")
        self.assertFalse(jest["shard"]["durations"])
        go = plan["frameworks"][1]
        self.assertIn("#83", go["shard"]["citations"])
        self.assertEqual(plan["status"], "available")

    def test_swift_and_checkpoint_are_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Package.swift").write_text("// swift-tools-version: 6.0\n")
            (root / "scripts").mkdir()
            (root / "scripts" / "run_checkpointed_unittests.py").write_text(
                "print('checkpoint')\n", encoding="utf-8"
            )
            plan = plan_ci_tests(root)

        ids = {item["framework_id"]: item for item in plan["frameworks"]}
        self.assertEqual(ids["swiftpm"]["shard"]["status"], "unavailable")
        self.assertIn("#86", ids["swiftpm"]["shard"]["citations"])
        self.assertEqual(ids["unittest-checkpoint"]["shard"]["status"], "unavailable")
        self.assertEqual(plan["checkpoint_jobs"]["decision"], "keep_unsharded")
        self.assertFalse(plan["silent_noop"])

    def test_unknown_tree_is_unavailable_not_a_silent_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("no tests\n", encoding="utf-8")
            plan = plan_ci_tests(root)

        self.assertEqual(plan["status"], "unavailable")
        self.assertEqual(plan["reason"], "no_recognized_test_framework")
        self.assertEqual(plan["frameworks"], [])
        self.assertFalse(plan["silent_noop"])

    def test_this_repository_keeps_checkpointed_jobs_and_sees_windows_shards(
        self,
    ) -> None:
        plan = plan_ci_tests(REPO_ROOT)
        ids = {item["framework_id"]: item for item in plan["frameworks"]}
        self.assertIn("pytest", ids)
        self.assertIn("unittest-checkpoint", ids)
        self.assertEqual(plan["checkpoint_jobs"]["decision"], "keep_unsharded")
        self.assertTrue(
            any(item["kind"] == "shard" for item in plan["already_authored"])
        )

    def test_invalid_mode_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CiTestPlanError) as raised:
                plan_ci_tests(Path(directory), mode="guess")
        self.assertEqual(raised.exception.code, "ci.plan_mode_invalid")

    def test_cli_emits_the_plan_and_help_names_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.toml").write_text('[package]\nname = "app"\n')
            status, envelope = invoke(
                "project", "ci-plan", str(root), "--mode", "shard"
            )
        self.assertEqual(status, 0, envelope)
        result = envelope["result"]
        self.assertEqual(result["frameworks"][0]["framework_id"], "cargo-nextest")
        self.assertIn("slice:m/n", result["frameworks"][0]["shard"]["mechanism"])
        self.assertIn("#84", result["frameworks"][0]["shard"]["citations"])
        self.assertEqual(result["frameworks"][0]["impact"]["status"], "unavailable")

        stdout = __import__("io").StringIO()
        stderr = __import__("io").StringIO()
        from literate_ai.cli import main

        with mock.patch.dict(os.environ, _PLAIN_TEXT_ENVIRONMENT, clear=True):
            help_status = main(
                ("project", "ci-plan", "help"), stdout=stdout, stderr=stderr
            )
        self.assertEqual(help_status, 0)
        self.assertIn("usage: litai project ci-plan", stdout.getvalue())

    def test_catalog_and_template_skills_match_and_wrap_the_command(self) -> None:
        relatives = (
            Path("SKILL.md"),
            Path("shard") / "SKILL.md",
            Path("impact") / "SKILL.md",
        )
        for relative in relatives:
            catalog = (CATALOG / relative).read_bytes()
            self.assertEqual(catalog, (TEMPLATE / relative).read_bytes())
            text = catalog.decode("utf-8")
            self.assertIn("litai project ci-plan", text)
        parent = (CATALOG / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("fail-closed", parent)
        self.assertIn("impact, then shard", parent)
        self.assertIn("keep_unsharded", parent)
        self.assertIn(".test_durations", parent)
        impact = (CATALOG / "impact" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("run_impact_pytest.py", impact)


if __name__ == "__main__":
    unittest.main()

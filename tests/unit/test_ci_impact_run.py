"""Fail-closed pytest-testmon wiring: skip only with a trusted map."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.ci_impact_run import (
    IMPACT_MAP_IDENTITY_NAME,
    IMPACT_MAP_NAME,
    inspect_impact_map,
    resolve_pytest_impact_run,
    run_pytest_impact,
    write_impact_map_identity,
)
from literate_ai.ci_test_plan import plan_ci_tests

REPO_ROOT = Path(__file__).resolve().parents[2]
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

_FAKE_SQLITE = b"SQLite format 3\x00" + b"\x00" * 64
_SUMMARY = re.compile(r"(\d+) (passed|failed|deselected|skipped|error|errors)")
_VENV_PYTHON = (
    Path("Scripts") / "python.exe" if os.name == "nt" else Path("bin") / "python"
)


def _git(root: Path, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["GIT_AUTHOR_NAME"] = "ci-impact-test"
    environment["GIT_AUTHOR_EMAIL"] = "ci-impact-test@example.com"
    environment["GIT_COMMITTER_NAME"] = "ci-impact-test"
    environment["GIT_COMMITTER_EMAIL"] = "ci-impact-test@example.com"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )


def _init_git(root: Path) -> str:
    _git(root, "init", "--quiet", "-b", "main")
    _git(root, "config", "user.name", "ci-impact-test")
    _git(root, "config", "user.email", "ci-impact-test@example.com")
    (root / "README").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README")
    _git(root, "commit", "-m", "seed")
    return subprocess.run(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_pytest_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["."]\n',
        encoding="utf-8",
    )
    (root / "alpha.py").write_text(
        "def value() -> int:\n    return 1\n", encoding="utf-8"
    )
    (root / "beta.py").write_text(
        "def value() -> int:\n    return 2\n", encoding="utf-8"
    )
    (root / "test_alpha.py").write_text(
        "import alpha\n\ndef test_alpha() -> None:\n    assert alpha.value() == 1\n",
        encoding="utf-8",
    )
    (root / "test_beta.py").write_text(
        "import beta\n\ndef test_beta() -> None:\n    assert beta.value() == 2\n",
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text("[pytest]\naddopts =\n", encoding="utf-8")


def _write_trusted_map(root: Path, revision: str | None = None) -> None:
    (root / IMPACT_MAP_NAME).write_bytes(_FAKE_SQLITE)
    write_impact_map_identity(root, revision=revision)


def _pytest_counts(output: str) -> dict[str, int]:
    counts = {
        "passed": 0,
        "failed": 0,
        "deselected": 0,
        "skipped": 0,
        "error": 0,
    }
    last = ""
    for line in output.splitlines():
        if _SUMMARY.search(line):
            last = line
    for count, label in _SUMMARY.findall(last):
        key = "error" if label == "errors" else label
        counts[key] = int(count)
    return counts


class ImpactMapTrustTests(unittest.TestCase):
    def test_missing_map_is_untrusted_even_when_durations_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".test_durations").write_text(
                json.dumps({"test_alpha.py::test_alpha": 0.1}),
                encoding="utf-8",
            )
            trust = inspect_impact_map(root)
            decision = resolve_pytest_impact_run(root, testmon_available=True)

        self.assertEqual(trust.reason, "missing_map")
        self.assertFalse(trust.trusted)
        self.assertEqual(decision["selection"], "full_suite")
        self.assertFalse(decision["use_testmon"])
        self.assertIn("--no-testmon", decision["argv"])
        self.assertNotIn("test_alpha.py::test_alpha", decision["argv"])
        self.assertNotIn(".test_durations", "".join(decision["argv"]))

    def test_bytes_that_are_not_sqlite_are_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / IMPACT_MAP_NAME).write_bytes(b"map")
            self.assertEqual(inspect_impact_map(root).reason, "map_invalid")

    def test_identity_that_names_durations_is_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git(root)
            (root / IMPACT_MAP_NAME).write_bytes(_FAKE_SQLITE)
            (root / IMPACT_MAP_IDENTITY_NAME).write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/ci-impact-map-identity@1",
                        "mechanism": "pytest-testmon",
                        "base_revision": "abc",
                        "durations_path": ".test_durations",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                inspect_impact_map(root).reason,
                "durations_are_not_skip_authority",
            )

    def test_stale_non_ancestor_revision_is_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_git(root)
            _write_trusted_map(root, revision="0" * 40)
            trust = inspect_impact_map(root)
            self.assertFalse(trust.trusted)
            self.assertEqual(trust.reason, "stale_revision")

    def test_ancestor_identity_is_trusted_for_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = _init_git(root)
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\n", encoding="utf-8"
            )
            _write_trusted_map(root, revision=revision)
            plan = plan_ci_tests(root, mode="compose")
            decision = resolve_pytest_impact_run(root, testmon_available=True)

        self.assertTrue(plan["impact_map_trusted"])
        self.assertEqual(plan["impact_map_reason"], "trusted")
        self.assertEqual(plan["selection"], "impact_then_shard")
        self.assertEqual(decision["selection"], "impact")
        self.assertIn("--testmon", decision["argv"])
        self.assertNotIn("--no-testmon", decision["argv"])

    def test_trusted_map_without_the_plugin_still_runs_the_full_suite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = _init_git(root)
            _write_trusted_map(root, revision=revision)
            decision = resolve_pytest_impact_run(root, testmon_available=False)
        self.assertEqual(decision["selection"], "full_suite")
        self.assertEqual(decision["reason"], "testmon_unavailable")
        self.assertNotIn("--testmon", decision["argv"])
        self.assertNotIn("--no-testmon", decision["argv"])


class ReleaseGateSkipTests(unittest.TestCase):
    def test_release_gates_do_not_skip_from_testmon_or_durations(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        match = re.search(r"^RELEASE_GATES := (.+)$", makefile, re.MULTILINE)
        self.assertIsNotNone(match)
        gates = match.group(1).split()
        self.assertIn("python-check", gates)
        self.assertNotIn("run_impact_pytest.py", makefile)
        self.assertNotIn("--testmon", makefile)
        self.assertNotIn("select_smoke_tests", makefile)
        self.assertIn("run_checkpointed_unittests.py", makefile)

        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--testmon", ci)
        self.assertNotIn("select_smoke_tests", ci)
        self.assertNotIn("run_impact_pytest", ci)
        self.assertIn("--durations-path=.test_durations", ci)
        self.assertIn("--splits", ci)

        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("--testmon", pyproject)
        addopts = ""
        for line in pyproject.splitlines():
            if "addopts" in line:
                addopts = line
        self.assertNotIn("testmon", addopts)

    def test_this_repository_authors_the_runner_but_keeps_an_untrusted_map(
        self,
    ) -> None:
        plan = plan_ci_tests(REPO_ROOT)
        self.assertTrue(plan["fail_closed_to_full_suite"])
        self.assertEqual(plan["impact_map_reason"], "missing_map")
        self.assertTrue(
            any(item["kind"] == "impact" for item in plan["already_authored"])
        )
        self.assertTrue(
            any(
                item.get("path") == "scripts/run_impact_pytest.py"
                for item in plan["already_authored"]
            )
        )

    def test_impact_skill_names_the_runner_and_forbids_duration_skip(self) -> None:
        for base in (CATALOG, TEMPLATE):
            text = (base / "impact" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("run_impact_pytest.py", text)
            self.assertIn(".test_durations", text)
            self.assertIn("python-check", text)


class LivePytestTestmonTests(unittest.TestCase):
    python: str
    _venv: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._venv = tempfile.TemporaryDirectory()
        venv = Path(cls._venv.name) / "venv"
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            raise unittest.SkipTest(
                f"could not create an isolated pytest-testmon venv: {created.stderr}"
            )
        python = venv / _VENV_PYTHON
        install = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "pytest-testmon==2.2.0",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            raise unittest.SkipTest(
                "pytest-testmon==2.2.0 could not be installed for live wiring: "
                + install.stderr
            )
        cls.python = str(python)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._venv.cleanup()

    def test_unrelated_change_deselects_unaffected_tests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_pytest_project(root)
            _init_git(root)
            _git(root, "add", ".")
            _git(root, "commit", "-m", "fixture")

            collected, collect_decision = run_pytest_impact(
                root,
                ("-v",),
                python=self.python,
                refresh=True,
                timeout=120,
            )
            self.assertEqual(
                collected.returncode, 0, collected.stdout + collected.stderr
            )
            self.assertTrue(collect_decision["use_testmon"])
            self.assertTrue((root / IMPACT_MAP_NAME).is_file())
            self.assertTrue((root / IMPACT_MAP_IDENTITY_NAME).is_file())
            self.assertTrue(inspect_impact_map(root).trusted)
            baseline_output = collected.stdout + collected.stderr
            baseline = _pytest_counts(baseline_output)
            self.assertGreaterEqual(baseline["passed"], 2, baseline_output)
            self.assertIn("test_alpha", baseline_output)
            self.assertIn("test_beta", baseline_output)

            (root / "beta.py").write_text(
                "def value() -> int:\n    return 20\n",
                encoding="utf-8",
            )
            (root / "test_beta.py").write_text(
                "import beta\n\n"
                "def test_beta() -> None:\n"
                "    assert beta.value() == 20\n",
                encoding="utf-8",
            )
            reduced, reduce_decision = run_pytest_impact(
                root,
                ("-v",),
                python=self.python,
                timeout=120,
            )
            reduced_output = reduced.stdout + reduced.stderr
            self.assertEqual(reduced.returncode, 0, reduced_output)
            self.assertEqual(reduce_decision["selection"], "impact")
            self.assertIn("--testmon", reduce_decision["argv"])
            counts = _pytest_counts(reduced_output)
            self.assertEqual(counts["passed"], 1, reduced_output)
            self.assertIn("test_beta", reduced_output)
            self.assertNotIn("test_alpha PASSED", reduced_output)
            self.assertLess(
                counts["passed"],
                baseline["passed"],
                "unrelated alpha tests must not rerun after a beta-only change: "
                + reduced_output,
            )

    def test_stale_identity_and_missing_map_run_the_full_suite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_pytest_project(root)
            _init_git(root)
            _git(root, "add", ".")
            _git(root, "commit", "-m", "fixture")
            collected, _ = run_pytest_impact(
                root,
                ("-q",),
                python=self.python,
                refresh=True,
                timeout=120,
            )
            self.assertEqual(
                collected.returncode, 0, collected.stdout + collected.stderr
            )

            write_impact_map_identity(root, revision="0" * 40)
            stale, stale_decision = run_pytest_impact(
                root,
                ("-q",),
                python=self.python,
                timeout=120,
            )
            self.assertEqual(stale.returncode, 0, stale.stdout + stale.stderr)
            self.assertEqual(stale_decision["selection"], "full_suite")
            self.assertIn("--no-testmon", stale_decision["argv"])
            stale_counts = _pytest_counts(stale.stdout + stale.stderr)
            self.assertEqual(stale_counts["passed"], 2)
            self.assertEqual(stale_counts["deselected"], 0)

            (root / IMPACT_MAP_NAME).unlink()
            (root / IMPACT_MAP_IDENTITY_NAME).unlink()
            missing, missing_decision = run_pytest_impact(
                root,
                ("-q",),
                python=self.python,
                timeout=120,
            )
            self.assertEqual(missing.returncode, 0, missing.stdout + missing.stderr)
            self.assertEqual(missing_decision["trust"]["reason"], "missing_map")
            missing_counts = _pytest_counts(missing.stdout + missing.stderr)
            self.assertEqual(missing_counts["passed"], 2)
            self.assertEqual(missing_counts["deselected"], 0)


if __name__ == "__main__":
    unittest.main()

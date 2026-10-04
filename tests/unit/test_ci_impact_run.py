"""Fail-closed pytest-testmon wiring: skip only with a trusted map."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.ci_impact_run import (
    IMPACT_MAP_NAME,
    inspect_impact_map,
    resolve_pytest_impact_run,
    write_impact_map_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_FAKE_SQLITE = b"SQLite format 3\x00" + b"\x00" * 64


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


def _write_trusted_map(root: Path, revision: str | None = None) -> None:
    (root / IMPACT_MAP_NAME).write_bytes(_FAKE_SQLITE)
    write_impact_map_identity(root, revision=revision)


class ImpactMapTrustTests(unittest.TestCase):
    def test_missing_or_untrusted_map_runs_the_full_suite(self) -> None:
        def missing(root: Path) -> None:
            (root / ".test_durations").write_text(
                json.dumps({"test_alpha.py::test_alpha": 0.1}), encoding="utf-8"
            )

        def not_sqlite(root: Path) -> None:
            (root / IMPACT_MAP_NAME).write_bytes(b"map")

        def stale(root: Path) -> None:
            _init_git(root)
            _write_trusted_map(root, revision="0" * 40)

        for reason, prepare in (
            ("missing_map", missing),
            ("map_invalid", not_sqlite),
            ("stale_revision", stale),
        ):
            with self.subTest(reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prepare(root)
                trust = inspect_impact_map(root)
                decision = resolve_pytest_impact_run(root, testmon_available=True)
                self.assertFalse(trust.trusted)
                self.assertEqual(trust.reason, reason)
                self.assertEqual(decision["selection"], "full_suite")
                self.assertFalse(decision["use_testmon"])
                self.assertIn("--no-testmon", decision["argv"])
                self.assertNotIn("test_alpha.py::test_alpha", decision["argv"])


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


if __name__ == "__main__":
    unittest.main()

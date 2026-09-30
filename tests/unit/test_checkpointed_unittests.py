"""Tests for per-test fail-fast repair checkpoints."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.contracts import ProjectInitializationOrigin
from literate_ai.test_checkpointing import _load_reusable_tests
from scripts.run_checkpointed_unittests import run_suite
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)


class CheckpointedUnittestTests(unittest.TestCase):
    def test_initialized_project_uses_installed_runner_for_repair_cycle(self) -> None:
        origin = ProjectInitializationOrigin(
            repository_url="ssh://git.example.test/operator/literate-ai.git",
            git_revision="a" * 40,
            distribution_name="literate-ai",
            distribution_version="0.2.0",
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "derived-project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: origin,
            ).initialize(
                project,
                flavor_selectors=("+bazel", "+python", "+macos"),
                source_intelligence_provider="none",
            )
            project = project.resolve()
            fixture = project / "_build" / "checkpoint-fixture"
            fixture.mkdir(parents=True)
            tests = project / "tests"
            tests.mkdir(parents=True)
            (tests / "__init__.py").write_bytes(b"")
            marker = fixture / "executions.txt"
            repair = fixture / "repaired"
            (tests / "test_fixture.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                f"MARKER = Path({str(marker)!r})\n"
                f"REPAIR = Path({str(repair)!r})\n"
                "class FixtureTests(unittest.TestCase):\n"
                "    def test_a(self):\n"
                "        with MARKER.open('a', encoding='utf-8') as stream:\n"
                "            stream.write('a\\n')\n"
                "    def test_b(self):\n"
                "        with MARKER.open('a', encoding='utf-8') as stream:\n"
                "            stream.write('b\\n')\n"
                "        self.assertTrue(REPAIR.exists(), 'repair me')\n",
                encoding="utf-8",
                newline="\n",
            )
            state = fixture / "checkpoint.json"
            command = (
                sys.executable,
                "-m",
                "literate_ai.test_checkpointing",
                "python",
                "--state",
                str(state),
                "--start-directory",
                str(tests),
            )

            first = subprocess.run(
                command, cwd=project, check=False, capture_output=True
            )
            self.assertEqual(first.returncode, 1, first.stderr.decode())
            self.assertTrue(marker.exists(), first.stderr.decode())
            self.assertEqual(
                marker.read_text(encoding="utf-8").splitlines(), ["a", "b"]
            )

            repair.touch()
            second = subprocess.run(
                command, cwd=project, check=False, capture_output=True
            )
            self.assertEqual(second.returncode, 2, second.stderr.decode())
            self.assertEqual(
                marker.read_text(encoding="utf-8").splitlines(), ["a", "b", "b"]
            )
            self.assertFalse(state.exists())

            third = subprocess.run(
                command, cwd=project, check=False, capture_output=True
            )
            self.assertEqual(third.returncode, 0, third.stderr.decode())
            self.assertEqual(
                marker.read_text(encoding="utf-8").splitlines(),
                ["a", "b", "b", "a", "b"],
            )

    def test_repeated_test_identities_receive_stable_occurrence_keys(self) -> None:
        executions: list[str] = []

        class Repeated(unittest.TestCase):
            def test_case(self) -> None:
                executions.append("case")

        suite = unittest.TestSuite((Repeated("test_case"), Repeated("test_case")))
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "python-tests.json"
            self.assertEqual(
                run_suite(suite, state_path=state, stream=io.StringIO()), 0
            )
        self.assertEqual(executions, ["case", "case"])

    def test_legacy_fingerprint_checkpoint_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "python-tests.json"
            fingerprints = {"fixture.a": "a" * 64, "fixture.b": "b" * 64}
            state.write_text(
                json.dumps(
                    {
                        "v": 2,
                        "s": "python-unittest",
                        "ok": [
                            ["fixture.a", fingerprints["fixture.a"]],
                            ["fixture.b", fingerprints["fixture.b"]],
                        ],
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _load_reusable_tests(
                    state,
                    "python-unittest",
                    ("fixture.a", "fixture.b"),
                    fingerprints,
                ),
                [],
            )

    def test_failure_resume_and_required_from_zero_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "python-tests.json"
            executions: list[str] = []
            failing = True

            class Cases(unittest.TestCase):
                def test_a(self) -> None:
                    executions.append("a")

                def test_b(self) -> None:
                    executions.append("b")
                    if failing:
                        self.fail("repair me")

            def suite() -> unittest.TestSuite:
                return unittest.defaultTestLoader.loadTestsFromTestCase(Cases)

            self.assertEqual(
                run_suite(suite(), state_path=state, stream=io.StringIO()), 1
            )
            self.assertEqual(executions, ["a", "b"])
            self.assertTrue(state.exists())

            failing = False
            executions.clear()
            self.assertEqual(
                run_suite(suite(), state_path=state, stream=io.StringIO()), 2
            )
            self.assertEqual(executions, ["b"])
            self.assertFalse(state.exists())

            executions.clear()
            self.assertEqual(
                run_suite(suite(), state_path=state, stream=io.StringIO()), 0
            )
            self.assertEqual(executions, ["a", "b"])

    def test_skips_are_reusable_and_legacy_missing_skips_do_not_poison_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "python-tests.json"
            executions: list[str] = []
            failing = True

            class Cases(unittest.TestCase):
                def test_a(self) -> None:
                    executions.append("a")

                @unittest.skip("platform fixture")
                def test_b(self) -> None:
                    executions.append("unreachable")

                def test_c(self) -> None:
                    executions.append("c")
                    if failing:
                        self.fail("repair me")

            def suite() -> unittest.TestSuite:
                return unittest.defaultTestLoader.loadTestsFromTestCase(Cases)

            self.assertEqual(
                run_suite(suite(), state_path=state, stream=io.StringIO()), 1
            )
            recorded = json.loads(state.read_text(encoding="utf-8"))
            completed = [
                step for payload in recorded["pins"].values() for step in payload["ok"]
            ]
            self.assertEqual(len(completed), 2)

            failing = False
            executions.clear()
            self.assertEqual(
                run_suite(suite(), state_path=state, stream=io.StringIO()), 2
            )
            self.assertEqual(executions, ["c"])
            self.assertFalse(state.exists())

    def test_class_level_skip_does_not_crash_the_checkpoint_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "python-tests.json"

            class Cases(unittest.TestCase):
                @classmethod
                def setUpClass(cls) -> None:
                    raise unittest.SkipTest("toolchain missing")

                def test_a(self) -> None:
                    self.fail("should be skipped with the class")

            def suite() -> unittest.TestSuite:
                return unittest.defaultTestLoader.loadTestsFromTestCase(Cases)

            self.assertEqual(
                run_suite(suite(), state_path=state, stream=io.StringIO()), 0
            )


if __name__ == "__main__":
    unittest.main()

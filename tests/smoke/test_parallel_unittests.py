"""Real subprocess coverage for checkpointed module scheduling and repair."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.parallel_unittests import available_workers

SOURCE = Path(__file__).resolve().parents[2] / "src"


class ParallelUnittestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = dict(os.environ, PYTHONPATH=str(SOURCE))
        self.state = self.root / "state.json"
        self.report = self.root / "report.json"

    def module(self, name: str, source: str) -> None:
        (self.root / f"test_{name}.py").write_text(
            textwrap.dedent(source), encoding="utf-8"
        )

    def run_suite(
        self, workers: int = 2, timeout: float = 20, extra: tuple[str, ...] = ()
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "literate_ai.test_checkpointing",
                "python",
                "--state",
                str(self.state),
                "--start-directory",
                ".",
                "--report",
                str(self.report),
                "--workers",
                str(workers),
                "--worker-timeout",
                str(timeout),
                *extra,
            ],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_worker_count_validation(self) -> None:
        for invalid in (0, -1, True, "2.5", "01", "invalid"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                available_workers(invalid)
        self.assertEqual(available_workers("3"), 3)
        self.assertGreaterEqual(available_workers("auto"), 1)

    def test_modules_overlap_with_private_directories(self) -> None:
        for name, other in (("a", "b"), ("b", "a")):
            self.module(
                name,
                f'''
                import os, tempfile, time, unittest
                from pathlib import Path
                class Tests(unittest.TestCase):
                    def test_overlap(self):
                        Path("{name}.ready").write_text("ready")
                        deadline = time.monotonic() + 8
                        while not Path("{other}.ready").exists():
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.01)
                        temporary = Path(tempfile.gettempdir())
                        self.assertEqual(temporary, Path(os.environ["TMPDIR"]))
                        self.assertNotEqual(temporary, Path(os.environ["OBJ_DIR"]))
                        self.assertFalse(temporary.is_relative_to(Path.cwd()))
                        self.assertTrue(Path(os.environ["LITAI_CONFIG_DIR"]).is_dir())
                        paths = str(temporary) + "\\n" + os.environ["OBJ_DIR"]
                        Path("{name}.paths").write_text(paths)
            ''',
            )
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(
            (self.root / "a.paths").read_text(), (self.root / "b.paths").read_text()
        )
        report = json.loads(self.report.read_text())
        self.assertEqual(report["summary"]["passed"], 2)

    def test_serial_module_is_an_exclusive_barrier(self) -> None:
        for name in ("a", "b", "c"):
            self.module(
                name,
                f'''
                import time, unittest
                from pathlib import Path
                LITAI_TEST_SERIAL = {name == "b"}
                class Tests(unittest.TestCase):
                    def test_exclusive(self):
                        if "{name}" == "b":
                            self.assertTrue(Path("a.finished").exists())
                            self.assertFalse(Path("c.started").exists())
                        Path("{name}.started").touch()
                        time.sleep(.05)
                        Path("{name}.finished").touch()
            ''',
            )
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fail_fast_and_repair_out_of_order(self) -> None:
        self.module(
            "a",
            """
            import time, unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_a_pass(self):
                    with Path("a.executions").open("a") as log:
                        log.write("a\\n")
                def test_repair(self):
                    time.sleep(.3)
                    self.assertTrue(Path("fixed").exists())
        """,
        )
        self.module(
            "b",
            """
            import unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_fast(self):
                    with Path("executions").open("a") as log:
                        log.write("b\\n")
                @unittest.skip("intentional repair skip")
                def test_skip(self): pass
        """,
        )
        first = self.run_suite()
        self.assertEqual(first.returncode, 1, first.stderr)
        (self.root / "fixed").touch()
        repair = self.run_suite()
        self.assertEqual(repair.returncode, 2, repair.stderr)
        self.assertEqual((self.root / "a.executions").read_text(), "a\n")
        self.assertEqual(json.loads(self.report.read_text())["summary"]["skipped"], 1)
        self.assertEqual((self.root / "executions").read_text(), "b\n")
        fresh = self.run_suite()
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual((self.root / "executions").read_text(), "b\nb\n")

    def test_worker_crash_is_nonpassing_and_does_not_reuse_partial_module(self) -> None:
        self.module(
            "a",
            """
            import os, unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_a_pass(self):
                    with Path("executions").open("a") as log:
                        log.write("a\\n")
                def test_b_crash(self):
                    if not Path("fixed").exists():
                        os._exit(7)
        """,
        )
        result = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("without a complete report", result.stderr)
        (self.root / "fixed").touch()
        result = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "executions").read_text(), "a\na\n")

    def test_deadline_is_nonpassing(self) -> None:
        self.module(
            "a",
            """
            import time, unittest
            class Tests(unittest.TestCase):
                def test_hang(self):
                    time.sleep(30)
        """,
        )
        result = self.run_suite(timeout=0.3)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("finite deadline", result.stderr)

    def test_subtest_failure_is_in_checkpoint_report(self) -> None:
        self.module(
            "a",
            """
            import unittest
            class Tests(unittest.TestCase):
                def test_subtest(self):
                    with self.subTest(value=1):
                        self.fail("subtest failure")
        """,
        )
        result = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(self.report.read_text())["summary"]["failed"], 1)

    def test_fail_fast_stops_owned_descendants_and_pending_dispatch(self) -> None:
        self.module(
            "a",
            """
            import time, unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_fail(self):
                    deadline = time.monotonic() + 8
                    while not Path("heartbeat").exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    self.fail("stop all workers")
        """,
        )
        self.module(
            "b",
            """
            import subprocess, sys, time, unittest
            class Tests(unittest.TestCase):
                def test_child(self):
                    code = ("import time; from pathlib import Path; "
                            "p=Path('heartbeat'); "
                            "exec('while True: "
                            "p.write_text(str(time.time_ns())); time.sleep(.02)')")
                    subprocess.Popen([sys.executable, "-c", code])
                    time.sleep(30)
        """,
        )
        self.module(
            "c",
            """
            import unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_pending(self): Path("unexpected-dispatch").touch()
        """,
        )
        result = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.root / "unexpected-dispatch").exists())
        heartbeat = self.root / "heartbeat"
        self.assertTrue(heartbeat.exists(), result.stderr)
        before = heartbeat.read_text()
        time.sleep(0.15)
        self.assertEqual(heartbeat.read_text(), before)

    def test_malformed_worker_events_fail_closed(self) -> None:
        self.module(
            "a",
            """
            import os, sys, unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_corrupt(self):
                    events = Path(sys.argv[1]).parent / "events.jsonl"
                    with events.open("a") as stream:
                        stream.write('{"schema":"wrong"}\\n')
                    os._exit(0)
        """,
        )
        result = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("invalid worker event schema", result.stderr)
        self.assertEqual(json.loads(self.report.read_text())["summary"]["failed"], 1)

    def test_duplicate_instance_occurrences_are_counted_in_both_modes(self) -> None:
        self.module(
            "duplicates",
            """
            import unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_case(self):
                    with Path("executions").open("a") as stream:
                        stream.write("once\\n")
        """,
        )
        observed = []
        for workers in (1, 2):
            self.state.unlink(missing_ok=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"""
import io, unittest
from pathlib import Path
from literate_ai.test_checkpointing import run_unittest_suite
from test_duplicates import Tests
test = Tests('test_case')
raise SystemExit(run_unittest_suite(unittest.TestSuite([test, test]),
    state_path=Path('state.json'), stream=io.StringIO(),
    report_path=Path('report.json'), workers={workers}))
""",
                ],
                cwd=self.root,
                env=self.environment,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(self.report.read_text())
            observed.append((report["summary"], report["outcomes"]))
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0][0]["passed"], 2)
        self.assertEqual(
            (self.root / "executions").read_text(), "once\nonce\nonce\nonce\n"
        )

    def test_known_failures_context_and_release_evidence_keep_their_semantics(
        self,
    ) -> None:
        from literate_ai.contracts.identity import ContentIdentity, canonical_identity
        from literate_ai.contracts.known_test_failures import (
            KnownTestFailureAnnotation,
            KnownTestFailureCause,
            KnownTestFailurePolicy,
        )
        from literate_ai.known_failure_checkpoints import annotate_known_failure

        self.module(
            "a",
            """
            import unittest
            from pathlib import Path
            class Tests(unittest.TestCase):
                def test_case(self):
                    with Path("executions").open("a") as stream:
                        stream.write("once\\n")
                    self.assertTrue(Path("fixed").exists())
        """,
        )
        context = canonical_identity({"worker": "a"})
        arguments = ("--context-identity", context.uri)
        result = self.run_suite(extra=arguments)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(self.report.read_text())
        annotate_known_failure(
            self.state,
            "python-unittest",
            KnownTestFailureAnnotation(
                pin_identity=ContentIdentity.from_dict(report["pin_identity"]),
                test_key=report["outcomes"][0]["test_key"],
                cause=KnownTestFailureCause.AUTHENTICATION_REQUIRED,
                context_identity=context,
                universal_authority=None,
                failure_identity=ContentIdentity.from_dict(
                    report["outcomes"][0]["failure_identity"]
                ),
                evidence_run_identity=canonical_identity({"evidence": "observed"}),
                evidence_node_id="n1",
                reason="Fixture requires an external acknowledgement",
                policy=KnownTestFailurePolicy.MANUAL_REVALIDATION,
            ),
        )
        result = self.run_suite(extra=arguments)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            json.loads(self.report.read_text())["summary"]["known_failed"], 1
        )
        self.assertEqual((self.root / "executions").read_text(), "once\n")
        result = self.run_suite(
            extra=("--context-identity", canonical_identity({"worker": "b"}).uri)
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            json.loads(self.report.read_text())["summary"]["known_failed"], 0
        )
        (self.root / "fixed").touch()
        result = self.run_suite(extra=(*arguments, "--release-evidence"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.report.read_text())["summary"]["passed"], 1)
        self.assertEqual((self.root / "executions").read_text(), "once\nonce\nonce\n")

    def test_windows_gate_exposes_parallel_fail_fast_and_serial_debugging(self) -> None:
        from scripts import windows_release_gate as gate

        for workers, expected in (("auto", "auto"), ("3", "3"), ("1", "0")):
            with (
                self.subTest(workers=workers),
                patch.object(gate, "_windows", return_value=False),
                patch.object(gate, "_template_commands", return_value=[]),
                patch.object(
                    gate, "_environment_python", return_value=Path(sys.executable)
                ),
                patch.object(gate, "_run") as execute,
            ):
                self.assertEqual(
                    gate.main(["--only", "tests", "--test-workers", workers]), 0
                )
                argv = execute.call_args.args[0]
                self.assertEqual(argv[argv.index("-n") + 1], expected)
                self.assertIn("--dist=loadfile", argv)
                self.assertIn("--maxfail=1", argv)
                self.assertTrue(any(item.startswith("--junitxml=") for item in argv))
        with patch.object(gate, "_run") as execute, self.assertRaises(SystemExit):
            gate.main(["--only", "tests", "--test-workers", "0"])
        execute.assert_not_called()

    def test_serial_parallel_outcome_parity(self) -> None:
        fixtures = {
            "success": """
                class Tests(unittest.TestCase):
                    def test_pass(self): pass
                    @unittest.skip("intentional")
                    def test_skip(self): pass
                    @unittest.expectedFailure
                    def test_xfail(self): self.fail("expected")
                    def test_subskip(self):
                        with self.subTest(part=1): self.skipTest("part")
            """,
            "setup_skip": """
                class Tests(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls): raise unittest.SkipTest("class skip")
                    def test_a(self): pass
                    def test_b(self): pass
            """,
            "module_skip": """
                def setUpModule(): raise unittest.SkipTest("module skip")
                class Tests(unittest.TestCase):
                    def test_a(self): pass
                    def test_b(self): pass
            """,
            "setup_error": """
                class Tests(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls): raise RuntimeError("setup")
                    def test_a(self): pass
                    def test_b(self): pass
            """,
            "teardown_error": """
                class Tests(unittest.TestCase):
                    @classmethod
                    def tearDownClass(cls): raise RuntimeError("teardown")
                    def test_a(self): pass
                    def test_b(self): pass
            """,
            "subtest_failure": """
                class Tests(unittest.TestCase):
                    def test_a(self):
                        with self.subTest(part=1): self.fail("subtest")
            """,
            "unexpected_success": """
                class Tests(unittest.TestCase):
                    @unittest.expectedFailure
                    def test_a(self): pass
            """,
        }
        for name, source in fixtures.items():
            with self.subTest(fixture=name):
                self.module("parity", "import unittest\n" + textwrap.dedent(source))
                observed = []
                for workers in (1, 2):
                    self.state.unlink(missing_ok=True)
                    result = self.run_suite(workers=workers)
                    report = json.loads(self.report.read_text())
                    observed.append(
                        (result.returncode, report["summary"], report["outcomes"])
                    )
                self.assertEqual(observed[0], observed[1])


if __name__ == "__main__":
    unittest.main()

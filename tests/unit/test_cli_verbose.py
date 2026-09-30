from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.builders import run_bounded_process
from literate_ai.cli.dispatch import main
from literate_ai.diagnostics import (
    log_operation,
    operation_log,
    trace_exception,
    trace_subprocess,
    verbose_diagnostics,
)


class CliVerboseTests(unittest.TestCase):
    def test_short_verbose_flag_is_global_before_the_command(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = main(["-v", "--json", "help"], stdout=stdout, stderr=stderr)
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        self.assertEqual(stderr.getvalue(), "")

    def test_verbose_is_global_after_the_command_and_preserves_json_stdout(
        self,
    ) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        status = main(["--json", "help", "--verbose"], stdout=stdout, stderr=stderr)
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        self.assertEqual(stderr.getvalue(), "")

    def test_bounded_subprocess_is_visible_only_in_verbose_scope(self) -> None:
        quiet = io.StringIO()
        verbose = io.StringIO()
        environment = {**os.environ, "LITAI_VERBOSE": ""}
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            with verbose_diagnostics(False, quiet):
                run_bounded_process(
                    (sys.executable, "-c", "print('child output')"),
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=10,
                    stdout_limit_bytes=4096,
                    stderr_limit_bytes=4096,
                    error_prefix="verbose_test",
                )
            with verbose_diagnostics(True, verbose):
                run_bounded_process(
                    (sys.executable, "-c", "print('child output')"),
                    cwd=cwd,
                    environment=environment,
                    timeout_seconds=10,
                    stdout_limit_bytes=4096,
                    stderr_limit_bytes=4096,
                    error_prefix="verbose_test",
                )
        self.assertEqual(quiet.getvalue(), "")
        self.assertIn("[litai:subprocess start]", verbose.getvalue())
        self.assertIn("[litai:subprocess exit=0]", verbose.getvalue())
        self.assertIn("child output", verbose.getvalue())

    def test_verbose_scope_is_inherited_without_global_environment_mutation(
        self,
    ) -> None:
        stream = io.StringIO()
        environment = {**os.environ}
        environment.pop("LITAI_VERBOSE", None)
        process_value = os.environ.get("LITAI_VERBOSE")
        with tempfile.TemporaryDirectory() as temporary:
            with verbose_diagnostics(True, stream):
                result = run_bounded_process(
                    (
                        sys.executable,
                        "-c",
                        "import os; print(os.environ.get('LITAI_VERBOSE', ''))",
                    ),
                    cwd=Path(temporary),
                    environment=environment,
                    timeout_seconds=10,
                    stdout_limit_bytes=4096,
                    stderr_limit_bytes=4096,
                    error_prefix="verbose_inheritance_test",
                )
                self.assertEqual(os.environ.get("LITAI_VERBOSE"), process_value)
        self.assertEqual(result.stdout.splitlines(), [b"1"])

    def test_known_secret_arguments_environment_and_output_are_redacted(self) -> None:
        stream = io.StringIO()
        environment = {"SERVICE_TOKEN": "ultra-secret"}
        with verbose_diagnostics(True, stream):
            trace_subprocess(
                ("tool", "--token", "literal-secret", "ultra-secret"),
                cwd=Path.cwd(),
                environment=environment,
                status=1,
                stderr="failed with ultra-secret",
            )
        report = stream.getvalue()
        self.assertNotIn("literal-secret", report)
        self.assertNotIn("ultra-secret", report)
        self.assertGreaterEqual(report.count("<redacted>"), 3)

    def test_verbose_exception_is_bounded_and_redacts_environment_secrets(self) -> None:
        stream = io.StringIO()
        secret = "exception-secret"
        previous = os.environ.get("SERVICE_TOKEN")
        os.environ["SERVICE_TOKEN"] = secret
        try:
            with verbose_diagnostics(True, stream):
                trace_exception("node", RuntimeError("x" * 5000 + secret))
        finally:
            if previous is None:
                os.environ.pop("SERVICE_TOKEN", None)
            else:
                os.environ["SERVICE_TOKEN"] = previous
        report = stream.getvalue()
        self.assertIn("[litai:exception] node: RuntimeError:", report)
        self.assertNotIn(secret, report)
        self.assertLess(len(report), 4200)

    def test_operation_log_records_duration_and_redacts_errors(self) -> None:
        secret = "structured-log-secret"
        previous = os.environ.get("SERVICE_TOKEN")
        os.environ["SERVICE_TOKEN"] = secret
        try:
            with tempfile.TemporaryDirectory() as temporary:
                log_path = Path(temporary) / "operations.ndjson"
                with self.assertRaisesRegex(RuntimeError, secret):
                    with operation_log(log_path):
                        with log_operation("qualification", component="example"):
                            raise RuntimeError("failed with " + secret)
                records = [
                    json.loads(line)
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                ]
        finally:
            if previous is None:
                os.environ.pop("SERVICE_TOKEN", None)
            else:
                os.environ["SERVICE_TOKEN"] = previous
        self.assertEqual(
            [record["event"] for record in records],
            ["operation.start", "operation.error"],
        )
        self.assertEqual(records[0]["component"], "example")
        self.assertGreaterEqual(records[1]["duration_ms"], 0)
        self.assertNotIn(secret, records[1]["error"])

    def test_structured_subprocess_log_redacts_arguments_and_records_duration(
        self,
    ) -> None:
        environment = {"SERVICE_TOKEN": "structured-secret"}
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "operations.ndjson"
            with operation_log(log_path):
                trace_subprocess(
                    ("tool", "--token", "literal-secret", "structured-secret"),
                    cwd=temporary,
                    environment=environment,
                    status=0,
                    started_at=datetime.now(UTC),
                )
            record = json.loads(log_path.read_text(encoding="utf-8"))
        self.assertEqual(record["event"], "subprocess.exit")
        self.assertEqual(record["exit_code"], 0)
        self.assertGreaterEqual(record["duration_ms"], 0)
        self.assertNotIn("literal-secret", json.dumps(record))
        self.assertNotIn("structured-secret", json.dumps(record))


if __name__ == "__main__":
    unittest.main()

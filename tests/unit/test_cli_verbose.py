from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.diagnostics import (
    operation_log,
    trace_exception,
    trace_subprocess,
    verbose_diagnostics,
)


class CliVerboseTests(unittest.TestCase):
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

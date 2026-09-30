from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.cli.dispatch import main
from literate_ai.diagnostics import DEBUG_EVENT_SCHEMA, debug_diagnostics, debug_stage

_PLAIN_TEXT_ENVIRONMENT = {
    key: value
    for key, value in os.environ.items()
    if key not in {"FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE"}
} | {"NO_COLOR": "1", "TERM": "dumb"}


class CliDebugFlagTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, _PLAIN_TEXT_ENVIRONMENT, clear=True):
            status = main(arguments, stdout=stdout, stderr=stderr)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_debug_before_and_after_the_verb_preserves_json_stdout(self) -> None:
        for arguments in (
            ("--debug", "--json", "help"),
            ("--json", "help", "--debug"),
            ("--debug=-", "--json", "help"),
        ):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(status, 0)
                result = json.loads(stdout)
                self.assertTrue(result["ok"])
                self.assertEqual(result["command"], "help")
                self.assertEqual(stderr, "")

    def test_debug_file_is_ndjson_and_leaves_stdout_as_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "debug.ndjson"
            status, stdout, stderr = self.invoke(
                f"--debug={log}",
                "--json",
                "version",
                "check",
                "--no-project",
            )
            self.assertEqual(status, 0)
            self.assertEqual(stderr, "")
            envelope = json.loads(stdout)
            self.assertEqual(envelope["schema"], "literate-ai/cli-result@1")
            self.assertTrue(log.is_file())
            records = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(records)
            self.assertTrue(
                all(item.get("schema") == DEBUG_EVENT_SCHEMA for item in records)
            )
            events = {item.get("event") for item in records}
            self.assertIn("operation.start", events)
            self.assertIn("operation.end", events)

    def test_debug_off_emits_no_stage_events(self) -> None:
        status, stdout, stderr = self.invoke(
            "--json", "version", "check", "--no-project"
        )
        self.assertEqual(status, 0)
        json.loads(stdout)
        self.assertEqual(stderr, "")

    def test_help_lists_sdlc_catalog_bands(self) -> None:
        status, stdout, stderr = self.invoke("help")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Adopt / template", stdout)
        self.assertIn("Dev workflow", stdout)
        self.assertIn("Inverse / aspirational", stdout)
        self.assertIn("--debug", stdout)
        self.assertNotIn("worker capability", stdout)

    def test_debug_stage_redacts_secrets_on_human_stream(self) -> None:
        stream = io.StringIO()
        stream.isatty = lambda: True  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, {"LITAI_TOKEN": "supersecret-token-value"}):
            with debug_diagnostics("-", json_mode=False, stderr=stream):
                with debug_stage("verify", detail="supersecret-token-value"):
                    pass
        text = stream.getvalue()
        self.assertIn("[litai:stage]", text)
        self.assertNotIn("supersecret-token-value", text)
        self.assertIn("<redacted>", text)

    def test_debug_traces_child_argv_without_child_output(self) -> None:
        stream = io.StringIO()
        stream.isatty = lambda: True  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, _PLAIN_TEXT_ENVIRONMENT, clear=True):
            with debug_diagnostics("-", json_mode=False, stderr=stream):
                from literate_ai.diagnostics import trace_subprocess

                trace_subprocess(
                    ("git", "status"),
                    cwd="/tmp/example",
                    status=0,
                    stdout="would-be-child-output\n",
                    stderr="would-be-child-error\n",
                )
        text = stream.getvalue()
        self.assertIn("[litai:command]", text)
        self.assertIn("argv=git status", text)
        self.assertIn("exit_code=0", text)
        self.assertNotIn("would-be-child-output", text)
        self.assertNotIn("would-be-child-error", text)

    def test_debug_file_records_subprocess_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "debug.ndjson"
            with debug_diagnostics(str(log), json_mode=True, stderr=io.StringIO()):
                from literate_ai.diagnostics import trace_subprocess

                trace_subprocess(("make", "test"), cwd=temporary, status=None)
                trace_subprocess(("make", "test"), cwd=temporary, status=2)
            records = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        events = [item.get("event") for item in records]
        self.assertIn("subprocess.start", events)
        self.assertIn("subprocess.exit", events)
        self.assertTrue(
            all(item.get("schema") == DEBUG_EVENT_SCHEMA for item in records)
        )
        exit_record = next(
            item for item in records if item.get("event") == "subprocess.exit"
        )
        self.assertEqual(exit_record["argv"], ["make", "test"])
        self.assertEqual(exit_record["exit_code"], 2)
        self.assertNotIn("stdout", exit_record)


if __name__ == "__main__":
    unittest.main()

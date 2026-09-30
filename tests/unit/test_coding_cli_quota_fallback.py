"""Public-API tests for coding-CLI JSON-task quota denial and fallback."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.models import CodingCliError, CodingCliTaskRunner
from literate_ai.adapters.models.coding_cli import _coding_cli_quota_denied

_CODEX_SPEND_CAP = (
    "You hit your spend cap set by the owner of your workspace. "
    "Ask an owner to increase your spend cap to continue."
)
_CLAUDE_SPEND_LIMIT = (
    "You've hit your individual spend limit · run /usage-credits to ask your "
    "admin for a higher limit"
)
_RESPONSE = {"schema": "test-response@1", "ok": True}


def _write_stub(directory: Path, name: str, source: str) -> Path:
    if os.name == "nt":
        script = directory / f"{name}.py"
        script.write_text(source, encoding="utf-8")
        path = directory / f"{name}.cmd"
        path.write_text(
            f'@"{sys.executable}" "%~dp0{name}.py" %*\n',
            encoding="utf-8",
        )
        return path
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


_CODEX_QUOTA_STUB = f"""
import sys

if tuple(sys.argv[1:3]) == ("login", "status"):
    raise SystemExit(0)
print({_CODEX_SPEND_CAP!r}, file=sys.stderr)
raise SystemExit(1)
"""

_CLAUDE_SUCCESS_STUB = """
import json
from pathlib import Path

Path(".literate-ai-task-response.json").write_text(
    json.dumps({"schema": "test-response@1", "ok": True}),
    encoding="utf-8",
)
"""

_CLAUDE_QUOTA_STUB = f"""
print({_CLAUDE_SPEND_LIMIT!r})
raise SystemExit(1)
"""


class CodingCliJsonTaskQuotaContractTests(unittest.TestCase):
    def test_quota_phrase_classification_is_cli_specific(self) -> None:
        ansi_codex = "\x1b[31m" + _CODEX_SPEND_CAP + "\x1b[0m"
        cases = (
            ("codex", _CODEX_SPEND_CAP, True),
            ("codex", ansi_codex, True),
            ("codex", _CLAUDE_SPEND_LIMIT, False),
            ("claude", _CLAUDE_SPEND_LIMIT, True),
            ("claude", _CODEX_SPEND_CAP, False),
            ("cursor-agent", _CODEX_SPEND_CAP, False),
            ("opencode", _CLAUDE_SPEND_LIMIT, False),
        )
        for cli, output, expected in cases:
            with self.subTest(cli=cli, expected=expected):
                self.assertEqual(_coding_cli_quota_denied(cli, output), expected)

    def test_json_task_reports_quota_denied_without_fallback_when_cli_is_pinned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary)
            _write_stub(tools, "codex", _CODEX_QUOTA_STUB)
            _write_stub(tools, "claude", _CLAUDE_SUCCESS_STUB)
            runner = CodingCliTaskRunner(
                environment={"CODING_CLI": "codex", "PATH": str(tools)}
            )
            with self.assertRaises(CodingCliError) as quota:
                runner.run_json_task("Return JSON.")
            self.assertEqual(quota.exception.code, "coding_cli.quota_denied")
            self.assertEqual(runner.selection.name, "codex")

    def test_json_task_quota_denial_falls_back_to_the_next_available_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary)
            _write_stub(tools, "codex", _CODEX_QUOTA_STUB)
            _write_stub(tools, "claude", _CLAUDE_SUCCESS_STUB)
            runner = CodingCliTaskRunner(environment={"PATH": str(tools)})
            self.assertEqual(runner.selection.name, "codex")
            result = runner.run_json_task("Return JSON.")
            self.assertEqual(runner.selection.name, "claude")
            self.assertEqual(result.response, _RESPONSE)
            self.assertEqual(result.coding_cli, "claude")

    def test_observed_claude_spend_limit_is_a_quota_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary)
            _write_stub(tools, "claude", _CLAUDE_QUOTA_STUB)
            runner = CodingCliTaskRunner(
                environment={"CODING_CLI": "claude", "PATH": str(tools)}
            )
            with self.assertRaises(CodingCliError) as quota:
                runner.run_json_task("Return JSON.")
            self.assertEqual(quota.exception.code, "coding_cli.quota_denied")

    def test_quota_denial_reraises_when_no_fallback_cli_is_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tools = Path(temporary)
            _write_stub(tools, "codex", _CODEX_QUOTA_STUB)
            runner = CodingCliTaskRunner(environment={"PATH": str(tools)})
            with self.assertRaises(CodingCliError) as quota:
                runner.run_json_task("Return JSON.")
            self.assertEqual(quota.exception.code, "coding_cli.quota_denied")
            self.assertEqual(runner.selection.name, "codex")


if __name__ == "__main__":
    unittest.main()

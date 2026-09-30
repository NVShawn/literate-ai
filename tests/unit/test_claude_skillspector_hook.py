from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "skillspector-scan.sh"
POSIX_SHELL = Path("/bin/sh")


class ClaudeSkillSpectorHookTests(unittest.TestCase):
    def _write_tool(self, directory: Path, name: str, body: str) -> Path:
        path = directory / name
        path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _run(
        self,
        directory: Path,
        *,
        file_path: str,
        scanner_status: int | None,
    ) -> subprocess.CompletedProcess[str]:
        self._write_tool(directory, "jq", 'printf "%s\\n" "$HOOK_PATH"\n')
        if scanner_status is not None:
            self._write_tool(
                directory,
                "skillspector",
                f'printf "%s\\n" "$*" > "$HOOK_LOG"\nexit {scanner_status}\n',
            )
        return subprocess.run(
            (str(POSIX_SHELL), str(HOOK)),
            input=json.dumps({"tool_input": {"file_path": file_path}}),
            text=True,
            capture_output=True,
            env={
                "PATH": str(directory),
                "HOOK_PATH": file_path,
                "HOOK_LOG": str(directory / "scanner.log"),
            },
            check=False,
        )

    def test_settings_delegate_without_suppressing_hook_failure(self) -> None:
        settings = json.loads(
            (ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        self.assertIn("skillspector-scan.sh", command)
        self.assertNotIn("|| true", command)

    @unittest.skipUnless(POSIX_SHELL.is_file(), "requires a POSIX shell")
    def test_non_skill_edit_does_not_require_scanner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary), file_path="docs/README.md", scanner_status=None
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(POSIX_SHELL.is_file(), "requires a POSIX shell")
    def test_missing_scanner_fails_closed_for_skill_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary),
                file_path="skills/example/SKILL.md",
                scanner_status=None,
            )
        self.assertEqual(result.returncode, 127)
        self.assertIn("skillspector is required", result.stderr)

    @unittest.skipUnless(POSIX_SHELL.is_file(), "requires a POSIX shell")
    def test_scanner_failure_is_propagated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary), file_path="skills/example/SKILL.md", scanner_status=19
            )
        self.assertEqual(result.returncode, 19)

    @unittest.skipUnless(POSIX_SHELL.is_file(), "requires a POSIX shell")
    def test_scanner_receives_exact_skill_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = self._run(
                directory,
                file_path="skills/example with spaces/SKILL.md",
                scanner_status=0,
            )
            invocation = (directory / "scanner.log").read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocation, "scan skills/example with spaces/SKILL.md\n")

    @unittest.skipUnless(POSIX_SHELL.is_file(), "requires a POSIX shell")
    def test_missing_json_parser_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                (str(POSIX_SHELL), str(HOOK)),
                input='{"tool_input":{"file_path":"skills/example/SKILL.md"}}',
                text=True,
                capture_output=True,
                env={"PATH": temporary},
                check=False,
            )
        self.assertEqual(result.returncode, 127)
        self.assertIn("jq is required", result.stderr)


if __name__ == "__main__":
    unittest.main()

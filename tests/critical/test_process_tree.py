from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters import _processes


class _Process:
    pid = 1729

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:
        self.killed = True


class ProcessTreeTerminationTests(unittest.TestCase):
    def test_windows_taskkill_uses_only_validated_system32_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            windows = root / "Windows"
            system32 = windows / "System32"
            system32.mkdir(parents=True)
            taskkill = system32 / "taskkill.exe"
            taskkill.write_bytes(b"system taskkill")
            taskkill.chmod(0o755)
            decoy = root / "project-bin"
            decoy.mkdir()
            (decoy / "taskkill.exe").write_bytes(b"project decoy")
            environment = {
                "SystemRoot": str(windows),
                "PATH": str(decoy),
                "LITERATE_AI_SECRET": "must-not-be-forwarded",
            }
            process = _Process()
            terminator = mock.Mock()
            terminator.wait.return_value = 0

            with mock.patch.object(
                _processes.subprocess, "Popen", return_value=terminator
            ) as popen:
                _processes.terminate_process_tree(
                    process, platform_name="nt", environment=environment
                )

            command = popen.call_args.args[0]
            options = popen.call_args.kwargs
            resolved_windows = windows.resolve()
            resolved_system32 = resolved_windows / "System32"
            resolved_taskkill = resolved_system32 / "taskkill.exe"
            self.assertEqual(command[0], str(resolved_taskkill))
            self.assertNotEqual(command[0], str(decoy / "taskkill.exe"))
            self.assertEqual(options["cwd"], resolved_system32)
            self.assertEqual(options["env"], {"SystemRoot": str(resolved_windows)})
            self.assertNotIn("PATH", options["env"])
            self.assertNotIn("LITERATE_AI_SECRET", options["env"])
            terminator.wait.assert_called_once_with(timeout=5)
            self.assertTrue(process.killed)


class RunWithTreeKillTests(unittest.TestCase):
    """Regression coverage for #79: `subprocess.run(timeout=)` call sites that
    only killed the direct child, leaving helper trees (git/pip/conan/worker
    bootstrap descendants) alive. `run_with_tree_kill` is the drop-in
    replacement every affected call site now routes through."""

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_kills_the_whole_process_tree_not_just_the_direct_child(
        self,
    ) -> None:
        """Reproducer from the issue: a wrapper that daemonizes a sleeper,
        then the runner times out. The sleeper must not remain."""

        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "grandchild.pid"
            grandchild_script = Path(temporary) / "grandchild.py"
            grandchild_script.write_text(
                "import os, time, sys\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            parent_script = Path(temporary) / "parent.py"
            parent_script.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(grandchild_script)!r}])\n"
                "time.sleep(60)\n"
            )

            with self.assertRaises(subprocess.TimeoutExpired):
                _processes.run_with_tree_kill(
                    (sys.executable, str(parent_script)),
                    timeout=1,
                )

            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild never started")
            grandchild_pid = int(pid_file.read_text().strip())

            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(
                alive, "grandchild survived timeout; only the direct child was killed"
            )


if __name__ == "__main__":
    unittest.main()

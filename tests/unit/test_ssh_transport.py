from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import _processes
from literate_ai.adapters.ssh_transport import (
    MAX_SSH_DIAGNOSTIC_BYTES,
    SSH_DIAGNOSTIC_TRUNCATION_MARKER,
    BoundedSshProcessRunner,
    SshTransportError,
    scp_arguments,
    ssh_arguments,
)
from literate_ai.diagnostics import verbose_diagnostics


class SshTransportTests(unittest.TestCase):
    def test_ssh_argv_is_noninteractive_and_wraps_the_remote_command_atomically(
        self,
    ) -> None:
        argv = ssh_arguments("user@host.example", "printf 'hello world'", 45)

        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectionAttempts=1", argv)
        self.assertIn("ConnectTimeout=30", argv)
        self.assertEqual(argv[-2], "user@host.example")
        self.assertEqual(argv[-1], "bash -lic 'printf '\"'\"'hello world'\"'\"''")

    def test_ssh_argv_transport_is_configurable_and_keeps_the_same_flags(
        self,
    ) -> None:
        argv = ssh_arguments(
            "user@host.example", "printf 'hello world'", 45, transport="s"
        )

        self.assertEqual(argv[0], "s")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectionAttempts=1", argv)
        self.assertIn("ConnectTimeout=30", argv)
        self.assertEqual(argv[-2], "user@host.example")
        self.assertEqual(argv[-1], "bash -lic 'printf '\"'\"'hello world'\"'\"''")

    def test_ssh_argv_forces_login_shell_semantics_so_dotfile_path_is_honored(
        self,
    ) -> None:
        """A bare `ssh host cmd` runs a non-login, non-interactive `$SHELL -c cmd`,
        which for bash sources neither .bashrc nor .bash_profile/.profile. A
        conventional .bashrc also bails out early for non-interactive shells, so
        `bash -lic` forces both login and interactive semantics -- a worker's real
        PATH (Homebrew, pyenv, nvm, ...) is honored without editing dotfiles."""

        argv = ssh_arguments("user@host.example", "make release-check", 10)

        self.assertEqual(argv[-1], "bash -lic 'make release-check'")

    def test_ssh_argv_can_preserve_a_windows_powershell_command(self) -> None:
        command = "powershell.exe -NoLogo -NoProfile -EncodedCommand ZQB4AGkAdAA="

        argv = ssh_arguments(
            "user@windows.example",
            command,
            10,
            login_shell=False,
        )

        self.assertEqual(argv[-2], "user@windows.example")
        self.assertEqual(argv[-1], command)
        self.assertNotIn("bash", argv[-1])

    def test_ssh_argv_rejects_an_unsafe_transport_name(self) -> None:
        for invalid in ("", "ssh -oProxyCommand=x", "../ssh", "ssh;rm -rf /"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SshTransportError) as raised:
                    ssh_arguments("user@host.example", "echo hi", 10, transport=invalid)
                self.assertEqual(
                    raised.exception.code, "execution.ssh_transport_invalid"
                )

    def test_scp_requires_an_exact_regular_file_and_literal_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "request.json"
            source.write_text("{}", encoding="utf-8")
            argv = scp_arguments(source, "user@host", "incoming/request.json", 5)
            self.assertEqual(
                argv[-2:], (str(source), "user@host:incoming/request.json")
            )
            with self.assertRaises(SshTransportError) as raised:
                scp_arguments(source, "user@host;touch-pwned", "request.json", 5)
            self.assertEqual(raised.exception.code, "execution.ssh_endpoint_invalid")

    def test_runner_times_out_and_bounds_both_output_streams(self) -> None:
        process = unittest.mock.MagicMock()
        process.__enter__.return_value = process
        process.__exit__.return_value = False
        process.wait.side_effect = [subprocess.TimeoutExpired(("ssh",), 1), 0]
        process.returncode = -9
        ownership = unittest.mock.MagicMock()
        ownership.popen_options = {}
        with (
            patch(
                "literate_ai.adapters.ssh_transport.create_process_tree_ownership",
                return_value=ownership,
            ),
            patch(
                "literate_ai.adapters.ssh_transport.subprocess.Popen",
                return_value=process,
            ),
            patch(
                "literate_ai.adapters.ssh_transport.terminate_process_tree"
            ) as terminate,
        ):
            with self.assertRaises(SshTransportError) as raised:
                BoundedSshProcessRunner().run(
                    ("ssh", "user@host", "true"), cwd=Path.cwd(), timeout_seconds=1
                )
        self.assertEqual(raised.exception.code, "execution.ssh_timed_out")
        # Regression for #70: passing `environment={}` here made
        # `windows_taskkill_executable` see no `SystemRoot` at all (an empty
        # mapping, not "use os.environ"), so `taskkill /T` never ran on
        # Windows. The runner must let the shared helper fall back to the
        # real process environment instead of shadowing it with `{}`.
        terminate.assert_called_once()
        self.assertIs(terminate.call_args.args[0], process)

    def test_runner_kills_the_process_if_it_is_still_alive_after_terminate(
        self,
    ) -> None:
        """Regression for #70: `process.wait()` after `terminate_process_tree`
        was unbounded, so a grandchild holding the inherited stdout/stderr
        pipe open could hang the runner forever. The runner must bound that
        wait and fall back to `process.kill()` instead of waiting forever."""

        process = unittest.mock.MagicMock()
        process.__enter__.return_value = process
        process.__exit__.return_value = False
        process.wait.side_effect = [
            subprocess.TimeoutExpired(("ssh",), 1),
            subprocess.TimeoutExpired(("ssh",), 5),
            0,
        ]
        process.returncode = -9
        ownership = unittest.mock.MagicMock()
        ownership.popen_options = {}
        with (
            patch(
                "literate_ai.adapters.ssh_transport.create_process_tree_ownership",
                return_value=ownership,
            ),
            patch(
                "literate_ai.adapters.ssh_transport.subprocess.Popen",
                return_value=process,
            ),
            patch(
                "literate_ai.adapters.ssh_transport.terminate_process_tree"
            ) as terminate,
        ):
            with self.assertRaises(SshTransportError) as raised:
                BoundedSshProcessRunner().run(
                    ("ssh", "user@host", "true"), cwd=Path.cwd(), timeout_seconds=1
                )
        self.assertEqual(raised.exception.code, "execution.ssh_timed_out")
        terminate.assert_called_once()
        self.assertIs(terminate.call_args.args[0], process)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 3)

    def test_runner_uses_platform_process_group_options_instead_of_posix_only_flag(
        self,
    ) -> None:
        """Regression for #81: a bare `start_new_session=True` is POSIX-only and
        silently ignored on Windows, leaving `terminate_process_tree` with no
        process-group membership to work with. The runner must instead pass
        `create_process_tree_ownership().popen_options` for the current
        platform, rather than hardcoding the POSIX-only kwarg itself."""

        process = unittest.mock.MagicMock()
        process.__enter__.return_value = process
        process.__exit__.return_value = False
        process.wait.return_value = 0
        process.returncode = 0
        process.pid = 4242
        windows_options = {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
        ownership = unittest.mock.MagicMock()
        ownership.popen_options = windows_options
        with (
            patch(
                "literate_ai.adapters.ssh_transport.create_process_tree_ownership",
                return_value=ownership,
            ),
            patch(
                "literate_ai.adapters.ssh_transport.subprocess.Popen",
                return_value=process,
            ) as popen,
        ):
            BoundedSshProcessRunner().run(
                ("ssh", "user@host", "true"), cwd=Path.cwd(), timeout_seconds=1
            )
        _, kwargs = popen.call_args
        self.assertNotIn("start_new_session", kwargs)
        self.assertEqual(kwargs.get("creationflags"), windows_options["creationflags"])
        ownership.bind.assert_called_once_with(process.pid)
        ownership.release.assert_called()

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_runner_still_makes_a_posix_session_leader_for_killpg(self) -> None:
        self.assertEqual(
            _processes.process_group_options(platform_name="posix"),
            {"start_new_session": True},
        )
        process = unittest.mock.MagicMock()
        process.__enter__.return_value = process
        process.__exit__.return_value = False
        process.wait.return_value = 0
        process.returncode = 0
        with patch(
            "literate_ai.adapters.ssh_transport.subprocess.Popen",
            return_value=process,
        ) as popen:
            BoundedSshProcessRunner().run(
                ("ssh", "user@host", "true"), cwd=Path.cwd(), timeout_seconds=1
            )
        _, kwargs = popen.call_args
        self.assertEqual(kwargs.get("start_new_session"), True)
        self.assertNotIn("creationflags", kwargs)

    def test_runner_reports_an_unavailable_transport_with_a_stable_code(self) -> None:
        with patch(
            "literate_ai.adapters.ssh_transport.subprocess.Popen",
            side_effect=FileNotFoundError("ssh"),
        ):
            with self.assertRaises(SshTransportError) as raised:
                BoundedSshProcessRunner().run(
                    ("ssh", "user@host", "true"), cwd=Path.cwd(), timeout_seconds=1
                )
        self.assertEqual(raised.exception.code, "execution.ssh_unavailable")

    def test_runner_emits_bounded_process_output_in_verbose_scope(self) -> None:
        diagnostics = io.StringIO()
        with verbose_diagnostics(True, diagnostics):
            result = BoundedSshProcessRunner().run(
                (
                    sys.executable,
                    "-c",
                    "import sys; print('out'); print('detail', file=sys.stderr)",
                ),
                cwd=Path.cwd(),
                timeout_seconds=5,
            )

        self.assertEqual(result.returncode, 0)
        report = diagnostics.getvalue()
        self.assertIn("[litai:subprocess start]", report)
        self.assertIn("[litai:subprocess exit=0]", report)
        self.assertIn("out", report)
        self.assertIn("detail", report)

    def test_runner_retains_diagnostic_tail_without_masking_result(self) -> None:
        result = BoundedSshProcessRunner().run(
            (
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('x' * 70000 + '\\nTYPED-RESULT\\n')",
            ),
            cwd=Path.cwd(),
            timeout_seconds=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr), MAX_SSH_DIAGNOSTIC_BYTES)
        self.assertTrue(result.stderr.startswith(SSH_DIAGNOSTIC_TRUNCATION_MARKER))
        line_ending = b"\r\n" if sys.platform == "win32" else b"\n"
        self.assertTrue(result.stderr.endswith(b"TYPED-RESULT" + line_ending))


if __name__ == "__main__":
    unittest.main()

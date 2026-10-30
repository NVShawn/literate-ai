from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.ssh_transport import (
    BoundedSshProcessRunner,
    SshTransportError,
    scp_arguments,
    scp_download_arguments,
    ssh_arguments,
)


class SshTransportTests(unittest.TestCase):
    def test_ssh_and_scp_keep_long_control_channels_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"source")
            commands = (
                ssh_arguments("user@host.example", "echo hi", 10),
                scp_arguments(source, "user@host.example", "/job/source.bin", 10),
                scp_download_arguments(
                    "user@host.example",
                    "/job/remote-evidence.tar.gz",
                    root / "evidence",
                    10,
                ),
            )
            for command in commands:
                with self.subTest(executable=command[0]):
                    self.assertIn("ConnectTimeout=10", command)
                    interval = command.index("ServerAliveInterval=30")
                    count = command.index("ServerAliveCountMax=3")
                    self.assertEqual(command[interval - 1], "-o")
                    self.assertEqual(command[count - 1], "-o")

    def test_scp_routes_upload_and_download_through_the_selected_ssh_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"source")
            calls = (
                (scp_arguments, (source, "user@host.example", "/job/source.bin", 9)),
                (
                    scp_download_arguments,
                    (
                        "user@host.example",
                        "/job/remote-evidence.tar.gz",
                        root / "evidence",
                        9,
                    ),
                ),
            )
            for function, args in calls:
                with self.subTest(helper=function.__name__):
                    default = function(*args)
                    self.assertEqual(default, function(*args, transport="ssh"))
                    routed = function(*args, transport="private-route")
                    self.assertEqual(routed[:3], ("scp", "-S", "private-route"))
                    self.assertEqual(routed[3:], default[1:])
                    for invalid in ("", "../ssh", "ssh -oProxyCommand=x", "ssh;exit"):
                        with self.assertRaises(SshTransportError) as caught:
                            function(*args, transport=invalid)
                        self.assertEqual(
                            caught.exception.code, "execution.ssh_transport_invalid"
                        )

    def test_ssh_argv_rejects_an_unsafe_transport_name(self) -> None:
        for invalid in ("", "ssh -oProxyCommand=x", "../ssh", "ssh;rm -rf /"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SshTransportError) as raised:
                    ssh_arguments("user@host.example", "echo hi", 10, transport=invalid)
                self.assertEqual(
                    raised.exception.code, "execution.ssh_transport_invalid"
                )

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


if __name__ == "__main__":
    unittest.main()

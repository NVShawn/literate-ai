from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.remote_transport import (
    BoundedRemoteProcessRunner,
    RemoteWorkerTransport,
)
from literate_ai.adapters.ssh_transport import (
    BoundedSshProcessRunner,
    SshTransportError,
    scp_arguments,
    scp_download_arguments,
    ssh_arguments,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
    WorkerReachability,
)


class SshTransportTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "native POSIX connector custody")
    def test_command_route_transfers_refuse_corruption_and_preserve_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "route adapter.py"
            adapter.write_text(
                "import pathlib,shutil,subprocess,sys\n"
                "op=sys.argv[1]\n"
                "if op=='execute':\n"
                " raise SystemExit(subprocess.run(['/bin/sh','-c',sys.argv[2]])"
                ".returncode)\n"
                "if pathlib.Path(sys.argv[2]).read_bytes()==b'no-output':\n"
                " raise SystemExit(0)\n"
                "shutil.copyfile(sys.argv[2],sys.argv[3])\n"
            )
            family = {"Darwin": "macos", "FreeBSD": "freebsd"}.get(
                platform.system(), "linux"
            )
            worker = ExecutionWorker(
                "route",
                ExecutionWorkerKind.REMOTE,
                requirements=ExecutionRequirements(os_family=family),
                workspace=str(root),
                lifecycle_executable=sys.executable,
                reachability=WorkerReachability(
                    (sys.executable, str(adapter), "execute", "{remote_command}"),
                    (
                        sys.executable,
                        str(adapter),
                        "upload",
                        "{local_file}",
                        "{remote_file}",
                    ),
                    (
                        sys.executable,
                        str(adapter),
                        "download",
                        "{remote_file}",
                        "{local_file}",
                    ),
                ),
            )
            transport = RemoteWorkerTransport(worker)
            source = root / "source bytes.bin"
            content = os.urandom(8192)
            source.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            remote = root / "remote ;$(literal)"
            remote.mkdir()
            archive = remote / "remote-evidence.tar.gz"
            uploaded = transport.upload(
                source, str(archive), cwd=root, timeout_seconds=10
            )
            self.assertEqual(uploaded.returncode, 0, uploaded.stderr)
            self.assertEqual(archive.read_bytes(), content)
            destination = root / "accepted evidence"
            result = transport.download(
                str(archive),
                destination,
                cwd=root,
                timeout_seconds=10,
                maximum_bytes=len(content),
                expected_digest=digest,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(destination.read_bytes(), content)
            self.assertEqual(destination.stat().st_nlink, 1)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            archive.write_bytes(bytes([content[0] ^ 1]) + content[1:])
            refused = root / "refused evidence"
            with self.assertRaises(SshTransportError) as caught:
                transport.download(
                    str(archive),
                    refused,
                    cwd=root,
                    timeout_seconds=10,
                    maximum_bytes=len(content),
                    expected_digest=digest,
                )
            self.assertEqual(
                caught.exception.code, "execution.remote_download_digest_mismatch"
            )
            self.assertFalse(refused.exists())
            with self.assertRaises(SshTransportError):
                transport.download(
                    str(archive), destination, cwd=root, timeout_seconds=10
                )
            self.assertEqual(destination.read_bytes(), content)
            with self.assertRaises(SshTransportError):
                transport.download(
                    str(archive),
                    refused,
                    cwd=root,
                    timeout_seconds=10,
                    maximum_bytes=len(content) - 1,
                )
            self.assertFalse(refused.exists())
            archive.write_bytes(b"no-output")
            with self.assertRaises(SshTransportError) as missing:
                transport.download(str(archive), refused, cwd=root, timeout_seconds=10)
            self.assertEqual(
                missing.exception.code, "execution.remote_file_unavailable"
            )
            self.assertFalse(refused.exists())
            archive.write_bytes(content)
            real_clock = time.monotonic
            connector_done = [False]
            hash_clock_calls = [0]

            class ExpiringRunner(BoundedRemoteProcessRunner):
                def run(self, *args, **kwargs):
                    result = super().run(*args, **kwargs)
                    connector_done[0] = True
                    return result

            def advancing_clock():
                if connector_done[0]:
                    hash_clock_calls[0] += 1
                return real_clock() + (60 if hash_clock_calls[0] > 5 else 0)

            # The connector really copies bytes; only the external clock advances
            # during post-transfer hashing. Expired custody must never publish.
            with (
                patch(
                    "literate_ai.adapters.remote_transport.time.monotonic",
                    side_effect=advancing_clock,
                ),
                self.assertRaises(SshTransportError) as expired,
            ):
                transport.download(
                    str(archive),
                    refused,
                    cwd=root,
                    timeout_seconds=10,
                    expected_digest=digest,
                    runner=ExpiringRunner(),
                )
            self.assertEqual(expired.exception.code, "execution.remote_timeout_invalid")
            self.assertFalse(refused.exists())
            self.assertGreater(hash_clock_calls[0], 5)
            self.assertEqual(source.read_bytes(), content)
            self.assertEqual(list(root.glob("litai-route-download-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX connector descendant control")
    def test_remote_connector_keeps_bounded_diagnostics_and_stops_owned_descendants(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = BoundedRemoteProcessRunner()
            result = runner.run(
                (sys.executable, "-c", "import os;os.write(2,b'x'*1048576+b'END')"),
                cwd=root,
                timeout_seconds=10,
            )
            self.assertEqual(result.returncode, 0)
            self.assertLessEqual(len(result.stderr), 65536)
            self.assertTrue(result.stderr.startswith(b"[litai:ssh stderr truncated;"))
            self.assertTrue(result.stderr.endswith(b"END"))
            escaped = root / "child-survived"
            child = (
                "import pathlib,time;time.sleep(2);"
                f"pathlib.Path({str(escaped)!r}).write_text('survived')"
            )
            command = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}]);"
                "time.sleep(60)"
            )
            with self.assertRaises(SshTransportError) as caught:
                runner.run((sys.executable, "-c", command), cwd=root, timeout_seconds=1)
            self.assertEqual(caught.exception.code, "execution.ssh_timed_out")
            time.sleep(2)
            self.assertFalse(
                escaped.exists(), "timed-out connector descendant survived"
            )

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

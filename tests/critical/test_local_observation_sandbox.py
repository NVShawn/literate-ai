from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.source_to_specification import (
    LocalObservationSandbox,
    SourceToSpecificationError,
    canonical_digest,
)


class LocalObservationSandboxTests(unittest.TestCase):
    def sandbox(self, root: Path, *, system: str, resolver):
        return LocalObservationSandbox(
            source_root=root,
            command=("/usr/bin/example-observer", "--json"),
            system=system,
            tool_resolver=resolver,
        )

    def authorization(self, request):
        return {
            "authorization_id": "observe-auth:test",
            "request_digest": canonical_digest(request),
            "privileges": request["requested_privileges"],
        }

    def request(self, sandbox):
        return {
            "runner_id": sandbox.runner_id,
            "harness_digest": sandbox.harness_digest,
            "source_digests": [sandbox.source_digest],
            "requested_privileges": ["processes"],
        }

    def test_missing_or_unsupported_sandbox_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            scratch = base / "scratch"
            scratch.mkdir()
            for system in ("Darwin", "Linux"):
                with self.subTest(system=system):
                    sandbox = self.sandbox(
                        root, system=system, resolver=lambda _name: None
                    )
                    with self.assertRaisesRegex(
                        SourceToSpecificationError, "unavailable"
                    ):
                        sandbox.command_for(scratch)
            unsupported = self.sandbox(
                root,
                system="Windows",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            with self.assertRaisesRegex(SourceToSpecificationError, "only macOS"):
                unsupported.command_for(scratch)

    def test_missing_or_mismatched_authorization_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = self.sandbox(
                root,
                system="Darwin",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            with patch("subprocess.Popen") as process:
                with self.assertRaisesRegex(
                    SourceToSpecificationError, "authorization"
                ):
                    sandbox.run(request, {})
                mismatch = self.authorization(request)
                mismatch["request_digest"] = "sha256:wrong"
                with self.assertRaisesRegex(
                    SourceToSpecificationError, "authorization"
                ):
                    sandbox.run(request, mismatch)
                process.assert_not_called()

    @unittest.skipUnless(
        hasattr(os, "killpg") and hasattr(signal, "SIGKILL"),
        "requires POSIX process groups",
    )
    def test_timeout_kills_the_complete_sandbox_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=("/usr/bin/example-observer",),
                timeout_seconds=0.01,
                system="Darwin",
                tool_resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            process = Mock(pid=4321)
            process.wait.side_effect = [
                subprocess.TimeoutExpired("sandbox", 0.01),
                0,
            ]
            with (
                patch("subprocess.Popen", return_value=process),
                patch("os.killpg") as kill_group,
            ):
                with self.assertRaisesRegex(SourceToSpecificationError, "timeout"):
                    sandbox.run(request, self.authorization(request))
            kill_group.assert_called_once_with(4321, signal.SIGKILL)

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("sandbox-exec"),
        "requires the macOS sandbox runtime",
    )
    def test_macos_runtime_denies_source_writes_network_and_host_environment(
        self,
    ) -> None:
        program = """import errno, json, os, socket
result = {'home_absent': 'HOME' not in os.environ}
try:
    open('source.txt', 'w').write('mutated')
    result['source_write_denied'] = False
except PermissionError:
    result['source_write_denied'] = True
connection = socket.socket()
try:
    connection.connect(('127.0.0.1', 9))
    result['network_denied'] = False
except OSError as exc:
    result['network_denied'] = exc.errno in (errno.EPERM, errno.EACCES)
print(json.dumps(result))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("read-only\n")
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=(sys.executable, "-c", program),
                system="Darwin",
            )
            result = sandbox.run(
                request := self.request(sandbox),
                self.authorization(request),
            )
            self.assertEqual(
                result,
                {
                    "home_absent": True,
                    "network_denied": True,
                    "source_write_denied": True,
                },
            )
            self.assertEqual(source.read_text(), "read-only\n")


if __name__ == "__main__":
    unittest.main()

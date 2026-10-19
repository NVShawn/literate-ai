from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.worker_capabilities import (
    _LINUX_SCRIPT,
    _WINDOWS_SCRIPT,
    WorkerCapabilityProbeError,
    _device,
    probe_worker_capabilities,
    probe_worker_catalog,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)


def worker(worker_id: str, family: str) -> ExecutionWorker:
    return ExecutionWorker(
        worker_id,
        ExecutionWorkerKind.SSH,
        requirements=ExecutionRequirements(os_family=family),
        endpoint=f"user@{worker_id}.example",
        workspace="~/literate-ai",
    )


class WorkerCapabilityTests(unittest.TestCase):
    def test_ssh_failures_preserve_cause_worker_and_exit_status(self):
        for diagnostic, cause in (
            ("Host key verification failed.", "host-key-verification"),
            ("REMOTE HOST IDENTIFICATION HAS CHANGED!", "changed-host-key"),
            ("Permission denied (publickey).", "authentication"),
            ("Could not resolve hostname fixture.invalid", "name-resolution"),
            ("Connection refused", "connection-refused"),
            ("Connection timed out", "timeout"),
        ):

            def runner(*_args, diagnostic=diagnostic):
                return subprocess.CompletedProcess(
                    ("ssh",), 255, b"", diagnostic.encode()
                )

            with (
                self.subTest(cause=cause),
                self.assertRaises(WorkerCapabilityProbeError) as caught,
            ):
                probe_worker_capabilities(
                    worker("failed", "linux"),
                    runner=runner,
                )
            self.assertEqual(caught.exception.worker_id, "failed")
            self.assertIn("SSH exited 255", caught.exception.message)
            self.assertIn(cause, caught.exception.message)
            self.assertIn(diagnostic, caught.exception.message)

    def test_catalog_retains_healthy_observation_and_identifies_only_failed_worker(
        self,
    ):
        document = dict(
            os_name="ubuntu",
            os_version="24.04",
            architecture="x86_64",
            physical_cores=2,
            logical_cores=2,
            memory_mib=4096,
            nvidia_status="absent",
            gpus=[],
            diagnostic=None,
        )

        def runner(argv, _seconds):
            if "user@failed.example" in argv:
                return subprocess.CompletedProcess(
                    argv, 255, b"", b"Host key verification failed."
                )
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(document).encode(), b""
            )

        errors = []
        result = probe_worker_catalog(
            ExecutionWorkerCatalog(
                (worker("failed", "linux"), worker("healthy", "linux"))
            ),
            runner=runner,
            errors=errors,
        )
        self.assertEqual([item.worker_id for item in result.workers], ["healthy"])
        self.assertEqual([item.worker_id for item in errors], ["failed"])
        self.assertIn("host-key-verification", errors[0].message)

    def test_native_gpu_serializer_preserves_unknown_and_known_memory(self):
        for memory in ("[N/A]", "N/A", "8192", "query-failed"):
            with (
                self.subTest(memory=memory),
                tempfile.TemporaryDirectory() as temporary,
            ):
                line = f"0, Fixture GPU, GPU-fixture, {memory}, 8.9, 999.0"
                if os.name == "nt":
                    shell = shutil.which("powershell") or shutil.which("pwsh")
                    self.assertIsNotNone(shell)
                    script = (
                        "function Get-Command { [pscustomobject]@{Source='FakeSmi'} };"
                        f"function FakeSmi {{ '{line}' }}; "
                        f"$LASTEXITCODE={1 if memory == 'query-failed' else 0};"
                        + "$smi="
                        + _WINDOWS_SCRIPT.split("$smi=", 1)[1].split(
                            "[ordered]@{os_name", 1
                        )[0]
                    )
                    command = [
                        shell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        script + "; ConvertTo-Json -InputObject @($devices) -Compress",
                    ]
                    environment = None
                else:
                    executable = Path(temporary) / "nvidia-smi"
                    executable.write_text(
                        "#!/bin/sh\n"
                        + (
                            "exit 1\n"
                            if memory == "query-failed"
                            else f"printf '%s\\n' '{line}'\n"
                        )
                    )
                    executable.chmod(0o755)
                    script = _LINUX_SCRIPT.split("gpu_status=", 1)[1].split(
                        "\nprintf '{", 1
                    )[0]
                    command = [
                        "/bin/sh",
                        "-c",
                        "gpu_status=" + script + '\nprintf "%s" "$gpu_json"',
                    ]
                    environment = {
                        **os.environ,
                        "PATH": temporary + os.pathsep + os.defpath,
                    }
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    env=environment,
                    # Cold PowerShell startup can exceed ten seconds on busy CI.
                    timeout=30 if os.name == "nt" else 10,
                    check=True,
                )
                devices = json.loads(result.stdout)
                if memory == "query-failed":
                    self.assertEqual(devices, [])
                    continue
                device = devices[0]
                self.assertEqual(
                    device["memory_mib"], 8192 if memory == "8192" else None
                )
                observed = _device(device, vendor="nvidia")
                self.assertEqual(observed.memory_mib, device["memory_mib"])
                self.assertEqual(observed.model, "Fixture GPU")

    def test_ssh_diagnostic_is_bounded_and_redacted_before_truncation(self):
        stderr = (
            b"Permission denied; token=inline-secret; "
            b"https://user:password@proxy/ env-secret\n" + b"x" * 5000
        )
        with (
            patch.dict(os.environ, {"PROBE_TEST_TOKEN": "env-secret"}),
            self.assertRaises(WorkerCapabilityProbeError) as caught,
        ):
            probe_worker_capabilities(
                worker("denied", "linux"),
                runner=lambda _argv, _seconds: subprocess.CompletedProcess(
                    ("ssh",), 255, b"", stderr
                ),
            )
        message = caught.exception.message
        self.assertLess(len(message), 4600)
        self.assertIn("truncated", message)
        for secret in ("inline-secret", "user:password", "env-secret"):
            self.assertNotIn(secret, message)


if __name__ == "__main__":
    unittest.main()

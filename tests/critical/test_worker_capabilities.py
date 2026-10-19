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
    _FREEBSD_SCRIPT,
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
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def worker(worker_id: str, family: str) -> ExecutionWorker:
    return ExecutionWorker(
        worker_id,
        ExecutionWorkerKind.SSH,
        requirements=ExecutionRequirements(os_family=family),
        endpoint=f"user@{worker_id}.example",
        workspace="~/literate-ai",
    )


class WorkerCapabilityTests(unittest.TestCase):
    def test_freebsd_probe_preserves_native_identity_and_bounded_transport(self):
        payload = {
            "os_name": "FreeBSD",
            "os_version": "16.0-CURRENT",
            "architecture": "x86_64",
            "physical_cores": 4,
            "logical_cores": 8,
            "memory_mib": 8192,
            "nvidia_status": "not-applicable",
            "gpus": [],
            "diagnostic": "FreeBSD GPU probing is not qualified",
        }
        calls = []

        def runner(argv, seconds):
            calls.append((argv, seconds))
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(payload).encode(), b""
            )

        result = probe_worker_capabilities(
            worker("bsd", "freebsd"), runner=runner, timeout_seconds=7
        )
        SchemaCatalog().validate(result.SCHEMA, result.to_dict())
        self.assertEqual(result.os_family, "freebsd")
        self.assertEqual(result.os_version, "16.0-CURRENT")
        self.assertEqual(result.cpu_architecture, "x86_64")
        self.assertEqual(result.physical_cpu_cores, 4)
        self.assertEqual(calls[0][1], 7)
        self.assertEqual(calls[0][0][0], "ssh")

    @unittest.skipIf(os.name == "nt", "POSIX probe program")
    def test_freebsd_program_reads_named_queries_and_normalizes_architecture(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            programs = {
                "uname": (
                    '#!/bin/sh\ncase "$1" in -s) echo FreeBSD;; '
                    "-m) echo amd64;; *) exit 1;; esac\n"
                ),
                "freebsd-version": (
                    '#!/bin/sh\n[ "$1" = -u ] || exit 1\necho 16.0-CURRENT\n'
                ),
                "sysctl": (
                    '#!/bin/sh\n[ "$1" = -n ] || exit 1\ncase "$2" in '
                    "kern.smp.cores) echo 4;; hw.ncpu) echo 8;; "
                    "hw.physmem) echo 8589934592;; *) exit 1;; esac\n"
                ),
            }
            for name, source in programs.items():
                path = Path(directory) / name
                path.write_text(source)
                path.chmod(0o700)
            result = subprocess.run(
                ("sh", "-c", _FREEBSD_SCRIPT),
                capture_output=True,
                env={**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"]},
                timeout=5,
                check=True,
            )
        value = json.loads(result.stdout)
        self.assertEqual(value["architecture"], "x86_64")
        self.assertEqual(value["physical_cores"], 4)
        self.assertEqual(value["logical_cores"], 8)
        self.assertEqual(value["memory_mib"], 8192)
        self.assertEqual(value["os_version"], "16.0-CURRENT")

    @unittest.skipIf(os.name == "nt", "POSIX probe program")
    def test_freebsd_program_fails_on_wrong_os_before_reading_hardware(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            uname = Path(directory) / "uname"
            uname.write_text("#!/bin/sh\necho Linux\n")
            uname.chmod(0o700)
            result = subprocess.run(
                ("sh", "-c", _FREEBSD_SCRIPT),
                capture_output=True,
                env={**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"]},
                timeout=5,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"expected FreeBSD worker", result.stderr)

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

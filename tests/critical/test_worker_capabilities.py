from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from literate_ai.adapters.worker_capabilities import (
    _FREEBSD_SCRIPT,
    WorkerCapabilityProbeError,
    probe_worker_capabilities,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
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

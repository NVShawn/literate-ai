from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.worker_capabilities import (
    WorkerCapabilityProbeError,
    probe_worker_capabilities,
    probe_worker_catalog,
    write_worker_observations,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    GpuRequirement,
    NvidiaProbeStatus,
    WorkerHardwareObservationCatalog,
)
from tests.unit.test_schema_catalog import SchemaCatalog


def worker(worker_id: str, family: str) -> ExecutionWorker:
    return ExecutionWorker(
        worker_id,
        ExecutionWorkerKind.SSH,
        requirements=ExecutionRequirements(os_family=family),
        endpoint=f"user@{worker_id}.example",
        workspace="~/literate-ai",
    )


def result(value: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(("probe",), 0, json.dumps(value).encode(), b"")


class WorkerCapabilityTests(unittest.TestCase):
    def test_ssh_errors_preserve_distinct_causes_and_worker_identity(self):
        cases = (
            ("Host key verification failed.", "host-key-verification"),
            ("REMOTE HOST IDENTIFICATION HAS CHANGED!", "changed-host-key"),
            ("Permission denied (publickey).", "authentication"),
            ("Could not resolve hostname example: unknown host", "name-resolution"),
            (
                "connect to host example port 22: Connection refused",
                "connection-refused",
            ),
            ("Connection timed out", "timeout"),
        )
        for stderr, cause in cases:
            with (
                self.subTest(cause=cause),
                self.assertRaises(WorkerCapabilityProbeError) as caught,
            ):
                probe_worker_capabilities(
                    worker("failing-worker", "linux"),
                    runner=lambda _argv, _seconds, stderr=stderr: (
                        subprocess.CompletedProcess(("ssh",), 255, b"", stderr.encode())
                    ),
                )
            self.assertEqual(caught.exception.worker_id, "failing-worker")
            self.assertIn("SSH exited 255", caught.exception.message)
            self.assertIn(cause, caught.exception.message)
            self.assertIn(stderr, caught.exception.message)

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

    def test_catalog_failure_names_only_the_failing_worker(self):
        catalog = ExecutionWorkerCatalog(
            (worker("healthy", "linux"), worker("unreachable", "linux"))
        )

        def runner(argv, _seconds):
            if "user@unreachable.example" in argv:
                return subprocess.CompletedProcess(
                    argv, 255, b"", b"Connection refused"
                )
            return result(
                {
                    "os_name": "ubuntu",
                    "os_version": "24.04",
                    "architecture": "x86_64",
                    "physical_cores": 4,
                    "logical_cores": 8,
                    "memory_mib": 16384,
                    "nvidia_status": "absent",
                    "gpus": [],
                    "diagnostic": None,
                }
            )

        with self.assertRaises(WorkerCapabilityProbeError) as caught:
            probe_worker_catalog(catalog, runner=runner)
        self.assertEqual(caught.exception.worker_id, "unreachable")
        self.assertNotIn("healthy", caught.exception.message)

    def test_invalid_observation_names_the_worker(self):
        value = {
            "os_name": "ubuntu",
            "os_version": "24.04",
            "architecture": "x86_64",
            "physical_cores": 4,
            "logical_cores": 8,
            "memory_mib": 16384,
            "nvidia_status": "ok",
            "gpus": [
                {
                    "index": 0,
                    "model": "GPU",
                    "uuid": "GPU-1",
                    "memory_mib": 0,
                    "compute_capability": "9.0",
                    "driver_version": "580",
                }
            ],
            "diagnostic": None,
        }
        with self.assertRaises(WorkerCapabilityProbeError) as caught:
            probe_worker_capabilities(
                worker("invalid-observation", "linux"),
                runner=lambda _argv, _seconds: result(value),
            )
        self.assertEqual(caught.exception.worker_id, "invalid-observation")
        self.assertEqual(caught.exception.code, "worker.probe_invalid")
        self.assertIn("memory_mib", caught.exception.message)

    def test_linux_nvidia_observation_is_typed_and_round_trips(self) -> None:
        value = {
            "os_name": "ubuntu",
            "os_version": "24.04",
            "architecture": "x86_64",
            "physical_cores": 8,
            "logical_cores": 16,
            "memory_mib": 32092,
            "nvidia_status": "ok",
            "gpus": [
                {
                    "index": 0,
                    "model": "RTX",
                    "uuid": "GPU-1",
                    "memory_mib": 24576,
                    "compute_capability": "8.9",
                    "driver_version": "580.1",
                }
            ],
            "diagnostic": None,
        }
        observed = probe_worker_capabilities(
            worker("gpu", "linux"),
            runner=lambda _argv, _timeout: result(value),
            observed_at="2026-08-12T00:00:00Z",
        )
        self.assertEqual(observed.nvidia_status, NvidiaProbeStatus.OK)
        self.assertEqual(observed.gpus[0].compute_capability, "8.9")
        catalog = WorkerHardwareObservationCatalog((observed,))
        self.assertEqual(
            WorkerHardwareObservationCatalog.from_dict(catalog.to_dict()), catalog
        )
        schemas = SchemaCatalog()
        schemas.validate(observed.SCHEMA, observed.to_dict())
        schemas.validate(observed.gpus[0].SCHEMA, observed.gpus[0].to_dict())
        schemas.validate(catalog.SCHEMA, catalog.to_dict())

    def test_absent_and_degraded_are_distinct(self) -> None:
        base = {
            "os_name": "ubuntu",
            "os_version": "24.04",
            "architecture": "x86_64",
            "physical_cores": 4,
            "logical_cores": 4,
            "memory_mib": 8192,
            "gpus": [],
        }
        absent = probe_worker_capabilities(
            worker("absent", "linux"),
            runner=lambda _a, _t: result(
                {**base, "nvidia_status": "absent", "diagnostic": None}
            ),
            observed_at="2026-08-12T00:00:00Z",
        )
        degraded = probe_worker_capabilities(
            worker("degraded", "windows"),
            runner=lambda _a, _t: result(
                {**base, "nvidia_status": "degraded", "diagnostic": "driver failed"}
            ),
            observed_at="2026-08-12T00:00:00Z",
        )
        self.assertEqual(absent.nvidia_status, NvidiaProbeStatus.ABSENT)
        self.assertEqual(degraded.nvidia_status, NvidiaProbeStatus.DEGRADED)

    def test_requirements_match_each_device_without_ranking_or_aggregation(
        self,
    ) -> None:
        value = {
            "os_name": "ubuntu",
            "os_version": "24.04",
            "architecture": "x86_64",
            "physical_cores": 16,
            "logical_cores": 32,
            "memory_mib": 65536,
            "nvidia_status": "ok",
            "gpus": [
                {
                    "index": index,
                    "model": "RTX",
                    "uuid": f"GPU-{index}",
                    "memory_mib": 24576,
                    "compute_capability": "8.9",
                    "driver_version": "580.1",
                }
                for index in range(2)
            ],
            "diagnostic": None,
        }
        observed = probe_worker_capabilities(
            worker("multi", "linux"),
            runner=lambda _argv, _timeout: result(value),
            observed_at="2026-08-12T00:00:00Z",
        )
        accepted = ExecutionRequirements(
            os_family="linux",
            os_version="24.04",
            cpu_architecture="X86_64",
            minimum_cpu_cores=32,
            minimum_memory_mib=64000,
            gpu=GpuRequirement(
                vendor="nvidia",
                model="RTX",
                minimum_count=2,
                minimum_memory_mib=24000,
                capabilities=("cuda", "sm-89"),
            ),
        )
        self.assertTrue(observed.satisfies(accepted))
        self.assertFalse(
            observed.satisfies(
                ExecutionRequirements(
                    os_family="linux",
                    gpu=GpuRequirement(minimum_count=3, capabilities=("cuda",)),
                )
            )
        )
        self.assertFalse(
            observed.satisfies(
                ExecutionRequirements(
                    os_family="linux",
                    gpu=GpuRequirement(capabilities=("sm-90",)),
                )
            )
        )

    def test_command_worker_fails_with_typed_protocol_error(self) -> None:
        candidate = ExecutionWorker(
            "external",
            ExecutionWorkerKind.COMMAND,
            requirements=ExecutionRequirements(os_family="linux"),
            command=("dispatcher",),
        )
        with self.assertRaises(WorkerCapabilityProbeError) as raised:
            probe_worker_capabilities(candidate)
        self.assertEqual(raised.exception.code, "worker.probe_protocol_unsupported")

    def test_multiline_json_with_trailing_transport_diagnostics_is_accepted(
        self,
    ) -> None:
        value = {
            "os_name": "macos",
            "os_version": "26.6.1",
            "architecture": "arm64",
            "physical_cores": 12,
            "logical_cores": 12,
            "memory_mib": 65536,
            "nvidia_status": "not-applicable",
            "system_profiler": {
                "SPDisplaysDataType": [
                    {"sppci_model": "Apple M4 Pro", "sppci_cores": "16"}
                ]
            },
            "diagnostic": None,
        }

        def multiline(
            _argv: tuple[str, ...], _timeout: int
        ) -> subprocess.CompletedProcess[bytes]:
            output = json.dumps(value, indent=2).encode() + b"\n<Objs>progress</Objs>"
            return subprocess.CompletedProcess(("probe",), 0, output, b"")

        observed = probe_worker_capabilities(
            worker("mac", "macos"),
            runner=multiline,
            observed_at="2026-08-12T00:00:00Z",
        )
        self.assertEqual(observed.gpus[0].model, "Apple M4 Pro")

    def test_transport_timeout_and_malformed_payload_have_distinct_codes(self) -> None:
        def timeout(
            _argv: tuple[str, ...], _seconds: int
        ) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired("ssh", 1)

        with self.assertRaises(WorkerCapabilityProbeError) as timed_out:
            probe_worker_capabilities(worker("timeout", "linux"), runner=timeout)
        self.assertEqual(timed_out.exception.code, "worker.probe_transport_failed")

        malformed = subprocess.CompletedProcess(("probe",), 0, b"not-json", b"")
        with self.assertRaises(WorkerCapabilityProbeError) as invalid:
            probe_worker_capabilities(
                worker("malformed", "linux"),
                runner=lambda _argv, _seconds: malformed,
            )
        self.assertEqual(invalid.exception.code, "worker.probe_malformed")

        denied = subprocess.CompletedProcess(
            ("ssh",), 255, b"", b"Permission denied (publickey).\n"
        )
        with self.assertRaises(WorkerCapabilityProbeError) as unauthenticated:
            probe_worker_capabilities(
                worker("denied", "linux"),
                runner=lambda _argv, _seconds: denied,
            )
        self.assertEqual(
            unauthenticated.exception.code, "worker.probe_transport_failed"
        )

    def test_ssh_probe_honors_configured_transport_and_platform_shell(self) -> None:
        value = {
            "os_name": "ubuntu",
            "os_version": "24.04",
            "architecture": "x86_64",
            "physical_cores": 4,
            "logical_cores": 4,
            "memory_mib": 8192,
            "nvidia_status": "absent",
            "gpus": [],
            "diagnostic": None,
        }
        observed_argv: list[tuple[str, ...]] = []

        def observe(
            argv: tuple[str, ...], _timeout: int
        ) -> subprocess.CompletedProcess[bytes]:
            observed_argv.append(argv)
            return result(value)

        candidate = ExecutionWorker(
            "custom-transport",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(os_family="linux"),
            endpoint="user@custom-transport.example",
            workspace="~/literate-ai",
            transport="s",
        )
        probe_worker_capabilities(candidate, runner=observe)

        self.assertEqual(len(observed_argv), 1)
        self.assertEqual(observed_argv[0][0], "s")
        self.assertIn("ConnectionAttempts=1", observed_argv[0])
        self.assertEqual(observed_argv[0][-2], candidate.endpoint)
        self.assertTrue(observed_argv[0][-1].startswith("bash -lic "))

        observed_argv.clear()
        windows_value = {
            **value,
            "os_name": "Microsoft Windows 11 Pro",
            "os_version": "11",
            "architecture": "AMD64",
        }

        def observe_windows(
            argv: tuple[str, ...], _timeout: int
        ) -> subprocess.CompletedProcess[bytes]:
            observed_argv.append(argv)
            return result(windows_value)

        windows = ExecutionWorker(
            "windows-custom-transport",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(os_family="windows"),
            endpoint="user@windows-custom-transport.example",
            workspace="~/literate-ai",
            transport="s",
        )
        probe_worker_capabilities(windows, runner=observe_windows)

        self.assertEqual(observed_argv[0][0], "s")
        self.assertTrue(observed_argv[0][-1].startswith("powershell.exe "))
        self.assertNotIn("bash", observed_argv[0][-1])

    def test_windows_fixture_uses_semantic_release_for_requirement_matching(
        self,
    ) -> None:
        value = {
            "os_name": "Microsoft Windows 11 Pro",
            "os_version": "11",
            "architecture": "AMD64",
            "physical_cores": 4,
            "logical_cores": 4,
            "memory_mib": 8172,
            "nvidia_status": "absent",
            "gpus": [],
            "diagnostic": None,
        }
        observed = probe_worker_capabilities(
            worker("windows", "windows"),
            runner=lambda _argv, _timeout: result(value),
            observed_at="2026-08-12T00:00:00Z",
        )
        self.assertTrue(
            observed.satisfies(
                ExecutionRequirements(
                    os_family="windows",
                    os_version="11",
                    cpu_architecture="amd64",
                )
            )
        )

    def test_catalog_subset_is_sorted_and_atomic_write_round_trips(self) -> None:
        workers = ExecutionWorkerCatalog((worker("a", "linux"), worker("b", "linux")))
        value = {
            "os_name": "ubuntu",
            "os_version": "24.04",
            "architecture": "x86_64",
            "physical_cores": 4,
            "logical_cores": 4,
            "memory_mib": 8192,
            "nvidia_status": "absent",
            "gpus": [],
            "diagnostic": None,
        }
        observed = probe_worker_catalog(
            workers,
            worker_ids=("b",),
            runner=lambda _a, _t: result(value),
            observed_at="2026-08-12T00:00:00Z",
        )
        self.assertEqual(tuple(item.worker_id for item in observed.workers), ("b",))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.json"
            write_worker_observations(path, observed)
            self.assertEqual(
                WorkerHardwareObservationCatalog.from_dict(
                    json.loads(path.read_text())
                ),
                observed,
            )
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "symlink setup is not portable on Windows CI")
    def test_observation_writer_refuses_a_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.json"
            outside.write_text("unchanged", encoding="utf-8")
            destination = root / "observations.json"
            destination.symlink_to(outside)

            with self.assertRaises(OSError):
                write_worker_observations(
                    destination, WorkerHardwareObservationCatalog(())
                )

            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.worker_capabilities import WorkerCapabilityProbeError
from literate_ai.cli.dispatch import main
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    NvidiaProbeStatus,
    WorkerHardwareObservation,
    WorkerHardwareObservationCatalog,
    canonical_json_bytes,
)


class WorkerProbeCliTests(unittest.TestCase):
    def test_probe_error_keeps_bounded_ssh_diagnostic_in_json(self):
        message = (
            "SSH exited 255 (host-key-verification). "
            + "x" * 700
            + " Host key verification failed."
        )
        error = WorkerCapabilityProbeError(
            "worker.probe_transport_failed", message, worker_id="unreachable"
        )
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch("literate_ai.cli.worker.load_execution_worker_catalog"),
                patch("literate_ai.cli.worker.probe_worker_catalog", side_effect=error),
            ):
                status = main(
                    [
                        "worker",
                        "probe",
                        "--all",
                        "--json",
                        "--worker-config",
                        str(Path(directory) / "workers.json"),
                        "--output",
                        str(Path(directory) / "observations.json"),
                    ],
                    stdout=output,
                    stderr=output,
                )
            self.assertNotEqual(status, 0)
            observed = json.loads(output.getvalue())["error"]
            self.assertEqual(observed["message"], error.message)
            self.assertFalse((Path(directory) / "observations.json").exists())

    def test_all_reports_failed_peer_and_removes_its_stale_observation(self):
        workers = tuple(
            ExecutionWorker(
                name,
                ExecutionWorkerKind.SSH,
                requirements=ExecutionRequirements(os_family="linux"),
                endpoint=f"user@{name}.invalid",
                workspace="~/litai",
            )
            for name in ("bad", "good")
        )

        def observation(name):
            return WorkerHardwareObservation(
                name,
                "2026-08-12T00:00:00Z",
                "linux",
                "ubuntu",
                "24.04",
                "x86_64",
                4,
                8,
                16384,
                (),
                NvidiaProbeStatus.ABSENT,
            )

        def probe(worker, **kwargs):
            if worker.worker_id == "bad":
                raise WorkerCapabilityProbeError(
                    "worker.probe_transport_failed",
                    "SSH exited 255 (authentication). Permission denied.",
                    worker_id="bad",
                )
            return observation("good")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "workers.json"
            config.write_bytes(
                canonical_json_bytes(ExecutionWorkerCatalog(workers).to_dict())
            )
            output_path = root / "observations.json"
            output_path.write_bytes(
                canonical_json_bytes(
                    WorkerHardwareObservationCatalog((observation("bad"),)).to_dict()
                )
            )
            output = io.StringIO()
            with patch(
                "literate_ai.adapters.worker_capabilities.probe_worker_capabilities",
                side_effect=probe,
            ):
                status = main(
                    [
                        "worker",
                        "probe",
                        "--all",
                        "--json",
                        "--worker-config",
                        str(config),
                        "--output",
                        str(output_path),
                    ],
                    stdout=output,
                    stderr=output,
                )
            result = json.loads(output.getvalue())["result"]
            self.assertEqual(status, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["probed"], ["good"])
            self.assertEqual(result["failures"][0]["worker_id"], "bad")
            self.assertIn("Permission denied", result["failures"][0]["message"])
            persisted = json.loads(output_path.read_text())
            self.assertEqual([w["worker_id"] for w in persisted["workers"]], ["good"])

    def test_scoped_help_is_available(self) -> None:
        output = io.StringIO()
        self.assertEqual(main(["worker", "probe", "help"], stdout=output), 0)
        self.assertIn("--worker-id", output.getvalue())
        self.assertIn("--dry-run", output.getvalue())

    def test_dry_run_does_not_write_and_selected_result_is_json(self) -> None:
        worker = ExecutionWorker(
            "remote",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(os_family="linux"),
            endpoint="user@host",
            workspace="~/literate-ai",
        )
        observation = WorkerHardwareObservation(
            "remote",
            "2026-08-12T00:00:00Z",
            "linux",
            "ubuntu",
            "24.04",
            "x86_64",
            4,
            8,
            16384,
            (),
            NvidiaProbeStatus.ABSENT,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workers = root / "workers.json"
            output_path = root / "observations.json"
            workers.write_bytes(
                canonical_json_bytes(ExecutionWorkerCatalog((worker,)).to_dict())
            )
            stdout = io.StringIO()
            with patch(
                "literate_ai.cli.worker.probe_worker_catalog",
                return_value=WorkerHardwareObservationCatalog((observation,)),
            ):
                status = main(
                    [
                        "worker",
                        "probe",
                        "--worker-id",
                        "remote",
                        "--worker-config",
                        str(workers),
                        "--output",
                        str(output_path),
                        "--dry-run",
                        "--json",
                    ],
                    stdout=stdout,
                )
            self.assertEqual(status, 0)
            self.assertFalse(output_path.exists())
            self.assertEqual(
                json.loads(stdout.getvalue())["result"]["probed"], ["remote"]
            )


if __name__ == "__main__":
    unittest.main()

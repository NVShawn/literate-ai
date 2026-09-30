from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

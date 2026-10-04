"""Native pressure collection is bounded and remote absence is explicit."""

import subprocess
import sys
import unittest
from types import SimpleNamespace

from literate_ai.adapters.worker_pressure import _probe_command, observe_worker_pressure
from literate_ai.application.worker_pressure import PressurePolicy
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.worker_capacity import CapacityProbeStatus


class WorkerPressureAdapterTests(unittest.TestCase):
    def setUp(self):
        self.policy = PressurePolicy(3, 8000, 1024, 100, 512, 0)

    def test_ssh_receiver_round_trip_binds_request_and_collects_native_memory(self):
        family = {"linux": "linux", "darwin": "macos", "win32": "windows"}[sys.platform]
        bindings = SimpleNamespace(
            worker=ExecutionWorker(
                "ssh-worker",
                ExecutionWorkerKind.SSH,
                requirements=ExecutionRequirements(os_family=family),
                endpoint="user@worker.invalid",
                workspace="/worker",
            ),
            os_family=family,
            python_executable=None,
            health_command=None,
        )

        def runner(_command, *, environment, timeout_seconds, input_bytes):
            return subprocess.run(
                _probe_command(sys.executable),
                env=environment,
                input=input_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )

        samples = observe_worker_pressure(bindings, self.policy, runner=runner)

        self.assertEqual(len(samples), 3)
        self.assertEqual(
            [sample.observed_at_ms for sample in samples],
            sorted({sample.observed_at_ms for sample in samples}),
        )
        self.assertTrue(
            all(
                sample.available_memory_bytes.status is CapacityProbeStatus.MEASURED
                for sample in samples
            )
        )


if __name__ == "__main__":
    unittest.main()

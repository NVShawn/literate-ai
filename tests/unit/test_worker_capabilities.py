from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from literate_ai.adapters.worker_capabilities import (
    WorkerCapabilityProbeError,
    probe_worker_capabilities,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
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

"""Remote cleanup investigation stays bounded, bound and read-only."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.worker_cleanup import (
    _probe_command,
    investigate_worker_cleanup,
)
from literate_ai.application.worker_cleanup import CleanupRoot, CleanupScanPolicy
from literate_ai.application.worker_cleanup_execution import (
    create_remote_cleanup_proposal,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
    canonical_identity,
)


class WorkerCleanupAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remote-cleanup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        candidate = self.root / "old-cache"
        candidate.mkdir()
        (candidate / ".complete").write_text("done")
        (candidate / "payload").write_bytes(b"x" * 32)
        self.family = {
            "linux": "linux",
            "darwin": "macos",
            "win32": "windows",
        }[sys.platform]
        self.bindings = SimpleNamespace(
            worker=ExecutionWorker(
                "remote",
                ExecutionWorkerKind.SSH,
                requirements=ExecutionRequirements(os_family=self.family),
                endpoint="user@worker.invalid",
                workspace="/worker",
            ),
            os_family=self.family,
            python_executable=None,
            health_command=None,
        )
        self.policy = CleanupScanPolicy(
            (
                CleanupRoot(
                    "task-cache",
                    str(self.root),
                    "task-owned",
                    "re-download pinned artifacts",
                    (".active",),
                    (".complete",),
                    ("cleanup-tool", "{target}"),
                ),
            ),
            1000,
            100,
            4,
            1,
        )

    def test_staged_receiver_measures_selected_worker_without_exposing_paths(self):
        def runner(_command, *, environment, timeout_seconds, input_bytes):
            return subprocess.run(
                _probe_command(sys.executable),
                env=environment,
                input=input_bytes,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )

        result = investigate_worker_cleanup(self.bindings, self.policy, runner=runner)

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].active_use, "inactive")
        self.assertNotIn(str(self.root), json.dumps(result.to_dict()))
        proposal = create_remote_cleanup_proposal(
            self.policy,
            result,
            worker_id="remote",
            policy_identity=canonical_identity("policy"),
            created_at_ms=1,
        )
        self.assertEqual(
            tuple(target.candidate for target in proposal.targets),
            result.candidates,
        )


if __name__ == "__main__":
    unittest.main()

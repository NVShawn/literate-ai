"""Cleanup execution requires exact current authority and remeasurement."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.application.worker_cleanup import CleanupRoot, CleanupScanPolicy
from literate_ai.application.worker_cleanup_execution import (
    CleanupAuthorization,
    create_cleanup_proposal,
    execute_authorized_cleanup,
)
from literate_ai.contracts.identity import canonical_identity


class WorkerCleanupExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cleanup-execution-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.candidate = self.root / "completed-task"
        self.candidate.mkdir()
        (self.candidate / ".complete").write_text("done")
        (self.candidate / "payload").write_bytes(b"x" * 32)
        self.cleanup_root = CleanupRoot(
            "task-cache",
            self.root,
            "task-owned",
            "re-download pinned inputs",
            (".active",),
            (".complete",),
            ("supported-cleaner", "{target}"),
        )
        self.policy = CleanupScanPolicy((self.cleanup_root,), 1000, 100, 4, 1)
        self.policy_identity = canonical_identity({"policy": "test"})
        self.proposal = create_cleanup_proposal(
            self.policy,
            worker_id="fixture-worker",
            policy_identity=self.policy_identity,
            created_at_ms=10,
        )

    def authorization(self, **changes):
        values = {
            "worker_id": "fixture-worker",
            "policy_identity": self.policy_identity,
            "proposal_identity": self.proposal.identity,
            "proposal_created_at_ms": self.proposal.created_at_ms,
            "target_ids": tuple(
                item.candidate.candidate_id for item in self.proposal.targets
            ),
            "operation": "configured-cleanup-tool",
            "expires_at_ms": 100,
        }
        values.update(changes)
        return CleanupAuthorization(**values)

    def test_refuses_missing_expired_or_wrong_target_authorization(self):
        def measure(_alias):
            return 0

        for authorization, now in (
            (self.authorization(expires_at_ms=10), 10),
            (self.authorization(target_ids=()), 20),
            (self.authorization(worker_id="other"), 20),
        ):
            with self.subTest(authorization=authorization, now=now):
                with self.assertRaisesRegex(ValueError, "authorization_invalid"):
                    execute_authorized_cleanup(
                        self.proposal,
                        authorization,
                        now_ms=now,
                        measure_available=measure,
                    )

    def test_refuses_changed_candidate_before_tool_invocation(self):
        (self.candidate / "payload").write_bytes(b"changed")
        calls = []

        with self.assertRaisesRegex(ValueError, "candidate_changed"):
            execute_authorized_cleanup(
                self.proposal,
                self.authorization(),
                now_ms=20,
                measure_available=lambda _alias: 0,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

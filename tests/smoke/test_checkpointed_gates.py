"""Tests for the fail-fast, repair-resumable release gate runner."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.run_checkpointed_gates import run_gates


class CheckpointedGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "release.json"
        self.gates = ("fast", "expensive", "final")
        self.evidence_environment = mock.patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        self.evidence_environment.start()
        self.addCleanup(self.evidence_environment.stop)

    def test_repaired_success_requires_a_clean_release_evidence_rerun(self) -> None:
        first_calls: list[str] = []

        def fail_expensive(gate: str) -> int:
            first_calls.append(gate)
            return 7 if gate == "expensive" else 0

        self.assertEqual(
            run_gates(
                suite="release",
                gates=self.gates,
                state_path=self.state,
                runner=fail_expensive,
            ),
            7,
        )
        self.assertEqual(first_calls, ["fast", "expensive"])
        recorded = json.loads(self.state.read_text(encoding="utf-8"))
        completed = [
            step for payload in recorded["pins"].values() for step in payload["ok"]
        ]
        self.assertEqual(completed, ["fast"])

        resumed_calls: list[str] = []
        self.assertEqual(
            run_gates(
                suite="release",
                gates=self.gates,
                state_path=self.state,
                runner=lambda gate: resumed_calls.append(gate) or 0,
            ),
            2,
        )
        self.assertEqual(resumed_calls, ["expensive", "final"])
        self.assertFalse(self.state.exists())

        release_calls: list[str] = []
        self.assertEqual(
            run_gates(
                suite="release",
                gates=self.gates,
                state_path=self.state,
                runner=lambda gate: release_calls.append(gate) or 0,
            ),
            0,
        )
        self.assertEqual(release_calls, list(self.gates))

    def test_changed_gate_plan_does_not_reuse_an_incompatible_checkpoint(self) -> None:
        run_gates(
            suite="release",
            gates=self.gates,
            state_path=self.state,
            runner=lambda gate: 9 if gate == "expensive" else 0,
        )
        changed = ("new-fast", *self.gates)
        calls: list[str] = []
        run_gates(
            suite="release",
            gates=changed,
            state_path=self.state,
            runner=lambda gate: calls.append(gate) or (9 if gate == "expensive" else 0),
        )
        self.assertEqual(calls, ["new-fast", "fast", "expensive"])


if __name__ == "__main__":
    unittest.main()

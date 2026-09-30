"""Tests for the fail-fast, repair-resumable release gate runner."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.application.pinned_test_dispatch import suite_fallback_pin
from literate_ai.evidence_ledger import load_run, open_run
from literate_ai.test_checkpointing import main as checkpoint_main
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

    def test_completed_run_resets_so_next_run_starts_at_first_gate(self) -> None:
        def complete_once() -> list[str]:
            calls: list[str] = []

            def pass_gate(gate: str) -> int:
                calls.append(gate)
                return 0

            self.assertEqual(
                run_gates(
                    suite="release",
                    gates=self.gates,
                    state_path=self.state,
                    runner=pass_gate,
                ),
                0,
            )
            self.assertEqual(calls, list(self.gates))
            self.assertFalse(self.state.exists())
            return calls

        self.assertEqual(complete_once(), list(self.gates))
        self.assertEqual(complete_once(), list(self.gates))

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

    def test_cross_pin_reordering_resets_the_global_gate_prefix(self) -> None:
        tcb = suite_fallback_pin("tcb", ("tcb-first", "tcb-final"))
        docs = suite_fallback_pin("docs", ("docs-second",))
        catalog = {
            "tcb-first": tcb,
            "docs-second": docs,
            "tcb-final": tcb,
        }
        run_gates(
            suite="release",
            gates=("tcb-first", "docs-second", "tcb-final"),
            state_path=self.state,
            runner=lambda gate: 9 if gate == "tcb-final" else 0,
            gate_pins=catalog,
        )

        calls: list[str] = []
        run_gates(
            suite="release",
            gates=("docs-second", "tcb-first", "tcb-final"),
            state_path=self.state,
            runner=lambda gate: calls.append(gate) or (9 if gate == "tcb-final" else 0),
            gate_pins=catalog,
        )

        self.assertEqual(calls, ["docs-second", "tcb-first", "tcb-final"])

    def test_resume_records_skipped_node_linked_to_original_gate(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        run = open_run(project, operation="release.check")
        self.assertIsNotNone(run)
        assert run is not None

        self.assertEqual(
            run_gates(
                suite="release-check",
                gates=("fast", "expensive"),
                state_path=self.state,
                runner=lambda gate: 7 if gate == "expensive" else 0,
                evidence_run=run,
            ),
            7,
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        original_id = state["nodes"]["fast"]
        self.assertEqual(
            run_gates(
                suite="release-check",
                gates=("fast", "expensive"),
                state_path=self.state,
                runner=lambda _gate: 0,
                evidence_run=run,
            ),
            2,
        )
        nodes = run.reduced()["nodes"]
        resumed = [
            node
            for node in nodes
            if node.get("path") == "release/gate/release-check/fast"
            and node.get("state") == "skipped"
        ]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]["pins"]["evidence_node_id"], original_id)

    def test_standalone_gate_entrypoint_opens_owned_evidence_run(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        state = project / "release.json"
        completed = mock.Mock(returncode=0)

        with (
            mock.patch("literate_ai.test_checkpointing.Path.cwd", return_value=project),
            mock.patch.dict(
                os.environ,
                {
                    "LITAI_EVIDENCE_RUN": "",
                    "LITAI_EVIDENCE_PARENT": "",
                },
                clear=False,
            ),
            mock.patch(
                "literate_ai.test_checkpointing.record_subprocess",
                return_value=completed,
            ),
        ):
            self.assertEqual(
                checkpoint_main(
                    [
                        "gates",
                        "--state",
                        str(state),
                        "--suite",
                        "release-check",
                        "fast",
                    ]
                ),
                0,
            )

        evidence_root = project / "_build" / "evidence"
        run_id = (evidence_root / "latest").resolve().name
        run = load_run(project, run_id)
        self.assertIsNotNone(run)
        assert run is not None
        report = run.reduced()
        outer = next(node for node in report["nodes"] if node["path"] == "release")
        gate = next(
            node
            for node in report["nodes"]
            if node["path"] == "release/gate/release-check/fast"
        )
        self.assertEqual(gate["parent_id"], outer["node_id"])
        self.assertEqual(gate["state"], "passed")

    def test_standalone_gate_failure_marks_outer_evidence_failed(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        state = project / "release.json"
        completed = mock.Mock(returncode=7)

        with (
            mock.patch("literate_ai.test_checkpointing.Path.cwd", return_value=project),
            mock.patch.dict(
                os.environ,
                {
                    "OBJ_DIR": "",
                    "LITAI_EVIDENCE_RUN": "",
                    "LITAI_EVIDENCE_PARENT": "",
                },
                clear=False,
            ),
            mock.patch(
                "literate_ai.test_checkpointing.record_subprocess",
                return_value=completed,
            ),
        ):
            self.assertEqual(
                checkpoint_main(
                    [
                        "gates",
                        "--state",
                        str(state),
                        "--suite",
                        "release-check",
                        "python-check",
                    ]
                ),
                7,
            )

        evidence_root = project / "_build" / "evidence"
        run_id = (evidence_root / "latest").resolve().name
        run = load_run(project, run_id)
        self.assertIsNotNone(run)
        assert run is not None
        outer = next(
            node for node in run.reduced()["nodes"] if node["path"] == "release"
        )
        self.assertEqual(outer["state"], "failed")
        self.assertIn("status 7", json.dumps(outer))

    def test_inherited_evidence_run_remains_open_after_gate(self) -> None:
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        ambient = open_run(project, operation="release")
        self.assertIsNotNone(ambient)
        assert ambient is not None
        completed = mock.Mock(returncode=0)

        with ambient.node("release-parent", operation="release") as parent:
            with (
                mock.patch(
                    "literate_ai.test_checkpointing.Path.cwd",
                    return_value=project,
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "LITAI_EVIDENCE_RUN": str(ambient.root),
                        "LITAI_EVIDENCE_PARENT": parent.node_id,
                    },
                    clear=False,
                ),
                mock.patch(
                    "literate_ai.test_checkpointing.record_subprocess",
                    return_value=completed,
                ),
            ):
                self.assertEqual(
                    checkpoint_main(
                        [
                            "gates",
                            "--state",
                            str(self.state),
                            "--suite",
                            "release-check",
                            "fast",
                        ]
                    ),
                    0,
                )

        report = ambient.reduced()
        self.assertNotIn("closed_at", report)
        gate = next(
            node
            for node in report["nodes"]
            if node["path"] == "release/gate/release-check/fast"
        )
        self.assertEqual(gate["state"], "passed")


if __name__ == "__main__":
    unittest.main()

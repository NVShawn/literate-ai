"""Actual TEST records enter controller custody without local TEST execution."""

import shutil
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from tests.support import fixtures_test_standard_transferred_build as build_fixture
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


class StandardTransferredTestTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = build_fixture.StandardTransferredBuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        fixture.receiver.retain_evidence_with(recorder)
        fixture.admit()
        self.evidence = fixture.receiver.test(fixture.plan, fixture.output.exports)
        # Include the original BUILD records plus actual TEST process/case records.
        self.records = tuple(
            sorted(
                dict((*fixture.recorder.entries, *recorder.entries)).items(),
                key=lambda x: x[0].uri,
            )
        )
        self.controller = controller = LocalStandardLifecyclePorts(
            source_trees=fixture.receiver.source_trees,
            object_root=fixture.root / "controller",
            contracts=tuple(fixture.receiver.contracts.values()),
            tool_bindings=(),
            command_phases=(),
        )
        controller.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        )
        _, execution = _fixture()
        candidate = controller.source_trees.evidence(
            fixture.plan.request.source_tree_identity
        ).candidate
        intent = controller.create(
            execution, execution.generation_plans[0], candidate, (), ()
        )
        controller.accept_finalized_plan(
            intent, fixture.inputs.authorization, fixture.plan
        )
        artifact = controller.object_root / "received"
        shutil.copytree(fixture.artifact, artifact)
        controller.admit_transferred_build(
            plan=fixture.plan,
            inputs=fixture.inputs,
            evidence=fixture.output.evidence,
            evidence_reader=QualificationEvidenceReader(
                self.records, max_bytes=5_000_000, max_records=1000
            ),
            artifact=artifact,
        )

    def admit(self, **changes):
        arguments = dict(
            plan=self.fixture.plan,
            exports=self.fixture.output.exports,
            evidence=self.evidence,
            records=self.records,
            admission_guard=lambda: None,
        )
        arguments.update(changes)
        return self.controller.admit_transferred_tests(**arguments)

    def test_actual_process_evidence_is_retained_without_controller_commands(self):
        with patch.object(
            self.controller,
            "_run_locked",
            side_effect=AssertionError("controller command"),
        ):
            self.assertEqual(self.admit(), self.evidence)
        self.assertEqual(
            self.controller._test_evidence[self.evidence.identity.uri], self.evidence
        )
        self.assertTrue(
            set(self.records).issubset(set(self.controller.retained_evidence_records()))
        )

    def test_missing_process_record_refuses_before_registration(self):
        case = self.evidence.cases[0]
        records = tuple(
            item for item in self.records if item[0] != case.observation_identity
        )
        with self.assertRaises(QualificationCaptureError):
            self.admit(records=records)
        self.assertEqual(self.controller._test_evidence, {})

    def test_self_consistent_changed_runner_or_custody_refuses(self):
        for field in (
            "runner_identity",
            "test_custody_identity",
            "generated_test_suite_identity",
        ):
            with self.subTest(field=field):
                evidence = replace(
                    self.evidence, **{field: canonical_identity("substituted")}
                )
                records = dict(self.records)
                records[evidence.identity] = canonical_json_bytes(evidence.to_dict())
                with self.assertRaises((ValueError, QualificationCaptureError)):
                    self.admit(
                        evidence=evidence,
                        records=tuple(sorted(records.items(), key=lambda x: x[0].uri)),
                    )
                self.assertEqual(self.controller._test_evidence, {})

    def test_retention_or_final_guard_failure_leaves_no_test_registration(self):
        for mode in ("retention", "guard"):
            with self.subTest(mode=mode):
                state = {"retained": False}
                original = self.controller.retain_evidence_record

                def retain(
                    identity, content, mode=mode, original=original, state=state
                ):
                    if mode == "retention":
                        raise RuntimeError("retention failed")
                    original(identity, content)
                    state["retained"] = True

                def guard(state=state):
                    if state["retained"]:
                        raise RuntimeError("worker changed")

                with patch.object(
                    self.controller, "retain_evidence_record", side_effect=retain
                ):
                    with self.assertRaises(RuntimeError):
                        self.admit(admission_guard=guard)
                self.assertEqual(self.controller._test_evidence, {})

    def test_unregistered_plan_refuses(self):
        self.controller._plans_by_revision.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            self.admit()
        self.assertEqual(self.controller._test_evidence, {})

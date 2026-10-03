"""ACCEPT receipts require exact current stages and complete reopened proof."""

import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from tests.unit import test_standard_transferred_execution as execution_fixture
from tests.unit.test_component_node_generation_preparation import _fixture


class TransferredAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = execution_fixture.StandardTransferredExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.fixture.admit()
        f.admit()
        self.controller = f.controller
        self.plan = f.build.plan
        self.evidence = f.build.receiver.accept(
            self.plan,
            f.fixture.evidence.identity,
            f.evidence.identity,
        )
        f.refresh_records()
        self.records = f.records
        self.generation = next(
            item
            for item in _fixture()[1].generation_plans
            if item.component_revision == self.plan.component_revision
        )

    def admit(self, **changes):
        return self.controller.admit_transferred_acceptance(
            **(
                dict(
                    plan=self.plan,
                    test_identity=self.evidence.generated_tests.identity,
                    execution_identity=self.evidence.execution.identity,
                    generation_plan=self.generation,
                    evidence=self.evidence,
                    records=self.records,
                    admission_guard=lambda: None,
                )
                | changes
            )
        )

    def test_actual_receipt_admitted_without_commands_or_local_acceptance(self):
        with (
            patch.object(
                self.controller, "_run_locked", side_effect=AssertionError("command")
            ),
            patch.object(
                self.controller, "accept", side_effect=AssertionError("local fallback")
            ),
        ):
            self.assertEqual(self.admit(), self.evidence)
        retained = dict(self.controller.retained_evidence_records())
        self.assertEqual(
            retained[self.evidence.identity],
            canonical_json_bytes(self.evidence.to_dict()),
        )
        self.assertEqual(self.controller.tool_bindings, {})

    def test_missing_acceptance_policy_source_or_process_record_refuses(self):
        for identity in (
            self.evidence.identity,
            self.evidence.acceptance_policy_identity,
            self.evidence.build.source_custody_identity,
            self.evidence.execution.stdout_identity,
            self.evidence.generated_tests.cases[0].observation_identity,
        ):
            with (
                self.subTest(identity=identity),
                self.assertRaises(QualificationCaptureError),
            ):
                self.admit(
                    records=tuple(item for item in self.records if item[0] != identity)
                )

    def test_self_consistent_foreign_policy_cannot_replace_standard_policy(self):
        policy = {"schema": "foreign-policy", "requires": []}
        identity = canonical_identity(policy)
        evidence = replace(self.evidence, acceptance_policy_identity=identity)
        records = dict(self.records)
        records[identity] = canonical_json_bytes(policy)
        records[evidence.identity] = canonical_json_bytes(evidence.to_dict())
        with self.assertRaises(ActionWireError):
            self.admit(
                evidence=evidence,
                records=tuple(sorted(records.items(), key=lambda item: item[0].uri)),
            )

    def test_missing_registered_stage_or_plan_refuses(self):
        for mapping in (
            self.controller._plans_by_revision,
            self.controller._build_evidence,
            self.controller._test_evidence,
            self.controller._execution_evidence,
        ):
            with self.subTest(mapping=id(mapping)), patch.dict(mapping, {}, clear=True):
                with self.assertRaises(LocalStandardLifecycleError):
                    self.admit()

    def test_changed_stage_during_retention_refuses(self):
        original = self.controller.retain_evidence_record

        def retain(identity, content):
            original(identity, content)
            self.controller._test_evidence.clear()

        with patch.object(
            self.controller, "retain_evidence_record", side_effect=retain
        ):
            with self.assertRaises(LocalStandardLifecycleError):
                self.admit()

    def test_changed_worker_during_retention_refuses(self):
        original = self.controller.retain_evidence_record
        state = {"changed": False}

        def retain(identity, content):
            original(identity, content)
            state["changed"] = True

        def guard():
            if state["changed"]:
                raise RuntimeError("worker changed")

        with patch.object(
            self.controller, "retain_evidence_record", side_effect=retain
        ):
            with self.assertRaisesRegex(RuntimeError, "worker changed"):
                self.admit(admission_guard=guard)

    def test_artifact_mutation_during_retention_refuses(self):
        original = self.controller.retain_evidence_record
        artifact = self.controller.artifact_path(self.evidence.build.exports[0])

        def retain(identity, content):
            original(identity, content)
            artifact.chmod(0o600)
            artifact.write_bytes(b"changed artifact")

        with patch.object(
            self.controller, "retain_evidence_record", side_effect=retain
        ):
            with self.assertRaises((ValueError, RuntimeError)):
                self.admit()

    def test_receipt_cannot_replace_a_different_successful_execution(self):
        f = self.fixture
        scope = f.scoped()
        execution = f.evidence
        self.controller.admit_transferred_execution(
            plan=self.plan,
            exports=self.evidence.build.exports,
            evidence=execution,
            records=f.records,
            admission_guard=lambda: None,
            scope=scope,
            provider_artifacts=(),
        )
        self.assertNotEqual(execution.identity, self.evidence.execution.identity)
        with self.assertRaises(LocalStandardLifecycleError):
            self.admit(execution_identity=execution.identity, records=f.records)

    def test_foreign_generation_plan_refuses(self):
        generation = next(
            item
            for item in _fixture()[1].generation_plans
            if item.component_revision != self.plan.component_revision
        )
        with self.assertRaises(ActionWireError):
            self.admit(generation_plan=generation)

    def test_retention_failure_refuses(self):
        with patch.object(
            self.controller,
            "retain_evidence_record",
            side_effect=RuntimeError("storage unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
                self.admit()

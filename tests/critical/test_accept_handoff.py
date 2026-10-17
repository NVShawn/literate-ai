"""Completed-stage handoffs reopen proof and remain stable under unrelated retention."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.fixtures_test_action_accept_execution as fixture_module
from literate_ai.adapters.accept_handoff import CompletedStagesAcceptHandoff
from literate_ai.adapters.action_accept_result_record import AcceptWorkerResult
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.storage import FileSystemCAS


class AcceptHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.AcceptWorkerExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.result = r = f.fixture
        self.ports = r.ports
        self.plan = r.value.execution_input.build_input.plan
        self.indexer = SimpleNamespace(
            deadline=r.worker.deadline,
            execution_plan=r.value.execution_input.build_input.execution_plan,
            cas=FileSystemCAS(f.root / "handoff-cas"),
        )
        self.handoff = CompletedStagesAcceptHandoff(
            self.indexer,
            self.ports,
            execution_input_for=lambda *args: r.value.execution_input,
        )

        value = r.value.execution_input
        self.handoff.retain_execution_provider_evidence(
            self.plan, value.scope, value.accepted_providers
        )

    def capture(self):
        return self.handoff(
            self.plan,
            self.result.evidence.generated_tests.identity,
            self.result.evidence.execution.identity,
        )

    def test_captured_proof_runs_actual_accept_without_local_acceptance(self):
        with patch.object(
            self.ports, "accept", side_effect=AssertionError("local accept")
        ):
            value = self.capture()
        content = value.to_bytes()
        result = self.fixture.run_worker(
            input_record=content,
            input_identity=record_identity(content),
            cas=self.indexer.cas,
        )
        admitted = AcceptWorkerResult.admit(
            result,
            record_identity(result),
            input_record=content,
            input_identity=record_identity(content),
            deadline=self.indexer.deadline,
        )
        self.assertEqual(admitted.evidence, self.result.evidence)

    def test_unrelated_retained_records_do_not_change_handoff(self):
        first = self.capture()
        raw = b"unrelated retained dispatch record"
        identity = record_identity(raw)
        self.ports.retain_evidence_record(identity, raw)
        second = self.capture()
        self.assertEqual(first, second)
        self.assertNotIn(
            identity.uri,
            {
                ref.identity
                for stage in (second.test_result, second.execution_result)
                for ref in stage.evidence_records
            },
        )

    def test_missing_process_proof_refuses(self):
        missing = self.result.evidence.generated_tests.cases[0].observation_identity
        records = tuple(
            item
            for item in self.ports.retained_evidence_records()
            if item[0] != missing
        )
        with patch.object(
            self.ports, "retained_evidence_records", return_value=records
        ):
            with self.assertRaises(QualificationCaptureError):
                self.capture()

    def test_stage_loss_during_cas_capture_refuses(self):
        original = self.indexer.cas.put_bytes

        def put(content, **kwargs):
            ref = original(content, **kwargs)
            self.ports._execution_evidence.clear()
            return ref

        with patch.object(self.indexer.cas, "put_bytes", side_effect=put):
            with self.assertRaises(LocalStandardLifecycleError):
                self.capture()

    def test_changed_input_during_capture_refuses(self):
        original = self.indexer.cas.put_bytes

        def put(content, **kwargs):
            ref = original(content, **kwargs)
            self.handoff.execution_input_for = lambda *args: None
            return ref

        with patch.object(self.indexer.cas, "put_bytes", side_effect=put):
            with self.assertRaises(TypeError):
                self.capture()

    def test_missing_receipts_refuse_before_capture(self):
        self.handoff._receipts.clear()
        with self.assertRaises(ActionWireError) as error:
            self.capture()
        self.assertEqual(error.exception.code, "action_accept.receipts_missing")

    def test_untyped_receipts_refuse(self):
        value = self.result.value.execution_input
        with self.assertRaises(ActionWireError):
            self.handoff.retain_execution_provider_evidence(
                self.plan, value.scope, list(value.accepted_providers)
            )

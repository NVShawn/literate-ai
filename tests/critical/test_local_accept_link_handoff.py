"""Local acceptance must retain and reopen complete proof before remote LINK."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.fixtures_test_action_accept_result as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.local_accept_handoff import LocalAcceptanceLinkHandoff


class LocalAcceptHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.AcceptWorkerResultTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.transfer()
        self.plan = f.value.execution_input.build_input.plan
        self.receipt = f.evidence
        self.port = LocalAcceptanceLinkHandoff(
            f.ports,
            SimpleNamespace(
                cas=f.cas,
                deadline=f.worker.deadline,
                execution_plan=f.value.execution_input.build_input.execution_plan,
            ),
            f.ports,
            handoff_for=lambda *args: f.value,
        )

    def accept(self):
        return self.port.accept(
            self.plan,
            self.receipt.generated_tests.identity,
            self.receipt.execution.identity,
        )

    def test_local_acceptance_reopens_exact_proof_and_stores_selected_records(self):
        self.assertEqual(self.accept(), self.receipt)
        value, result = self.port.link_handoff(self.plan, self.receipt)
        self.assertEqual(value, self.fixture.value)
        self.assertEqual(result.evidence, self.receipt)
        self.assertTrue(result.evidence_records)
        for ref in result.evidence_records:
            self.fixture.cas.verify(ref)

    def test_receipt_not_completed_by_this_acceptor_is_refused(self):
        with self.assertRaises(ActionWireError):
            self.port.link_handoff(self.plan, self.receipt)

    def test_changed_stage_custody_is_refused_after_acceptance(self):
        self.accept()
        with patch.object(
            self.fixture.ports, "acceptance_stage_evidence", return_value=()
        ):
            with self.assertRaises(ActionWireError):
                self.port.link_handoff(self.plan, self.receipt)

    def test_missing_proof_is_not_reconstructed_from_receipt(self):
        self.accept()
        with patch.object(
            self.fixture.ports, "retained_evidence_records", return_value=()
        ):
            with self.assertRaises(ValueError):
                self.port.link_handoff(self.plan, self.receipt)

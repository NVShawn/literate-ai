"""Command ACCEPT transport verifies returned proof using shared admitted capacity."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.fixtures_test_action_accept_result as result_fixture
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_acceptor import CommandComponentAcceptor
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)


class CommandAcceptorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = result_fixture.AcceptWorkerResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.value = f.value
        self.ports = f.ports
        self.plan = self.value.execution_input.build_input.plan
        self.test = self.value.test_result.evidence.identity
        self.executed = self.value.execution_result.evidence.identity
        root = f.worker.fixture.root
        self.marker = root / "test-dispatched"
        input_path, result_path = root / "test-input.json", root / "test-result.json"
        input_path.write_bytes(f.raw)
        result_path.write_bytes(f.result.to_bytes())
        # The child returns real previously produced ACCEPT proof for this exact input.
        # Configured receiver tests separately exercise the actual ACCEPT child.
        code = f"""
import sys
from pathlib import Path
from literate_ai.adapters.action_dispatch_wire import (
 decode_action_request,encode_action_response,MAX_ACTION_WIRE_BYTES,
)
from literate_ai.adapters.action_accept import admit_accept_action
request,deadline,records=decode_action_request(sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES+1))
admit_accept_action(request,deadline,records,expected_worker_identity=request.worker.worker_identity)
expected = Path({str(input_path)!r}).read_bytes()
assert records[request.predecessor_result_identities[0]] == expected
Path({str(self.marker)!r}).touch()
sys.stdout.buffer.write(encode_action_response(request,result_record=Path({str(result_path)!r}).read_bytes()))
"""
        self.worker = ExecutionWorker(
            "tester",
            ExecutionWorkerKind.COMMAND,
            command=(sys.executable, "-I", "-c", code),
        )
        self.catalog = ExecutionWorkerCatalog((self.worker,))
        self.admitted = LifecycleActionWorker(
            self.worker.worker_id,
            self.worker.identity,
            self.catalog.identity,
            canonical_identity("hardware"),
        )
        self.healthy = True
        self.tools_available = True

        def revalidate(worker):
            self.assertEqual(worker, self.admitted)
            if not self.healthy:
                raise ActionWireError("fixture.changed", "worker changed")

        self.indexer = CommandGenerationIndexer(
            self.value.execution_input.build_input.execution_plan,
            self.ports.source_trees,
            lambda source: self.ports.source_trees.evidence(source).candidate,
            f.cas,
            self.catalog,
            (self.admitted,),
            f.worker.deadline,
            cwd=root,
            revalidate_worker=revalidate,
            environment=dict(os.environ),
        )
        self.admission = SimpleNamespace(
            catalog=self.catalog,
            workers=(self.admitted,),
            identity=canonical_identity("admission"),
            supports_phase=lambda worker, phase: (
                self.tools_available
                and worker == self.admitted
                and phase is LifecycleActionKind.ACCEPT
            ),
            supports_test=lambda worker, tools: (
                self.tools_available and worker == self.admitted
            ),
        )
        self.acceptor = CommandComponentAcceptor(
            self.indexer,
            self.admission,
            self.ports,
            handoff_for=lambda plan, test, execution: self.value,
            result_source=self.fetch,
        )

    def fetch(self, worker, reference):
        self.assertEqual(worker, self.worker)
        return self.fixture.worker.cas.get_bytes(reference)

    def assert_released(self):
        reservation = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reservation)
        reservation.release()

    def run_accept(self):
        return self.acceptor.accept(self.plan, self.test, self.executed)

    def reserve(self):
        return self.acceptor.try_reserve_accept(self.plan, self.test, self.executed)

    def test_actual_transport_returns_verified_receipt_without_local_accept(self):
        with patch.object(
            self.ports, "accept", side_effect=AssertionError("local fallback")
        ):
            self.assertEqual(self.run_accept(), self.fixture.evidence)
        self.assertTrue(self.marker.exists())
        self.assert_released()

    def test_shared_capacity_is_reserved_before_dispatch(self):
        occupied = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(self.reserve())
        self.assertFalse(self.marker.exists())
        occupied.release()
        reservation = self.reserve()
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.run(), self.fixture.evidence)
        self.assert_released()

    def test_changed_worker_after_reservation_refuses_and_releases(self):
        reservation = self.reserve()
        self.healthy = False
        with self.assertRaises(ActionWireError):
            reservation.run()
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_missing_phase_refuses_before_reservation(self):
        self.tools_available = False
        with self.assertRaises(ActionWireError):
            self.reserve()
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_each_missing_registered_stage_refuses_before_dispatch(self):
        for mapping in (
            self.ports._build_evidence,
            self.ports._test_evidence,
            self.ports._execution_evidence,
        ):
            with self.subTest(mapping=id(mapping)), patch.dict(mapping, {}, clear=True):
                with self.assertRaises(LocalStandardLifecycleError):
                    self.run_accept()
            self.assertFalse(self.marker.exists())
            self.assert_released()

    def test_corrupt_return_proof_refuses_and_releases(self):
        self.acceptor.result_source = lambda worker, reference: b"corrupt"
        with self.assertRaises(ActionWireError):
            self.run_accept()
        self.assertTrue(self.marker.exists())
        self.assert_released()

    def test_stage_removed_after_reservation_refuses_before_dispatch(self):
        reservation = self.reserve()
        self.ports._test_evidence.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            reservation.run()
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_link_handoff_requires_successful_import_and_rechecks_stage_custody(self):
        with self.assertRaises(ActionWireError):
            self.acceptor.link_handoff(self.plan, self.fixture.evidence)
        receipt = self.run_accept()
        value, result = self.acceptor.link_handoff(self.plan, receipt)
        self.assertEqual(value, self.value)
        self.assertEqual(result, self.fixture.result)
        self.ports._test_evidence.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            self.acceptor.link_handoff(self.plan, receipt)

    def test_failed_import_exposes_no_link_handoff(self):
        self.acceptor.result_source = lambda worker, reference: b"corrupt"
        with self.assertRaises(ActionWireError):
            self.run_accept()
        with self.assertRaises(ActionWireError) as error:
            self.acceptor.link_handoff(self.plan, self.fixture.evidence)
        self.assertEqual(error.exception.code, "action_accept.link_unavailable")

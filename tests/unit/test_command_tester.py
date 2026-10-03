"""Command TEST transport verifies returned proof using shared admitted capacity."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_indexer import CommandGenerationIndexer
from literate_ai.adapters.command_tester import CommandComponentTester
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
from tests.unit import test_action_test_result as result_fixture


class CommandTesterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = result_fixture.ActionTestResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.value = f.fixture.value
        self.ports = f.ports
        self.plan = self.value.build_input.plan
        self.exports = self.value.build_result.evidence.exports
        root = f.fixture.fixture.root
        self.marker = root / "test-dispatched"
        input_path, result_path = root / "test-input.json", root / "test-result.json"
        input_path.write_bytes(f.fixture.content)
        result_path.write_bytes(f.content)
        # The child returns real previously produced TEST proof for this exact input.
        # Configured receiver tests separately exercise the actual TEST child.
        code = f"""
import sys
from pathlib import Path
from literate_ai.adapters.action_dispatch_wire import (
 decode_action_request,encode_action_response,MAX_ACTION_WIRE_BYTES,
)
from literate_ai.adapters.action_test import admit_test_action
request,deadline,records=decode_action_request(sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES+1))
admit_test_action(request,deadline,records,expected_worker_identity=request.worker.worker_identity)
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
            self.value.build_input.execution_plan,
            self.ports.source_trees,
            lambda source: self.ports.source_trees.evidence(source).candidate,
            f.cas,
            self.catalog,
            (self.admitted,),
            f.fixture.deadline,
            cwd=root,
            revalidate_worker=revalidate,
            environment=dict(os.environ),
        )
        self.admission = SimpleNamespace(
            catalog=self.catalog,
            workers=(self.admitted,),
            identity=canonical_identity("admission"),
            supports_phase=lambda worker, phase: (
                worker == self.admitted and phase is LifecycleActionKind.TEST
            ),
            supports_test=lambda worker, tools: (
                self.tools_available and worker == self.admitted
            ),
        )
        self.tester = CommandComponentTester(
            self.indexer,
            self.admission,
            self.ports,
            handoff_for=lambda plan, exports: self.value,
            result_source=self.fetch,
        )

    def fetch(self, worker, reference):
        self.assertEqual(worker, self.worker)
        return self.fixture.fixture.cas.get_bytes(reference)

    def assert_released(self):
        reservation = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reservation)
        reservation.release()

    def test_actual_transport_imports_verified_test_without_local_commands(self):
        with patch.object(
            self.ports, "test", side_effect=AssertionError("local TEST fallback")
        ):
            result = self.tester.test(self.plan, self.exports)
        self.assertTrue(self.marker.exists())
        self.assertEqual(result.passed_count, 3)
        self.assertEqual(self.ports._test_evidence[result.identity.uri], result)
        self.assert_released()

    def test_shared_index_capacity_reserves_before_execution(self):
        occupied = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(self.tester.try_reserve_test(self.plan, self.exports))
        self.assertFalse(self.marker.exists())
        occupied.release()
        reservation = self.tester.try_reserve_test(self.plan, self.exports)
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.run().passed_count, 3)
        self.assert_released()

    def test_missing_runner_refuses_before_reservation_and_dispatch(self):
        self.tools_available = False
        with self.assertRaises(ActionWireError) as error:
            self.tester.try_reserve_test(self.plan, self.exports)
        self.assertEqual(error.exception.code, "action_test.tools_unavailable")
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_changed_worker_after_reservation_refuses_and_releases(self):
        reservation = self.tester.try_reserve_test(self.plan, self.exports)
        self.healthy = False
        with self.assertRaises(ActionWireError):
            reservation.run()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.ports._test_evidence, {})
        self.assert_released()

    def test_wrong_exports_or_handoff_refuse_before_dispatch(self):
        for changes in ({"exports": ()}, {"handoff": None}):
            with self.subTest(changes=changes):
                original = self.tester.handoff_for
                try:
                    if "handoff" in changes:
                        self.tester.handoff_for = lambda plan, exports: None
                    with self.assertRaises(ActionWireError):
                        self.tester.test(
                            self.plan, changes.get("exports", self.exports)
                        )
                finally:
                    self.tester.handoff_for = original
                self.assertFalse(self.marker.exists())
                self.assert_released()

    def test_missing_registered_build_refuses_before_dispatch(self):
        self.ports._build_evidence.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            self.tester.test(self.plan, self.exports)
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_corrupt_return_records_leave_test_unregistered_and_release(self):
        self.tester.result_source = lambda worker, reference: b"corrupt"
        with self.assertRaises(ActionWireError):
            self.tester.test(self.plan, self.exports)
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.ports._test_evidence, {})
        self.assert_released()

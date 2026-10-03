"""Command EXECUTE transport verifies returned proof using shared admitted capacity."""

import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.command_executor import CommandComponentExecutor
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
from tests.unit import test_action_execute_result as result_fixture


class CommandExecutorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = result_fixture.ActionExecuteResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.value = f.fixture.value
        self.ports = f.ports
        self.scope = self.value.scope
        self.providers = self.value.provider_artifacts
        self.plan = self.value.build_input.plan
        self.exports = self.value.build_result.evidence.exports
        root = f.fixture.fixture.root
        self.marker = root / "test-dispatched"
        input_path, result_path = root / "test-input.json", root / "test-result.json"
        input_path.write_bytes(f.fixture.content)
        result_path.write_bytes(f.content)
        # The child returns real previously produced EXECUTE proof for this exact input.
        # Configured receiver tests separately exercise the actual EXECUTE child.
        code = f"""
import sys
from pathlib import Path
from literate_ai.adapters.action_dispatch_wire import (
 decode_action_request,encode_action_response,MAX_ACTION_WIRE_BYTES,
)
from literate_ai.adapters.action_execute import admit_execute_action
request,deadline,records=decode_action_request(sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES+1))
admit_execute_action(request,deadline,records,expected_worker_identity=request.worker.worker_identity)
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
                worker == self.admitted and phase is LifecycleActionKind.EXECUTE
            ),
            supports_execute=lambda worker, tools: (
                self.tools_available and worker == self.admitted
            ),
        )
        self.executor = CommandComponentExecutor(
            self.indexer,
            self.admission,
            self.ports,
            handoff_for=lambda *args: self.value,
            result_source=self.fetch,
        )
        self.executor.retain_execution_provider_evidence(
            self.plan, self.scope, self.value.accepted_providers
        )

    def fetch(self, worker, reference):
        self.assertEqual(worker, self.worker)
        return self.fixture.fixture.cas.get_bytes(reference)

    def assert_released(self):
        reservation = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNotNone(reservation)
        reservation.release()

    def test_actual_transport_imports_verified_execution_without_local_commands(self):
        with patch.object(
            self.ports,
            "_run_locked",
            side_effect=AssertionError("local EXECUTE fallback"),
        ):
            result = self.executor.execute_scoped(
                self.plan, self.exports, self.scope, self.providers
            )
        self.assertTrue(self.marker.exists())
        self.assertEqual(result.execution_authority.input_scope, self.scope)
        self.assertEqual(
            self.ports.execution_stdout, self.fixture.fixture.ports.execution_stdout
        )
        self.assertEqual(self.ports._execution_evidence[result.identity.uri], result)
        self.assert_released()

    def test_shared_index_capacity_reserves_before_execution(self):
        occupied = self.indexer.slots.try_reserve(lambda worker, slot: None)
        self.assertIsNone(
            self.executor.try_reserve_execute(
                self.plan, self.exports, self.scope, self.providers
            )
        )
        self.assertFalse(self.marker.exists())
        occupied.release()
        reservation = self.executor.try_reserve_execute(
            self.plan, self.exports, self.scope, self.providers
        )
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.run().execution_authority.input_scope, self.scope)
        self.assert_released()

    def test_missing_runtime_refuses_before_reservation_and_dispatch(self):
        self.tools_available = False
        with self.assertRaises(ActionWireError) as error:
            self.executor.try_reserve_execute(
                self.plan, self.exports, self.scope, self.providers
            )
        self.assertEqual(error.exception.code, "action_execute.tools_unavailable")
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_changed_worker_after_reservation_refuses_and_releases(self):
        reservation = self.executor.try_reserve_execute(
            self.plan, self.exports, self.scope, self.providers
        )
        self.healthy = False
        with self.assertRaises(ActionWireError):
            reservation.run()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.ports._execution_evidence, {})
        self.assert_released()

    def test_wrong_exports_or_handoff_refuse_before_dispatch(self):
        for changes in ({"exports": ()}, {"handoff": None}):
            with self.subTest(changes=changes):
                original = self.executor.handoff_for
                try:
                    if "handoff" in changes:
                        self.executor.handoff_for = lambda *args: None
                    with self.assertRaises(ActionWireError):
                        self.executor.execute_scoped(
                            self.plan,
                            changes.get("exports", self.exports),
                            self.scope,
                            self.providers,
                        )
                finally:
                    self.executor.handoff_for = original
                self.assertFalse(self.marker.exists())
                self.assert_released()

    def test_missing_registered_build_refuses_before_dispatch(self):
        self.ports._build_evidence.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            self.executor.execute_scoped(
                self.plan, self.exports, self.scope, self.providers
            )
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_corrupt_return_records_leave_execution_unregistered_and_release(self):
        self.executor.result_source = lambda worker, reference: b"corrupt"
        with self.assertRaises(ActionWireError):
            self.executor.execute_scoped(
                self.plan, self.exports, self.scope, self.providers
            )
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.ports._execution_evidence, {})
        self.assert_released()

    def test_missing_receipts_unscoped_inputs_and_wrong_providers_never_prepare(self):
        callback = Mock(side_effect=AssertionError("prepared invalid inputs"))
        self.executor.handoff_for = callback
        with self.assertRaises(ActionWireError):
            self.executor.execute(self.plan, self.exports)
        with self.assertRaises(ActionWireError):
            self.executor.execute_scoped(
                self.plan, self.exports, self.scope, self.exports
            )
        self.executor._receipts.clear()
        with self.assertRaises(ActionWireError):
            self.executor.execute_scoped(
                self.plan, self.exports, self.scope, self.providers
            )
        callback.assert_not_called()
        self.assertFalse(self.marker.exists())
        self.assert_released()

    def test_receipt_delivery_is_idempotent_and_refuses_foreign_scope(self):
        self.executor.retain_execution_provider_evidence(self.plan, self.scope, ())
        self.assertEqual(len(self.executor._receipts), 1)
        for changes in (
            {"execution_plan_identity": canonical_identity("foreign-execution")},
            {"build_plan_identity": canonical_identity("foreign-build")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ActionWireError):
                self.executor.retain_execution_provider_evidence(
                    self.plan, replace(self.scope, **changes), ()
                )
        with self.assertRaises(ActionWireError):
            self.executor.retain_execution_provider_evidence(
                self.plan, self.scope, (object(),)
            )
        self.assertEqual(len(self.executor._receipts), 1)

    def test_receipt_loss_after_reservation_refuses_and_releases(self):
        reservation = self.executor.try_reserve_execute(
            self.plan, self.exports, self.scope, self.providers
        )
        self.executor._receipts.clear()
        with self.assertRaises(ActionWireError):
            reservation.run()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.ports._execution_evidence, {})
        self.assertEqual(self.ports.execution_stdout, {})
        self.assert_released()

    def test_conflicting_receipts_for_same_scope_do_not_replace_accepted_state(self):
        from tests.unit import test_action_execute_providers as provider_fixture

        fixture = provider_fixture.ExecuteProviderTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        value = fixture.value
        indexer = SimpleNamespace(
            catalog=self.catalog,
            workers=(self.admitted,),
            execution_plan=value.build_input.execution_plan,
        )
        executor = CommandComponentExecutor(
            indexer, self.admission, self.ports, handoff_for=lambda *args: None
        )
        plan = value.build_input.plan
        executor.retain_execution_provider_evidence(
            plan, value.scope, value.accepted_providers
        )
        changed = replace(
            fixture.provider,
            acceptance_policy_identity=canonical_identity("different-policy"),
        )
        with self.assertRaises(ActionWireError) as error:
            executor.retain_execution_provider_evidence(plan, value.scope, (changed,))
        self.assertEqual(error.exception.code, "action_execute.receipts_changed")
        self.assertEqual(
            executor._receipts[(plan.identity, value.scope.identity)],
            value.accepted_providers,
        )

"""FINALIZE wire admission preserves exact production PACKAGE edges and receiver."""

import unittest
from dataclasses import replace
from unittest.mock import Mock

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize import (
    admit_finalize_action,
    reopen_finalize_action,
)
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
    LifecycleActionWorker,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes


def make_finalize_request(value, proof, deadline):
    worker = LifecycleActionWorker(
        "finalize",
        canonical_identity("worker"),
        canonical_identity("catalog"),
        canonical_identity("observed"),
    )
    execution = value.package_input.execution_plan
    action = next(
        node
        for node in plan_lifecycle_action_dag(execution, worker_ids=(worker.worker_id,))
        if node.kind is LifecycleActionKind.FINALIZE
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            execution.identity, execution.root_revision, LifecycleActionKind.FINALIZE
        )
    )
    raw = value.to_bytes()
    input_id = record_identity(raw)
    request = LifecycleActionDispatchRequest(
        canonical_identity("schedule"),
        action,
        worker,
        0,
        (value.package_result_identity,),
        deadline.identity,
        (
            input_id,
            *sorted(
                set(proof) - {value.package_result_identity}, key=lambda item: item.uri
            ),
        ),
    )
    return request, dict(proof) | {input_id: raw, record_identity(payload): payload}


class FinalizeActionTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.FinalizeWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        package = b"package descriptor: no execution proof claimed"
        self.value = replace(f.value, package_result_identity=record_identity(package))
        self.request, self.records = make_finalize_request(
            self.value, {self.value.package_result_identity: package}, f.deadline
        )

    def admit(self, **changes):
        args = dict(
            request=self.request,
            deadline=self.fixture.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
        )
        return admit_finalize_action(**(args | changes))

    def test_exact_descriptors_admit_without_claiming_finalization(self):
        self.assertEqual(self.admit(), self.value)

    def test_wrong_worker_phase_deadline_and_predecessor_refuse(self):
        action = self.request.action
        for changes in (
            dict(expected_worker_identity=canonical_identity("foreign")),
            dict(
                request=replace(
                    self.request, deadline_identity=canonical_identity("foreign")
                )
            ),
            dict(
                request=replace(
                    self.request,
                    action=replace(action, kind=LifecycleActionKind.PACKAGE),
                )
            ),
            dict(
                request=replace(
                    self.request, action=replace(action, predecessor_ids=())
                )
            ),
            dict(
                request=replace(
                    self.request,
                    predecessor_result_identities=(canonical_identity("foreign"),),
                )
            ),
        ):
            with self.subTest(changes=changes), self.assertRaises(ActionWireError):
                self.admit(**changes)

    def test_missing_extra_corrupt_and_changed_payload_refuse(self):
        missing = dict(self.records)
        missing.pop(self.value.package_result_identity)
        for records in (
            missing,
            self.records | {record_identity(b"extra"): b"extra"},
            self.records | {self.value.package_result_identity: b"corrupt"},
            self.records | {self.request.action.payload_identity: b"foreign payload"},
        ):
            with self.assertRaises(ActionWireError):
                self.admit(records=records)

    def test_cancelled_action_does_not_read_cas_or_invoke_verifier(self):
        cas, verifier = Mock(), Mock()
        with self.assertRaises(ActionWireError) as error:
            reopen_finalize_action(
                self.request,
                self.fixture.deadline,
                self.records,
                expected_worker_identity=self.request.worker.worker_identity,
                cas=cas,
                admission_guard=lambda: None,
                verify_package=verifier,
                cancelled=lambda: True,
            )
        self.assertEqual(error.exception.code, "action_finalize.cancelled")
        self.assertEqual(cas.mock_calls, [])
        verifier.assert_not_called()

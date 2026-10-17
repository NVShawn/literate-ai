"""PACKAGE dispatch cannot substitute worker, production edges, or exact inputs."""

import unittest
from dataclasses import replace
from unittest.mock import Mock

import tests.support.fixtures_test_action_package_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_package import (
    admit_package_action,
    execute_package_action,
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


def make_package_request(value, proof, deadline):
    worker = LifecycleActionWorker(
        "package",
        canonical_identity("worker"),
        canonical_identity("catalog"),
        canonical_identity("observed"),
    )
    node = next(
        node
        for node in plan_lifecycle_action_dag(
            value.execution_plan, worker_ids=(worker.worker_id,)
        )
        if node.kind is LifecycleActionKind.PACKAGE
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            value.execution_plan.identity,
            value.execution_plan.root_revision,
            LifecycleActionKind.PACKAGE,
        )
    )
    raw = value.to_bytes()
    input_id = record_identity(raw)
    predecessors = tuple(identity for _, identity in value.link_results)
    request = LifecycleActionDispatchRequest(
        canonical_identity("schedule"),
        node,
        worker,
        0,
        predecessors,
        deadline.identity,
        (input_id, *sorted(set(proof) - set(predecessors), key=lambda item: item.uri)),
    )
    return request, dict(proof) | {input_id: raw, record_identity(payload): payload}


class PackageActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.PackageWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        proof = {
            identity: canonical_json_bytes(action)
            for action, identity in f.value.link_results
        }
        self.request, self.records = make_package_request(f.value, proof, f.deadline)

    def admit(self, **changes):
        args = dict(
            request=self.request,
            deadline=self.fixture.deadline,
            records=self.records,
            expected_worker_identity=self.request.worker.worker_identity,
        )
        args.update(changes)
        return admit_package_action(**args)

    def test_exact_dispatch_admits_descriptors_without_claiming_proof(self):
        self.assertEqual(self.admit(), self.fixture.value)

    def test_worker_phase_deadline_and_predecessor_substitution_refuse(self):
        for change in (
            {"expected_worker_identity": canonical_identity("foreign")},
            {
                "request": replace(
                    self.request, deadline_identity=canonical_identity("deadline")
                )
            },
            {
                "request": replace(
                    self.request,
                    action=replace(
                        self.request.action, kind=LifecycleActionKind.FINALIZE
                    ),
                )
            },
            {
                "request": replace(
                    self.request,
                    action=replace(self.request.action, predecessor_ids=()),
                )
            },
            {
                "request": replace(
                    self.request,
                    predecessor_result_identities=tuple(
                        reversed(self.request.predecessor_result_identities)
                    ),
                )
            },
        ):
            with self.subTest(change=change), self.assertRaises(ActionWireError):
                self.admit(**change)

    def test_missing_extra_and_corrupt_records_refuse(self):
        missing = dict(self.records)
        missing.pop(self.request.predecessor_result_identities[0])
        corrupt = dict(self.records)
        corrupt[self.request.input_record_identities[0]] = b"corrupt"
        for records in (
            missing,
            corrupt,
            self.records | {record_identity(b"extra"): b"extra"},
        ):
            with self.assertRaises(ActionWireError):
                self.admit(records=records)

    def test_cancellation_refuses_before_cas_or_packager_use(self):
        cas, adapter = Mock(), Mock()
        with self.assertRaises(ActionWireError) as error:
            execute_package_action(
                self.request,
                self.fixture.deadline,
                self.records,
                expected_worker_identity=self.request.worker.worker_identity,
                cas=cas,
                admission_guard=lambda: None,
                package_adapter=adapter,
                packager_identity=self.fixture.value.plan.packager_identity,
                cancelled=lambda: True,
            )
        self.assertEqual(error.exception.code, "action_package.cancelled")
        self.assertEqual(cas.mock_calls, [])
        self.assertEqual(adapter.mock_calls, [])

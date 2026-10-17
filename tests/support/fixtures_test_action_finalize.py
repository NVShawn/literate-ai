"""FINALIZE wire admission preserves exact production PACKAGE edges and receiver."""

import unittest
from dataclasses import replace

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_finalize import (
    admit_finalize_action,
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
    return (request, dict(proof) | {input_id: raw, record_identity(payload): payload})


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
        return admit_finalize_action(**args | changes)

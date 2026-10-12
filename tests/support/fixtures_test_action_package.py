"""PACKAGE dispatch cannot substitute worker, production edges, or exact inputs."""

import unittest

import tests.support.fixtures_test_action_package_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_package import (
    admit_package_action,
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
    predecessors = tuple((identity for _, identity in value.link_results))
    request = LifecycleActionDispatchRequest(
        canonical_identity("schedule"),
        node,
        worker,
        0,
        predecessors,
        deadline.identity,
        (input_id, *sorted(set(proof) - set(predecessors), key=lambda item: item.uri)),
    )
    return (request, dict(proof) | {input_id: raw, record_identity(payload): payload})


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

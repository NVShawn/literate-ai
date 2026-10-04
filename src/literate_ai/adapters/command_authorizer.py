"""Production AUTHORIZE port sharing admitted worker capacity with source indexing."""

from __future__ import annotations

from functools import partial

from literate_ai.adapters.action_authorization import authorization_action_inputs
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes


class CommandBuildAuthorizer:
    def __init__(self, indexer, admission, local_ports):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError(
                "AUTHORIZE and INDEX must share the same admitted worker pool"
            )
        self.indexer, self.admission, self.local_ports = indexer, admission, local_ports
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.AUTHORIZE)
        )
        if not self.eligible:
            raise ValueError("no admitted AUTHORIZE worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.AUTHORIZE
        }
        self.generation_plans = {
            plan.component_revision: plan
            for plan in indexer.execution_plan.generation_plans
        }

    def try_reserve_authorization(self, intent, index):
        return self.indexer.slots.try_reserve(
            partial(self._execute, intent, index),
            eligible_worker_ids=self.eligible,
        )

    def authorize(self, intent, index):
        with self.indexer.slots.acquire(eligible_worker_ids=self.eligible) as (
            worker,
            slot,
        ):
            return self._execute(intent, index, worker, slot)

    def _execute(self, intent, index, worker, slot):
        self.indexer.deadline.remaining()
        if not self.admission.supports_phase(worker, LifecycleActionKind.AUTHORIZE):
            raise ActionWireError(
                "action_authorization.unsupported_phase",
                "worker is not admitted for AUTHORIZE",
            )
        inputs = self.local_ports.authorization_inputs(intent, index)
        expected = inputs.authorize()
        generation = self.generation_plans[intent.component_revision]
        index_record = canonical_json_bytes(
            {
                "schema": "literate-ai/disabled-source-index@1",
                "component_revision": intent.component_revision.uri,
                "source": intent.source_tree_identity.uri,
            }
        )
        if record_identity(index_record) != index:
            raise ActionWireError(
                "action_authorization.index_mismatch",
                "completed index differs from admitted source policy",
            )
        content = authorization_action_inputs(
            self.indexer.execution_plan.identity,
            generation.identity,
            intent,
            index_record,
            inputs.issued_at,
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                self.indexer.execution_plan.identity,
                intent.component_revision,
                LifecycleActionKind.AUTHORIZE,
                generation.identity,
            )
        )
        records = {record_identity(content): content, record_identity(payload): payload}
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-authorization-schedule@1",
                    "execution_plan": self.indexer.execution_plan.identity.uri,
                    "admission": self.admission.identity.uri,
                }
            ),
            self.nodes[intent.component_revision],
            worker,
            slot,
            (record_identity(content),),
            self.indexer.deadline.identity,
        )
        results = {}
        outcome = self.indexer._dispatcher(records, results).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(outcome.failure_code, "worker authorization failed")
        expected_bytes = canonical_json_bytes(expected.to_dict())
        if (
            outcome.result_identity != record_identity(expected_bytes)
            or results.get(outcome.result_identity) != expected_bytes
        ):
            raise ActionWireError(
                "action_authorization.result_mismatch",
                "worker returned another authorization",
            )
        self.indexer.deadline.remaining()
        self.indexer.remember_action_result(expected_bytes)
        return self.local_ports.accept_build_authorization(inputs, expected)

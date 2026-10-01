"""Queued BUILD_INTENT dispatch using the shared admitted action-worker slots."""

from __future__ import annotations

from functools import partial

from literate_ai.adapters.action_build_intent import build_intent_action_predecessors
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


class CommandBuildIntentDispatcher:
    def __init__(self, indexer, admission, local_ports):
        if (
            indexer.catalog.identity != admission.catalog.identity
            or indexer.workers != admission.workers
        ):
            raise ValueError("BUILD_INTENT and INDEX must share admitted workers")
        self.indexer, self.admission, self.local_ports = indexer, admission, local_ports
        self.eligible = tuple(
            worker.worker_id
            for worker in indexer.workers
            if admission.supports_phase(worker, LifecycleActionKind.BUILD_INTENT)
        )
        if not self.eligible:
            raise ValueError("no admitted BUILD_INTENT worker")
        self.nodes = {
            node.component_revision: node
            for node in plan_lifecycle_action_dag(
                indexer.execution_plan, worker_ids=self.eligible
            )
            if node.kind is LifecycleActionKind.BUILD_INTENT
        }

    def try_reserve_intent(
        self,
        execution_plan,
        generation_plan,
        candidate,
        index_identity,
        provider_artifacts,
        package_artifacts,
        accepted_providers,
    ):
        return self.indexer.slots.try_reserve(
            partial(
                self._execute,
                execution_plan,
                generation_plan,
                candidate,
                index_identity,
                provider_artifacts,
                package_artifacts,
                accepted_providers,
            ),
            eligible_worker_ids=self.eligible,
        )

    def _execute(
        self,
        execution_plan,
        generation_plan,
        candidate,
        index_identity,
        provider_artifacts,
        package_artifacts,
        accepted_providers,
        worker,
        slot,
    ):
        self.indexer.deadline.remaining()
        if (
            execution_plan.identity != self.indexer.execution_plan.identity
            or not self.admission.supports_phase(
                worker, LifecycleActionKind.BUILD_INTENT
            )
        ):
            raise ActionWireError(
                "action_intent.admission_mismatch", "build-intent admission changed"
            )
        arguments = (
            execution_plan,
            generation_plan,
            candidate,
            provider_artifacts,
            package_artifacts,
        )
        inputs = self.local_ports.build_intent_inputs(*arguments)
        expected = inputs.create()
        index_record = canonical_json_bytes(
            {
                "schema": "literate-ai/disabled-source-index@1",
                "component_revision": candidate.component_revision.uri,
                "source": candidate.tree_identity.uri,
            }
        )
        if record_identity(index_record) != index_identity:
            raise ActionWireError(
                "action_intent.index_mismatch",
                "current index differs from the admitted source policy",
            )
        handoffs = build_intent_action_predecessors(
            execution_plan, inputs, index_record, accepted_providers
        )
        payload = canonical_json_bytes(
            lifecycle_action_payload(
                execution_plan.identity,
                candidate.component_revision,
                LifecycleActionKind.BUILD_INTENT,
                generation_plan.identity,
            )
        )
        records = {
            record_identity(content): content
            for content in (*handoffs.values(), payload)
        }
        action = self.nodes[candidate.component_revision]
        request = LifecycleActionDispatchRequest(
            canonical_identity(
                {
                    "schema": "literate-ai/command-build-intent-schedule@1",
                    "execution_plan": execution_plan.identity.uri,
                    "admission": self.admission.identity.uri,
                }
            ),
            action,
            worker,
            slot,
            tuple(record_identity(handoffs[key]) for key in action.predecessor_ids),
            self.indexer.deadline.identity,
        )
        results = {}
        outcome = self.indexer._dispatcher(records, results).dispatch(request)
        if outcome.failure_code is not None:
            raise ActionWireError(
                outcome.failure_code, "worker build-intent construction failed"
            )
        expected_bytes = canonical_json_bytes(expected.to_dict())
        if (
            outcome.result_identity != record_identity(expected_bytes)
            or results.get(outcome.result_identity) != expected_bytes
        ):
            raise ActionWireError(
                "action_intent.result_mismatch", "worker returned another build intent"
            )
        self.indexer.deadline.remaining()
        self.indexer.remember_action_result(expected_bytes)
        return self.local_ports.accept_build_intent(*arguments, expected)

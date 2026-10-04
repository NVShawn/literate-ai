"""Shared fixtures extracted from ``tests.unit.test_action_build_action``."""








from literate_ai.adapters.action_dispatch_wire import record_identity


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



def build_request(value, deadline):
    worker = LifecycleActionWorker(
        "builder",
        canonical_identity("worker"),
        canonical_identity("catalog"),
        canonical_identity("observation"),
    )
    action = next(
        node
        for node in plan_lifecycle_action_dag(
            value.execution_plan, worker_ids=(worker.worker_id,)
        )
        if node.kind is LifecycleActionKind.BUILD
        and node.component_revision == value.candidate.component_revision
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            value.execution_plan_identity,
            value.candidate.component_revision,
            LifecycleActionKind.BUILD,
            value.generation_plan_identity,
        )
    )
    content = value.to_bytes()
    return (
        LifecycleActionDispatchRequest(
            canonical_identity("schedule"),
            action,
            worker,
            0,
            (record_identity(content),),
            deadline.identity,
        ),
        {record_identity(content): content, record_identity(payload): payload},
    )


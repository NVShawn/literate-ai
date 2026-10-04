"""Shared fixtures extracted from ``tests.unit.test_action_execute_action``."""








from literate_ai.adapters.action_dispatch_wire import record_identity

from literate_ai.adapters.action_execute import (
    execute_predecessor_records,
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

from literate_ai.contracts import (
    canonical_identity,
    canonical_json_bytes,
)



def make_execute_request(value, deadline):
    build = value.build_input
    worker = LifecycleActionWorker(
        "builder",
        canonical_identity("worker"),
        canonical_identity("catalog"),
        canonical_identity("observation"),
    )
    action = next(
        node
        for node in plan_lifecycle_action_dag(
            build.execution_plan, worker_ids=(worker.worker_id,)
        )
        if node.kind is LifecycleActionKind.EXECUTE
        and node.component_revision == build.candidate.component_revision
    )
    payload = canonical_json_bytes(
        lifecycle_action_payload(
            build.execution_plan_identity,
            build.candidate.component_revision,
            LifecycleActionKind.EXECUTE,
            build.generation_plan_identity,
        )
    )
    identities, records = execute_predecessor_records(value, action)
    records[record_identity(payload)] = payload
    return (
        LifecycleActionDispatchRequest(
            canonical_identity("schedule"),
            action,
            worker,
            0,
            identities,
            deadline.identity,
        ),
        records,
    )


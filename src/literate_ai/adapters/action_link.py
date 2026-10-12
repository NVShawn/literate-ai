"""Dispatch LINK only after exact DAG admission and complete artifact proof."""

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_link_dependencies import reopen_link_result_closure
from literate_ai.adapters.action_link_proof import reopen_link_component
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.adapters.action_link_result import LinkWorkerResult
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_json_bytes


def _invalid():
    raise ActionWireError("action_link.dispatch_invalid", "LINK dispatch differs")


def admit_link_action(request, deadline, records, *, expected_worker_identity):
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_link.worker_mismatch", "receiver binds another worker"
        )
    action = request.action
    if action.kind is not LifecycleActionKind.LINK:
        raise ActionWireError("action_link.unsupported_phase", "receiver requires LINK")
    if (
        request.deadline_identity != deadline.identity
        or not request.input_record_identities
    ):
        _invalid()
    deadline.remaining()
    required = {
        action.payload_identity,
        *request.input_record_identities,
        *request.predecessor_result_identities,
    }
    if set(records) != required or any(
        not isinstance(raw, bytes) for raw in records.values()
    ):
        _invalid()
    if sum(map(len, records.values())) > MAX_ACTION_RECORD_BYTES or any(
        record_identity(raw) != identity for identity, raw in records.items()
    ):
        _invalid()
    input_id = request.input_record_identities[0]
    value = LinkWorkerInput.admit(records[input_id], input_id, deadline)
    build = value.build
    expected = next(
        (
            node
            for node in plan_lifecycle_action_dag(
                build.execution_plan, worker_ids=action.eligible_worker_ids
            )
            if node.kind is LifecycleActionKind.LINK
            and node.component_revision == build.plan.component_revision
        ),
        None,
    )
    if expected is None or (
        action.action_id != expected.action_id
        or action.component_revision != expected.component_revision
        or action.predecessor_ids != expected.predecessor_ids
        or records[action.payload_identity]
        != canonical_json_bytes(
            lifecycle_action_payload(
                build.execution_plan_identity,
                action.component_revision,
                LifecycleActionKind.LINK,
                build.generation_plan_identity,
            )
        )
    ):
        _invalid()
    accepted = value.acceptance_result.to_bytes()
    accepted_id = record_identity(accepted)
    predecessors = dict(value.dependency_links)
    predecessors[
        lifecycle_action_id(action.component_revision, LifecycleActionKind.ACCEPT)
    ] = accepted_id
    if (
        tuple(predecessors[key] for key in action.predecessor_ids)
        != request.predecessor_result_identities
        or records.get(accepted_id) != accepted
    ):
        _invalid()
    deadline.remaining()
    return value


def execute_link_action(
    request,
    deadline,
    records,
    *,
    expected_worker_identity,
    cas,
    admission_guard,
    blob_source=None,
    cancelled=lambda: False,
):
    if not callable(admission_guard) or not callable(cancelled):
        raise TypeError("LINK requires live admission and cancellation")
    records = dict(records)
    value = admit_link_action(
        request, deadline, records, expected_worker_identity=expected_worker_identity
    )

    def current():
        deadline.remaining()
        if cancelled():
            raise ActionWireError("action_link.cancelled", "LINK action was cancelled")
        admission_guard()
        deadline.remaining()

    current()
    input_id = request.input_record_identities[0]
    own_accept = record_identity(value.acceptance_result.to_bytes())
    dependency_records = {
        identity: raw
        for identity, raw in records.items()
        if identity not in {input_id, own_accept, request.action.payload_identity}
    }
    reopen_link_result_closure(
        value.dependency_links,
        execution_plan_identity=value.build.execution_plan_identity,
        records=dependency_records,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        blob_source=blob_source,
    )
    reopen_link_component(
        input_record=records[input_id],
        input_identity=input_id,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        blob_source=blob_source,
    )
    current()
    result = LinkWorkerResult.expected(records[input_id], input_id, deadline).to_bytes()
    LinkWorkerResult.admit(
        result,
        record_identity(result),
        input_record=records[input_id],
        input_identity=input_id,
        deadline=deadline,
    )
    current()
    return result

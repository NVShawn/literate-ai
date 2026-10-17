"""Bind FINALIZE dispatch to its production FINALIZE predecessor and exact inputs."""

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_json_bytes


def _invalid():
    raise ActionWireError(
        "action_finalize.dispatch_invalid", "FINALIZE dispatch differs"
    )


def admit_finalize_action(request, deadline, records, *, expected_worker_identity):
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_finalize.worker_mismatch", "receiver binds another worker"
        )
    action = request.action
    if action.kind is not LifecycleActionKind.FINALIZE:
        raise ActionWireError(
            "action_finalize.unsupported_phase", "receiver requires FINALIZE"
        )
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
    value = FinalizeWorkerInput.admit(records[input_id], input_id, deadline)
    expected = next(
        node
        for node in plan_lifecycle_action_dag(
            value.package_input.execution_plan, worker_ids=action.eligible_worker_ids
        )
        if node.kind is LifecycleActionKind.FINALIZE
    )
    if (
        action.action_id != expected.action_id
        or action.component_revision != expected.component_revision
        or action.predecessor_ids != expected.predecessor_ids
        or request.predecessor_result_identities != (value.package_result_identity,)
        or records[action.payload_identity]
        != canonical_json_bytes(
            lifecycle_action_payload(
                value.package_input.execution_plan.identity,
                value.package_input.execution_plan.root_revision,
                LifecycleActionKind.FINALIZE,
            )
        )
    ):
        _invalid()
    deadline.remaining()
    return value


def reopen_finalize_action(
    request,
    deadline,
    records,
    *,
    expected_worker_identity,
    cas,
    admission_guard,
    verify_package,
    blob_source=None,
    cancelled=lambda: False,
):
    """Admit dispatch and reopen input proof; this does not launch root commands."""
    from literate_ai.adapters.action_finalize_proof import reopen_finalize_input

    if not callable(admission_guard) or not callable(cancelled):
        raise TypeError("FINALIZE requires live admission and cancellation")
    records = dict(records)
    admit_finalize_action(
        request, deadline, records, expected_worker_identity=expected_worker_identity
    )

    def current():
        deadline.remaining()
        if cancelled():
            raise ActionWireError(
                "action_finalize.cancelled", "FINALIZE action was cancelled"
            )
        admission_guard()
        deadline.remaining()

    current()
    input_id = request.input_record_identities[0]
    proof = {
        identity: raw
        for identity, raw in records.items()
        if identity not in {input_id, request.action.payload_identity}
    }
    value, package = reopen_finalize_input(
        input_record=records[input_id],
        input_identity=input_id,
        records=proof,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        verify_package=verify_package,
        blob_source=blob_source,
    )
    current()
    return value, package

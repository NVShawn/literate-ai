"""Admit exact PACKAGE dispatch before invoking privately configured execution."""

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_package_execution import execute_package_input
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.action_dag_planning import (
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_json_bytes


def _invalid():
    raise ActionWireError("action_package.dispatch_invalid", "PACKAGE dispatch differs")


def admit_package_action(request, deadline, records, *, expected_worker_identity):
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_package.worker_mismatch", "receiver binds another worker"
        )
    action = request.action
    if action.kind is not LifecycleActionKind.PACKAGE:
        raise ActionWireError(
            "action_package.unsupported_phase", "receiver requires PACKAGE"
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
    value = PackageWorkerInput.admit(records[input_id], input_id, deadline)
    expected = next(
        (
            node
            for node in plan_lifecycle_action_dag(
                value.execution_plan, worker_ids=action.eligible_worker_ids
            )
            if node.kind is LifecycleActionKind.PACKAGE
        ),
        None,
    )
    if expected is None:
        _invalid()
    if (
        action.action_id != expected.action_id
        or action.component_revision != expected.component_revision
        or action.predecessor_ids != expected.predecessor_ids
        or request.predecessor_result_identities
        != tuple(identity for _, identity in value.link_results)
        or records[action.payload_identity]
        != canonical_json_bytes(
            lifecycle_action_payload(
                value.execution_plan.identity,
                value.execution_plan.root_revision,
                LifecycleActionKind.PACKAGE,
            )
        )
    ):
        _invalid()
    deadline.remaining()
    return value


def execute_package_action(
    request,
    deadline,
    records,
    *,
    expected_worker_identity,
    cas,
    admission_guard,
    package_adapter,
    packager_identity,
    blob_source=None,
    read_created_blob=None,
    cancelled=lambda: False,
):
    if not callable(admission_guard) or not callable(cancelled):
        raise TypeError("PACKAGE requires live admission and cancellation")
    records = dict(records)
    admit_package_action(
        request, deadline, records, expected_worker_identity=expected_worker_identity
    )

    def current():
        deadline.remaining()
        if cancelled():
            raise ActionWireError(
                "action_package.cancelled", "PACKAGE action was cancelled"
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
    result = execute_package_input(
        input_record=records[input_id],
        input_identity=input_id,
        records=proof,
        cas=cas,
        deadline=deadline,
        admission_guard=current,
        package_adapter=package_adapter,
        packager_identity=packager_identity,
        blob_source=blob_source,
        read_created_blob=read_created_blob,
    )
    current()
    return result

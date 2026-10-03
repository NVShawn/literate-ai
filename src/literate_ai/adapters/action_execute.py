"""EXECUTE action admission around the worker-owned supervised process."""

from collections.abc import Callable, Mapping
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_execute_process import run_execute_worker_process
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.application.action_dag_planning import (
    lifecycle_action_id,
    lifecycle_action_payload,
    plan_lifecycle_action_dag,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionKind,
)
from literate_ai.contracts import ContentIdentity, canonical_json_bytes


def _invalid():
    raise ActionWireError("action_execute.dispatch_invalid", "EXECUTE dispatch differs")


def execute_input_identity(request):
    predecessors = request.action.predecessor_ids
    test_id = lifecycle_action_id(
        request.action.component_revision, LifecycleActionKind.TEST
    )
    if (
        len(request.predecessor_result_identities) != len(predecessors)
        or test_id not in predecessors
    ):
        _invalid()
    return request.predecessor_result_identities[predecessors.index(test_id)]


def execute_predecessor_records(value, action):
    """Bind the BUILD handoff and each direct runtime ACCEPT to its DAG position."""
    content = value.to_bytes()
    by_action = {
        lifecycle_action_id(
            action.component_revision, LifecycleActionKind.TEST
        ): content,
        **{
            lifecycle_action_id(
                receipt.component_revision, LifecycleActionKind.ACCEPT
            ): canonical_json_bytes(receipt.to_dict())
            for receipt in value.accepted_providers
        },
    }
    try:
        ordered = tuple(by_action[action_id] for action_id in action.predecessor_ids)
    except KeyError:
        _invalid()
    return tuple(record_identity(record) for record in ordered), {
        record_identity(record): record for record in ordered
    }


def admit_execute_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
) -> ExecuteWorkerInput:
    """Admit dispatch authority before private worker preparation or execution."""
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_execute.worker_mismatch", "receiver binds another worker"
        )
    action = request.action
    if action.kind is not LifecycleActionKind.EXECUTE:
        raise ActionWireError(
            "action_execute.unsupported_phase", "receiver requires EXECUTE"
        )
    if (
        request.deadline_identity != deadline.identity
        or action.action_id
        != lifecycle_action_id(action.component_revision, LifecycleActionKind.EXECUTE)
    ):
        _invalid()
    deadline.remaining()
    input_identity = execute_input_identity(request)
    if set(records) != {
        action.payload_identity,
        *request.predecessor_result_identities,
    } or any(not isinstance(content, bytes) for content in records.values()):
        _invalid()
    if sum(map(len, records.values())) > MAX_ACTION_RECORD_BYTES or any(
        record_identity(content) != identity for identity, content in records.items()
    ):
        _invalid()
    content = records[input_identity]
    admitted = ExecuteWorkerInput.admit(content, input_identity, deadline)
    build = admitted.build_input
    expected = next(
        (
            node
            for node in plan_lifecycle_action_dag(
                build.execution_plan, worker_ids=action.eligible_worker_ids
            )
            if node.kind is LifecycleActionKind.EXECUTE
            and node.component_revision == build.candidate.component_revision
        ),
        None,
    )
    if expected is None or action.predecessor_ids != expected.predecessor_ids:
        _invalid()
    identities, predecessor_records = execute_predecessor_records(admitted, action)
    if identities != request.predecessor_result_identities or any(
        records[identity] != record for identity, record in predecessor_records.items()
    ):
        _invalid()
    if build.candidate.component_revision != action.component_revision or records[
        action.payload_identity
    ] != canonical_json_bytes(
        lifecycle_action_payload(
            build.execution_plan_identity,
            action.component_revision,
            LifecycleActionKind.EXECUTE,
            build.generation_plan_identity,
        )
    ):
        _invalid()
    return admitted


def execute_execution_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
    launcher: LocalComponentToolBinding,
    cwd: Path,
    environment: Mapping[str, str],
    cancelled: Callable[[], bool] = lambda: False,
    cas_root: Path | None = None,
    workspace_root: Path | None = None,
) -> bytes:
    """Run the exact BUILD/result handoff with private launch authority."""
    admit_execute_action(
        request, deadline, records, expected_worker_identity=expected_worker_identity
    )
    input_identity = execute_input_identity(request)
    content = records[input_identity]
    result = run_execute_worker_process(
        launcher=launcher,
        input_record=content,
        input_identity=input_identity,
        deadline=deadline,
        cwd=cwd,
        environment=environment,
        cancelled=cancelled,
        cas_root=cas_root,
        workspace_root=workspace_root,
    )
    ExecuteWorkerResult.admit(
        result,
        record_identity(result),
        input_identity=input_identity,
        input_record=content,
        deadline=deadline,
    )
    return result

"""GENERATE action admission around the worker-owned supervised process."""

from collections.abc import Callable, Mapping
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_generate_process import run_generate_worker_process
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.action_generate_result import GenerateWorkerResult
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
    raise ActionWireError(
        "action_generate.dispatch_invalid", "GENERATE dispatch differs"
    )


def admit_generate_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
) -> GenerateWorkerInput:
    """Admit dispatch authority before private worker preparation or execution."""
    if request.worker.worker_identity != expected_worker_identity:
        raise ActionWireError(
            "action_generate.worker_mismatch", "receiver binds another worker"
        )
    action = request.action
    if action.kind is not LifecycleActionKind.GENERATE:
        raise ActionWireError(
            "action_generate.unsupported_phase", "receiver requires GENERATE"
        )
    if (
        request.deadline_identity != deadline.identity
        or action.action_id
        != lifecycle_action_id(action.component_revision, LifecycleActionKind.GENERATE)
        or request.predecessor_result_identities
        or len(request.input_record_identities) != 1
    ):
        _invalid()
    deadline.remaining()
    input_identity = request.input_record_identities[0]
    if set(records) != {action.payload_identity, input_identity} or any(
        not isinstance(content, bytes) for content in records.values()
    ):
        _invalid()
    if sum(map(len, records.values())) > MAX_ACTION_RECORD_BYTES or any(
        record_identity(content) != identity for identity, content in records.items()
    ):
        _invalid()
    content = records[input_identity]
    admitted = GenerateWorkerInput.admit(content, input_identity, deadline)
    expected = next(
        (
            node
            for node in plan_lifecycle_action_dag(
                admitted.execution_plan, worker_ids=action.eligible_worker_ids
            )
            if node.kind is LifecycleActionKind.GENERATE
            and node.component_revision == admitted.plan.component_revision
        ),
        None,
    )
    if expected is None or action.predecessor_ids != expected.predecessor_ids:
        _invalid()
    if admitted.plan.component_revision != action.component_revision or records[
        action.payload_identity
    ] != canonical_json_bytes(
        lifecycle_action_payload(
            admitted.execution_plan.identity,
            action.component_revision,
            LifecycleActionKind.GENERATE,
            admitted.generation_plan_identity,
        )
    ):
        _invalid()
    return admitted


def execute_generate_action(
    request: LifecycleActionDispatchRequest,
    deadline: ActionDispatchDeadline,
    records: Mapping[ContentIdentity, bytes],
    *,
    expected_worker_identity: ContentIdentity,
    admission_guard: Callable[[], None],
    launcher: LocalComponentToolBinding,
    cwd: Path,
    environment: Mapping[str, str],
    cancelled: Callable[[], bool] = lambda: False,
    cas_root: Path | None = None,
    workspace_root: Path | None = None,
) -> bytes:
    """Run the exact bounded generation request with private launch authority."""
    admit_generate_action(
        request, deadline, records, expected_worker_identity=expected_worker_identity
    )
    input_identity = request.input_record_identities[0]
    content = records[input_identity]
    result = run_generate_worker_process(
        admission_guard=admission_guard,
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
    GenerateWorkerResult.admit(
        result,
        record_identity(result),
        input_identity=input_identity,
        input_record=content,
        deadline=deadline,
    )
    return result

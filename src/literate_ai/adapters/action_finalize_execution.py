"""Compose verified FINALIZE inputs, private runtime, root stages and bounded return."""

from dataclasses import dataclass

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_finalize_inputs import materialize_finalize_inputs
from literate_ai.adapters.action_finalize_result_record import FinalizeWorkerResult
from literate_ai.adapters.action_finalize_runtime import bind_finalize_runtime
from literate_ai.adapters.action_finalize_stages import execute_finalize_stages
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder


@dataclass(frozen=True)
class FinalizeWorkerRuntime:
    ports: object
    python_observer: object | None = None


def execute_worker_finalize_from_cas(
    *,
    input_record,
    input_identity,
    records,
    cas,
    workspace_root,
    deadline,
    admission_guard,
    verify_package,
    runtime_factory,
    require_execution_authority,
    blob_source=None,
):
    """The caller must supervise this execution and supply private live authority."""
    if not all(
        callable(item)
        for item in (
            admission_guard,
            verify_package,
            runtime_factory,
            require_execution_authority,
        )
    ):
        raise TypeError("FINALIZE requires private runtime, verifiers and authority")
    if not workspace_root.is_absolute():
        raise ActionWireError(
            "action_finalize.workspace_invalid", "absolute workspace required"
        )
    require_safe_directory(workspace_root)
    owned = directory_node(workspace_root)

    def current():
        deadline.remaining()
        admission_guard()
        if directory_node(workspace_root) != owned:
            raise ActionWireError(
                "action_finalize.workspace_invalid", "workspace changed"
            )
        deadline.remaining()

    current()
    recorder = QualificationEvidenceRecorder(
        max_bytes=MAX_BUILD_EVIDENCE_BYTES, max_records=MAX_BUILD_EVIDENCE_RECORDS
    )
    with materialize_finalize_inputs(
        input_record=input_record,
        input_identity=input_identity,
        records=records,
        cas=cas,
        workspace_root=workspace_root,
        deadline=deadline,
        admission_guard=current,
        verify_package=verify_package,
        blob_source=blob_source,
    ) as prepared:
        current()
        with runtime_factory(prepared) as runtime:
            if not isinstance(runtime, FinalizeWorkerRuntime):
                raise TypeError("private factory must return a FINALIZE runtime")
            ports = runtime.ports
            require_safe_directory(ports.object_root)
            root = workspace_root.resolve(strict=True)
            outputs = ports.object_root.resolve(strict=True)
            if outputs == root or not outputs.is_relative_to(root):
                raise ActionWireError(
                    "action_finalize.workspace_invalid",
                    "runtime output escapes workspace",
                )
            ports.retain_evidence_with(recorder)
            with bind_finalize_runtime(
                prepared,
                ports,
                admission_guard=current,
                python_observer=runtime.python_observer,
            ):
                evidence = execute_finalize_stages(
                    prepared,
                    ports=ports,
                    deadline=deadline,
                    admission_guard=current,
                    require_execution_authority=require_execution_authority,
                )
                recorder.remember_json(evidence.to_dict())
                references = []
                for identity, raw in recorder.entries:
                    current()
                    if len(raw) > MAX_ACTION_RECORD_BYTES:
                        raise ActionWireError(
                            "action_finalize.record_invalid",
                            "evidence record exceeds bound",
                        )
                    ref = cas.put_bytes(raw)
                    if ref.identity != identity.uri:
                        raise ActionWireError(
                            "action_finalize.record_invalid",
                            "CAS record identity differs",
                        )
                    references.append(ref)
                result = FinalizeWorkerResult(
                    input_identity,
                    evidence,
                    tuple(sorted(references, key=lambda ref: ref.identity)),
                ).to_bytes()
                FinalizeWorkerResult.admit(
                    result,
                    record_identity(result),
                    input_record=input_record,
                    input_identity=input_identity,
                    deadline=deadline,
                )
                current()
    current()
    return result

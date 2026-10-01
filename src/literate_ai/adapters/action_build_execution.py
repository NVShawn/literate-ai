"""One admitted BUILD operation inside a supervised, privately composed worker."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_result import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
    capture_build_result,
)
from literate_ai.adapters.action_build_source import materialize_build_source
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.storage import FileSystemCAS


def execute_worker_build(
    *,
    input_record: bytes,
    input_identity: ContentIdentity,
    deadline: ActionDispatchDeadline,
    ports: LocalStandardLifecyclePorts,
    cas: FileSystemCAS,
    blob_source: Callable[[BlobRef], bytes] | None = None,
) -> bytes:
    """Admit current context, execute BUILD, and retain its verified portable result.

    The caller owns private runtime composition and the supervising process. Evidence
    recording must precede provider admission; this operation never replaces it.
    """
    admitted = BuildWorkerInput.admit(
        input_record, input_identity, deadline, now=datetime.now(UTC)
    )
    ports.retained_evidence_records()
    custody = ports.source_trees.evidence(admitted.candidate.tree_identity)
    if (
        custody.candidate != admitted.candidate
        or custody.identity != admitted.source_custody_identity
    ):
        raise ActionWireError(
            "action_build.source_invalid", "worker source custody differs"
        )
    inputs = admitted.inputs
    ports.accept_build_intent(
        admitted.execution_plan,
        admitted.generation_plan,
        admitted.candidate,
        inputs.providers,
        inputs.package_artifacts,
        inputs.intent,
    )
    if ports.plan_finalization_inputs(inputs.intent, inputs.authorization) != inputs:
        raise ActionWireError("action_build.input_invalid", "worker plan inputs differ")
    ports.accept_finalized_plan(inputs.intent, inputs.authorization, admitted.plan)
    deadline.remaining()
    inputs.authorization.grant.require_valid(
        inputs.intent.build_request, now=datetime.now(UTC)
    )
    from literate_ai.adapters.action_build_providers import materialize_build_providers

    def require_current():
        deadline.remaining()
        if ports.build_execution_inputs(admitted.plan) != inputs:
            raise ActionWireError("action_build.input_invalid", "worker inputs changed")

    with materialize_build_providers(
        admitted=admitted,
        ports=ports,
        cas=cas,
        deadline=deadline,
        require_current=require_current,
        blob_source=blob_source,
    ):
        output = ports.build(admitted.plan, inputs.providers)
        return capture_build_result(
            input_record=input_record,
            input_identity=input_identity,
            deadline=deadline,
            ports=ports,
            output=output,
            records=ports.retained_evidence_records(),
            cas=cas,
        )


def execute_worker_build_from_cas(
    *,
    input_record: bytes,
    input_identity: ContentIdentity,
    deadline: ActionDispatchDeadline,
    cas: FileSystemCAS,
    workspace_root: Path,
    runtime_factory: Callable[
        [BuildWorkerInput, LocalSourceTreeRegistry, QualificationEvidenceRecorder],
        AbstractContextManager[LocalStandardLifecyclePorts],
    ],
    blob_source: Callable[[BlobRef], bytes] | None = None,
    owned_workspace: Path | None = None,
) -> bytes:
    """Run a complete CAS source-to-result operation under private startup binding.

    The factory is trusted worker configuration, never a request-selected import.
    It owns specialized target/provider/SDK bindings and installs the supplied
    recorder before any provider admission. Run this operation inside the bounded
    BUILD supervisor; it does not create a replacement process supervisor.
    """
    admitted = BuildWorkerInput.admit(
        input_record, input_identity, deadline, now=datetime.now(UTC)
    )
    recorder = QualificationEvidenceRecorder(
        max_bytes=MAX_BUILD_EVIDENCE_BYTES,
        max_records=MAX_BUILD_EVIDENCE_RECORDS,
    )
    with materialize_build_source(
        plan=admitted.plan,
        inputs=admitted.inputs,
        candidate=admitted.candidate,
        files=admitted.files,
        validation_inputs=admitted.source_validation,
        source_generation_identity=admitted.source_generation_identity,
        source_custody_identity=admitted.source_custody_identity,
        deadline=deadline,
        cas=cas,
        workspace_root=workspace_root,
        blob_source=blob_source,
    ) as registry:
        with runtime_factory(admitted, registry, recorder) as ports:
            if owned_workspace is not None:
                require_safe_directory(owned_workspace)
                require_safe_directory(ports.object_root)
                owned = owned_workspace.resolve(strict=True)
                artifacts = ports.object_root.resolve(strict=True)
                if artifacts == owned or not artifacts.is_relative_to(owned):
                    raise ActionWireError(
                        "action_build.workspace_invalid",
                        "BUILD output escapes owned workspace",
                    )
            result = execute_worker_build(
                input_record=input_record,
                input_identity=input_identity,
                deadline=deadline,
                ports=ports,
                cas=cas,
                blob_source=blob_source,
            )
    deadline.remaining()
    admitted.inputs.authorization.grant.require_valid(
        admitted.inputs.intent.build_request, now=datetime.now(UTC)
    )
    return result

"""Execute locked EXECUTE over verified BUILD custody without rebuilding artifacts."""

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_build_providers import materialize_provider_artifacts
from literate_ai.adapters.action_build_result import import_build_result
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput
from literate_ai.adapters.action_execute_result_record import ExecuteWorkerResult
from literate_ai.adapters.qualification_capture import QualificationEvidenceReader
from literate_ai.adapters.standard_execution_admission import (
    verify_transferred_execution,
)


def execute_worker_execution(
    *, input_record, input_identity, deadline, ports, cas, blob_source=None
):
    """Caller supplies the private EXECUTE runtime and process supervision."""
    admitted = ExecuteWorkerInput.admit(input_record, input_identity, deadline)
    build = admitted.build_input
    inputs = build.inputs
    ports.retained_evidence_records()
    custody = ports.source_trees.evidence(build.candidate.tree_identity)
    if (
        custody.candidate != build.candidate
        or custody.identity != build.source_custody_identity
    ):
        raise ActionWireError(
            "action_execute.source_invalid", "EXECUTE source custody differs"
        )
    ports.accept_build_intent(
        build.execution_plan,
        build.generation_plan,
        build.candidate,
        inputs.providers,
        inputs.package_artifacts,
        inputs.intent,
    )
    if ports.plan_finalization_inputs(inputs.intent, inputs.authorization) != inputs:
        raise ActionWireError(
            "action_execute.input_invalid", "EXECUTE plan inputs differ"
        )
    ports.accept_finalized_plan(inputs.intent, inputs.authorization, build.plan)

    def guard():
        deadline.remaining()
        if ports.build_execution_inputs(build.plan) != inputs:
            raise ActionWireError(
                "action_execute.input_invalid", "EXECUTE inputs changed"
            )

    guard()
    with materialize_provider_artifacts(
        receipts=admitted.accepted_providers,
        transfers=admitted.provider_builds,
        execution_plan=build.execution_plan,
        providers=admitted.provider_artifacts,
        ports=ports,
        cas=cas,
        deadline=deadline,
        require_current=guard,
        blob_source=blob_source,
    ):
        raw_input = build.to_bytes()
        raw_result = admitted.build_result.to_bytes()
        output = import_build_result(
            content=raw_result,
            result_identity=record_identity(raw_result),
            input_record=raw_input,
            input_identity=record_identity(raw_input),
            deadline=deadline,
            ports=ports,
            cas=cas,
            retain_record=ports.retain_evidence_record,
            blob_source=blob_source,
            admission_guard=guard,
        )
        guard()
        evidence = ports.execute_scoped(
            build.plan, output.exports, admitted.scope, admitted.provider_artifacts
        )
        guard()
        records = ports.retained_evidence_records()
        reader = QualificationEvidenceReader(
            records,
            max_bytes=MAX_BUILD_EVIDENCE_BYTES,
            max_records=MAX_BUILD_EVIDENCE_RECORDS,
        )
        verify_transferred_execution(
            reader,
            plan=build.plan,
            build=output.evidence,
            source_custody=custody,
            contract=inputs.contract,
            evidence=evidence,
            scope=admitted.scope,
            provider_artifacts=admitted.provider_artifacts,
            now=ports.clock(),
        )
        references = []
        for identity, content in records:
            guard()
            if len(content) > MAX_ACTION_RECORD_BYTES:
                raise ActionWireError(
                    "action_execute.record_invalid", "EXECUTE record exceeds bound"
                )
            reference = cas.put_bytes(content)
            if reference.identity != identity.uri:
                raise ActionWireError(
                    "action_execute.record_invalid", "EXECUTE record identity differs"
                )
            references.append(reference)
        result = ExecuteWorkerResult(
            input_identity, evidence, tuple(references)
        ).to_bytes()
        ExecuteWorkerResult.admit(
            result,
            record_identity(result),
            input_record=input_record,
            input_identity=input_identity,
            deadline=deadline,
        )
        guard()
        return result


def execute_worker_execution_from_cas(
    *,
    input_record,
    input_identity,
    deadline,
    cas,
    workspace_root,
    runtime_factory,
    blob_source=None,
    owned_workspace=None,
):
    """Materialize exact source for a privately composed, supervised EXECUTE runtime."""
    from literate_ai._filesystem import require_safe_directory
    from literate_ai.adapters.action_build_source import materialize_build_source
    from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder

    admitted = ExecuteWorkerInput.admit(input_record, input_identity, deadline)
    build = admitted.build_input
    recorder = QualificationEvidenceRecorder(
        max_bytes=MAX_BUILD_EVIDENCE_BYTES, max_records=MAX_BUILD_EVIDENCE_RECORDS
    )
    with materialize_build_source(
        plan=build.plan,
        inputs=build.inputs,
        candidate=build.candidate,
        files=build.files,
        validation_inputs=build.source_validation,
        source_generation_identity=build.source_generation_identity,
        source_custody_identity=build.source_custody_identity,
        deadline=deadline,
        cas=cas,
        workspace_root=workspace_root,
        blob_source=blob_source,
    ) as registry:
        with runtime_factory(build, registry, recorder) as ports:
            if owned_workspace is not None:
                require_safe_directory(owned_workspace)
                require_safe_directory(ports.object_root)
                owned = owned_workspace.resolve(strict=True)
                artifacts = ports.object_root.resolve(strict=True)
                if artifacts == owned or not artifacts.is_relative_to(owned):
                    raise ActionWireError(
                        "action_execute.workspace_invalid",
                        "EXECUTE output escapes owned workspace",
                    )
            result = execute_worker_execution(
                input_record=input_record,
                input_identity=input_identity,
                deadline=deadline,
                ports=ports,
                cas=cas,
                blob_source=blob_source,
            )
    ExecuteWorkerInput.admit(input_record, input_identity, deadline)
    return result

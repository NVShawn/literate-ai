"""Execute locked TEST over verified BUILD custody without rebuilding artifacts."""

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_build_providers import materialize_build_providers
from literate_ai.adapters.action_build_result import import_build_result
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_test_record import TestWorkerInput, TestWorkerResult
from literate_ai.adapters.qualification_capture import QualificationEvidenceReader
from literate_ai.adapters.standard_test_admission import verify_transferred_tests


def execute_worker_test(
    *, input_record, input_identity, deadline, ports, cas, blob_source=None
):
    """The caller supplies private TEST runtime composition and process supervision."""
    admitted = TestWorkerInput.admit(input_record, input_identity, deadline)
    build = admitted.build_input
    inputs = build.inputs
    ports.retained_evidence_records()
    custody = ports.source_trees.evidence(build.candidate.tree_identity)
    if (
        custody.candidate != build.candidate
        or custody.identity != build.source_custody_identity
    ):
        raise ActionWireError(
            "action_test.source_invalid", "TEST source custody differs"
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
        raise ActionWireError("action_test.input_invalid", "TEST plan inputs differ")
    ports.accept_finalized_plan(inputs.intent, inputs.authorization, build.plan)

    def guard():
        deadline.remaining()
        if ports.build_execution_inputs(build.plan) != inputs:
            raise ActionWireError("action_test.input_invalid", "TEST inputs changed")

    guard()
    with materialize_build_providers(
        admitted=build,
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
        evidence = ports.test(build.plan, output.exports)
        guard()
        records = ports.retained_evidence_records()
        reader = QualificationEvidenceReader(
            records,
            max_bytes=MAX_BUILD_EVIDENCE_BYTES,
            max_records=MAX_BUILD_EVIDENCE_RECORDS,
        )
        verify_transferred_tests(
            reader,
            plan=build.plan,
            build=output.evidence,
            source_custody=custody,
            contract=inputs.contract,
            evidence=evidence,
        )
        references = []
        for identity, content in records:
            guard()
            if len(content) > MAX_ACTION_RECORD_BYTES:
                raise ActionWireError(
                    "action_test.record_invalid", "TEST record exceeds bound"
                )
            reference = cas.put_bytes(content)
            if reference.identity != identity.uri:
                raise ActionWireError(
                    "action_test.record_invalid", "TEST record identity differs"
                )
            references.append(reference)
        result = TestWorkerResult(
            input_identity, evidence, tuple(references)
        ).to_bytes()
        TestWorkerResult.admit(
            result,
            record_identity(result),
            input_record=input_record,
            input_identity=input_identity,
            deadline=deadline,
        )
        guard()
        return result


def execute_worker_test_from_cas(
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
    """Materialize exact source for a privately composed, supervised TEST runtime."""
    from literate_ai._filesystem import require_safe_directory
    from literate_ai.adapters.action_build_source import materialize_build_source
    from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder

    admitted = TestWorkerInput.admit(input_record, input_identity, deadline)
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
                        "action_test.workspace_invalid",
                        "TEST output escapes owned workspace",
                    )
            result = execute_worker_test(
                input_record=input_record,
                input_identity=input_identity,
                deadline=deadline,
                ports=ports,
                cas=cas,
                blob_source=blob_source,
            )
    TestWorkerInput.admit(input_record, input_identity, deadline)
    return result

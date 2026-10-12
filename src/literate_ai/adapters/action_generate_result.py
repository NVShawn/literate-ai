"""Exact GENERATE results carry bounded source and provenance proof references."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_generate_record import GenerateWorkerInput
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    capture_qualification_source_records,
    verify_qualification_generation_records,
    verify_qualification_source_records,
)
from literate_ai.contracts import (
    ContentIdentity,
    SourceGenerationRunOutput,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef


def _invalid():
    raise ActionWireError(
        "action_generate.result_invalid", "GENERATE result custody refused"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


@dataclass(frozen=True)
class GenerateWorkerResult:
    input_identity: ContentIdentity
    output: SourceGenerationRunOutput
    evidence_records: tuple[BlobRef, ...]

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/generate-worker-result@1",
                input_identity=self.input_identity.uri,
                output=self.output.to_dict(),
                evidence_records=[ref.to_dict() for ref in self.evidence_records],
            )
        )

    @classmethod
    def admit(cls, content, identity, *, input_record, input_identity, deadline):
        value = GenerateWorkerInput.admit(input_record, input_identity, deadline)
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_ACTION_RECORD_BYTES
            or record_identity(content) != identity
        ):
            _invalid()
        try:
            doc = json.loads(content, object_pairs_hook=_pairs)
            if (
                not isinstance(doc, dict)
                or set(doc)
                != {"schema", "input_identity", "output", "evidence_records"}
                or doc["schema"] != "literate-ai/generate-worker-result@1"
                or canonical_json_bytes(doc) != content
            ):
                _invalid()
            raw_refs = doc["evidence_records"]
            if (
                not isinstance(raw_refs, list)
                or not 1 <= len(raw_refs) <= MAX_BUILD_EVIDENCE_RECORDS
            ):
                _invalid()
            result = cls(
                ContentIdentity.parse_uri(doc["input_identity"]),
                SourceGenerationRunOutput.from_dict(doc["output"]),
                tuple(BlobRef.from_dict(ref) for ref in raw_refs),
            )
            refs = tuple(ref.identity for ref in result.evidence_records)
            if (
                result.input_identity != input_identity
                or refs != tuple(sorted(set(refs)))
                or sum(ref.size for ref in result.evidence_records)
                > MAX_BUILD_EVIDENCE_BYTES
            ):
                _invalid()
            output, plan, request = result.output, value.plan, value.request
            expected = dict(
                source_generation_request_identity=request.identity,
                component_generation_plan_identity=plan.identity,
                generation_key_identity=plan.generation_key.identity,
                context_manifest_identity=request.context_manifest_identity,
                prompt_identity=request.prompt_identity,
                recipe_identity=value.recipe_identity,
                workspace_allocation_identity=value.workspace_allocation_identity,
            )
            if any(
                getattr(record, field) != wanted
                for record in (output.candidate, output.provenance)
                for field, wanted in expected.items()
            ):
                _invalid()
            if (
                output.candidate.component_revision != plan.component_revision
                or output.provenance.generated_component_revision_identity
                != plan.component_revision
                or output.provenance.component_lock_identity
                != value.execution_plan.component_lock_identity
                or output.provenance.application_root_revision_identity
                != value.execution_plan.root_revision
                or not {
                    output.identity.uri,
                    output.candidate_identity.uri,
                    output.provenance_identity.uri,
                }
                <= set(refs)
            ):
                _invalid()
            reviewed = value.retained.authorization if value.retained else None
            if output.provenance.retained_source_identity != reviewed or (
                reviewed is not None
                and output.candidate.planned_coding_cli_request_identity != reviewed
            ):
                _invalid()
            observed = output.runtime_observation
            if observed is not None:
                budget = request.budget
                if any(
                    actual is not None and actual > bound
                    for actual, bound in (
                        (observed.model_attempts, budget.max_model_attempts),
                        (observed.wall_time_ms, budget.max_wall_time_ms),
                        (observed.model_tokens, budget.max_model_tokens),
                        (observed.cost_microunits, budget.max_cost_microunits),
                    )
                ):
                    _invalid()
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ActionWireError(
                "action_generate.result_invalid", "GENERATE result envelope refused"
            ) from exc
        deadline.remaining()
        return result

    def verify_records(self, records):
        reader = QualificationEvidenceReader(
            records,
            max_bytes=MAX_BUILD_EVIDENCE_BYTES,
            max_records=MAX_BUILD_EVIDENCE_RECORDS,
        )
        if tuple(
            (identity.uri, len(content)) for identity, content in records
        ) != tuple((ref.identity, ref.size) for ref in self.evidence_records):
            _invalid()
        for document, identity in (
            (self.output.to_dict(), self.output.identity),
            (self.output.candidate.to_dict(), self.output.candidate_identity),
            (self.output.provenance.to_dict(), self.output.provenance_identity),
        ):
            if reader.read_bytes(identity) != canonical_json_bytes(document):
                _invalid()
        verify_qualification_source_records(reader, candidate=self.output.candidate)
        verify_qualification_generation_records(reader, source_output=self.output)
        return reader


def capture_generate_result(*, input_record, input_identity, output, cas, deadline):
    GenerateWorkerInput.admit(input_record, input_identity, deadline)
    if not isinstance(output, SourceGenerationRunOutput):
        _invalid()
    recorder = QualificationEvidenceRecorder(
        max_bytes=MAX_BUILD_EVIDENCE_BYTES, max_records=MAX_BUILD_EVIDENCE_RECORDS
    )
    capture_qualification_source_records(recorder, candidate=output.candidate, cas=cas)
    for document in (
        output.to_dict(),
        output.candidate.to_dict(),
        output.provenance.to_dict(),
    ):
        recorder.remember_json(document)
    refs = []
    for identity, content in recorder.entries:
        deadline.remaining()
        ref = cas.put_bytes(content)
        if ref.identity != identity.uri:
            _invalid()
        refs.append(ref)
    result = GenerateWorkerResult(input_identity, output, tuple(refs))
    raw = result.to_bytes()
    result = GenerateWorkerResult.admit(
        raw,
        record_identity(raw),
        input_record=input_record,
        input_identity=input_identity,
        deadline=deadline,
    )
    result.verify_records(recorder.entries)
    deadline.remaining()
    return result

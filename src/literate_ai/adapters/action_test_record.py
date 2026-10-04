"""Bounded TEST handoffs retain exact admitted BUILD and source authority."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_build_record import BuildWorkerInput
from literate_ai.adapters.action_build_result import BuildWorkerResult
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    ContentIdentity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.standard_post_source_evidence import (
    StandardGeneratedTestExecutionEvidence,
)

TEST_INPUT_IDENTITY_ENV = "LITAI_TEST_INPUT_IDENTITY"
TEST_DEADLINE_ENV = "LITAI_TEST_DEADLINE"
TEST_CAS_ENV = "LITAI_TEST_CAS"
TEST_WORKSPACE_ENV = "LITAI_TEST_WORKSPACE"


def _invalid():
    raise ActionWireError("action_test.record_invalid", "TEST record authority refused")


def _require_current(build, deadline):
    deadline.remaining()
    build.inputs.authorization.grant.require_valid(
        build.inputs.intent.build_request, now=datetime.now(UTC)
    )


def _load(content, identity, fields, schema):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _invalid()
            result[key] = value
        return result

    if (
        not isinstance(content, bytes)
        or len(content) > MAX_ACTION_RECORD_BYTES
        or not isinstance(identity, ContentIdentity)
        or record_identity(content) != identity
    ):
        _invalid()
    try:
        value = json.loads(content, object_pairs_hook=pairs)
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema"] != schema
            or canonical_json_bytes(value) != content
        ):
            _invalid()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_test.record_invalid", "TEST envelope refused"
        ) from exc


@dataclass(frozen=True)
class TestWorkerInput:
    build_input: BuildWorkerInput
    build_result: BuildWorkerResult

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/test-worker-input@1",
                build_input=json.loads(self.build_input.to_bytes()),
                build_result=json.loads(self.build_result.to_bytes()),
            )
        )

    @classmethod
    def admit(cls, content, identity, deadline):
        deadline.remaining()
        value = _load(
            content,
            identity,
            {"schema", "build_input", "build_result"},
            "literate-ai/test-worker-input@1",
        )
        raw_input = canonical_json_bytes(value["build_input"])
        input_identity = record_identity(raw_input)
        admitted = BuildWorkerInput.admit(
            raw_input, input_identity, deadline, now=datetime.now(UTC)
        )
        raw_result = canonical_json_bytes(value["build_result"])
        result = BuildWorkerResult.admit(
            raw_result,
            record_identity(raw_result),
            input_record=raw_input,
            input_identity=input_identity,
            deadline=deadline,
        )
        _require_current(admitted, deadline)
        return cls(admitted, result)


@dataclass(frozen=True)
class TestWorkerResult:
    input_identity: ContentIdentity
    evidence: StandardGeneratedTestExecutionEvidence
    evidence_records: tuple[BlobRef, ...]

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/test-worker-result@1",
                input_identity=self.input_identity.uri,
                evidence=self.evidence.to_dict(),
                evidence_records=[item.to_dict() for item in self.evidence_records],
            )
        )

    @classmethod
    def admit(cls, content, identity, *, input_record, input_identity, deadline):
        admitted = TestWorkerInput.admit(input_record, input_identity, deadline)
        value = _load(
            content,
            identity,
            {"schema", "input_identity", "evidence", "evidence_records"},
            "literate-ai/test-worker-result@1",
        )
        try:
            records = value["evidence_records"]
            if (
                not isinstance(records, list)
                or not 1 <= len(records) <= MAX_BUILD_EVIDENCE_RECORDS
            ):
                _invalid()
            result = cls(
                ContentIdentity.parse_uri(value["input_identity"]),
                StandardGeneratedTestExecutionEvidence.from_dict(value["evidence"]),
                tuple(BlobRef.from_dict(item) for item in records),
            )
            identities = tuple(item.identity for item in result.evidence_records)
            build = admitted.build_result.evidence
            if (
                result.input_identity != input_identity
                or any(
                    item.size > MAX_ACTION_RECORD_BYTES
                    for item in result.evidence_records
                )
                or sum(item.size for item in result.evidence_records)
                > MAX_BUILD_EVIDENCE_BYTES
                or identities != tuple(sorted(set(identities)))
                or result.evidence.identity.uri not in identities
                or result.evidence.build_evidence_identity != build.identity
                or result.evidence.component_revision != build.component_revision
                or result.evidence.export_identities != build.export_identities
                or result.evidence.generated_test_suite_identity
                != admitted.build_input.candidate.generated_test_suite_identity
            ):
                _invalid()
            _require_current(admitted.build_input, deadline)
            return result
        except ActionWireError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise ActionWireError(
                "action_test.record_invalid", "TEST result refused"
            ) from exc


def required_test_toolchains(inputs) -> tuple[ContentIdentity, ...]:
    """Select every TEST runner, independent of BUILD-only tool requirements."""
    contract = inputs.contract
    contracts = (
        contract.entrypoint_command_contracts()
        if contract.is_multi_entrypoint
        else (contract,)
    )
    return tuple(
        sorted(
            {
                item.tool_binding(ComponentCommandPhase.TEST).toolchain_identity
                for item in contracts
            },
            key=lambda item: item.uri,
        )
    )

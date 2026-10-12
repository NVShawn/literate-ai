"""Bounded EXECUTE results bind current scoped authority and retained proof."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_execute_record import ExecuteWorkerInput, _pairs
from literate_ai.contracts import (
    ContentIdentity,
    StandardExecutionEvidence,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.standard_execution_inputs import (
    standard_execution_runtime_identity,
)


def _invalid():
    raise ActionWireError(
        "action_execute.result_invalid", "EXECUTE result authority refused"
    )


@dataclass(frozen=True)
class ExecuteWorkerResult:
    input_identity: ContentIdentity
    evidence: StandardExecutionEvidence
    evidence_records: tuple[BlobRef, ...]

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/execute-worker-result@1",
                input_identity=self.input_identity.uri,
                evidence=self.evidence.to_dict(),
                evidence_records=[item.to_dict() for item in self.evidence_records],
            )
        )

    @classmethod
    def admit(cls, content, identity, *, input_record, input_identity, deadline):
        admitted = ExecuteWorkerInput.admit(input_record, input_identity, deadline)
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_ACTION_RECORD_BYTES
            or not isinstance(identity, ContentIdentity)
            or record_identity(content) != identity
        ):
            _invalid()
        try:
            value = json.loads(content, object_pairs_hook=_pairs)
            if (
                not isinstance(value, dict)
                or set(value)
                != {"schema", "input_identity", "evidence", "evidence_records"}
                or value["schema"] != "literate-ai/execute-worker-result@1"
                or canonical_json_bytes(value) != content
            ):
                _invalid()
            refs = value["evidence_records"]
            if (
                not isinstance(refs, list)
                or not 1 <= len(refs) <= MAX_BUILD_EVIDENCE_RECORDS
            ):
                _invalid()
            result = cls(
                ContentIdentity.parse_uri(value["input_identity"]),
                StandardExecutionEvidence.from_dict(value["evidence"]),
                tuple(BlobRef.from_dict(item) for item in refs),
            )
            evidence = result.evidence
            authority = evidence.execution_authority
            build = admitted.build_result.evidence
            contract = admitted.build_input.inputs.contract
            identities = tuple(item.identity for item in result.evidence_records)
            if (
                result.input_identity != input_identity
                or any(
                    item.size > MAX_ACTION_RECORD_BYTES
                    for item in result.evidence_records
                )
                or sum(item.size for item in result.evidence_records)
                > MAX_BUILD_EVIDENCE_BYTES
                or identities != tuple(sorted(set(identities)))
                or evidence.identity.uri not in identities
                or evidence.component_revision != build.component_revision
                or evidence.build_evidence_identity != build.identity
                or evidence.export_identities != build.export_identities
                or evidence.artifact_custody_identity != build.artifact_custody_identity
                or evidence.provider_artifact_identities
                != admitted.scope.provider_artifact_identities
                or authority is None
                or authority.input_scope != admitted.scope
                or authority.source_tree_identity != build.source_tree_identity
                or authority.command_contract_identity != contract.identity
                or authority.runtime_identity
                != standard_execution_runtime_identity(contract)
            ):
                _invalid()
            evidence.require_authorized(now=datetime.now(UTC))
            deadline.remaining()
            return result
        except ActionWireError:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            UnicodeError,
            RecursionError,
        ) as exc:
            raise ActionWireError(
                "action_execute.result_invalid", "EXECUTE result refused"
            ) from exc

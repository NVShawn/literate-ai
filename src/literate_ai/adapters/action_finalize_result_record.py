"""Bounded FINALIZE result descriptors; referenced proof still needs verification."""

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
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_package_execution import PackageWorkerResult
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)


@dataclass(frozen=True)
class FinalizeWorkerResult:
    input_identity: ContentIdentity
    evidence: StandardRootIntegrationEvidence
    evidence_records: tuple[BlobRef, ...]

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/finalize-worker-result@1",
                input_identity=self.input_identity.uri,
                evidence=self.evidence.to_dict(),
                evidence_records=[ref.to_dict() for ref in self.evidence_records],
            )
        )

    @classmethod
    def admit(cls, content, identity, *, input_record, input_identity, deadline):
        """Check closed descriptors and exact request binding, not oracle truth."""
        value = FinalizeWorkerInput.admit(input_record, input_identity, deadline)
        try:
            if (
                not isinstance(content, bytes)
                or len(content) > MAX_ACTION_RECORD_BYTES
                or not isinstance(identity, ContentIdentity)
                or record_identity(content) != identity
            ):
                raise ValueError("invalid result bytes")
            doc = json.loads(content)
            if (
                not isinstance(doc, dict)
                or set(doc)
                != {"schema", "input_identity", "evidence", "evidence_records"}
                or doc["schema"] != "literate-ai/finalize-worker-result@1"
                or not isinstance(doc["evidence_records"], list)
                or not 1 <= len(doc["evidence_records"]) <= MAX_BUILD_EVIDENCE_RECORDS
            ):
                raise ValueError("invalid result envelope")
            result = cls(
                ContentIdentity.parse_uri(doc["input_identity"]),
                StandardRootIntegrationEvidence.from_dict(doc["evidence"]),
                tuple(BlobRef.from_dict(ref) for ref in doc["evidence_records"]),
            )
            evidence = result.evidence
            package_input = value.package_input
            package_record = PackageWorkerResult(
                record_identity(package_input.to_bytes()), evidence.package_result
            ).to_bytes()
            refs = result.evidence_records
            identities = tuple(ref.identity for ref in refs)
            required = (
                evidence.identity,
                evidence.root_generated_integration_test_identity,
                evidence.packaged_execution_identity,
                evidence.independent_acceptance_identity,
            )
            if (
                result.to_bytes() != content
                or result.input_identity != input_identity
                or evidence.component_lock_identity != value.component_lock.identity
                or evidence.execution_plan_identity
                != package_input.execution_plan.identity
                or evidence.project_build_plan_identity != value.project_plan.identity
                or evidence.artifact_graph != package_input.artifact_graph
                or evidence.package_plan != package_input.plan
                or record_identity(package_record) != value.package_result_identity
                or identities != tuple(sorted(set(identities)))
                or any(item.uri not in identities for item in required)
                or any(ref.size > MAX_ACTION_RECORD_BYTES for ref in refs)
                or sum(ref.size for ref in refs) > MAX_BUILD_EVIDENCE_BYTES
            ):
                raise ValueError("result differs from exact request or bounds")
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ActionWireError(
                "action_finalize.result_invalid", "FINALIZE result refused"
            ) from exc
        deadline.remaining()
        return result

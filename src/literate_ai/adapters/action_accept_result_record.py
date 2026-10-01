"""Bounded ACCEPT results compose exact requested stages under the Standard policy."""

import json
from dataclasses import dataclass

from literate_ai.adapters.action_accept_record import AcceptWorkerInput, _pairs
from literate_ai.adapters.action_build_limits import (
    MAX_BUILD_EVIDENCE_BYTES,
    MAX_BUILD_EVIDENCE_RECORDS,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
    StandardComponentAcceptanceEvidence,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef


def _invalid():
    raise ActionWireError(
        "action_accept.result_invalid", "ACCEPT result authority refused"
    )


@dataclass(frozen=True)
class AcceptWorkerResult:
    input_identity: ContentIdentity
    evidence: StandardComponentAcceptanceEvidence
    evidence_records: tuple[BlobRef, ...]

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema="literate-ai/accept-worker-result@1",
                input_identity=self.input_identity.uri,
                evidence=self.evidence.to_dict(),
                evidence_records=[item.to_dict() for item in self.evidence_records],
            )
        )

    @classmethod
    def admit(cls, content, identity, *, input_record, input_identity, deadline):
        admitted = AcceptWorkerInput.admit(input_record, input_identity, deadline)
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
                or value["schema"] != "literate-ai/accept-worker-result@1"
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
                StandardComponentAcceptanceEvidence.from_dict(value["evidence"]),
                tuple(BlobRef.from_dict(ref) for ref in refs),
            )
            evidence = result.evidence
            build = admitted.execution_input.build_input
            identities = tuple(ref.identity for ref in result.evidence_records)
            required = (
                evidence.identity,
                evidence.acceptance_policy_identity,
                evidence.build.identity,
                evidence.generated_tests.identity,
                evidence.execution.identity,
            )
            if (
                result.input_identity != input_identity
                or evidence.build != admitted.execution_input.build_result.evidence
                or evidence.generated_tests != admitted.test_result.evidence
                or evidence.execution != admitted.execution_result.evidence
                or evidence.source_generation_identity
                != build.source_generation_identity
                or evidence.generated_test_suite_identity
                != build.candidate.generated_test_suite_identity
                or evidence.acceptance_policy_identity
                != canonical_identity(
                    {
                        "schema": "literate-ai/local-standard-acceptance-policy@1",
                        "requires": ["build", "generated-tests", "execution"],
                    }
                )
                or identities != tuple(sorted(set(identities)))
                or any(item.uri not in identities for item in required)
                or any(
                    ref.size > MAX_ACTION_RECORD_BYTES
                    for ref in result.evidence_records
                )
                or sum(ref.size for ref in result.evidence_records)
                > MAX_BUILD_EVIDENCE_BYTES
            ):
                _invalid()
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
                "action_accept.result_invalid", "ACCEPT result refused"
            ) from exc

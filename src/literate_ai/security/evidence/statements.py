"""in-toto v1 statements for the closed Literate AI evidence predicates.

The standard outer statement ignores extension fields. Our predicates explicitly
opt out of that rule and reject unknown fields. Only these four predicates and one
SHA-256 subject are supported; this is not a general in-toto policy engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from literate_ai.contracts.identity import canonical_json_bytes

from .dsse import MAX_PAYLOAD_BYTES, DsseEnvelope
from .ed25519 import verify_ed25519_envelope
from .records import PREDICATE_TYPES, EvidencePredicate

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
STATEMENT_MEDIA_TYPE = "application/vnd.in-toto+json"


class EvidenceStatementError(ValueError):
    """Sanitized statement parsing error, without producer-controlled content."""

    def __init__(self, code: str = "evidence.statement.invalid"):
        self.code = code
        super().__init__(code)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceStatementError("evidence.statement.duplicate-json-key")
        result[key] = value
    return result


def _nonfinite(_value: str):
    raise EvidenceStatementError()


@dataclass(frozen=True, slots=True)
class EvidenceStatement:
    predicate: EvidencePredicate

    def __post_init__(self):
        if type(self.predicate) not in PREDICATE_TYPES.values():
            raise EvidenceStatementError("evidence.statement.predicate-unsupported")

    def to_dict(self) -> dict[str, object]:
        return {
            "_type": STATEMENT_TYPE,
            "subject": [
                {"name": "_", "digest": {"sha256": self.predicate.subject.digest}}
            ],
            "predicateType": self.predicate.SCHEMA,
            "predicate": self.predicate.to_dict(),
        }

    def to_bytes(self) -> bytes:
        result = canonical_json_bytes(self.to_dict())
        if len(result) > MAX_PAYLOAD_BYTES:
            raise EvidenceStatementError("evidence.statement.size-invalid")
        return result

    @classmethod
    def from_bytes(cls, payload: bytes) -> EvidenceStatement:
        if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD_BYTES:
            raise EvidenceStatementError("evidence.statement.size-invalid")
        try:
            data = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite,
            )
            if not isinstance(data, dict) or data.get("_type") != STATEMENT_TYPE:
                raise EvidenceStatementError()
            kind = data.get("predicateType")
            if not isinstance(kind, str) or kind not in PREDICATE_TYPES:
                raise EvidenceStatementError("evidence.statement.predicate-unsupported")
            predicate = PREDICATE_TYPES[kind].from_dict(data.get("predicate"))
            subjects = data.get("subject")
            if not isinstance(subjects, list) or len(subjects) != 1:
                raise EvidenceStatementError("evidence.statement.subject-invalid")
            subject = subjects[0]
            if not isinstance(subject, dict) or not isinstance(
                subject.get("digest"), dict
            ):
                raise EvidenceStatementError("evidence.statement.subject-invalid")
            if "name" in subject and not isinstance(subject["name"], str):
                raise EvidenceStatementError("evidence.statement.subject-invalid")
            if subject["digest"].get("sha256") != predicate.subject.digest:
                raise EvidenceStatementError("evidence.statement.subject-mismatch")
            return cls(predicate)
        except EvidenceStatementError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise EvidenceStatementError() from None


@dataclass(frozen=True, slots=True)
class AuthenticatedEvidenceStatement:
    """Signature-authenticated assertions, still requiring context/closure admission.

    This in-memory result retains the original bytes, including standard extension
    fields. It is not a serializable proof, receipt, or execution authorization.
    """

    statement: EvidenceStatement
    payload: bytes
    signer_key_identities: tuple[str, ...]


def verify_evidence_statement(
    envelope: DsseEnvelope,
    *,
    trusted_public_keys: tuple[bytes, ...],
    minimum_signatures: int = 1,
) -> AuthenticatedEvidenceStatement:
    """Authenticate first, parse the same bytes once, and retain them for custody."""

    verified = verify_ed25519_envelope(
        envelope,
        trusted_public_keys=trusted_public_keys,
        expected_payload_type=STATEMENT_MEDIA_TYPE,
        minimum_signatures=minimum_signatures,
    )
    return AuthenticatedEvidenceStatement(
        EvidenceStatement.from_bytes(verified.payload),
        verified.payload,
        verified.signer_key_identities,
    )

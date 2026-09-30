"""Deterministic local source and review attestations for bootstrap promotion."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .contracts import canonical_digest, canonical_value
from .errors import SourceToSpecificationError


def _require_strong_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise SourceToSpecificationError(
            "attestation.key_weak", "local trust key must contain at least 32 bytes"
        )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def read_local_trust_key(path: str | Path) -> bytes:
    """Read a high-entropy local trust key without following symbolic links."""

    key_path = Path(path)
    if key_path.is_symlink() or not key_path.is_file():
        raise SourceToSpecificationError(
            "attestation.key_unavailable", "local trust key is not a regular file"
        )
    try:
        key = key_path.read_bytes()
    except OSError as exc:
        raise SourceToSpecificationError(
            "attestation.key_unavailable", "local trust key could not be read"
        ) from exc
    _require_strong_key(key)
    return key


def _key_id(key: bytes) -> str:
    return f"sha256:{hashlib.sha256(key).hexdigest()}"


def _signature(payload: dict[str, Any], key: bytes) -> str:
    digest = hmac.new(key, _canonical_bytes(payload), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


@dataclass(frozen=True, slots=True)
class LocalSourceAttestation:
    """Actor/machine assertion over one exact static source inventory."""

    schema: ClassVar[str] = "literate-ai/local-source-attestation@1"

    source_snapshot_id: str
    signer: str
    machine: str
    key_id: str
    signature: str
    algorithm: str = "hmac-sha256"

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def payload(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "source_snapshot_id": self.source_snapshot_id,
            "signer": self.signer,
            "machine": self.machine,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
        }

    def to_dict(self) -> dict[str, str]:
        return {**self.payload(), "signature": self.signature}


def sign_source_inventory(
    source_snapshot_id: str,
    *,
    signer: str,
    machine: str,
    key: bytes,
) -> LocalSourceAttestation:
    _require_strong_key(key)
    if not signer.strip() or not machine.strip():
        raise SourceToSpecificationError(
            "attestation.identity_required", "source signer and machine are required"
        )
    unsigned = LocalSourceAttestation(
        source_snapshot_id=source_snapshot_id,
        signer=signer.strip(),
        machine=machine.strip(),
        key_id=_key_id(key),
        signature="pending",
    )
    return LocalSourceAttestation(
        source_snapshot_id=unsigned.source_snapshot_id,
        signer=unsigned.signer,
        machine=unsigned.machine,
        key_id=unsigned.key_id,
        algorithm=unsigned.algorithm,
        signature=_signature(unsigned.payload(), key),
    )


def parse_source_attestation(value: Any) -> LocalSourceAttestation:
    if not isinstance(value, dict):
        raise SourceToSpecificationError(
            "attestation.invalid", "source attestation must be an object"
        )
    expected = {
        "schema",
        "source_snapshot_id",
        "signer",
        "machine",
        "key_id",
        "signature",
        "algorithm",
    }
    if set(value) != expected or value.get("schema") != LocalSourceAttestation.schema:
        raise SourceToSpecificationError(
            "attestation.invalid", "source attestation schema or fields are invalid"
        )
    fields = expected - {"schema"}
    if not all(isinstance(value[field], str) and value[field] for field in fields):
        raise SourceToSpecificationError(
            "attestation.invalid", "source attestation fields are invalid"
        )
    return LocalSourceAttestation(
        source_snapshot_id=value["source_snapshot_id"],
        signer=value["signer"],
        machine=value["machine"],
        key_id=value["key_id"],
        signature=value["signature"],
        algorithm=value["algorithm"],
    )


def verify_source_attestation(
    attestation: LocalSourceAttestation,
    *,
    expected_source_snapshot_id: str,
    key: bytes,
) -> None:
    _require_strong_key(key)
    if attestation.algorithm != "hmac-sha256" or attestation.key_id != _key_id(key):
        raise SourceToSpecificationError(
            "attestation.trust_mismatch", "source attestation uses another trust key"
        )
    if attestation.source_snapshot_id != expected_source_snapshot_id:
        raise SourceToSpecificationError(
            "attestation.source_mismatch", "source attestation targets another snapshot"
        )
    expected_signature = _signature(attestation.payload(), key)
    if not hmac.compare_digest(attestation.signature, expected_signature):
        raise SourceToSpecificationError(
            "attestation.signature_invalid", "source attestation signature is invalid"
        )


def sign_review(review: dict[str, Any], *, actor: str, key: bytes) -> dict[str, str]:
    _require_strong_key(key)
    if not actor.strip():
        raise SourceToSpecificationError(
            "review.actor_required", "review attestation actor is required"
        )
    review_digest = canonical_digest(review)
    payload = {
        "schema": "literate-ai/local-review-attestation@1",
        "review_digest": review_digest,
        "actor": actor,
        "key_id": _key_id(key),
        "algorithm": "hmac-sha256",
    }
    return {**payload, "signature": _signature(payload, key)}


def verify_review_attestation(
    value: Any,
    *,
    review: dict[str, Any],
    actor: str,
    key: bytes,
) -> None:
    _require_strong_key(key)
    if not isinstance(value, dict):
        raise SourceToSpecificationError(
            "review.attestation_required", "signed local review is required"
        )
    expected = {
        "schema",
        "review_digest",
        "actor",
        "key_id",
        "algorithm",
        "signature",
    }
    if set(value) != expected:
        raise SourceToSpecificationError(
            "review.attestation_invalid", "review attestation fields are invalid"
        )
    payload = {key_name: value[key_name] for key_name in expected - {"signature"}}
    valid = (
        value.get("schema") == "literate-ai/local-review-attestation@1"
        and value.get("algorithm") == "hmac-sha256"
        and value.get("review_digest") == canonical_digest(review)
        and value.get("actor") == actor
        and value.get("key_id") == _key_id(key)
        and isinstance(value.get("signature"), str)
        and hmac.compare_digest(value["signature"], _signature(payload, key))
    )
    if not valid:
        raise SourceToSpecificationError(
            "review.attestation_invalid", "review attestation signature is invalid"
        )


__all__ = [
    "LocalSourceAttestation",
    "parse_source_attestation",
    "read_local_trust_key",
    "sign_review",
    "sign_source_inventory",
    "verify_review_attestation",
    "verify_source_attestation",
]

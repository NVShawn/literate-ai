"""Local Ed25519 DSSE signatures over exact retained bytes using PyCA.

Explicit trusted public keys are required for every verification. Key-ID hints
never grant trust. A valid signature alone does not admit a run, a grant, or a
receipt: subject, context, validity, revocation and retention policy are separate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .dsse import MAX_SIGNATURES, DsseEnvelope, DsseError, DsseSignature
from .dsse import preauthentication_bytes as _pae


def _key_identity(public_key: bytes) -> str:
    return "sha256:" + hashlib.sha256(public_key).hexdigest()


class Ed25519EvidenceSigner:
    """Explicit in-memory private key; no ambient key loading or publication."""

    def __init__(self, private_key: bytes):
        if not isinstance(private_key, bytes) or len(private_key) != 32:
            raise DsseError("evidence.ed25519.private-key-invalid")
        self._key = Ed25519PrivateKey.from_private_bytes(private_key)

    @property
    def public_key(self) -> bytes:
        return self._key.public_key().public_bytes_raw()

    @property
    def key_identity(self) -> str:
        return _key_identity(self.public_key)

    def sign(self, payload_type: str, payload: bytes) -> DsseEnvelope:
        signed = self._key.sign(_pae(payload_type, payload))
        return DsseEnvelope(
            payload_type, payload, (DsseSignature(signed, self.key_identity),)
        )


@dataclass(frozen=True, slots=True)
class VerifiedDssePayload:
    """Verification return value, not a serializable proof accepted from callers."""

    payload_type: str
    payload: bytes
    signer_key_identities: tuple[str, ...]


def verify_ed25519_envelope(
    envelope: DsseEnvelope,
    *,
    trusted_public_keys: tuple[bytes, ...],
    expected_payload_type: str,
    minimum_signatures: int = 1,
) -> VerifiedDssePayload:
    """Verify a threshold of distinct trusted keys and return the same payload.

    The caller supplies the currently trusted keys after its own trust-policy checks.
    Duplicate signatures or aliases of one public key cannot satisfy a higher threshold.
    """

    if not isinstance(envelope, DsseEnvelope):
        raise DsseError("evidence.dsse.envelope-invalid")
    if not isinstance(expected_payload_type, str) or not expected_payload_type:
        raise DsseError("evidence.dsse.expected-type-required")
    if envelope.payload_type != expected_payload_type:
        raise DsseError("evidence.dsse.payload-type-mismatch")
    if (
        not isinstance(trusted_public_keys, tuple)
        or not trusted_public_keys
        or len(trusted_public_keys) > MAX_SIGNATURES
        or any(
            not isinstance(key, bytes) or len(key) != 32 for key in trusted_public_keys
        )
    ):
        raise DsseError("evidence.ed25519.trusted-keys-invalid")
    keys = set(trusted_public_keys)
    if (
        isinstance(minimum_signatures, bool)
        or not isinstance(minimum_signatures, int)
        or not 1 <= minimum_signatures <= len(keys)
    ):
        raise DsseError("evidence.dsse.threshold-invalid")
    message = _pae(envelope.payload_type, envelope.payload)
    verified = set()
    for raw_key in keys:
        public_key = Ed25519PublicKey.from_public_bytes(raw_key)
        for signature in envelope.signatures:
            try:
                public_key.verify(signature.signature, message)
            except InvalidSignature:
                continue
            verified.add(_key_identity(raw_key))
            break
    if len(verified) < minimum_signatures:
        raise DsseError("evidence.dsse.trusted-signature-threshold-unmet")
    return VerifiedDssePayload(
        envelope.payload_type, envelope.payload, tuple(sorted(verified))
    )

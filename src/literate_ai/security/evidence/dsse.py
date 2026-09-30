"""Bounded DSSE 1.0.2 envelopes with the external protocol's parsing semantics.

See https://github.com/secure-systems-lab/dsse/blob/master/envelope.md and
https://github.com/secure-systems-lab/dsse/blob/master/protocol.md.
Parsing never establishes signer trust or authorizes execution.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE_BYTES = 16 * 1024 * 1024
MAX_SIGNATURES = 64
MAX_SIGNATURE_BYTES = 4096
MAX_KEY_ID_BYTES = 512
MAX_PAYLOAD_TYPE_BYTES = 1024


class DsseError(ValueError):
    """A malformed or unverifiable envelope, without echoing untrusted payloads."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _text(value: object, limit: int, code: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise DsseError(code)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise DsseError(code) from None
    if len(encoded) > limit:
        raise DsseError(code)
    return value


def _bytes(value: object, limit: int, code: str) -> bytes:
    if not isinstance(value, bytes) or len(value) > limit:
        raise DsseError(code)
    return value


def _decode(value: object, limit: int, code: str) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        raise DsseError(code)
    try:
        content = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise DsseError(code) from None
    return _bytes(content, limit, code)


def preauthentication_bytes(payload_type: str, payload: bytes) -> bytes:
    """Encode the exact payload type and bytes using DSSE's length framing."""

    kind = _text(
        payload_type, MAX_PAYLOAD_TYPE_BYTES, "evidence.dsse.payload-type-invalid"
    ).encode("utf-8")
    body = _bytes(payload, MAX_PAYLOAD_BYTES, "evidence.dsse.payload-invalid")
    return b" ".join(
        (
            b"DSSEv1",
            str(len(kind)).encode("ascii"),
            kind,
            str(len(body)).encode("ascii"),
            body,
        )
    )


@dataclass(frozen=True, slots=True)
class DsseSignature:
    signature: bytes
    key_id: str = ""

    def __post_init__(self):
        _bytes(self.signature, MAX_SIGNATURE_BYTES, "evidence.dsse.signature-invalid")
        _text(self.key_id, MAX_KEY_ID_BYTES, "evidence.dsse.key-id-invalid")

    def to_dict(self) -> dict[str, str]:
        result = {"sig": base64.b64encode(self.signature).decode("ascii")}
        if self.key_id:
            result["keyid"] = self.key_id
        return result

    @classmethod
    def from_dict(cls, value: object) -> DsseSignature:
        if not isinstance(value, dict) or "sig" not in value:
            raise DsseError("evidence.dsse.signature-invalid")
        # DSSE requires ignoring extension fields, unlike closed run predicates.
        return cls(
            _decode(
                value["sig"], MAX_SIGNATURE_BYTES, "evidence.dsse.signature-invalid"
            ),
            _text(
                value.get("keyid", ""), MAX_KEY_ID_BYTES, "evidence.dsse.key-id-invalid"
            ),
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DsseError("evidence.dsse.duplicate-json-key")
        result[key] = value
    return result


def _nonfinite(_value: str):
    raise DsseError("evidence.dsse.json-invalid")


@dataclass(frozen=True, slots=True)
class DsseEnvelope:
    payload_type: str
    payload: bytes
    signatures: tuple[DsseSignature, ...]

    def __post_init__(self):
        _text(
            self.payload_type,
            MAX_PAYLOAD_TYPE_BYTES,
            "evidence.dsse.payload-type-invalid",
        )
        _bytes(self.payload, MAX_PAYLOAD_BYTES, "evidence.dsse.payload-invalid")
        if (
            not isinstance(self.signatures, tuple)
            or len(self.signatures) > MAX_SIGNATURES
            or any(not isinstance(item, DsseSignature) for item in self.signatures)
        ):
            raise DsseError("evidence.dsse.signatures-invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "payload": base64.b64encode(self.payload).decode("ascii"),
            "payloadType": self.payload_type,
            "signatures": [item.to_dict() for item in self.signatures],
        }

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, value: object) -> DsseEnvelope:
        if not isinstance(value, dict) or not {
            "payload",
            "payloadType",
            "signatures",
        }.issubset(value):
            raise DsseError("evidence.dsse.envelope-invalid")
        signatures = value["signatures"]
        if not isinstance(signatures, list) or len(signatures) > MAX_SIGNATURES:
            raise DsseError("evidence.dsse.signatures-invalid")
        return cls(
            _text(
                value["payloadType"],
                MAX_PAYLOAD_TYPE_BYTES,
                "evidence.dsse.payload-type-invalid",
            ),
            _decode(
                value["payload"], MAX_PAYLOAD_BYTES, "evidence.dsse.payload-invalid"
            ),
            tuple(DsseSignature.from_dict(item) for item in signatures),
        )

    @classmethod
    def from_bytes(cls, value: bytes) -> DsseEnvelope:
        _bytes(value, MAX_ENVELOPE_BYTES, "evidence.dsse.envelope-size-invalid")
        try:
            parsed = json.loads(
                value.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite,
            )
        except DsseError:
            raise
        except (ValueError, RecursionError):
            raise DsseError("evidence.dsse.json-invalid") from None
        return cls.from_dict(parsed)

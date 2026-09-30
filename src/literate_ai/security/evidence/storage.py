"""Evidence storage ports and bounded verified bytes, without producer admission."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from literate_ai.contracts._validation import int_value
from literate_ai.contracts.blobs import BlobRef

from .records import EvidenceArtifact, EvidenceLocator


class EvidenceStorageError(ValueError):
    """Stable sanitized errors; no paths, URLs, headers or evidence content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class EvidenceNotFoundError(EvidenceStorageError):
    def __init__(self):
        super().__init__("evidence.storage.not-found")


@dataclass(frozen=True, slots=True)
class EvidenceReadLimits:
    maximum_blob_bytes: int = 64 * 1024 * 1024
    maximum_total_bytes: int = 256 * 1024 * 1024
    maximum_objects: int = 1024

    def __post_init__(self):
        int_value(
            self.maximum_blob_bytes,
            "evidence.maximum_blob_bytes",
            minimum=1,
            maximum=2**31,
        )
        int_value(
            self.maximum_total_bytes,
            "evidence.maximum_total_bytes",
            minimum=self.maximum_blob_bytes,
            maximum=2**34,
        )
        int_value(
            self.maximum_objects, "evidence.maximum_objects", minimum=1, maximum=65536
        )

    def require_reference(self, reference: BlobRef) -> None:
        if not isinstance(reference, BlobRef):
            raise EvidenceStorageError("evidence.storage.reference-invalid")
        if reference.size > self.maximum_blob_bytes:
            raise EvidenceStorageError("evidence.storage.blob-limit")
        try:
            EvidenceArtifact("object", reference)
        except ValueError:
            raise EvidenceStorageError("evidence.storage.reference-invalid") from None


DEFAULT_EVIDENCE_READ_LIMITS = EvidenceReadLimits()


def verify_evidence_bytes(reference: BlobRef, content: bytes) -> bytes:
    """Verify the bytes that will be consumed, never a separate read or path."""

    if not isinstance(content, bytes) or len(content) != reference.size:
        raise EvidenceStorageError("evidence.storage.size-mismatch")
    if hashlib.sha256(content).hexdigest() != reference.digest:
        raise EvidenceStorageError("evidence.storage.digest-mismatch")
    return content


class EvidenceStore(Protocol):
    """An explicitly configured immutable-object store; reads enforce local limits."""

    def get_bytes(self, reference: BlobRef) -> bytes: ...

    def put_bytes(self, content: bytes, *, media_type: str) -> BlobRef: ...


@dataclass(frozen=True, slots=True)
class ResolvedEvidence:
    reference: BlobRef
    content: bytes
    locator: EvidenceLocator

    def __post_init__(self):
        if (
            not isinstance(self.locator, EvidenceLocator)
            or self.locator.subject != self.reference
        ):
            raise EvidenceStorageError("evidence.storage.locator-mismatch")
        verify_evidence_bytes(self.reference, self.content)


class EvidenceResolver(Protocol):
    """Resolve a finite explicitly required set; no implicit remote discovery."""

    def resolve_many(
        self,
        references: tuple[BlobRef, ...],
        *,
        locators: tuple[EvidenceLocator, ...],
    ) -> tuple[ResolvedEvidence, ...]: ...

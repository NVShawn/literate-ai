"""Compact current-evidence indexes; their references must be reverified on use."""

from dataclasses import dataclass
from typing import ClassVar

from literate_ai.contracts._validation import contract_fields, list_value
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .records import DSSE_MEDIA_TYPE

PLAN_MEDIA_TYPE = "application/vnd.literate-ai.evidence-verification-plan.v2+json"


@dataclass(frozen=True, slots=True)
class CurrentEvidenceMap:
    """An index of immutable evidence, never a serialized authentication grant.

    Independent project/plan/policy authority and fresh graph verification are
    required on use. The map contains no timestamps, histories, raw logs or tokens.
    """

    project_authority: ContentIdentity
    trust_policy: ContentIdentity
    receipt: BlobRef
    plan: BlobRef
    matrix: BlobRef
    retention_roots: tuple[BlobRef, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:current-evidence-map"

    def __post_init__(self):
        if (
            not isinstance(self.project_authority, ContentIdentity)
            or not isinstance(self.trust_policy, ContentIdentity)
            or any(
                not isinstance(r, BlobRef)
                for r in (self.receipt, self.plan, self.matrix)
            )
            or self.receipt.media_type != "application/json"
            or self.plan.media_type != PLAN_MEDIA_TYPE
            or self.matrix.media_type != DSSE_MEDIA_TYPE
            or not isinstance(self.retention_roots, tuple)
            or not 1 <= len(self.retention_roots) <= 1024
            or any(
                not isinstance(r, BlobRef) or r.media_type != DSSE_MEDIA_TYPE
                for r in self.retention_roots
            )
        ):
            raise ValueError("evidence.current.invalid")
        identities = tuple(r.identity for r in self.retention_roots)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("evidence.current.invalid")

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "project_authority": self.project_authority.to_dict(),
            "trust_policy": self.trust_policy.to_dict(),
            "receipt": self.receipt.to_dict(),
            "plan": self.plan.to_dict(),
            "matrix": self.matrix.to_dict(),
            "retention_roots": [r.to_dict() for r in self.retention_roots],
        }

    @classmethod
    def from_dict(cls, value):
        d = contract_fields(
            value,
            path="CurrentEvidenceMap",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "project_authority",
                    "trust_policy",
                    "receipt",
                    "plan",
                    "matrix",
                    "retention_roots",
                }
            ),
        )
        roots = list_value(d["retention_roots"], "retention_roots")
        if not 1 <= len(roots) <= 1024:
            raise ValueError("evidence.current.invalid")
        return cls(
            ContentIdentity.from_dict(d["project_authority"]),
            ContentIdentity.from_dict(d["trust_policy"]),
            BlobRef.from_dict(d["receipt"]),
            BlobRef.from_dict(d["plan"]),
            BlobRef.from_dict(d["matrix"]),
            tuple(BlobRef.from_dict(r) for r in roots),
        )

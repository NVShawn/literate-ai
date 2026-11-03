"""Explicit original-package assertions; decoding grants no live authority."""

from dataclasses import dataclass

from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy

ORIGINAL_EVIDENCE_SCHEMA = "literate-ai/retained-unit-original-verification-evidence@1"
ORIGINAL_VERIFICATION_MODE = "original-bytes-with-fresh-admission"
ORIGINAL_EVIDENCE_ROLES = (
    "execution_request",
    "retained_receipt",
    "planning_evidence",
    "accepted_run_evidence",
    "link_proof",
    "result_index",
)


@dataclass(frozen=True, slots=True)
class OriginalRetainedUnitEvidence:
    """Named bounded metadata roots, never a retained/SDK/LINK capability."""

    retained_plan_identity: ContentIdentity
    worker_identity: ContentIdentity
    execution_request: BlobRef
    retained_receipt: BlobRef
    planning_evidence: BlobRef
    accepted_run_evidence: BlobRef
    link_proof: BlobRef
    result_index: BlobRef

    def __post_init__(self):
        if not all(
            isinstance(value, ContentIdentity)
            for value in (self.retained_plan_identity, self.worker_identity)
        ):
            raise TypeError("retained.unit.typed-original-context-required")
        for role in ORIGINAL_EVIDENCE_ROLES:
            reference = getattr(self, role)
            if (
                not isinstance(reference, BlobRef)
                or reference.media_type != "application/json"
                or not 0 < reference.size <= 64 * 1024**2
            ):
                raise ValueError("retained.unit.bounded-original-evidence-required")

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def to_dict(self):
        return {
            "schema": ORIGINAL_EVIDENCE_SCHEMA,
            "authority_mode": "retained-project",
            "retained_plan_identity": self.retained_plan_identity.uri,
            "worker_identity": self.worker_identity.uri,
            "roots": {
                role: getattr(self, role).to_dict() for role in ORIGINAL_EVIDENCE_ROLES
            },
            "admitted": False,
            "release_authorized": False,
        }

    @classmethod
    def from_dict(cls, value):
        if (
            type(value) is not dict
            or set(value)
            != {
                "schema",
                "authority_mode",
                "retained_plan_identity",
                "worker_identity",
                "roots",
                "admitted",
                "release_authorized",
            }
            or value["schema"] != ORIGINAL_EVIDENCE_SCHEMA
            or value["authority_mode"] != "retained-project"
            or value["admitted"] is not False
            or value["release_authorized"] is not False
            or type(value["roots"]) is not dict
            or set(value["roots"]) != set(ORIGINAL_EVIDENCE_ROLES)
        ):
            raise ValueError("retained.unit.closed-original-evidence-required")
        return cls(
            ContentIdentity.parse_uri(value["retained_plan_identity"]),
            ContentIdentity.parse_uri(value["worker_identity"]),
            *(
                BlobRef.from_dict(value["roots"][role])
                for role in ORIGINAL_EVIDENCE_ROLES
            ),
        )

    def require_budget(self, policy):
        if not isinstance(policy, RetainedUnitPackagePolicy):
            raise TypeError("retained.unit.explicit-verification-policy-required")
        references = {}
        for role in ORIGINAL_EVIDENCE_ROLES:
            reference = getattr(self, role)
            previous = references.get(reference.identity)
            if previous is not None and previous != reference:
                raise ValueError("retained.unit.original-evidence-reference-differs")
            references[reference.identity] = reference
        total = sum(reference.size for reference in references.values())
        if (
            self.result_index.size > policy.max_index_bytes
            or self.planning_evidence.size
            > min(8 * 1024**2, policy.max_provider_metadata_bytes)
            or total > policy.max_provider_metadata_bytes
            or 16 * total > policy.max_provider_memory_bytes
        ):
            raise ValueError("retained.unit.original-evidence-policy-exceeded")

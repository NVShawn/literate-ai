"""Version-2 contracts for explicitly reviewed specification refinements.

These contracts deliberately do not attempt to prove that a narrower document cannot
contradict an ancestor.  A coding model may surface a conflict and propose text, but
that output remains unverified until an exact, separately authored review accepts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    parse_tuple,
    string_value,
    unique,
)
from .identity import (
    SCHEMA_V2_PREFIX,
    ContentIdentity,
    ContentReference,
    contract_identity,
)

SEMANTIC_CONFLICT_SCHEMA = f"{SCHEMA_V2_PREFIX}semantic-conflict"
SEMANTIC_REFINEMENT_PROPOSAL_SCHEMA = f"{SCHEMA_V2_PREFIX}semantic-refinement-proposal"
SEMANTIC_REFINEMENT_REVIEW_SCHEMA = f"{SCHEMA_V2_PREFIX}semantic-refinement-review"
SEMANTIC_REFINEMENT_REQUEST_SCHEMA = f"{SCHEMA_V2_PREFIX}semantic-refinement-request"
SEMANTIC_REFINEMENT_RESOLUTION_SCHEMA = (
    f"{SCHEMA_V2_PREFIX}semantic-refinement-resolution"
)


class ProposalVerification(StrEnum):
    UNVERIFIED = "unverified"


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


def _references(
    value: Any, path: str, *, minimum: int = 0
) -> tuple[ContentReference, ...]:
    result = parse_tuple(value, path, ContentReference.from_dict)
    if len(result) < minimum:
        fail(path, f"must contain at least {minimum} references")
    unique(tuple(item.uri for item in result), path, "reference URIs")
    unique(tuple(item.identity.uri for item in result), path, "reference identities")
    return result


def _identities(
    value: Any, path: str, *, minimum: int = 0
) -> tuple[ContentIdentity, ...]:
    result = parse_tuple(value, path, ContentIdentity.from_dict)
    if len(result) < minimum:
        fail(path, f"must contain at least {minimum} identities")
    unique(tuple(item.uri for item in result), path, "content identities")
    return result


def _require_kind(reference: ContentReference, kind: str, path: str) -> None:
    if reference.kind != kind:
        fail(path, f"kind must be {kind!r}")


@dataclass(frozen=True, slots=True)
class SemanticConflict:
    """One model-identified conflict; its existence is not yet verified."""

    conflict_id: str
    documents: tuple[ContentReference, ...]
    model: ContentReference
    evidence: tuple[ContentReference, ...]
    description: str
    verification: ProposalVerification = ProposalVerification.UNVERIFIED

    SCHEMA: ClassVar[str] = SEMANTIC_CONFLICT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.conflict_id, "SemanticConflict.conflict_id")
        if len(self.documents) < 2:
            fail("SemanticConflict.documents", "must contain at least two documents")
        unique(
            tuple(item.uri for item in self.documents),
            "SemanticConflict.documents",
            "document URIs",
        )
        unique(
            tuple(item.identity.uri for item in self.documents),
            "SemanticConflict.documents",
            "document identities",
        )
        for index, document in enumerate(self.documents):
            _require_kind(
                document,
                "specification-document",
                f"SemanticConflict.documents[{index}]",
            )
        _require_kind(self.model, "coding-model", "SemanticConflict.model")
        if not self.evidence:
            fail("SemanticConflict.evidence", "must not be empty")
        unique(
            tuple(item.uri for item in self.evidence),
            "SemanticConflict.evidence",
            "evidence URIs",
        )
        unique(
            tuple(item.identity.uri for item in self.evidence),
            "SemanticConflict.evidence",
            "evidence identities",
        )
        string_value(self.description, "SemanticConflict.description", max_length=65536)
        if self.verification is not ProposalVerification.UNVERIFIED:
            fail(
                "SemanticConflict.verification",
                "model-identified conflicts must remain unverified",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "conflict_id": self.conflict_id,
            "documents": [
                item.to_dict()
                for item in sorted(self.documents, key=lambda item: item.uri)
            ],
            "model": self.model.to_dict(),
            "evidence": [
                item.to_dict()
                for item in sorted(self.evidence, key=lambda item: item.uri)
            ],
            "description": self.description,
            "verification": self.verification.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SemanticConflict"
    ) -> SemanticConflict:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "conflict_id",
                    "documents",
                    "model",
                    "evidence",
                    "description",
                    "verification",
                }
            ),
        )
        return cls(
            conflict_id=string_value(data["conflict_id"], f"{path}.conflict_id"),
            documents=_references(data["documents"], f"{path}.documents", minimum=2),
            model=ContentReference.from_dict(data["model"], path=f"{path}.model"),
            evidence=_references(data["evidence"], f"{path}.evidence", minimum=1),
            description=string_value(
                data["description"], f"{path}.description", max_length=65536
            ),
            verification=enum_value(
                ProposalVerification, data["verification"], f"{path}.verification"
            ),
        )


@dataclass(frozen=True, slots=True)
class SemanticRefinementProposal:
    """Model-authored refinement text with no review authority."""

    proposal_id: str
    conflict_id: str
    conflict_identity: ContentIdentity
    replacement: str
    verification: ProposalVerification = ProposalVerification.UNVERIFIED

    SCHEMA: ClassVar[str] = SEMANTIC_REFINEMENT_PROPOSAL_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.proposal_id, "SemanticRefinementProposal.proposal_id")
        string_value(self.conflict_id, "SemanticRefinementProposal.conflict_id")
        if not isinstance(self.conflict_identity, ContentIdentity):
            fail(
                "SemanticRefinementProposal.conflict_identity",
                "must be a ContentIdentity",
            )
        if self.verification is not ProposalVerification.UNVERIFIED:
            fail(
                "SemanticRefinementProposal.verification",
                "model proposals must remain unverified",
            )
        string_value(
            self.replacement, "SemanticRefinementProposal.replacement", max_length=65536
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "proposal_id": self.proposal_id,
            "conflict_id": self.conflict_id,
            "conflict_identity": self.conflict_identity.to_dict(),
            "replacement": self.replacement,
            "verification": self.verification.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SemanticRefinementProposal"
    ) -> SemanticRefinementProposal:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "proposal_id",
                    "conflict_id",
                    "conflict_identity",
                    "replacement",
                    "verification",
                }
            ),
        )
        return cls(
            proposal_id=string_value(data["proposal_id"], f"{path}.proposal_id"),
            conflict_id=string_value(data["conflict_id"], f"{path}.conflict_id"),
            conflict_identity=ContentIdentity.from_dict(
                data["conflict_identity"], path=f"{path}.conflict_identity"
            ),
            replacement=string_value(
                data["replacement"], f"{path}.replacement", max_length=65536
            ),
            verification=enum_value(
                ProposalVerification, data["verification"], f"{path}.verification"
            ),
        )


@dataclass(frozen=True, slots=True)
class RefinementReviewBinding:
    """The complete immutable input set a reviewer actually considered."""

    proposal_identity: ContentIdentity
    document_identities: tuple[ContentIdentity, ...]
    model_identity: ContentIdentity
    evidence_identities: tuple[ContentIdentity, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_identity, ContentIdentity):
            fail(
                "RefinementReviewBinding.proposal_identity", "must be a ContentIdentity"
            )
        if len(self.document_identities) < 2:
            fail(
                "RefinementReviewBinding.document_identities",
                "must contain at least two identities",
            )
        unique(
            tuple(item.uri for item in self.document_identities),
            "RefinementReviewBinding.document_identities",
            "document identities",
        )
        if not isinstance(self.model_identity, ContentIdentity):
            fail("RefinementReviewBinding.model_identity", "must be a ContentIdentity")
        if not self.evidence_identities:
            fail("RefinementReviewBinding.evidence_identities", "must not be empty")
        unique(
            tuple(item.uri for item in self.evidence_identities),
            "RefinementReviewBinding.evidence_identities",
            "evidence identities",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_identity": self.proposal_identity.to_dict(),
            "document_identities": [
                item.to_dict()
                for item in sorted(self.document_identities, key=lambda item: item.uri)
            ],
            "model_identity": self.model_identity.to_dict(),
            "evidence_identities": [
                item.to_dict()
                for item in sorted(self.evidence_identities, key=lambda item: item.uri)
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RefinementReviewBinding"
    ) -> RefinementReviewBinding:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "proposal_identity",
                    "document_identities",
                    "model_identity",
                    "evidence_identities",
                }
            ),
        )
        return cls(
            proposal_identity=ContentIdentity.from_dict(
                data["proposal_identity"], path=f"{path}.proposal_identity"
            ),
            document_identities=_identities(
                data["document_identities"], f"{path}.document_identities", minimum=2
            ),
            model_identity=ContentIdentity.from_dict(
                data["model_identity"], path=f"{path}.model_identity"
            ),
            evidence_identities=_identities(
                data["evidence_identities"], f"{path}.evidence_identities", minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class SemanticRefinementReview:
    review_id: str
    conflict_id: str
    proposal_id: str
    binding: RefinementReviewBinding
    reviewer: ContentReference
    decision: ReviewDecision
    rationale: str

    SCHEMA: ClassVar[str] = SEMANTIC_REFINEMENT_REVIEW_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.review_id, "SemanticRefinementReview.review_id")
        string_value(self.conflict_id, "SemanticRefinementReview.conflict_id")
        string_value(self.proposal_id, "SemanticRefinementReview.proposal_id")
        _require_kind(self.reviewer, "reviewer", "SemanticRefinementReview.reviewer")
        if not isinstance(self.decision, ReviewDecision):
            fail("SemanticRefinementReview.decision", "must be a ReviewDecision")
        string_value(
            self.rationale, "SemanticRefinementReview.rationale", max_length=65536
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "review_id": self.review_id,
            "conflict_id": self.conflict_id,
            "proposal_id": self.proposal_id,
            "binding": self.binding.to_dict(),
            "reviewer": self.reviewer.to_dict(),
            "decision": self.decision.value,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SemanticRefinementReview"
    ) -> SemanticRefinementReview:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "review_id",
                    "conflict_id",
                    "proposal_id",
                    "binding",
                    "reviewer",
                    "decision",
                    "rationale",
                }
            ),
        )
        return cls(
            review_id=string_value(data["review_id"], f"{path}.review_id"),
            conflict_id=string_value(data["conflict_id"], f"{path}.conflict_id"),
            proposal_id=string_value(data["proposal_id"], f"{path}.proposal_id"),
            binding=RefinementReviewBinding.from_dict(
                data["binding"], path=f"{path}.binding"
            ),
            reviewer=ContentReference.from_dict(
                data["reviewer"], path=f"{path}.reviewer"
            ),
            decision=enum_value(ReviewDecision, data["decision"], f"{path}.decision"),
            rationale=string_value(
                data["rationale"], f"{path}.rationale", max_length=65536
            ),
        )


@dataclass(frozen=True, slots=True)
class SemanticRefinementRequest:
    """A closed document set and all proposed refinement decisions for it."""

    documents: tuple[ContentReference, ...]
    conflicts: tuple[SemanticConflict, ...]
    proposals: tuple[SemanticRefinementProposal, ...]
    reviews: tuple[SemanticRefinementReview, ...]

    SCHEMA: ClassVar[str] = SEMANTIC_REFINEMENT_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if not self.documents:
            fail("SemanticRefinementRequest.documents", "must not be empty")
        unique(
            tuple(item.uri for item in self.documents),
            "SemanticRefinementRequest.documents",
            "document URIs",
        )
        unique(
            tuple(item.identity.uri for item in self.documents),
            "SemanticRefinementRequest.documents",
            "document identities",
        )
        for index, document in enumerate(self.documents):
            _require_kind(
                document,
                "specification-document",
                f"SemanticRefinementRequest.documents[{index}]",
            )
        unique(
            tuple(item.conflict_id for item in self.conflicts),
            "SemanticRefinementRequest.conflicts",
            "conflict IDs",
        )
        unique(
            tuple(item.proposal_id for item in self.proposals),
            "SemanticRefinementRequest.proposals",
            "proposal IDs",
        )
        unique(
            tuple(item.review_id for item in self.reviews),
            "SemanticRefinementRequest.reviews",
            "review IDs",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "documents": [
                item.to_dict()
                for item in sorted(self.documents, key=lambda item: item.uri)
            ],
            "conflicts": [
                item.to_dict()
                for item in sorted(self.conflicts, key=lambda item: item.conflict_id)
            ],
            "proposals": [
                item.to_dict()
                for item in sorted(self.proposals, key=lambda item: item.proposal_id)
            ],
            "reviews": [
                item.to_dict()
                for item in sorted(self.reviews, key=lambda item: item.review_id)
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SemanticRefinementRequest"
    ) -> SemanticRefinementRequest:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"documents", "conflicts", "proposals", "reviews"}),
        )
        return cls(
            documents=_references(data["documents"], f"{path}.documents", minimum=1),
            conflicts=parse_tuple(
                data["conflicts"], f"{path}.conflicts", SemanticConflict.from_dict
            ),
            proposals=parse_tuple(
                data["proposals"],
                f"{path}.proposals",
                SemanticRefinementProposal.from_dict,
            ),
            reviews=parse_tuple(
                data["reviews"],
                f"{path}.reviews",
                SemanticRefinementReview.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class AcceptedSemanticRefinement:
    conflict_id: str
    conflict_identity: ContentIdentity
    proposal_id: str
    proposal_identity: ContentIdentity
    review_id: str
    review_identity: ContentIdentity

    def __post_init__(self) -> None:
        string_value(self.conflict_id, "AcceptedSemanticRefinement.conflict_id")
        string_value(self.proposal_id, "AcceptedSemanticRefinement.proposal_id")
        string_value(self.review_id, "AcceptedSemanticRefinement.review_id")
        for name in ("conflict_identity", "proposal_identity", "review_identity"):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"AcceptedSemanticRefinement.{name}", "must be a ContentIdentity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "conflict_identity": self.conflict_identity.to_dict(),
            "proposal_id": self.proposal_id,
            "proposal_identity": self.proposal_identity.to_dict(),
            "review_id": self.review_id,
            "review_identity": self.review_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "AcceptedSemanticRefinement"
    ) -> AcceptedSemanticRefinement:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "conflict_id",
                    "conflict_identity",
                    "proposal_id",
                    "proposal_identity",
                    "review_id",
                    "review_identity",
                }
            ),
        )
        return cls(
            conflict_id=string_value(data["conflict_id"], f"{path}.conflict_id"),
            conflict_identity=ContentIdentity.from_dict(
                data["conflict_identity"], path=f"{path}.conflict_identity"
            ),
            proposal_id=string_value(data["proposal_id"], f"{path}.proposal_id"),
            proposal_identity=ContentIdentity.from_dict(
                data["proposal_identity"], path=f"{path}.proposal_identity"
            ),
            review_id=string_value(data["review_id"], f"{path}.review_id"),
            review_identity=ContentIdentity.from_dict(
                data["review_identity"], path=f"{path}.review_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class SemanticRefinementResolution:
    request_identity: ContentIdentity
    document_closure: tuple[ContentIdentity, ...]
    accepted: tuple[AcceptedSemanticRefinement, ...]

    SCHEMA: ClassVar[str] = SEMANTIC_REFINEMENT_RESOLUTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.request_identity, ContentIdentity):
            fail(
                "SemanticRefinementResolution.request_identity",
                "must be a ContentIdentity",
            )
        unique(
            tuple(item.uri for item in self.document_closure),
            "SemanticRefinementResolution.document_closure",
            "document identities",
        )
        unique(
            tuple(item.conflict_id for item in self.accepted),
            "SemanticRefinementResolution.accepted",
            "conflict IDs",
        )
        unique(
            tuple(item.proposal_id for item in self.accepted),
            "SemanticRefinementResolution.accepted",
            "proposal IDs",
        )
        unique(
            tuple(item.review_id for item in self.accepted),
            "SemanticRefinementResolution.accepted",
            "review IDs",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity.to_dict(),
            "document_closure": [
                item.to_dict()
                for item in sorted(self.document_closure, key=lambda item: item.uri)
            ],
            "accepted": [
                item.to_dict()
                for item in sorted(self.accepted, key=lambda item: item.conflict_id)
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SemanticRefinementResolution"
    ) -> SemanticRefinementResolution:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"request_identity", "document_closure", "accepted"}),
        )
        return cls(
            request_identity=ContentIdentity.from_dict(
                data["request_identity"], path=f"{path}.request_identity"
            ),
            document_closure=_identities(
                data["document_closure"], f"{path}.document_closure"
            ),
            accepted=parse_tuple(
                data["accepted"],
                f"{path}.accepted",
                AcceptedSemanticRefinement.from_dict,
            ),
        )


__all__ = [
    "SEMANTIC_CONFLICT_SCHEMA",
    "SEMANTIC_REFINEMENT_PROPOSAL_SCHEMA",
    "SEMANTIC_REFINEMENT_REQUEST_SCHEMA",
    "SEMANTIC_REFINEMENT_RESOLUTION_SCHEMA",
    "SEMANTIC_REFINEMENT_REVIEW_SCHEMA",
    "AcceptedSemanticRefinement",
    "ProposalVerification",
    "RefinementReviewBinding",
    "ReviewDecision",
    "SemanticConflict",
    "SemanticRefinementProposal",
    "SemanticRefinementRequest",
    "SemanticRefinementResolution",
    "SemanticRefinementReview",
]

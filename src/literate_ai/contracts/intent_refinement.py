"""Provider-neutral intent-refinement contracts (ADR 0013).

Refinement produces a content-identified design draft. The draft is never
generation authority. Blocking questions prevent acceptance. Target-bound
choices stay outside portable Component behavior.
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
from .identity import ContentIdentity, contract_identity

INTENT_REFINEMENT_REQUEST_SCHEMA = "literate-ai/intent-refinement-request@1"
DESIGN_DRAFT_SCHEMA = "literate-ai/design-draft@1"
DESIGN_ACCEPT_REQUEST_SCHEMA = "literate-ai/design-accept-request@1"
DESIGN_ACCEPTANCE_RECEIPT_SCHEMA = "literate-ai/design-acceptance-receipt@1"


class ConcernKind(StrEnum):
    APPLICATION_SURFACE = "application-surface"
    COMPUTATION = "computation"
    PROTOCOL = "protocol"
    MEASUREMENT = "measurement"
    TARGET = "target"
    DEPLOYMENT = "deployment"
    TRUST = "trust"


class SufficiencyStatus(StrEnum):
    SUFFICIENT = "sufficient"
    NEEDS_AUTHORITY = "needs-authority"
    TARGET_UNRESOLVED = "target-unresolved"
    UNSUPPORTED_REPRESENTATION = "unsupported-representation"
    OUT_OF_SCOPE = "out-of-scope"


class UnresolvedKind(StrEnum):
    BLOCKING = "blocking"
    DEFAULTABLE = "defaultable"
    TARGET_BOUND = "target-bound"
    IMPLEMENTATION_LOCAL = "implementation-local"


class DesignStatus(StrEnum):
    COMPLETE = "complete"
    NEEDS_DECISIONS = "needs-decisions"


@dataclass(frozen=True, slots=True)
class ConcernRecord:
    concern_id: str
    kind: ConcernKind
    summary: str
    proposed_authority: str
    sufficiency: SufficiencyStatus

    def __post_init__(self) -> None:
        string_value(self.concern_id, "ConcernRecord.concern_id")
        string_value(self.summary, "ConcernRecord.summary", max_length=65536)
        string_value(
            self.proposed_authority, "ConcernRecord.proposed_authority", max_length=4096
        )
        if not isinstance(self.kind, ConcernKind):
            fail("ConcernRecord.kind", "must be a ConcernKind")
        if not isinstance(self.sufficiency, SufficiencyStatus):
            fail("ConcernRecord.sufficiency", "must be a SufficiencyStatus")

    def to_dict(self) -> dict[str, str]:
        return {
            "concern_id": self.concern_id,
            "kind": self.kind.value,
            "summary": self.summary,
            "proposed_authority": self.proposed_authority,
            "sufficiency": self.sufficiency.value,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "ConcernRecord") -> ConcernRecord:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "concern_id",
                    "kind",
                    "summary",
                    "proposed_authority",
                    "sufficiency",
                }
            ),
        )
        return cls(
            concern_id=string_value(data["concern_id"], f"{path}.concern_id"),
            kind=enum_value(ConcernKind, data["kind"], f"{path}.kind"),
            summary=string_value(data["summary"], f"{path}.summary", max_length=65536),
            proposed_authority=string_value(
                data["proposed_authority"], f"{path}.proposed_authority"
            ),
            sufficiency=enum_value(
                SufficiencyStatus, data["sufficiency"], f"{path}.sufficiency"
            ),
        )


@dataclass(frozen=True, slots=True)
class UnresolvedInput:
    question_id: str
    kind: UnresolvedKind
    concern_id: str
    prompt: str
    why_observable: str

    def __post_init__(self) -> None:
        string_value(self.question_id, "UnresolvedInput.question_id")
        string_value(self.concern_id, "UnresolvedInput.concern_id")
        string_value(self.prompt, "UnresolvedInput.prompt", max_length=65536)
        string_value(
            self.why_observable, "UnresolvedInput.why_observable", max_length=65536
        )
        if not isinstance(self.kind, UnresolvedKind):
            fail("UnresolvedInput.kind", "must be an UnresolvedKind")

    def to_dict(self) -> dict[str, str]:
        return {
            "question_id": self.question_id,
            "kind": self.kind.value,
            "concern_id": self.concern_id,
            "prompt": self.prompt,
            "why_observable": self.why_observable,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "UnresolvedInput") -> UnresolvedInput:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {"question_id", "kind", "concern_id", "prompt", "why_observable"}
            ),
        )
        return cls(
            question_id=string_value(data["question_id"], f"{path}.question_id"),
            kind=enum_value(UnresolvedKind, data["kind"], f"{path}.kind"),
            concern_id=string_value(data["concern_id"], f"{path}.concern_id"),
            prompt=string_value(data["prompt"], f"{path}.prompt", max_length=65536),
            why_observable=string_value(
                data["why_observable"], f"{path}.why_observable", max_length=65536
            ),
        )


@dataclass(frozen=True, slots=True)
class ProposedComponent:
    component_id: str
    role: str
    boundary_reason: str
    concern_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        string_value(self.component_id, "ProposedComponent.component_id")
        string_value(self.role, "ProposedComponent.role")
        string_value(
            self.boundary_reason, "ProposedComponent.boundary_reason", max_length=65536
        )
        unique(self.concern_ids, "ProposedComponent.concern_ids")
        if not self.concern_ids:
            fail("ProposedComponent.concern_ids", "must not be empty")
        for index, item in enumerate(self.concern_ids):
            string_value(item, f"ProposedComponent.concern_ids[{index}]")

    def to_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "role": self.role,
            "boundary_reason": self.boundary_reason,
            "concern_ids": list(self.concern_ids),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProposedComponent"
    ) -> ProposedComponent:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {"component_id", "role", "boundary_reason", "concern_ids"}
            ),
        )
        concern_ids = data["concern_ids"]
        if not isinstance(concern_ids, list) or any(
            not isinstance(item, str) for item in concern_ids
        ):
            fail(f"{path}.concern_ids", "must be an array of strings")
        return cls(
            component_id=string_value(data["component_id"], f"{path}.component_id"),
            role=string_value(data["role"], f"{path}.role"),
            boundary_reason=string_value(
                data["boundary_reason"], f"{path}.boundary_reason", max_length=65536
            ),
            concern_ids=tuple(concern_ids),
        )


@dataclass(frozen=True, slots=True)
class IntentRefinementRequest:
    mission: str
    source: str

    SCHEMA: ClassVar[str] = INTENT_REFINEMENT_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.mission, "IntentRefinementRequest.mission", max_length=65536)
        string_value(self.source, "IntentRefinementRequest.source")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": self.SCHEMA,
            "mission": self.mission,
            "source": self.source,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "IntentRefinementRequest"
    ) -> IntentRefinementRequest:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"mission", "source"}),
        )
        return cls(
            mission=string_value(data["mission"], f"{path}.mission", max_length=65536),
            source=string_value(data["source"], f"{path}.source"),
        )


@dataclass(frozen=True, slots=True)
class DesignDraft:
    request_identity: str
    status: DesignStatus
    mission: str
    components: tuple[ProposedComponent, ...]
    concerns: tuple[ConcernRecord, ...]
    unresolved: tuple[UnresolvedInput, ...]
    non_goals: tuple[str, ...]

    SCHEMA: ClassVar[str] = DESIGN_DRAFT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.request_identity, "DesignDraft.request_identity")
        string_value(self.mission, "DesignDraft.mission", max_length=65536)
        if not isinstance(self.status, DesignStatus):
            fail("DesignDraft.status", "must be a DesignStatus")
        if not self.components:
            fail("DesignDraft.components", "must not be empty")
        unique(
            tuple(item.component_id for item in self.components),
            "DesignDraft.components",
            "component IDs",
        )
        unique(
            tuple(item.concern_id for item in self.concerns),
            "DesignDraft.concerns",
            "concern IDs",
        )
        unique(
            tuple(item.question_id for item in self.unresolved),
            "DesignDraft.unresolved",
            "question IDs",
        )
        unique(self.non_goals, "DesignDraft.non_goals")
        concern_ids = {item.concern_id for item in self.concerns}
        for component in self.components:
            missing = [
                item for item in component.concern_ids if item not in concern_ids
            ]
            if missing:
                fail(
                    "DesignDraft.components",
                    f"{component.component_id} cites unknown concerns",
                )
        for question in self.unresolved:
            if question.concern_id not in concern_ids:
                fail(
                    "DesignDraft.unresolved",
                    f"{question.question_id} cites an unknown concern",
                )
        blocking = tuple(
            item for item in self.unresolved if item.kind is UnresolvedKind.BLOCKING
        )
        if blocking and self.status is DesignStatus.COMPLETE:
            fail(
                "DesignDraft.status",
                "blocking questions require needs-decisions",
            )
        if not blocking and self.status is DesignStatus.NEEDS_DECISIONS:
            fail(
                "DesignDraft.status",
                "needs-decisions requires at least one blocking question",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def blocking_questions(self) -> tuple[UnresolvedInput, ...]:
        return tuple(
            item for item in self.unresolved if item.kind is UnresolvedKind.BLOCKING
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity,
            "status": self.status.value,
            "mission": self.mission,
            "components": [
                item.to_dict()
                for item in sorted(self.components, key=lambda item: item.component_id)
            ],
            "concerns": [
                item.to_dict()
                for item in sorted(self.concerns, key=lambda item: item.concern_id)
            ],
            "unresolved": [
                item.to_dict()
                for item in sorted(self.unresolved, key=lambda item: item.question_id)
            ],
            "non_goals": list(self.non_goals),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "DesignDraft") -> DesignDraft:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "request_identity",
                    "status",
                    "mission",
                    "components",
                    "concerns",
                    "unresolved",
                    "non_goals",
                }
            ),
        )
        non_goals = data["non_goals"]
        if not isinstance(non_goals, list) or any(
            not isinstance(item, str) for item in non_goals
        ):
            fail(f"{path}.non_goals", "must be an array of strings")
        return cls(
            request_identity=string_value(
                data["request_identity"], f"{path}.request_identity"
            ),
            status=enum_value(DesignStatus, data["status"], f"{path}.status"),
            mission=string_value(data["mission"], f"{path}.mission", max_length=65536),
            components=parse_tuple(
                data["components"], f"{path}.components", ProposedComponent.from_dict
            ),
            concerns=parse_tuple(
                data["concerns"], f"{path}.concerns", ConcernRecord.from_dict
            ),
            unresolved=parse_tuple(
                data["unresolved"], f"{path}.unresolved", UnresolvedInput.from_dict
            ),
            non_goals=tuple(non_goals),
        )


@dataclass(frozen=True, slots=True)
class DesignDecision:
    question_id: str
    answer: str

    def __post_init__(self) -> None:
        string_value(self.question_id, "DesignDecision.question_id")
        string_value(self.answer, "DesignDecision.answer", max_length=65536)

    def to_dict(self) -> dict[str, str]:
        return {"question_id": self.question_id, "answer": self.answer}

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "DesignDecision") -> DesignDecision:
        data = fields(value, path=path, required=frozenset({"question_id", "answer"}))
        return cls(
            question_id=string_value(data["question_id"], f"{path}.question_id"),
            answer=string_value(data["answer"], f"{path}.answer", max_length=65536),
        )


@dataclass(frozen=True, slots=True)
class DesignAcceptRequest:
    draft_identity: str
    decisions: tuple[DesignDecision, ...]

    SCHEMA: ClassVar[str] = DESIGN_ACCEPT_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.draft_identity, "DesignAcceptRequest.draft_identity")
        unique(
            tuple(item.question_id for item in self.decisions),
            "DesignAcceptRequest.decisions",
            "question IDs",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "draft_identity": self.draft_identity,
            "decisions": [
                item.to_dict()
                for item in sorted(self.decisions, key=lambda item: item.question_id)
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "DesignAcceptRequest"
    ) -> DesignAcceptRequest:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"draft_identity", "decisions"}),
        )
        return cls(
            draft_identity=string_value(
                data["draft_identity"], f"{path}.draft_identity"
            ),
            decisions=parse_tuple(
                data["decisions"], f"{path}.decisions", DesignDecision.from_dict
            ),
        )


@dataclass(frozen=True, slots=True)
class DesignAcceptanceReceipt:
    draft_identity: str
    request_identity: str
    accepted_component_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]

    SCHEMA: ClassVar[str] = DESIGN_ACCEPTANCE_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.draft_identity, "DesignAcceptanceReceipt.draft_identity")
        string_value(self.request_identity, "DesignAcceptanceReceipt.request_identity")
        unique(
            self.accepted_component_ids,
            "DesignAcceptanceReceipt.accepted_component_ids",
        )
        unique(self.decision_ids, "DesignAcceptanceReceipt.decision_ids")
        if not self.accepted_component_ids:
            fail(
                "DesignAcceptanceReceipt.accepted_component_ids",
                "must not be empty",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "draft_identity": self.draft_identity,
            "request_identity": self.request_identity,
            "accepted_component_ids": list(self.accepted_component_ids),
            "decision_ids": list(self.decision_ids),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "DesignAcceptanceReceipt"
    ) -> DesignAcceptanceReceipt:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "draft_identity",
                    "request_identity",
                    "accepted_component_ids",
                    "decision_ids",
                }
            ),
        )
        components = data["accepted_component_ids"]
        decisions = data["decision_ids"]
        if not isinstance(components, list) or any(
            not isinstance(item, str) for item in components
        ):
            fail(f"{path}.accepted_component_ids", "must be an array of strings")
        if not isinstance(decisions, list) or any(
            not isinstance(item, str) for item in decisions
        ):
            fail(f"{path}.decision_ids", "must be an array of strings")
        return cls(
            draft_identity=string_value(
                data["draft_identity"], f"{path}.draft_identity"
            ),
            request_identity=string_value(
                data["request_identity"], f"{path}.request_identity"
            ),
            accepted_component_ids=tuple(components),
            decision_ids=tuple(decisions),
        )


__all__ = [
    "DESIGN_ACCEPTANCE_RECEIPT_SCHEMA",
    "DESIGN_ACCEPT_REQUEST_SCHEMA",
    "DESIGN_DRAFT_SCHEMA",
    "INTENT_REFINEMENT_REQUEST_SCHEMA",
    "ConcernKind",
    "ConcernRecord",
    "DesignAcceptRequest",
    "DesignAcceptanceReceipt",
    "DesignDecision",
    "DesignDraft",
    "DesignStatus",
    "IntentRefinementRequest",
    "ProposedComponent",
    "SufficiencyStatus",
    "UnresolvedInput",
    "UnresolvedKind",
]

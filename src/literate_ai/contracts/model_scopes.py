"""Immutable lexical model-selection scopes for bounded agent work."""

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
)
from .generation_cache import (
    CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
    source_cache_model_selector,
)
from .identity import ContentIdentity, contract_identity

MODEL_SCOPE_BINDING_SCHEMA = "urn:literate-ai:schema:v2:model-scope-binding"
_GENERATION_PROVIDERS = frozenset(
    {"codex", "claude", "cursor-agent", "opencode", "inherited-session"}
)


class ModelScopeKind(StrEnum):
    PIPELINE = "pipeline"
    COMPONENT = "component"
    FLAVOR_ROLE = "flavor-role"
    SKILL_INVOCATION = "skill-invocation"


class ModelScopeDecision(StrEnum):
    DEFAULT = "default"
    INHERIT = "inherit"
    OVERRIDE = "override"


class ModelScopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ModelScopeResolutionStep:
    scope_kind: ModelScopeKind
    owner_identity: ContentIdentity
    candidate_owner_identities: tuple[ContentIdentity, ...]
    configured_selectors: tuple[str, ...]
    selected_selector: str
    decision: ModelScopeDecision

    def __post_init__(self) -> None:
        if not isinstance(self.scope_kind, ModelScopeKind):
            fail("ModelScopeResolutionStep.scope_kind", "must be a ModelScopeKind")
        if not isinstance(self.owner_identity, ContentIdentity):
            fail(
                "ModelScopeResolutionStep.owner_identity",
                "must be a ContentIdentity",
            )
        if any(
            not isinstance(item, ContentIdentity)
            for item in self.candidate_owner_identities
        ):
            fail(
                "ModelScopeResolutionStep.candidate_owner_identities",
                "must contain only ContentIdentity values",
            )
        if self.candidate_owner_identities != tuple(
            sorted(set(self.candidate_owner_identities), key=lambda item: item.uri)
        ):
            fail(
                "ModelScopeResolutionStep.candidate_owner_identities",
                "must be sorted and unique",
            )
        if any(not item for item in self.configured_selectors):
            fail(
                "ModelScopeResolutionStep.configured_selectors",
                "must contain only non-empty selectors",
            )
        string_value(
            self.selected_selector, "ModelScopeResolutionStep.selected_selector"
        )
        if not isinstance(self.decision, ModelScopeDecision):
            fail("ModelScopeResolutionStep.decision", "must be a ModelScopeDecision")
        if self.decision is ModelScopeDecision.OVERRIDE:
            if not self.configured_selectors or not self.candidate_owner_identities:
                fail(
                    "ModelScopeResolutionStep",
                    "an override requires configured selectors and their owners",
                )
        elif self.configured_selectors or self.candidate_owner_identities:
            fail(
                "ModelScopeResolutionStep",
                "default and inherited steps cannot claim override candidates",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "scope_kind": self.scope_kind.value,
            "owner_identity": self.owner_identity.to_dict(),
            "candidate_owner_identities": [
                item.to_dict() for item in self.candidate_owner_identities
            ],
            "configured_selectors": list(self.configured_selectors),
            "selected_selector": self.selected_selector,
            "decision": self.decision.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ModelScopeResolutionStep"
    ) -> ModelScopeResolutionStep:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "scope_kind",
                    "owner_identity",
                    "candidate_owner_identities",
                    "configured_selectors",
                    "selected_selector",
                    "decision",
                }
            ),
        )
        selectors = data["configured_selectors"]
        if not isinstance(selectors, list):
            fail(f"{path}.configured_selectors", "must be an array")
        return cls(
            scope_kind=enum_value(
                ModelScopeKind, data["scope_kind"], f"{path}.scope_kind"
            ),
            owner_identity=ContentIdentity.from_dict(
                data["owner_identity"], path=f"{path}.owner_identity"
            ),
            candidate_owner_identities=parse_tuple(
                data["candidate_owner_identities"],
                f"{path}.candidate_owner_identities",
                ContentIdentity.from_dict,
            ),
            configured_selectors=tuple(
                string_value(item, f"{path}.configured_selectors[{index}]")
                for index, item in enumerate(selectors)
            ),
            selected_selector=string_value(
                data["selected_selector"], f"{path}.selected_selector"
            ),
            decision=enum_value(
                ModelScopeDecision, data["decision"], f"{path}.decision"
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelScopeBinding:
    scope_kind: ModelScopeKind
    owner_identity: ContentIdentity
    parent_binding_identity: ContentIdentity | None
    provider_id: str
    model_selector: str
    resolution_trace: tuple[ModelScopeResolutionStep, ...]

    SCHEMA: ClassVar[str] = MODEL_SCOPE_BINDING_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.scope_kind, ModelScopeKind):
            fail("ModelScopeBinding.scope_kind", "must be a ModelScopeKind")
        if not isinstance(self.owner_identity, ContentIdentity):
            fail("ModelScopeBinding.owner_identity", "must be a ContentIdentity")
        if self.parent_binding_identity is not None and not isinstance(
            self.parent_binding_identity, ContentIdentity
        ):
            fail(
                "ModelScopeBinding.parent_binding_identity",
                "must be a ContentIdentity or null",
            )
        string_value(self.provider_id, "ModelScopeBinding.provider_id")
        if self.provider_id not in _GENERATION_PROVIDERS:
            fail(
                "ModelScopeBinding.provider_id",
                "must identify a supported generation provider",
            )
        string_value(self.model_selector, "ModelScopeBinding.model_selector")
        if not self.resolution_trace:
            fail("ModelScopeBinding.resolution_trace", "must not be empty")
        if any(
            not isinstance(item, ModelScopeResolutionStep)
            for item in self.resolution_trace
        ):
            fail(
                "ModelScopeBinding.resolution_trace",
                "must contain only ModelScopeResolutionStep values",
            )
        final = self.resolution_trace[-1]
        if (
            final.scope_kind is not self.scope_kind
            or final.owner_identity != self.owner_identity
            or final.selected_selector != self.model_selector
        ):
            fail(
                "ModelScopeBinding.resolution_trace",
                "final trace step must describe the binding",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def explicit_model(self) -> str | None:
        return (
            None
            if self.model_selector == CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR
            else self.model_selector
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "scope_kind": self.scope_kind.value,
            "owner_identity": self.owner_identity.to_dict(),
            "parent_binding_identity": (
                None
                if self.parent_binding_identity is None
                else self.parent_binding_identity.to_dict()
            ),
            "provider_id": self.provider_id,
            "model_selector": self.model_selector,
            "resolution_trace": [item.to_dict() for item in self.resolution_trace],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ModelScopeBinding"
    ) -> ModelScopeBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "scope_kind",
                    "owner_identity",
                    "parent_binding_identity",
                    "provider_id",
                    "model_selector",
                    "resolution_trace",
                }
            ),
        )
        parent = data["parent_binding_identity"]
        return cls(
            scope_kind=enum_value(
                ModelScopeKind, data["scope_kind"], f"{path}.scope_kind"
            ),
            owner_identity=ContentIdentity.from_dict(
                data["owner_identity"], path=f"{path}.owner_identity"
            ),
            parent_binding_identity=(
                None
                if parent is None
                else ContentIdentity.from_dict(
                    parent, path=f"{path}.parent_binding_identity"
                )
            ),
            provider_id=string_value(data["provider_id"], f"{path}.provider_id"),
            model_selector=string_value(
                data["model_selector"], f"{path}.model_selector"
            ),
            resolution_trace=parse_tuple(
                data["resolution_trace"],
                f"{path}.resolution_trace",
                ModelScopeResolutionStep.from_dict,
            ),
        )


def resolve_model_scope(
    *,
    scope_kind: ModelScopeKind,
    owner_identity: ContentIdentity,
    provider_id: str,
    parent: ModelScopeBinding | None = None,
    candidates: tuple[tuple[ContentIdentity, str], ...] = (),
) -> ModelScopeBinding:
    """Resolve one immutable lexical frame without mutable process-global state."""

    if not isinstance(scope_kind, ModelScopeKind):
        raise TypeError("scope_kind must be a ModelScopeKind")
    if not isinstance(owner_identity, ContentIdentity):
        raise TypeError("owner_identity must be a ContentIdentity")
    if provider_id not in _GENERATION_PROVIDERS:
        raise ModelScopeError(
            "model_scope.provider_unsupported",
            "model scope provider must identify a supported generation provider",
        )
    if any(not isinstance(item[0], ContentIdentity) for item in candidates):
        raise TypeError("model scope candidate owners must be ContentIdentity values")
    candidate_owners = tuple(item[0] for item in candidates)
    if len(candidate_owners) != len(set(candidate_owners)):
        raise ModelScopeError(
            "model_scope.candidate_duplicate",
            "one scope cannot bind the same candidate owner more than once",
        )
    normalized = tuple(
        sorted(
            (
                (
                    candidate_owner,
                    source_cache_model_selector(
                        selector,
                        path=f"model scope {scope_kind.value} candidate",
                    ),
                )
                for candidate_owner, selector in candidates
            ),
            key=lambda item: (item[0].uri, item[1]),
        )
    )
    selectors = tuple(sorted({item[1] for item in normalized}))
    if len(selectors) > 1:
        raise ModelScopeError(
            "model_scope.ambiguous",
            f"equally specific {scope_kind.value} bindings disagree for {provider_id}",
        )
    if selectors:
        selected = selectors[0]
        decision = ModelScopeDecision.OVERRIDE
        candidate_owners = tuple(
            sorted({item[0] for item in normalized}, key=lambda item: item.uri)
        )
        configured = tuple(item[1] for item in normalized)
    elif parent is not None:
        if parent.provider_id != provider_id:
            raise ModelScopeError(
                "model_scope.provider_mismatch",
                "an inner model scope cannot reinterpret its parent provider",
            )
        selected = parent.model_selector
        decision = ModelScopeDecision.INHERIT
        candidate_owners = ()
        configured = ()
    else:
        selected = CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR
        decision = ModelScopeDecision.DEFAULT
        candidate_owners = ()
        configured = ()
    step = ModelScopeResolutionStep(
        scope_kind,
        owner_identity,
        candidate_owners,
        configured,
        selected,
        decision,
    )
    return ModelScopeBinding(
        scope_kind,
        owner_identity,
        None if parent is None else parent.identity,
        string_value(provider_id, "model scope provider_id"),
        selected,
        (*(parent.resolution_trace if parent is not None else ()), step),
    )


__all__ = [
    "MODEL_SCOPE_BINDING_SCHEMA",
    "ModelScopeBinding",
    "ModelScopeDecision",
    "ModelScopeError",
    "ModelScopeKind",
    "ModelScopeResolutionStep",
    "resolve_model_scope",
]

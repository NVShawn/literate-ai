"""Bounded, auditable coding-agent context and complexity contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    parse_tuple,
    string_tuple,
    string_value,
)
from ..identity import ContentIdentity, contract_identity

COMPONENT_GENERATION_CONTEXT_MANIFEST_SCHEMA = (
    "urn:literate-ai:schema:v2:component-generation-context-manifest"
)
GENERATION_COMPLEXITY_BUDGET_SCHEMA = (
    "urn:literate-ai:schema:v2:generation-complexity-budget"
)
GENERATION_COMPLEXITY_DECISION_SCHEMA = (
    "urn:literate-ai:schema:v2:generation-complexity-decision"
)
BOUNDED_COMPONENT_GENERATION_REQUEST_SCHEMA = (
    "urn:literate-ai:schema:v2:bounded-component-generation-request"
)


class ContextAuthorityKind(StrEnum):
    """Classification is explicit so forbidden material cannot masquerade as a spec."""

    FRAMEWORK_ENVELOPE = "framework-envelope"
    LOCAL_SPECIFICATION = "local-specification"
    LOCAL_FLAVOR = "local-flavor"
    LOCAL_SKILL = "local-skill"
    LOCAL_WORKFLOW = "local-workflow"
    LOCAL_ROUTING_POLICY = "local-routing-policy"
    LOCAL_PUBLIC_INTERFACE = "local-public-interface"
    LOCAL_ASSET_METADATA = "local-asset-metadata"
    LOCAL_NATIVE_SDK_METADATA = "local-native-sdk-metadata"
    DIRECT_PUBLIC_INTERFACE = "direct-public-interface"

    PRIVATE_DEPENDENCY_SPECIFICATION = "private-dependency-specification"
    DEPENDENCY_SOURCE = "dependency-source"
    DEPENDENCY_TEST = "dependency-test"
    DEPENDENCY_MANIFEST = "dependency-manifest"
    DEPENDENCY_LOCK = "dependency-lock"
    INVERSE_JOURNAL = "inverse-journal"
    ACCEPTANCE_ORACLE = "acceptance-oracle"
    TRANSITIVE_PUBLIC_INTERFACE = "transitive-public-interface"


class ContextVisibility(StrEnum):
    FRAMEWORK = "framework"
    LOCAL_AUTHORITY = "local-authority"
    DIRECT_PUBLIC_INTERFACE = "direct-public-interface"
    FORBIDDEN = "forbidden"


_ALLOWED_VISIBILITY = {
    ContextAuthorityKind.FRAMEWORK_ENVELOPE: ContextVisibility.FRAMEWORK,
    ContextAuthorityKind.LOCAL_SPECIFICATION: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.LOCAL_FLAVOR: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.LOCAL_SKILL: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.LOCAL_WORKFLOW: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.LOCAL_ROUTING_POLICY: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.LOCAL_PUBLIC_INTERFACE: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.LOCAL_ASSET_METADATA: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.LOCAL_NATIVE_SDK_METADATA: ContextVisibility.LOCAL_AUTHORITY,
    ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE: (
        ContextVisibility.DIRECT_PUBLIC_INTERFACE
    ),
}


def deterministic_token_estimate(byte_count: int) -> int:
    """Return the stable, deliberately conservative pre-egress token estimate."""

    return (byte_count + 3) // 4


@dataclass(frozen=True, slots=True)
class ComponentGenerationContextSegment:
    index: int
    authority_kind: ContextAuthorityKind
    source_component_revision: ContentIdentity | None
    reason: str
    content_identity: ContentIdentity
    byte_count: int
    estimated_tokens: int
    visibility: ContextVisibility

    def __post_init__(self) -> None:
        int_value(self.index, "ComponentGenerationContextSegment.index", maximum=65535)
        if not isinstance(self.authority_kind, ContextAuthorityKind):
            fail(
                "ComponentGenerationContextSegment.authority_kind",
                "must be a ContextAuthorityKind",
            )
        if self.source_component_revision is not None and not isinstance(
            self.source_component_revision, ContentIdentity
        ):
            fail(
                "ComponentGenerationContextSegment.source_component_revision",
                "must be a ContentIdentity or null",
            )
        string_value(self.reason, "ComponentGenerationContextSegment.reason")
        if not isinstance(self.content_identity, ContentIdentity):
            fail(
                "ComponentGenerationContextSegment.content_identity",
                "must be a ContentIdentity",
            )
        count = int_value(
            self.byte_count,
            "ComponentGenerationContextSegment.byte_count",
            maximum=2**40,
        )
        estimate = int_value(
            self.estimated_tokens,
            "ComponentGenerationContextSegment.estimated_tokens",
            maximum=2**40,
        )
        if estimate != deterministic_token_estimate(count):
            fail(
                "ComponentGenerationContextSegment.estimated_tokens",
                "must use the deterministic ceil(UTF-8-bytes / 4) estimate",
            )
        if not isinstance(self.visibility, ContextVisibility):
            fail(
                "ComponentGenerationContextSegment.visibility",
                "must be a ContextVisibility",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "authority_kind": self.authority_kind.value,
            "source_component_revision": (
                self.source_component_revision.to_dict()
                if self.source_component_revision is not None
                else None
            ),
            "reason": self.reason,
            "content_identity": self.content_identity.to_dict(),
            "byte_count": self.byte_count,
            "estimated_tokens": self.estimated_tokens,
            "visibility": self.visibility.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationContextSegment"
    ) -> ComponentGenerationContextSegment:
        names = frozenset(
            {
                "index",
                "authority_kind",
                "source_component_revision",
                "reason",
                "content_identity",
                "byte_count",
                "estimated_tokens",
                "visibility",
            }
        )
        data = fields(
            value,
            path=path,
            required=names,
        )
        source = data["source_component_revision"]
        return cls(
            index=int_value(data["index"], f"{path}.index", maximum=65535),
            authority_kind=enum_value(
                ContextAuthorityKind, data["authority_kind"], f"{path}.authority_kind"
            ),
            source_component_revision=(
                None
                if source is None
                else ContentIdentity.from_dict(
                    source, path=f"{path}.source_component_revision"
                )
            ),
            reason=string_value(data["reason"], f"{path}.reason"),
            content_identity=ContentIdentity.from_dict(
                data["content_identity"], path=f"{path}.content_identity"
            ),
            byte_count=int_value(
                data["byte_count"], f"{path}.byte_count", maximum=2**40
            ),
            estimated_tokens=int_value(
                data["estimated_tokens"],
                f"{path}.estimated_tokens",
                maximum=2**40,
            ),
            visibility=enum_value(
                ContextVisibility, data["visibility"], f"{path}.visibility"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentGenerationContextManifest:
    component_revision: ContentIdentity
    generation_key_identity: ContentIdentity
    segments: tuple[ComponentGenerationContextSegment, ...]

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_CONTEXT_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.component_revision, ContentIdentity) or not isinstance(
            self.generation_key_identity, ContentIdentity
        ):
            fail(
                "ComponentGenerationContextManifest",
                "Component and plan identities must be ContentIdentity values",
            )
        if not self.segments:
            fail("ComponentGenerationContextManifest.segments", "must not be empty")
        if len(self.segments) > 4096:
            fail(
                "ComponentGenerationContextManifest.segments",
                "must contain at most 4096 segments",
            )
        if tuple(item.index for item in self.segments) != tuple(
            range(len(self.segments))
        ):
            fail(
                "ComponentGenerationContextManifest.segments",
                "must use contiguous prompt order starting at zero",
            )
        identities = tuple(item.content_identity.uri for item in self.segments)
        if len(set(identities)) != len(identities):
            fail(
                "ComponentGenerationContextManifest.segments",
                "must not repeat content identities",
            )
        for item in self.segments:
            expected = _ALLOWED_VISIBILITY.get(item.authority_kind)
            if expected is None or item.visibility is not expected:
                fail(
                    f"ComponentGenerationContextManifest.segments[{item.index}]",
                    "contains authority that is forbidden from coding-agent context",
                )
            if item.authority_kind is ContextAuthorityKind.FRAMEWORK_ENVELOPE:
                if item.source_component_revision is not None:
                    fail(
                        f"ComponentGenerationContextManifest.segments[{item.index}]",
                        "framework envelope must not claim a source Component",
                    )
            elif item.source_component_revision is None:
                fail(
                    f"ComponentGenerationContextManifest.segments[{item.index}]",
                    "non-framework authority requires its exact source Component",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def byte_count(self) -> int:
        return sum(item.byte_count for item in self.segments)

    @property
    def estimated_tokens(self) -> int:
        return deterministic_token_estimate(self.byte_count)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "segments": [item.to_dict() for item in self.segments],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationContextManifest"
    ) -> ComponentGenerationContextManifest:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"component_revision", "generation_key_identity", "segments"}
            ),
        )
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            parse_tuple(
                data["segments"],
                f"{path}.segments",
                ComponentGenerationContextSegment.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class GenerationComplexityBudget:
    max_prompt_bytes: int
    max_estimated_tokens: int
    max_document_count: int
    max_direct_interface_bytes: int
    max_dependency_fan_in: int
    max_model_attempts: int
    max_wall_time_ms: int
    max_model_tokens: int
    max_cost_microunits: int

    SCHEMA: ClassVar[str] = GENERATION_COMPLEXITY_BUDGET_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "max_prompt_bytes",
            "max_estimated_tokens",
            "max_document_count",
            "max_direct_interface_bytes",
            "max_dependency_fan_in",
            "max_model_attempts",
            "max_wall_time_ms",
            "max_model_tokens",
            "max_cost_microunits",
        ):
            int_value(
                getattr(self, name),
                f"GenerationComplexityBudget.{name}",
                minimum=1,
                maximum=2**63 - 1,
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name)
                for name in (
                    "max_prompt_bytes",
                    "max_estimated_tokens",
                    "max_document_count",
                    "max_direct_interface_bytes",
                    "max_dependency_fan_in",
                    "max_model_attempts",
                    "max_wall_time_ms",
                    "max_model_tokens",
                    "max_cost_microunits",
                )
            },
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "GenerationComplexityBudget"
    ) -> GenerationComplexityBudget:
        names = frozenset(
            {
                "max_prompt_bytes",
                "max_estimated_tokens",
                "max_document_count",
                "max_direct_interface_bytes",
                "max_dependency_fan_in",
                "max_model_attempts",
                "max_wall_time_ms",
                "max_model_tokens",
                "max_cost_microunits",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            **{
                name: int_value(
                    data[name], f"{path}.{name}", minimum=1, maximum=2**63 - 1
                )
                for name in names
            }
        )


@dataclass(frozen=True, slots=True)
class ComplexityContributor:
    content_identity: ContentIdentity
    authority_kind: ContextAuthorityKind
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.content_identity, ContentIdentity):
            fail(
                "ComplexityContributor.content_identity",
                "must be a ContentIdentity",
            )
        if self.authority_kind not in _ALLOWED_VISIBILITY:
            fail(
                "ComplexityContributor.authority_kind",
                "must be permitted context authority",
            )
        int_value(self.byte_count, "ComplexityContributor.byte_count", maximum=2**40)

    def to_dict(self) -> dict[str, object]:
        return {
            "content_identity": self.content_identity.to_dict(),
            "authority_kind": self.authority_kind.value,
            "byte_count": self.byte_count,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComplexityContributor"
    ) -> ComplexityContributor:
        if not isinstance(value, dict):
            fail(path, "must be an object")
        if set(value) != {"content_identity", "authority_kind", "byte_count"}:
            fail(path, "must contain exact contributor fields")
        return cls(
            ContentIdentity.from_dict(
                value["content_identity"], path=f"{path}.content_identity"
            ),
            enum_value(
                ContextAuthorityKind,
                value["authority_kind"],
                f"{path}.authority_kind",
            ),
            int_value(value["byte_count"], f"{path}.byte_count", maximum=2**40),
        )


@dataclass(frozen=True, slots=True)
class GenerationComplexityDecision:
    manifest_identity: ContentIdentity
    budget_identity: ContentIdentity
    prompt_bytes: int
    estimated_tokens: int
    document_count: int
    direct_interface_bytes: int
    dependency_fan_in: int
    allowed: bool
    violations: tuple[str, ...]
    largest_local_contributors: tuple[ComplexityContributor, ...]
    largest_interface_contributors: tuple[ComplexityContributor, ...]

    SCHEMA: ClassVar[str] = GENERATION_COMPLEXITY_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.manifest_identity, ContentIdentity) or not isinstance(
            self.budget_identity, ContentIdentity
        ):
            fail(
                "GenerationComplexityDecision",
                "manifest and budget identities must be ContentIdentity values",
            )
        for name in (
            "prompt_bytes",
            "estimated_tokens",
            "document_count",
            "direct_interface_bytes",
            "dependency_fan_in",
        ):
            int_value(
                getattr(self, name),
                f"GenerationComplexityDecision.{name}",
                maximum=2**63 - 1,
            )
        if not isinstance(self.allowed, bool):
            fail("GenerationComplexityDecision.allowed", "must be a boolean")
        if tuple(sorted(set(self.violations))) != self.violations:
            fail(
                "GenerationComplexityDecision.violations",
                "must be unique and canonically ordered",
            )
        if self.allowed == bool(self.violations):
            fail(
                "GenerationComplexityDecision.allowed",
                "must be true exactly when there are no violations",
            )
        for name in (
            "largest_local_contributors",
            "largest_interface_contributors",
        ):
            values = getattr(self, name)
            if len(values) > 10 or any(
                not isinstance(item, ComplexityContributor) for item in values
            ):
                fail(f"GenerationComplexityDecision.{name}", "must be contributors")
            keys = tuple(
                (-item.byte_count, item.content_identity.uri) for item in values
            )
            if keys != tuple(sorted(keys)):
                fail(
                    f"GenerationComplexityDecision.{name}",
                    "must use descending byte count and identity tie-break order",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "manifest_identity": self.manifest_identity.to_dict(),
            "budget_identity": self.budget_identity.to_dict(),
            "prompt_bytes": self.prompt_bytes,
            "estimated_tokens": self.estimated_tokens,
            "document_count": self.document_count,
            "direct_interface_bytes": self.direct_interface_bytes,
            "dependency_fan_in": self.dependency_fan_in,
            "allowed": self.allowed,
            "violations": list(self.violations),
            "largest_local_contributors": [
                item.to_dict() for item in self.largest_local_contributors
            ],
            "largest_interface_contributors": [
                item.to_dict() for item in self.largest_interface_contributors
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "GenerationComplexityDecision"
    ) -> GenerationComplexityDecision:
        names = frozenset(
            {
                "manifest_identity",
                "budget_identity",
                "prompt_bytes",
                "estimated_tokens",
                "document_count",
                "direct_interface_bytes",
                "dependency_fan_in",
                "allowed",
                "violations",
                "largest_local_contributors",
                "largest_interface_contributors",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            ContentIdentity.from_dict(
                data["manifest_identity"], path=f"{path}.manifest_identity"
            ),
            ContentIdentity.from_dict(
                data["budget_identity"], path=f"{path}.budget_identity"
            ),
            int_value(data["prompt_bytes"], f"{path}.prompt_bytes"),
            int_value(data["estimated_tokens"], f"{path}.estimated_tokens"),
            int_value(data["document_count"], f"{path}.document_count"),
            int_value(data["direct_interface_bytes"], f"{path}.direct_interface_bytes"),
            int_value(data["dependency_fan_in"], f"{path}.dependency_fan_in"),
            bool_value(data["allowed"], f"{path}.allowed"),
            tuple(sorted(string_tuple(data["violations"], f"{path}.violations"))),
            parse_tuple(
                data["largest_local_contributors"],
                f"{path}.largest_local_contributors",
                ComplexityContributor.from_dict,
            ),
            parse_tuple(
                data["largest_interface_contributors"],
                f"{path}.largest_interface_contributors",
                ComplexityContributor.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class BoundedComponentGenerationRequest:
    component_generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    context_manifest: ComponentGenerationContextManifest
    budget: GenerationComplexityBudget
    budget_decision: GenerationComplexityDecision
    prompt_identity: ContentIdentity

    SCHEMA: ClassVar[str] = BOUNDED_COMPONENT_GENERATION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "complexity_decision_identity",
            "prompt_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(
                    f"BoundedComponentGenerationRequest.{name}",
                    "must be a ContentIdentity",
                )
        if (
            not isinstance(self.context_manifest, ComponentGenerationContextManifest)
            or not isinstance(self.budget, GenerationComplexityBudget)
            or not isinstance(self.budget_decision, GenerationComplexityDecision)
        ):
            fail(
                "BoundedComponentGenerationRequest",
                "must contain typed context, budget, and decision contracts",
            )
        if (
            self.context_manifest_identity != self.context_manifest.identity
            or self.complexity_decision_identity != self.budget_decision.identity
        ):
            fail(
                "BoundedComponentGenerationRequest",
                "explicit context and decision identities must bind their exact "
                "contracts",
            )
        if (
            self.context_manifest.generation_key_identity
            != self.generation_key_identity
        ):
            fail(
                "BoundedComponentGenerationRequest.context_manifest",
                "must bind the exact Component generation key",
            )
        if self.budget_decision.manifest_identity != self.context_manifest.identity:
            fail(
                "BoundedComponentGenerationRequest.budget_decision",
                "must bind the exact context manifest",
            )
        if self.budget_decision.budget_identity != self.budget.identity:
            fail(
                "BoundedComponentGenerationRequest.budget_decision",
                "must bind the exact complexity budget",
            )
        if not self.budget_decision.allowed:
            fail(
                "BoundedComponentGenerationRequest.budget_decision",
                "must allow model egress",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_generation_plan_identity": (
                self.component_generation_plan_identity.to_dict()
            ),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "context_manifest_identity": self.context_manifest_identity.to_dict(),
            "complexity_decision_identity": (
                self.complexity_decision_identity.to_dict()
            ),
            "context_manifest": self.context_manifest.to_dict(),
            "budget": self.budget.to_dict(),
            "budget_decision": self.budget_decision.to_dict(),
            "prompt_identity": self.prompt_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "BoundedComponentGenerationRequest"
    ) -> BoundedComponentGenerationRequest:
        names = frozenset(
            {
                "component_generation_plan_identity",
                "generation_key_identity",
                "context_manifest_identity",
                "complexity_decision_identity",
                "context_manifest",
                "budget",
                "budget_decision",
                "prompt_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            ContentIdentity.from_dict(
                data["component_generation_plan_identity"],
                path=f"{path}.component_generation_plan_identity",
            ),
            ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            ContentIdentity.from_dict(
                data["context_manifest_identity"],
                path=f"{path}.context_manifest_identity",
            ),
            ContentIdentity.from_dict(
                data["complexity_decision_identity"],
                path=f"{path}.complexity_decision_identity",
            ),
            ComponentGenerationContextManifest.from_dict(
                data["context_manifest"], path=f"{path}.context_manifest"
            ),
            GenerationComplexityBudget.from_dict(data["budget"], path=f"{path}.budget"),
            GenerationComplexityDecision.from_dict(
                data["budget_decision"], path=f"{path}.budget_decision"
            ),
            ContentIdentity.from_dict(
                data["prompt_identity"], path=f"{path}.prompt_identity"
            ),
        )


__all__ = [
    "BOUNDED_COMPONENT_GENERATION_REQUEST_SCHEMA",
    "COMPONENT_GENERATION_CONTEXT_MANIFEST_SCHEMA",
    "GENERATION_COMPLEXITY_BUDGET_SCHEMA",
    "GENERATION_COMPLEXITY_DECISION_SCHEMA",
    "BoundedComponentGenerationRequest",
    "ComplexityContributor",
    "ComponentGenerationContextManifest",
    "ComponentGenerationContextSegment",
    "ContextAuthorityKind",
    "ContextVisibility",
    "GenerationComplexityBudget",
    "GenerationComplexityDecision",
    "deterministic_token_estimate",
]

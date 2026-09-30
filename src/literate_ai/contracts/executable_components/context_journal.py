"""Privacy-safe journals and measurements for bounded forward generation context.

These records deliberately retain identities, classifications, and measurements while
omitting prompt bytes, segment reasons, and authority content.  Exact content remains
bound by the context-manifest and prompt identities without being copied into durable
benchmark or cache-report surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    enum_value,
    fail,
    int_value,
    parse_tuple,
)
from ..identity import ContentIdentity, canonical_identity, contract_identity
from ._common import identity
from .context import (
    BoundedComponentGenerationRequest,
    ComponentGenerationContextSegment,
    ContextAuthorityKind,
    ContextVisibility,
    GenerationComplexityBudget,
    GenerationComplexityDecision,
    deterministic_token_estimate,
)
from .source_generation import (
    SourceGenerationDisposition,
    SourceGenerationNodeResult,
)

FORWARD_GENERATION_CONTEXT_SEGMENT_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v2:forward-generation-context-segment-binding"
)
DIRECT_INTERFACE_CONTEXT_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v2:direct-interface-context-binding"
)
FORWARD_GENERATION_PROMPT_JOURNAL_SCHEMA = (
    "urn:literate-ai:schema:v2:forward-generation-prompt-journal"
)
COMPONENT_CONTEXT_BENCHMARK_RECORD_SCHEMA = (
    "urn:literate-ai:schema:v2:component-context-benchmark-record"
)
FORWARD_GENERATION_CONTEXT_CACHE_ENTRY_SCHEMA = (
    "urn:literate-ai:schema:v2:forward-generation-context-cache-entry"
)
FORWARD_GENERATION_CONTEXT_CACHE_REPORT_SCHEMA = (
    "urn:literate-ai:schema:v2:forward-generation-context-cache-report"
)

_SAFE_JOURNAL_VISIBILITY = {
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


def _optional_identity(value: object, path: str) -> ContentIdentity | None:
    if value is None:
        return None
    return ContentIdentity.from_dict(value, path=path)


def _optional_count(value: object, path: str) -> int | None:
    if value is None:
        return None
    return int_value(value, path, maximum=2**63 - 1)


@dataclass(frozen=True, slots=True)
class ForwardGenerationContextSegmentBinding:
    """One manifest segment with no content or prompt instruction text."""

    index: int
    authority_kind: ContextAuthorityKind
    source_component_revision: ContentIdentity | None
    content_identity: ContentIdentity
    byte_count: int
    estimated_tokens: int
    visibility: ContextVisibility

    SCHEMA: ClassVar[str] = FORWARD_GENERATION_CONTEXT_SEGMENT_BINDING_SCHEMA

    def __post_init__(self) -> None:
        int_value(
            self.index,
            "ForwardGenerationContextSegmentBinding.index",
            maximum=65535,
        )
        if not isinstance(self.authority_kind, ContextAuthorityKind):
            fail(
                "ForwardGenerationContextSegmentBinding.authority_kind",
                "must be a ContextAuthorityKind",
            )
        if self.source_component_revision is not None:
            identity(
                self.source_component_revision,
                "ForwardGenerationContextSegmentBinding.source_component_revision",
            )
        identity(
            self.content_identity,
            "ForwardGenerationContextSegmentBinding.content_identity",
        )
        int_value(
            self.byte_count,
            "ForwardGenerationContextSegmentBinding.byte_count",
            maximum=2**40,
        )
        int_value(
            self.estimated_tokens,
            "ForwardGenerationContextSegmentBinding.estimated_tokens",
            maximum=2**40,
        )
        if not isinstance(self.visibility, ContextVisibility):
            fail(
                "ForwardGenerationContextSegmentBinding.visibility",
                "must be a ContextVisibility",
            )
        if _SAFE_JOURNAL_VISIBILITY.get(self.authority_kind) is not self.visibility:
            fail(
                "ForwardGenerationContextSegmentBinding.visibility",
                "authority kind is forbidden or has the wrong journal visibility",
            )
        if self.authority_kind is ContextAuthorityKind.FRAMEWORK_ENVELOPE:
            if self.source_component_revision is not None:
                fail(
                    "ForwardGenerationContextSegmentBinding.source_component_revision",
                    "framework authority must not claim a source Component",
                )
        elif self.source_component_revision is None:
            fail(
                "ForwardGenerationContextSegmentBinding.source_component_revision",
                "non-framework authority requires its source Component",
            )
        if self.estimated_tokens != deterministic_token_estimate(self.byte_count):
            fail(
                "ForwardGenerationContextSegmentBinding.estimated_tokens",
                "must use the deterministic context token estimate",
            )

    @classmethod
    def from_manifest_segment(
        cls, segment: ComponentGenerationContextSegment
    ) -> ForwardGenerationContextSegmentBinding:
        if not isinstance(segment, ComponentGenerationContextSegment):
            raise TypeError("segment must be a ComponentGenerationContextSegment")
        return cls(
            segment.index,
            segment.authority_kind,
            segment.source_component_revision,
            segment.content_identity,
            segment.byte_count,
            segment.estimated_tokens,
            segment.visibility,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "index": self.index,
            "authority_kind": self.authority_kind.value,
            "source_component_revision": (
                None
                if self.source_component_revision is None
                else self.source_component_revision.to_dict()
            ),
            "content_identity": self.content_identity.to_dict(),
            "byte_count": self.byte_count,
            "estimated_tokens": self.estimated_tokens,
            "visibility": self.visibility.value,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "ForwardGenerationContextSegmentBinding",
    ) -> ForwardGenerationContextSegmentBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "index",
                    "authority_kind",
                    "source_component_revision",
                    "content_identity",
                    "byte_count",
                    "estimated_tokens",
                    "visibility",
                }
            ),
        )
        return cls(
            int_value(data["index"], f"{path}.index", maximum=65535),
            enum_value(
                ContextAuthorityKind,
                data["authority_kind"],
                f"{path}.authority_kind",
            ),
            _optional_identity(
                data["source_component_revision"],
                f"{path}.source_component_revision",
            ),
            ContentIdentity.from_dict(
                data["content_identity"], path=f"{path}.content_identity"
            ),
            int_value(data["byte_count"], f"{path}.byte_count", maximum=2**40),
            int_value(
                data["estimated_tokens"],
                f"{path}.estimated_tokens",
                maximum=2**40,
            ),
            enum_value(ContextVisibility, data["visibility"], f"{path}.visibility"),
        )


@dataclass(frozen=True, slots=True)
class DirectInterfaceContextBinding:
    """Exact public interface bytes admitted from one direct dependency."""

    prompt_index: int
    provider_component_revision: ContentIdentity
    public_interface_identity: ContentIdentity
    byte_count: int
    estimated_tokens: int

    SCHEMA: ClassVar[str] = DIRECT_INTERFACE_CONTEXT_BINDING_SCHEMA

    def __post_init__(self) -> None:
        int_value(
            self.prompt_index,
            "DirectInterfaceContextBinding.prompt_index",
            maximum=65535,
        )
        identity(
            self.provider_component_revision,
            "DirectInterfaceContextBinding.provider_component_revision",
        )
        identity(
            self.public_interface_identity,
            "DirectInterfaceContextBinding.public_interface_identity",
        )
        int_value(
            self.byte_count,
            "DirectInterfaceContextBinding.byte_count",
            maximum=2**40,
        )
        int_value(
            self.estimated_tokens,
            "DirectInterfaceContextBinding.estimated_tokens",
            maximum=2**40,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "prompt_index": self.prompt_index,
            "provider_component_revision": self.provider_component_revision.to_dict(),
            "public_interface_identity": self.public_interface_identity.to_dict(),
            "byte_count": self.byte_count,
            "estimated_tokens": self.estimated_tokens,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "DirectInterfaceContextBinding"
    ) -> DirectInterfaceContextBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "prompt_index",
                    "provider_component_revision",
                    "public_interface_identity",
                    "byte_count",
                    "estimated_tokens",
                }
            ),
        )
        return cls(
            int_value(data["prompt_index"], f"{path}.prompt_index", maximum=65535),
            ContentIdentity.from_dict(
                data["provider_component_revision"],
                path=f"{path}.provider_component_revision",
            ),
            ContentIdentity.from_dict(
                data["public_interface_identity"],
                path=f"{path}.public_interface_identity",
            ),
            int_value(data["byte_count"], f"{path}.byte_count", maximum=2**40),
            int_value(
                data["estimated_tokens"],
                f"{path}.estimated_tokens",
                maximum=2**40,
            ),
        )


@dataclass(frozen=True, slots=True)
class ForwardGenerationPromptJournal:
    """Durable metadata journal for one bounded forward-generation prompt.

    No field can contain raw prompt, specification, skill, workflow, routing, or public
    interface bytes.  Their exact identities and measured segment boundaries remain
    auditable through the bound context manifest.
    """

    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    bounded_request_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_budget_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    prompt_identity: ContentIdentity
    complexity_budget: GenerationComplexityBudget
    complexity_decision: GenerationComplexityDecision
    segment_bindings: tuple[ForwardGenerationContextSegmentBinding, ...]
    direct_interface_bindings: tuple[DirectInterfaceContextBinding, ...]
    node_result_identity: ContentIdentity

    SCHEMA: ClassVar[str] = FORWARD_GENERATION_PROMPT_JOURNAL_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
            "bounded_request_identity",
            "context_manifest_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
            "prompt_identity",
            "node_result_identity",
        ):
            identity(getattr(self, name), f"ForwardGenerationPromptJournal.{name}")
        if not isinstance(self.complexity_budget, GenerationComplexityBudget):
            fail(
                "ForwardGenerationPromptJournal.complexity_budget",
                "must be a GenerationComplexityBudget",
            )
        if not isinstance(self.complexity_decision, GenerationComplexityDecision):
            fail(
                "ForwardGenerationPromptJournal.complexity_decision",
                "must be a GenerationComplexityDecision",
            )
        if (
            self.complexity_budget.identity != self.complexity_budget_identity
            or self.complexity_decision.identity != self.complexity_decision_identity
            or self.complexity_decision.manifest_identity
            != self.context_manifest_identity
            or self.complexity_decision.budget_identity
            != self.complexity_budget_identity
        ):
            fail(
                "ForwardGenerationPromptJournal",
                "budget, decision, and manifest identities do not form one exact "
                "binding",
            )
        if not self.segment_bindings:
            fail(
                "ForwardGenerationPromptJournal.segment_bindings",
                "must not be empty",
            )
        if tuple(item.index for item in self.segment_bindings) != tuple(
            range(len(self.segment_bindings))
        ):
            fail(
                "ForwardGenerationPromptJournal.segment_bindings",
                "must preserve contiguous manifest prompt order",
            )
        if any(
            not isinstance(item, ForwardGenerationContextSegmentBinding)
            for item in self.segment_bindings
        ):
            fail(
                "ForwardGenerationPromptJournal.segment_bindings",
                "must contain typed segment bindings",
            )
        if len({item.content_identity.uri for item in self.segment_bindings}) != len(
            self.segment_bindings
        ):
            fail(
                "ForwardGenerationPromptJournal.segment_bindings",
                "must not repeat exact context content identities",
            )
        for item in self.segment_bindings:
            if item.visibility is ContextVisibility.LOCAL_AUTHORITY and (
                item.source_component_revision != self.component_revision
            ):
                fail(
                    "ForwardGenerationPromptJournal.segment_bindings",
                    "local authority must belong to the generated Component",
                )
        expected_interfaces = tuple(
            DirectInterfaceContextBinding(
                item.index,
                item.source_component_revision,
                item.content_identity,
                item.byte_count,
                item.estimated_tokens,
            )
            for item in self.segment_bindings
            if item.visibility is ContextVisibility.DIRECT_PUBLIC_INTERFACE
            and item.source_component_revision is not None
        )
        if self.direct_interface_bindings != expected_interfaces:
            fail(
                "ForwardGenerationPromptJournal.direct_interface_bindings",
                "must exactly project every direct public-interface segment",
            )
        if (
            sum(item.byte_count for item in self.segment_bindings)
            != self.complexity_decision.prompt_bytes
            or deterministic_token_estimate(
                sum(item.byte_count for item in self.segment_bindings)
            )
            != self.complexity_decision.estimated_tokens
            or len(self.segment_bindings) != self.complexity_decision.document_count
            or sum(item.byte_count for item in self.direct_interface_bindings)
            != self.complexity_decision.direct_interface_bytes
            or len(
                {
                    item.provider_component_revision.uri
                    for item in self.direct_interface_bindings
                }
            )
            != self.complexity_decision.dependency_fan_in
        ):
            fail(
                "ForwardGenerationPromptJournal",
                "journal measurements differ from the exact complexity decision",
            )
        if not self.complexity_decision.allowed:
            fail(
                "ForwardGenerationPromptJournal.complexity_decision",
                "durable prompt journal requires an allowed pre-egress decision",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generation_plan_identity": self.generation_plan_identity.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "bounded_request_identity": self.bounded_request_identity.to_dict(),
            "context_manifest_identity": self.context_manifest_identity.to_dict(),
            "complexity_budget_identity": self.complexity_budget_identity.to_dict(),
            "complexity_decision_identity": self.complexity_decision_identity.to_dict(),
            "prompt_identity": self.prompt_identity.to_dict(),
            "complexity_budget": self.complexity_budget.to_dict(),
            "complexity_decision": self.complexity_decision.to_dict(),
            "segment_bindings": [item.to_dict() for item in self.segment_bindings],
            "direct_interface_bindings": [
                item.to_dict() for item in self.direct_interface_bindings
            ],
            "node_result_identity": self.node_result_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ForwardGenerationPromptJournal"
    ) -> ForwardGenerationPromptJournal:
        names = frozenset(
            {
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
                "bounded_request_identity",
                "context_manifest_identity",
                "complexity_budget_identity",
                "complexity_decision_identity",
                "prompt_identity",
                "complexity_budget",
                "complexity_decision",
                "segment_bindings",
                "direct_interface_bindings",
                "node_result_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            generation_plan_identity=ContentIdentity.from_dict(
                data["generation_plan_identity"],
                path=f"{path}.generation_plan_identity",
            ),
            generation_key_identity=ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            bounded_request_identity=ContentIdentity.from_dict(
                data["bounded_request_identity"],
                path=f"{path}.bounded_request_identity",
            ),
            context_manifest_identity=ContentIdentity.from_dict(
                data["context_manifest_identity"],
                path=f"{path}.context_manifest_identity",
            ),
            complexity_budget_identity=ContentIdentity.from_dict(
                data["complexity_budget_identity"],
                path=f"{path}.complexity_budget_identity",
            ),
            complexity_decision_identity=ContentIdentity.from_dict(
                data["complexity_decision_identity"],
                path=f"{path}.complexity_decision_identity",
            ),
            prompt_identity=ContentIdentity.from_dict(
                data["prompt_identity"], path=f"{path}.prompt_identity"
            ),
            complexity_budget=GenerationComplexityBudget.from_dict(
                data["complexity_budget"], path=f"{path}.complexity_budget"
            ),
            complexity_decision=GenerationComplexityDecision.from_dict(
                data["complexity_decision"], path=f"{path}.complexity_decision"
            ),
            segment_bindings=parse_tuple(
                data["segment_bindings"],
                f"{path}.segment_bindings",
                ForwardGenerationContextSegmentBinding.from_dict,
            ),
            direct_interface_bindings=parse_tuple(
                data["direct_interface_bindings"],
                f"{path}.direct_interface_bindings",
                DirectInterfaceContextBinding.from_dict,
            ),
            node_result_identity=ContentIdentity.from_dict(
                data["node_result_identity"], path=f"{path}.node_result_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentContextBenchmarkRecord:
    """Benchmark-facing context metrics with exact journal and result custody."""

    prompt_journal_identity: ContentIdentity
    component_revision: ContentIdentity
    node_result_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_budget_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    disposition: SourceGenerationDisposition
    framework_context_bytes: int
    local_authority_bytes: int
    delivered_dependency_context_bytes: int
    selected_direct_public_interface_bytes: int
    private_transitive_leakage_bytes: int
    prompt_bytes: int
    estimated_tokens: int
    actual_model_tokens: int | None
    document_count: int
    dependency_fan_in: int

    SCHEMA: ClassVar[str] = COMPONENT_CONTEXT_BENCHMARK_RECORD_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "prompt_journal_identity",
            "component_revision",
            "node_result_identity",
            "context_manifest_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
        ):
            identity(getattr(self, name), f"ComponentContextBenchmarkRecord.{name}")
        if not isinstance(self.disposition, SourceGenerationDisposition):
            fail(
                "ComponentContextBenchmarkRecord.disposition",
                "must be a SourceGenerationDisposition",
            )
        for name in (
            "framework_context_bytes",
            "local_authority_bytes",
            "delivered_dependency_context_bytes",
            "selected_direct_public_interface_bytes",
            "private_transitive_leakage_bytes",
            "prompt_bytes",
            "estimated_tokens",
            "document_count",
            "dependency_fan_in",
        ):
            int_value(
                getattr(self, name),
                f"ComponentContextBenchmarkRecord.{name}",
                maximum=2**63 - 1,
            )
        _optional_count(
            self.actual_model_tokens,
            "ComponentContextBenchmarkRecord.actual_model_tokens",
        )
        if self.private_transitive_leakage_bytes != 0:
            fail(
                "ComponentContextBenchmarkRecord.private_transitive_leakage_bytes",
                "a bounded forward-generation record must report zero leakage",
            )
        if (
            self.delivered_dependency_context_bytes
            != self.selected_direct_public_interface_bytes
        ):
            fail(
                "ComponentContextBenchmarkRecord.delivered_dependency_context_bytes",
                "dependency context must equal selected direct public-interface bytes",
            )
        if (
            self.framework_context_bytes
            + self.local_authority_bytes
            + self.delivered_dependency_context_bytes
            != self.prompt_bytes
        ):
            fail(
                "ComponentContextBenchmarkRecord.prompt_bytes",
                "context-class byte totals must equal the measured prompt bytes",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "prompt_journal_identity": self.prompt_journal_identity.to_dict(),
            "component_revision": self.component_revision.to_dict(),
            "node_result_identity": self.node_result_identity.to_dict(),
            "context_manifest_identity": self.context_manifest_identity.to_dict(),
            "complexity_budget_identity": self.complexity_budget_identity.to_dict(),
            "complexity_decision_identity": (
                self.complexity_decision_identity.to_dict()
            ),
            "disposition": self.disposition.value,
            "framework_context_bytes": self.framework_context_bytes,
            "local_authority_bytes": self.local_authority_bytes,
            "delivered_dependency_context_bytes": (
                self.delivered_dependency_context_bytes
            ),
            "selected_direct_public_interface_bytes": (
                self.selected_direct_public_interface_bytes
            ),
            "private_transitive_leakage_bytes": self.private_transitive_leakage_bytes,
            "prompt_bytes": self.prompt_bytes,
            "estimated_tokens": self.estimated_tokens,
            "actual_model_tokens": self.actual_model_tokens,
            "document_count": self.document_count,
            "dependency_fan_in": self.dependency_fan_in,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentContextBenchmarkRecord"
    ) -> ComponentContextBenchmarkRecord:
        names = frozenset(
            {
                "prompt_journal_identity",
                "component_revision",
                "node_result_identity",
                "context_manifest_identity",
                "complexity_budget_identity",
                "complexity_decision_identity",
                "disposition",
                "framework_context_bytes",
                "local_authority_bytes",
                "delivered_dependency_context_bytes",
                "selected_direct_public_interface_bytes",
                "private_transitive_leakage_bytes",
                "prompt_bytes",
                "estimated_tokens",
                "actual_model_tokens",
                "document_count",
                "dependency_fan_in",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            prompt_journal_identity=ContentIdentity.from_dict(
                data["prompt_journal_identity"],
                path=f"{path}.prompt_journal_identity",
            ),
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            node_result_identity=ContentIdentity.from_dict(
                data["node_result_identity"], path=f"{path}.node_result_identity"
            ),
            context_manifest_identity=ContentIdentity.from_dict(
                data["context_manifest_identity"],
                path=f"{path}.context_manifest_identity",
            ),
            complexity_budget_identity=ContentIdentity.from_dict(
                data["complexity_budget_identity"],
                path=f"{path}.complexity_budget_identity",
            ),
            complexity_decision_identity=ContentIdentity.from_dict(
                data["complexity_decision_identity"],
                path=f"{path}.complexity_decision_identity",
            ),
            disposition=enum_value(
                SourceGenerationDisposition,
                data["disposition"],
                f"{path}.disposition",
            ),
            framework_context_bytes=int_value(
                data["framework_context_bytes"], f"{path}.framework_context_bytes"
            ),
            local_authority_bytes=int_value(
                data["local_authority_bytes"], f"{path}.local_authority_bytes"
            ),
            delivered_dependency_context_bytes=int_value(
                data["delivered_dependency_context_bytes"],
                f"{path}.delivered_dependency_context_bytes",
            ),
            selected_direct_public_interface_bytes=int_value(
                data["selected_direct_public_interface_bytes"],
                f"{path}.selected_direct_public_interface_bytes",
            ),
            private_transitive_leakage_bytes=int_value(
                data["private_transitive_leakage_bytes"],
                f"{path}.private_transitive_leakage_bytes",
            ),
            prompt_bytes=int_value(data["prompt_bytes"], f"{path}.prompt_bytes"),
            estimated_tokens=int_value(
                data["estimated_tokens"], f"{path}.estimated_tokens"
            ),
            actual_model_tokens=_optional_count(
                data["actual_model_tokens"], f"{path}.actual_model_tokens"
            ),
            document_count=int_value(data["document_count"], f"{path}.document_count"),
            dependency_fan_in=int_value(
                data["dependency_fan_in"], f"{path}.dependency_fan_in"
            ),
        )


class ContextCacheOutcome(StrEnum):
    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"


@dataclass(frozen=True, slots=True)
class ForwardGenerationContextCacheEntry:
    """Cache-facing context binding; contains neither prompt nor authority bytes."""

    component_revision: ContentIdentity
    cache_key_identity: ContentIdentity
    prompt_journal_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_budget_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    direct_interface_binding_identity: ContentIdentity
    outcome: ContextCacheOutcome

    SCHEMA: ClassVar[str] = FORWARD_GENERATION_CONTEXT_CACHE_ENTRY_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "cache_key_identity",
            "prompt_journal_identity",
            "context_manifest_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
            "direct_interface_binding_identity",
        ):
            identity(getattr(self, name), f"ForwardGenerationContextCacheEntry.{name}")
        if not isinstance(self.outcome, ContextCacheOutcome):
            fail(
                "ForwardGenerationContextCacheEntry.outcome",
                "must be a ContextCacheOutcome",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "component_revision",
                    "cache_key_identity",
                    "prompt_journal_identity",
                    "context_manifest_identity",
                    "complexity_budget_identity",
                    "complexity_decision_identity",
                    "direct_interface_binding_identity",
                )
            },
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ForwardGenerationContextCacheEntry"
    ) -> ForwardGenerationContextCacheEntry:
        identity_names = frozenset(
            {
                "component_revision",
                "cache_key_identity",
                "prompt_journal_identity",
                "context_manifest_identity",
                "complexity_budget_identity",
                "complexity_decision_identity",
                "direct_interface_binding_identity",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=identity_names | {"outcome"},
        )
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            outcome=enum_value(ContextCacheOutcome, data["outcome"], f"{path}.outcome"),
        )


@dataclass(frozen=True, slots=True)
class ForwardGenerationContextCacheReport:
    """Canonical aggregate of privacy-safe per-Component cache context evidence."""

    entries: tuple[ForwardGenerationContextCacheEntry, ...]

    SCHEMA: ClassVar[str] = FORWARD_GENERATION_CONTEXT_CACHE_REPORT_SCHEMA

    def __post_init__(self) -> None:
        if any(
            not isinstance(item, ForwardGenerationContextCacheEntry)
            for item in self.entries
        ):
            fail(
                "ForwardGenerationContextCacheReport.entries",
                "must contain typed context cache entries",
            )
        keys = tuple(
            (item.component_revision.uri, item.cache_key_identity.uri)
            for item in self.entries
        )
        if keys != tuple(sorted(set(keys))):
            fail(
                "ForwardGenerationContextCacheReport.entries",
                "must be unique and canonical by Component and cache key",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def hits(self) -> int:
        return sum(item.outcome is ContextCacheOutcome.HIT for item in self.entries)

    @property
    def misses(self) -> int:
        return sum(item.outcome is ContextCacheOutcome.MISS for item in self.entries)

    @property
    def bypasses(self) -> int:
        return sum(item.outcome is ContextCacheOutcome.BYPASS for item in self.entries)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "entries": [item.to_dict() for item in self.entries],
            "hits": self.hits,
            "misses": self.misses,
            "bypasses": self.bypasses,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ForwardGenerationContextCacheReport"
    ) -> ForwardGenerationContextCacheReport:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"entries", "hits", "misses", "bypasses"}),
        )
        report = cls(
            parse_tuple(
                data["entries"],
                f"{path}.entries",
                ForwardGenerationContextCacheEntry.from_dict,
            )
        )
        for name in ("hits", "misses", "bypasses"):
            if int_value(data[name], f"{path}.{name}") != getattr(report, name):
                fail(f"{path}.{name}", "does not match the exact entry set")
        return report


def create_forward_generation_prompt_journal(
    request: BoundedComponentGenerationRequest,
    result: SourceGenerationNodeResult,
) -> ForwardGenerationPromptJournal:
    """Project one exact bounded request/result pair into a safe durable journal."""

    if not isinstance(request, BoundedComponentGenerationRequest):
        raise TypeError("request must be a BoundedComponentGenerationRequest")
    if not isinstance(result, SourceGenerationNodeResult):
        raise TypeError("result must be a SourceGenerationNodeResult")
    if (
        result.component_revision != request.context_manifest.component_revision
        or result.generation_plan_identity != request.component_generation_plan_identity
        or result.generation_key_identity != request.generation_key_identity
        or result.context_manifest_identity != request.context_manifest_identity
        or result.complexity_budget_identity != request.budget.identity
        or result.complexity_decision_identity != request.complexity_decision_identity
        or result.prompt_identity != request.prompt_identity
    ):
        fail(
            "create_forward_generation_prompt_journal",
            "node result does not bind the exact bounded generation request",
        )
    segments = tuple(
        ForwardGenerationContextSegmentBinding.from_manifest_segment(item)
        for item in request.context_manifest.segments
    )
    interfaces = tuple(
        DirectInterfaceContextBinding(
            item.index,
            item.source_component_revision,
            item.content_identity,
            item.byte_count,
            item.estimated_tokens,
        )
        for item in segments
        if item.visibility is ContextVisibility.DIRECT_PUBLIC_INTERFACE
        and item.source_component_revision is not None
    )
    return ForwardGenerationPromptJournal(
        request.context_manifest.component_revision,
        request.component_generation_plan_identity,
        request.generation_key_identity,
        request.identity,
        request.context_manifest_identity,
        request.budget.identity,
        request.complexity_decision_identity,
        request.prompt_identity,
        request.budget,
        request.budget_decision,
        segments,
        interfaces,
        result.identity,
    )


def create_component_context_benchmark_record(
    journal: ForwardGenerationPromptJournal,
    result: SourceGenerationNodeResult,
) -> ComponentContextBenchmarkRecord:
    """Create exact benchmark metrics without re-emitting context content."""

    if not isinstance(journal, ForwardGenerationPromptJournal):
        raise TypeError("journal must be a ForwardGenerationPromptJournal")
    if not isinstance(result, SourceGenerationNodeResult):
        raise TypeError("result must be a SourceGenerationNodeResult")
    if (
        result.identity != journal.node_result_identity
        or result.component_revision != journal.component_revision
    ):
        fail(
            "create_component_context_benchmark_record",
            "node result does not bind the exact prompt journal",
        )
    framework_bytes = sum(
        item.byte_count
        for item in journal.segment_bindings
        if item.visibility is ContextVisibility.FRAMEWORK
    )
    local_bytes = sum(
        item.byte_count
        for item in journal.segment_bindings
        if item.visibility is ContextVisibility.LOCAL_AUTHORITY
    )
    interface_bytes = sum(item.byte_count for item in journal.direct_interface_bindings)
    observation = result.runtime_observation
    return ComponentContextBenchmarkRecord(
        journal.identity,
        journal.component_revision,
        result.identity,
        journal.context_manifest_identity,
        journal.complexity_budget_identity,
        journal.complexity_decision_identity,
        result.disposition,
        framework_bytes,
        local_bytes,
        interface_bytes,
        interface_bytes,
        0,
        journal.complexity_decision.prompt_bytes,
        journal.complexity_decision.estimated_tokens,
        None if observation is None else observation.model_tokens,
        journal.complexity_decision.document_count,
        journal.complexity_decision.dependency_fan_in,
    )


def create_forward_generation_context_cache_entry(
    journal: ForwardGenerationPromptJournal,
    *,
    cache_key_identity: ContentIdentity,
    outcome: ContextCacheOutcome,
) -> ForwardGenerationContextCacheEntry:
    if not isinstance(journal, ForwardGenerationPromptJournal):
        raise TypeError("journal must be a ForwardGenerationPromptJournal")
    identity(cache_key_identity, "cache_key_identity")
    interface_identity = canonical_identity(
        [item.to_dict() for item in journal.direct_interface_bindings]
    )
    return ForwardGenerationContextCacheEntry(
        journal.component_revision,
        cache_key_identity,
        journal.identity,
        journal.context_manifest_identity,
        journal.complexity_budget_identity,
        journal.complexity_decision_identity,
        interface_identity,
        outcome,
    )


__all__ = [
    "COMPONENT_CONTEXT_BENCHMARK_RECORD_SCHEMA",
    "DIRECT_INTERFACE_CONTEXT_BINDING_SCHEMA",
    "FORWARD_GENERATION_CONTEXT_CACHE_ENTRY_SCHEMA",
    "FORWARD_GENERATION_CONTEXT_CACHE_REPORT_SCHEMA",
    "FORWARD_GENERATION_CONTEXT_SEGMENT_BINDING_SCHEMA",
    "FORWARD_GENERATION_PROMPT_JOURNAL_SCHEMA",
    "ComponentContextBenchmarkRecord",
    "ContextCacheOutcome",
    "DirectInterfaceContextBinding",
    "ForwardGenerationContextCacheEntry",
    "ForwardGenerationContextCacheReport",
    "ForwardGenerationContextSegmentBinding",
    "ForwardGenerationPromptJournal",
    "create_component_context_benchmark_record",
    "create_forward_generation_context_cache_entry",
    "create_forward_generation_prompt_journal",
]

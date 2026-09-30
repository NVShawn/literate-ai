"""Pure projection of one Component plan into bounded coding-agent context."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from literate_ai.contracts.executable_components.context import (
    BoundedComponentGenerationRequest,
    ComplexityContributor,
    ComponentGenerationContextManifest,
    ComponentGenerationContextSegment,
    ContextAuthorityKind,
    ContextVisibility,
    GenerationComplexityBudget,
    GenerationComplexityDecision,
    deterministic_token_estimate,
)
from literate_ai.contracts.executable_components.planning import ComponentGenerationPlan
from literate_ai.contracts.generation_cache import SourceDerivationCacheKey
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
)


class ComponentGenerationContextError(ValueError):
    """A candidate context violated the pre-egress projection boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GenerationComplexityBudgetExceeded(ComponentGenerationContextError):
    """The exact decision is retained even though no request may leave the process."""

    def __init__(self, decision: GenerationComplexityDecision) -> None:
        self.decision = decision
        local = _contributor_summary(decision.largest_local_contributors)
        interfaces = _contributor_summary(decision.largest_interface_contributors)
        super().__init__(
            "generation_context.budget_exceeded",
            "generation context exceeds: "
            + ", ".join(decision.violations)
            + f"; largest local contributors: {local}; largest direct interfaces: "
            + interfaces
            + "; refactor the Component or narrow its public interfaces explicitly",
        )


@dataclass(frozen=True, slots=True)
class PromptSegmentInput:
    """Exact bytes proposed for one authority reference in a generation plan."""

    authority_kind: ContextAuthorityKind
    source_component_revision: ContentIdentity
    reason: str
    content_identity: ContentIdentity
    content: bytes


@dataclass(frozen=True, slots=True)
class PreparedComponentGenerationRequest:
    """Identity-bound request plus prompt bytes; adapters receive this seam only."""

    request: BoundedComponentGenerationRequest
    prompt: bytes


def bind_component_generation_context_cache_key(
    cache_key: SourceDerivationCacheKey,
    prepared: PreparedComponentGenerationRequest,
) -> SourceDerivationCacheKey:
    """Bind only exact bounded prompt authority into a source-cache request key.

    The Component plan identity deliberately binds the complete graph, so it is not a
    suitable node-source invalidation key: unrelated sibling changes would cause a
    miss.  The context manifest already contains every local authority and direct
    public interface permitted to affect this Component.  Its decision additionally
    proves that the measured prompt was allowed to leave the process.
    """

    if not isinstance(cache_key, SourceDerivationCacheKey):
        raise TypeError("cache_key must be a SourceDerivationCacheKey")
    if not isinstance(prepared, PreparedComponentGenerationRequest):
        raise TypeError("prepared must be a PreparedComponentGenerationRequest")
    request = prepared.request
    if cache_key.request_identity != request.prompt_identity:
        raise ComponentGenerationContextError(
            "generation_context.cache_prompt_mismatch",
            "source cache key must initially bind this exact prepared prompt",
        )
    bounded_request_identity = canonical_identity(
        {
            "schema": "literate-ai/bounded-component-source-cache-request@1",
            "prompt_identity": request.prompt_identity.uri,
            "context_manifest_identity": request.context_manifest_identity.uri,
            "complexity_decision_identity": request.complexity_decision_identity.uri,
        }
    )
    return replace(cache_key, request_identity=bounded_request_identity)


def _raw_identity(content: bytes) -> ContentIdentity:
    return ContentIdentity.parse_uri(f"sha256:{hashlib.sha256(content).hexdigest()}")


def _contributor_summary(values: tuple[ComplexityContributor, ...]) -> str:
    if not values:
        return "none"
    return ", ".join(
        f"{item.authority_kind.value}:{item.content_identity.uri} ({item.byte_count}B)"
        for item in values[:3]
    )


def _allowed_identity_order(
    plan: ComponentGenerationPlan,
) -> dict[ContextAuthorityKind, tuple[str, ...]]:
    key = plan.generation_key
    return {
        ContextAuthorityKind.LOCAL_SPECIFICATION: tuple(
            item.uri for item in key.specification_identities
        ),
        ContextAuthorityKind.LOCAL_FLAVOR: tuple(
            item.uri for item in key.flavor_identities
        ),
        ContextAuthorityKind.LOCAL_SKILL: tuple(
            item.uri for item in key.skill_identities
        ),
        ContextAuthorityKind.LOCAL_WORKFLOW: (key.workflow_identity.uri,),
        ContextAuthorityKind.LOCAL_ROUTING_POLICY: (key.routing_identity.uri,),
        ContextAuthorityKind.LOCAL_PUBLIC_INTERFACE: tuple(
            item.uri for item in key.exported_public_interface_identities
        ),
        ContextAuthorityKind.LOCAL_ASSET_METADATA: tuple(
            item.uri for item in key.asset_identities
        ),
        ContextAuthorityKind.LOCAL_NATIVE_SDK_METADATA: tuple(
            item.uri for item in key.native_sdk_input_identities
        ),
        ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE: tuple(
            item.uri for item in key.direct_public_interface_identities
        ),
    }


def _canonical_inputs(
    plan: ComponentGenerationPlan,
    inputs: tuple[PromptSegmentInput, ...],
) -> tuple[PromptSegmentInput, ...]:
    orders = _allowed_identity_order(plan)
    expected = {
        (kind, uri) for kind, identities in orders.items() for uri in identities
    }
    seen: dict[tuple[ContextAuthorityKind, str], PromptSegmentInput] = {}
    provider_by_interface = {
        edge.public_interface_identity.uri: edge.provider_revision
        for edge in plan.direct_generation_edges
        if edge.public_interface_identity is not None
    }
    for item in inputs:
        if not isinstance(item, PromptSegmentInput):
            raise TypeError("context inputs must be PromptSegmentInput values")
        if not isinstance(item.authority_kind, ContextAuthorityKind):
            raise ComponentGenerationContextError(
                "generation_context.authority_unknown",
                "context authority kind is not recognized",
            )
        if item.authority_kind not in orders:
            raise ComponentGenerationContextError(
                "generation_context.authority_forbidden",
                f"{item.authority_kind.value} is forbidden from coding-agent context",
            )
        candidate = (item.authority_kind, item.content_identity.uri)
        if candidate not in expected:
            raise ComponentGenerationContextError(
                "generation_context.authority_unselected",
                "context contains authority absent from this exact generation plan: "
                f"{item.authority_kind.value} {item.content_identity.uri}",
            )
        if candidate in seen:
            raise ComponentGenerationContextError(
                "generation_context.authority_duplicate",
                "context repeats an exact planned authority",
            )
        if _raw_identity(item.content) != item.content_identity:
            raise ComponentGenerationContextError(
                "generation_context.content_drift",
                f"context bytes do not match {item.content_identity.uri}",
            )
        expected_source = (
            provider_by_interface[item.content_identity.uri]
            if item.authority_kind is ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE
            else plan.component_revision
        )
        if item.source_component_revision != expected_source:
            raise ComponentGenerationContextError(
                "generation_context.source_component_mismatch",
                "context authority is attributed to the wrong source Component",
            )
        if not item.reason.strip():
            raise ComponentGenerationContextError(
                "generation_context.reason_missing",
                "every prompt segment requires a reason for inclusion",
            )
        seen[candidate] = item
    missing = expected - set(seen)
    if missing:
        kind, uri = sorted(missing, key=lambda pair: (pair[0].value, pair[1]))[0]
        raise ComponentGenerationContextError(
            "generation_context.authority_incomplete",
            f"context omits planned authority {kind.value} {uri}",
        )
    return tuple(
        seen[(kind, uri)]
        for kind in (
            ContextAuthorityKind.LOCAL_SPECIFICATION,
            ContextAuthorityKind.LOCAL_FLAVOR,
            ContextAuthorityKind.LOCAL_SKILL,
            ContextAuthorityKind.LOCAL_WORKFLOW,
            ContextAuthorityKind.LOCAL_ROUTING_POLICY,
            ContextAuthorityKind.LOCAL_PUBLIC_INTERFACE,
            ContextAuthorityKind.LOCAL_ASSET_METADATA,
            ContextAuthorityKind.LOCAL_NATIVE_SDK_METADATA,
            ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE,
        )
        for uri in orders[kind]
    )


def _contributors(
    segments: tuple[ComponentGenerationContextSegment, ...],
    visibility: ContextVisibility,
) -> tuple[ComplexityContributor, ...]:
    values = tuple(
        ComplexityContributor(
            item.content_identity, item.authority_kind, item.byte_count
        )
        for item in segments
        if item.visibility is visibility
    )
    return tuple(
        sorted(values, key=lambda item: (-item.byte_count, item.content_identity.uri))[
            :10
        ]
    )


def prepare_component_generation_context(
    plan: ComponentGenerationPlan,
    *,
    framework_envelope: bytes,
    authority_segments: tuple[PromptSegmentInput, ...],
    budget: GenerationComplexityBudget,
) -> PreparedComponentGenerationRequest:
    """Validate, order, meter, and identity-bind exact prompt bytes before egress."""

    if not isinstance(plan, ComponentGenerationPlan):
        raise TypeError("bounded context requires a ComponentGenerationPlan")
    if not isinstance(framework_envelope, bytes) or not framework_envelope:
        raise ComponentGenerationContextError(
            "generation_context.framework_envelope_invalid",
            "framework envelope must be non-empty exact bytes",
        )
    if not isinstance(budget, GenerationComplexityBudget):
        raise TypeError("budget must be a GenerationComplexityBudget")
    ordered = _canonical_inputs(plan, authority_segments)
    envelope_identity = _raw_identity(framework_envelope)
    manifest_segments: list[ComponentGenerationContextSegment] = [
        ComponentGenerationContextSegment(
            0,
            ContextAuthorityKind.FRAMEWORK_ENVELOPE,
            None,
            "trusted framework generation envelope",
            envelope_identity,
            len(framework_envelope),
            deterministic_token_estimate(len(framework_envelope)),
            ContextVisibility.FRAMEWORK,
        )
    ]
    for index, item in enumerate(ordered, start=1):
        visibility = (
            ContextVisibility.DIRECT_PUBLIC_INTERFACE
            if item.authority_kind is ContextAuthorityKind.DIRECT_PUBLIC_INTERFACE
            else ContextVisibility.LOCAL_AUTHORITY
        )
        manifest_segments.append(
            ComponentGenerationContextSegment(
                index,
                item.authority_kind,
                item.source_component_revision,
                item.reason,
                item.content_identity,
                len(item.content),
                deterministic_token_estimate(len(item.content)),
                visibility,
            )
        )
    manifest = ComponentGenerationContextManifest(
        plan.component_revision, plan.generation_key.identity, tuple(manifest_segments)
    )
    direct_segments = tuple(
        item
        for item in manifest.segments
        if item.visibility is ContextVisibility.DIRECT_PUBLIC_INTERFACE
    )
    measurements = {
        "prompt_bytes": manifest.byte_count,
        "estimated_tokens": manifest.estimated_tokens,
        "document_count": len(manifest.segments),
        "direct_interface_bytes": sum(item.byte_count for item in direct_segments),
        "dependency_fan_in": len(
            {item.source_component_revision.uri for item in direct_segments}
        ),
    }
    limits = {
        "prompt_bytes": budget.max_prompt_bytes,
        "estimated_tokens": budget.max_estimated_tokens,
        "document_count": budget.max_document_count,
        "direct_interface_bytes": budget.max_direct_interface_bytes,
        "dependency_fan_in": budget.max_dependency_fan_in,
    }
    violations = tuple(
        sorted(
            name for name, measured in measurements.items() if measured > limits[name]
        )
    )
    decision = GenerationComplexityDecision(
        manifest.identity,
        budget.identity,
        **measurements,
        allowed=not violations,
        violations=violations,
        largest_local_contributors=_contributors(
            manifest.segments, ContextVisibility.LOCAL_AUTHORITY
        ),
        largest_interface_contributors=_contributors(
            manifest.segments, ContextVisibility.DIRECT_PUBLIC_INTERFACE
        ),
    )
    if violations:
        raise GenerationComplexityBudgetExceeded(decision)

    # The manifest carries exact segment boundaries; the adapter receives the exact
    # concatenation whose byte count is the pre-egress measurement above.
    prompt_parts = (framework_envelope, *(item.content for item in ordered))
    prompt = b"".join(prompt_parts)
    request = BoundedComponentGenerationRequest(
        plan.identity,
        plan.generation_key.identity,
        manifest.identity,
        decision.identity,
        manifest,
        budget,
        decision,
        _raw_identity(prompt),
    )
    return PreparedComponentGenerationRequest(request, prompt)


__all__ = [
    "ComponentGenerationContextError",
    "GenerationComplexityBudgetExceeded",
    "PreparedComponentGenerationRequest",
    "PromptSegmentInput",
    "bind_component_generation_context_cache_key",
    "prepare_component_generation_context",
]

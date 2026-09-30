"""Policy-driven transitive Component composition over descriptor metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from literate_ai.contracts import (
    Capability,
    CapabilityConstraint,
    CapabilityRequirement,
    ComponentRevisionRef,
    ContentIdentity,
    DependencyEdge,
    canonical_identity,
)
from literate_ai.registry import ComponentDescriptor, DescriptorRegistry

from .policy import SelectionError, SelectionPolicy, UniqueSelectionPolicy
from .versions import version_satisfies


class CompositionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProviderCandidateDecision:
    revision_identity: ContentIdentity
    coordinate: str
    capability_version: str | None
    eligible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_identity": self.revision_identity.to_dict(),
            "coordinate": self.coordinate,
            "capability_version": self.capability_version,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class RequirementResolutionDecision:
    source_revision: ContentIdentity
    requirement: CapabilityRequirement
    policy_identity: ContentIdentity
    candidates: tuple[ProviderCandidateDecision, ...]
    selected_revision: ContentIdentity | None
    selected_capability: Capability | None

    def __post_init__(self) -> None:
        selected_revision = self.selected_revision is not None
        selected_capability = self.selected_capability is not None
        if selected_revision != selected_capability:
            raise CompositionError(
                "invalid-resolution-decision",
                "selected revision and capability must both be present or absent",
            )
        if not selected_revision and not self.requirement.optional:
            raise CompositionError(
                "invalid-resolution-decision",
                "only an optional requirement may remain unresolved",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_revision": self.source_revision.to_dict(),
            "requirement": self.requirement.to_dict(),
            "policy_identity": self.policy_identity.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "selected_revision": (
                None
                if self.selected_revision is None
                else self.selected_revision.to_dict()
            ),
            "selected_capability": (
                None
                if self.selected_capability is None
                else self.selected_capability.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class ComponentComposition:
    root_revision: ContentIdentity
    revisions: tuple[ContentIdentity, ...]
    edges: tuple[DependencyEdge, ...]
    decisions: tuple[RequirementResolutionDecision, ...]
    root_ref: ComponentRevisionRef
    revision_refs: tuple[ComponentRevisionRef, ...]

    def __post_init__(self) -> None:
        if self.root_ref.revision_identity != self.root_revision:
            raise CompositionError(
                "root-ref-mismatch", "root Component ref does not match root revision"
            )
        referenced_revisions = tuple(
            item.revision_identity for item in self.revision_refs
        )
        if referenced_revisions != self.revisions:
            raise CompositionError(
                "revision-ref-mismatch",
                "Component refs must bind every composed revision in order",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "root_revision": self.root_revision.to_dict(),
                "revisions": [item.to_dict() for item in self.revisions],
                "edges": [item.to_dict() for item in self.edges],
                "decisions": [item.to_dict() for item in self.decisions],
                "root_ref": self.root_ref.to_dict(),
                "revision_refs": [item.to_dict() for item in self.revision_refs],
            }
        )


@dataclass(frozen=True, slots=True)
class _EligibleProvider:
    descriptor: ComponentDescriptor
    capability: Capability


def _constraint_reason(
    constraint: CapabilityConstraint, descriptor: ComponentDescriptor
) -> str | None:
    actual = descriptor.attribute_values(constraint.key)
    if actual is None:
        return f"missing constraint attribute {constraint.key}"
    expected = set(constraint.values)
    actual_set = set(actual)
    if constraint.operator in ("in", "equals"):
        accepted = bool(actual_set & expected)
    elif constraint.operator == "not-in":
        accepted = not bool(actual_set & expected)
    elif constraint.operator == "contains-all":
        accepted = expected <= actual_set
    else:
        raise CompositionError(
            "unsupported-constraint", f"unsupported operator {constraint.operator!r}"
        )
    return (
        None
        if accepted
        else f"constraint {constraint.key} {constraint.operator} failed"
    )


class ComponentComposer:
    def __init__(
        self,
        registry: DescriptorRegistry,
        policy: SelectionPolicy[ComponentDescriptor] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or UniqueSelectionPolicy()

    def compose(
        self, root_revision: ContentIdentity | ComponentRevisionRef
    ) -> ComponentComposition:
        if isinstance(root_revision, ComponentRevisionRef):
            root_descriptor = self._registry.component_exact(root_revision)
            root_identity = root_revision.revision_identity
            root_ref = root_revision
        else:
            root_descriptor = self._registry.component(root_revision.uri)
            root_identity = root_revision
            root_ref = root_descriptor.ref
        revisions: list[ContentIdentity] = []
        revision_refs: list[ComponentRevisionRef] = []
        edges: list[DependencyEdge] = []
        decisions: list[RequirementResolutionDecision] = []
        visited: set[str] = set()
        active: list[str] = []

        def visit(revision: ContentIdentity) -> None:
            uri = revision.uri
            if uri in active:
                cycle = active[active.index(uri) :] + [uri]
                raise CompositionError("dependency-cycle", " -> ".join(cycle))
            if uri in visited:
                return
            try:
                descriptor = self._registry.component(uri)
            except KeyError as error:
                raise CompositionError("unknown-revision", str(error)) from error
            if not descriptor.is_available:
                raise CompositionError(
                    "root-unavailable" if not active else "dependency-unavailable",
                    f"{descriptor.coordinate} is unavailable",
                )
            active.append(uri)
            revisions.append(revision)
            revision_refs.append(descriptor.ref)
            for requirement in descriptor.definition.requires:
                decision, selected = self._resolve(descriptor, requirement)
                decisions.append(decision)
                if selected is None:
                    continue
                edge = DependencyEdge(
                    descriptor.revision_identity,
                    selected.descriptor.revision_identity,
                    requirement,
                    selected.capability,
                    decision.identity,
                )
                edges.append(edge)
                visit(selected.descriptor.revision_identity)
            active.pop()
            visited.add(uri)

        visit(root_identity)
        return ComponentComposition(
            root_identity,
            tuple(revisions),
            tuple(edges),
            tuple(decisions),
            root_ref,
            tuple(revision_refs),
        )

    def _resolve(
        self, source: ComponentDescriptor, requirement: CapabilityRequirement
    ) -> tuple[RequirementResolutionDecision, _EligibleProvider | None]:
        candidate_decisions: list[ProviderCandidateDecision] = []
        eligible: list[_EligibleProvider] = []
        for descriptor in self._registry.component_providers(requirement.capability):
            matching = next(
                (
                    item
                    for item in descriptor.definition.provides
                    if item.name == requirement.capability
                ),
                None,
            )
            assert matching is not None
            reasons: list[str] = []
            if not descriptor.is_available:
                reasons.extend(item.code for item in descriptor.availability_reasons)
            try:
                if not version_satisfies(matching.version, requirement.version_range):
                    reasons.append(
                        f"version {matching.version} does not satisfy "
                        f"{requirement.version_range}"
                    )
            except ValueError as error:
                raise CompositionError("invalid-version-range", str(error)) from error
            for constraint in requirement.constraints:
                reason = _constraint_reason(constraint, descriptor)
                if reason is not None:
                    reasons.append(reason)
            accepted = not reasons
            candidate_decisions.append(
                ProviderCandidateDecision(
                    descriptor.revision_identity,
                    descriptor.coordinate,
                    matching.version,
                    accepted,
                    tuple(reasons) if reasons else ("eligible",),
                )
            )
            if accepted:
                eligible.append(_EligibleProvider(descriptor, matching))
        if not eligible and requirement.optional:
            return (
                RequirementResolutionDecision(
                    source.revision_identity,
                    requirement,
                    self._policy.identity,
                    tuple(candidate_decisions),
                    None,
                    None,
                ),
                None,
            )
        try:
            selected_descriptors = self._policy.choose(
                tuple(item.descriptor for item in eligible),
                subject=f"{source.coordinate}:{requirement.requirement_id}",
            )
        except SelectionError as error:
            raise CompositionError(error.code, str(error)) from error
        if len(selected_descriptors) != 1:
            raise CompositionError(
                "invalid-policy-result", "Component policy must select exactly one"
            )
        selected = next(
            item for item in eligible if item.descriptor is selected_descriptors[0]
        )
        decision = RequirementResolutionDecision(
            source.revision_identity,
            requirement,
            self._policy.identity,
            tuple(candidate_decisions),
            selected.descriptor.revision_identity,
            selected.capability,
        )
        return decision, selected


__all__ = [
    "ComponentComposer",
    "ComponentComposition",
    "CompositionError",
    "ProviderCandidateDecision",
    "RequirementResolutionDecision",
]

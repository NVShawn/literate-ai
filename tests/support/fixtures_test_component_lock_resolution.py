from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_component_lock_resolution``."""




from literate_ai.application.component_lock_resolution import (
    CatalogAttribute,
    ComponentLockResolutionPlan,
    FlavorCandidateInput,
    RequirementProviderInput,
    ResolvedComponentNodeInput,
)

from literate_ai.contracts.capabilities import (
    CapabilityConstraint,
    CapabilityRequirement,
    DependencyKind,
)

from literate_ai.contracts.flavors import CandidateStatus

from literate_ai.contracts.repositories import RepositorySourceLock

from tests.support.fixtures_test_component_lock_contracts import (
    component_authoring,
    identity,
    reference,
)

def resolved_node(
    authoring,
    *,
    selected: str,
    rejected: str,
    attributes: tuple[CatalogAttribute, ...] = (),
    repository_sources: tuple[RepositorySourceLock, ...] = (),
) -> ResolvedComponentNodeInput:
    interfaces = tuple(
        reference(
            "public-interface-contract",
            provided.interface.uri,
            f"{provided.name}-interface",
        )
        for provided in authoring.provides
        if provided.interface is not None
    )
    return ResolvedComponentNodeInput(
        authoring=authoring,
        specifications=tuple(
            reference("specification", uri, f"specification-{uri}")
            for uri in authoring.specification_roots
        ),
        authoring_inputs=tuple(
            reference(selector.kind, selector.uri, f"authoring-input-{index}")
            for index, selector in enumerate(authoring.authoring_inputs)
        ),
        workflow_definition=reference("workflow", "workflows/host.json", "workflow"),
        routing_policy=reference("routing-policy", "routing/default.json", "routing"),
        acceptance_contracts=(
            reference("acceptance-contract", "acceptance/execution.json", "acceptance"),
        ),
        repository_sources=repository_sources,
        public_interfaces=interfaces,
        flavor_candidates=(
            FlavorCandidateInput(
                "language",
                rejected,
                identity(f"flavor-{rejected}"),
                CandidateStatus.REJECTED,
                ("not selected by target",),
            ),
            FlavorCandidateInput(
                "language",
                selected,
                identity(f"flavor-{selected}"),
                CandidateStatus.SELECTED,
            ),
        ),
        catalog_attributes=attributes,
    )

def resolution_plan() -> ComponentLockResolutionPlan:
    requirement = CapabilityRequirement(
        "pricing",
        "pricing-api",
        ">=1,<2",
        DependencyKind.GENERATION,
        constraints=(CapabilityConstraint("regions", "contains-all", ("eu", "us")),),
    )
    consumer = component_authoring("invoice-cli", requirements=(requirement,))
    provider = component_authoring(
        "pricing", interface=("pricing-api", "pricing-interface")
    )
    return ComponentLockResolutionPlan(
        target_name="host",
        target_profile_identity=identity("target-profile"),
        selection_policy_identity=identity("selection-policy"),
        resolver_identity=identity("component-lock-resolver"),
        catalog_identity=identity("catalog"),
        root_authoring_identity=consumer.identity,
        # Deliberately reverse discovery order; resolution must canonicalize it.
        nodes=(
            resolved_node(
                provider,
                selected="python",
                rejected="rust",
                attributes=(CatalogAttribute("regions", ("us", "eu")),),
            ),
            resolved_node(consumer, selected="rust", rejected="python"),
        ),
        requirement_providers=(
            RequirementProviderInput(
                consumer.identity,
                "pricing",
                provider.identity,
            ),
        ),
    )


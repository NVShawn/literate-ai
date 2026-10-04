from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_component_authoring_migration``."""

import hashlib
import json

from literate_ai.contracts.capabilities import (
    Capability,
    CapabilityRequirement,
    DependencyKind,
)
from literate_ai.contracts.components import ComponentDefinition, Entrypoint
from literate_ai.contracts.flavors import (
    FlavorAxis,
    FlavorCardinality,
    FlavorSlot,
)
from literate_ai.contracts.identity import (
    ComponentCoordinate,
    ContentIdentity,
    ContentReference,
)
from literate_ai.contracts.repositories import (
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
)
from tests.support.fixtures_test_component_lock_contracts import reference


def repository_dependency() -> RepositorySourceDependency:
    return RepositorySourceDependency(
        dependency_id="portable-json",
        repository_url="https://example.com/portable/json.git",
        revision_selector=RepositoryRevisionSelector(
            RepositoryRevisionKind.BRANCH, "main"
        ),
        dependency_kind=DependencyKind.BUILD,
        optional=False,
        integration_contract=reference(
            "integration-contract",
            "integration/portable-json.json",
            "resolved-integration-contract",
        ),
    )


def pretty_repository_bytes(
    dependency: RepositorySourceDependency,
) -> bytes:
    return (json.dumps(dependency.to_dict(), indent=2) + "\n").encode("utf-8")


def raw_identity(content: bytes) -> ContentIdentity:
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest())


def definition(
    *, capability_contract=None, with_repository: bool = True
) -> ComponentDefinition:
    dependency = repository_dependency()
    return ComponentDefinition(
        coordinate=ComponentCoordinate("samples", "migration-fixture"),
        version="1.2.3",
        display_name="Migration fixture",
        description="Preserve human intent and discard resolver identities.",
        profiles=("portable", "cli"),
        sample=True,
        provides=(
            Capability("zeta", "2.0.0"),
            Capability("alpha", "1.0.0", capability_contract),
        ),
        requires=(
            CapabilityRequirement(
                "runtime",
                "runtime-api",
                ">=1,<2",
                DependencyKind.RUNTIME,
                optional=True,
            ),
        ),
        specification_provider="literate-markdown",
        specification_roots=("specs/overview.md", "specs/protocol.md"),
        authoring_inputs=(
            reference(
                "specification-to-source-skill",
                "skills/plan.json",
                "resolved-plan-skill",
            ),
            reference(
                "specification-to-source-skill",
                "skills/implement.json",
                "resolved-implementation-skill",
            ),
        ),
        workflow_definition=reference(
            "workflow", "workflows/build.json", "resolved-workflow"
        ),
        routing_policy=reference(
            "routing-policy", "routing/default.json", "resolved-routing"
        ),
        flavor_slots=(
            FlavorSlot(
                "language",
                FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                FlavorCardinality.EXACTLY_ONE,
                "portable-application",
            ),
        ),
        entrypoints=(Entrypoint("run", "portable-application", "source/main"),),
        acceptance_contracts=(
            reference(
                "acceptance-contract",
                "acceptance/execution.json",
                "resolved-acceptance",
            ),
        ),
        source_dependencies=(
            (
                ContentReference(
                    "repository-source-dependency",
                    "dependencies/portable-json.json",
                    raw_identity(pretty_repository_bytes(dependency)),
                ),
            )
            if with_repository
            else ()
        ),
    )

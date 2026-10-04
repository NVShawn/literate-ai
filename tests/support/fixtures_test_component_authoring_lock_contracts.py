"""Shared test fixtures extracted from test_component_authoring_lock_contracts."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.contracts.capabilities import CapabilityRequirement, DependencyKind
from literate_ai.contracts.component_locking import (
    AuthoredProvidedCapability,
    AuthoredRepositorySourceDependency,
    ComponentAssetSelector,
    ComponentAuthoring,
    ComponentContentSelector,
)
from literate_ai.contracts.components import Entrypoint
from literate_ai.contracts.flavors import FlavorAxis, FlavorCardinality, FlavorSlot
from literate_ai.contracts.identity import ComponentCoordinate, canonical_identity
from literate_ai.contracts.repositories import (
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]


def schema_catalog() -> SchemaCatalog:
    catalog = SchemaCatalog(ROOT / "schemas/v2")
    for path in sorted((ROOT / "schemas/v1").glob("*.schema.json")):
        catalog._collect(json.loads(path.read_text(encoding="utf-8")))
    return catalog


def official_validator(resource_id: str) -> Draft202012Validator:
    registry = Registry()
    for directory in (ROOT / "schemas/v1", ROOT / "schemas/v2"):
        for path in sorted(directory.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
    return Draft202012Validator({"$ref": resource_id}, registry=registry.crawl())


def selector(kind: str, uri: str, *, pinned: bool = False) -> ComponentContentSelector:
    return ComponentContentSelector(
        kind,
        uri,
        canonical_identity({"pin": uri}) if pinned else None,
    )


def authored_component() -> ComponentAuthoring:
    return ComponentAuthoring(
        coordinate=ComponentCoordinate("samples", "invoice-cli"),
        version="1.0.0",
        display_name="Invoice CLI",
        description="A portable invoice application.",
        profiles=("application", "portable", "sample"),
        sample=True,
        provides=(
            AuthoredProvidedCapability(
                "invoice-api",
                "1.0.0",
                selector("public-interface-contract", "interfaces/invoice.json"),
            ),
        ),
        requires=(
            CapabilityRequirement(
                "money",
                "money-api",
                ">=1,<2",
                DependencyKind.GENERATION,
            ),
        ),
        specification_provider="literate-markdown",
        specification_roots=("specs/spec.md", "specs/contracts.json"),
        authoring_inputs=(
            selector("specification-to-source-skill", "skills/implement.json"),
            selector("specification-to-source-skill", "skills/plan.json", pinned=True),
        ),
        workflow_definition=selector("workflow", "workflows/host.json"),
        routing_policy=selector("routing-policy", "routing/default.json"),
        flavor_slots=(
            FlavorSlot(
                "language",
                FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                FlavorCardinality.EXACTLY_ONE,
                "invoice-api",
            ),
            FlavorSlot(
                "os",
                FlavorAxis.PLATFORM_OS,
                FlavorCardinality.EXACTLY_ONE,
                "invoice-api",
            ),
        ),
        entrypoints=(Entrypoint("run", "portable-application", "run"),),
        acceptance_contracts=(
            selector("acceptance-contract", "acceptance/execution.json"),
        ),
        source_dependencies=(
            AuthoredRepositorySourceDependency(
                "sqlite",
                "https://github.com/sqlite/sqlite.git",
                RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "trunk"),
                DependencyKind.BUILD,
                integration_contract=selector(
                    "integration-contract", "dependencies/sqlite.md"
                ),
            ),
        ),
        assets=(
            ComponentAssetSelector(
                "currency-table",
                "assets/currencies.csv",
                "source/data/currencies.csv",
                "runtime-data",
                "text/csv",
            ),
        ),
    )

"""Legacy welded ComponentDefinition to ComponentAuthoring migration tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from literate_ai.application.component_authoring_migration import (
    ComponentAuthoringMigrationError,
    LoadedRepositoryDependency,
    migrate_component_authoring,
    project_component_authoring,
)
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
from tests.support.fixtures_test_component_lock_contracts import identity, reference


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


class ComponentAuthoringMigrationTests(unittest.TestCase):
    def test_migration_preserves_intent_and_drops_resolver_identities(self) -> None:
        legacy = definition()
        dependency = repository_dependency()
        observed: list[ContentReference] = []

        raw = pretty_repository_bytes(dependency)

        def load(reference_value: ContentReference) -> LoadedRepositoryDependency:
            observed.append(reference_value)
            return LoadedRepositoryDependency(dependency, raw_identity(raw))

        authored = migrate_component_authoring(legacy, load)

        self.assertEqual(observed, [legacy.source_dependencies[0]])
        self.assertNotEqual(dependency.identity, raw_identity(raw))
        self.assertEqual(authored.coordinate, legacy.coordinate)
        self.assertEqual(authored.version, legacy.version)
        self.assertEqual(authored.profiles, ("cli", "portable"))
        self.assertEqual([item.name for item in authored.provides], ["alpha", "zeta"])
        self.assertEqual(authored.specification_roots, legacy.specification_roots)
        selectors = (
            *authored.authoring_inputs,
            authored.workflow_definition,
            authored.routing_policy,
            *authored.acceptance_contracts,
        )
        self.assertTrue(all(item.pin is None for item in selectors))
        self.assertEqual(
            authored.source_dependencies[0].repository_url,
            dependency.repository_url,
        )
        integration = authored.source_dependencies[0].integration_contract
        self.assertIsNotNone(integration)
        assert integration is not None
        self.assertEqual(integration.uri, "integration/portable-json.json")
        self.assertIsNone(integration.pin)

    def test_mechanical_reference_identity_changes_do_not_change_authoring(
        self,
    ) -> None:
        before = definition(with_repository=False)
        changed = replace(
            before,
            authoring_inputs=tuple(
                replace(item, identity=identity(f"changed-{index}"))
                for index, item in enumerate(before.authoring_inputs)
            ),
            workflow_definition=replace(
                before.workflow_definition, identity=identity("changed-workflow")
            ),
            routing_policy=replace(
                before.routing_policy, identity=identity("changed-routing")
            ),
            acceptance_contracts=(
                replace(
                    before.acceptance_contracts[0],
                    identity=identity("changed-acceptance"),
                ),
            ),
        )

        self.assertEqual(
            project_component_authoring(before),
            project_component_authoring(changed),
        )

    def test_non_null_legacy_capability_contract_fails_as_ambiguous(self) -> None:
        legacy = definition(
            capability_contract=identity("legacy-interface-without-uri"),
        )
        loads = 0

        def load(_reference: ContentReference) -> LoadedRepositoryDependency:
            nonlocal loads
            loads += 1
            dependency = repository_dependency()
            return LoadedRepositoryDependency(
                dependency, raw_identity(pretty_repository_bytes(dependency))
            )

        with self.assertRaisesRegex(
            ComponentAuthoringMigrationError, "no authored interface URI"
        ) as raised:
            migrate_component_authoring(legacy, load)
        self.assertEqual(raised.exception.code, "ambiguous-capability-contract")
        self.assertEqual(loads, 0)

    def test_repository_loader_must_bind_the_exact_observed_raw_bytes(
        self,
    ) -> None:
        legacy = definition()
        dependency = repository_dependency()
        mismatched = LoadedRepositoryDependency(
            dependency,
            identity("different-raw-repository-document"),
        )
        with self.assertRaisesRegex(
            ComponentAuthoringMigrationError, "changed raw bytes"
        ) as mismatch:
            migrate_component_authoring(legacy, lambda _reference: mismatched)
        self.assertEqual(
            mismatch.exception.code,
            "repository-dependency-content-identity-mismatch",
        )

        with self.assertRaisesRegex(
            ComponentAuthoringMigrationError, "untyped"
        ) as invalid:
            migrate_component_authoring(legacy, lambda _reference: object())  # type: ignore[return-value]
        self.assertEqual(invalid.exception.code, "repository-dependency-invalid")

    def test_pure_projection_requires_every_loaded_repository_dependency(self) -> None:
        legacy = definition()
        with self.assertRaisesRegex(
            ComponentAuthoringMigrationError, "every and only"
        ) as raised:
            project_component_authoring(legacy)
        self.assertEqual(raised.exception.code, "repository-dependency-count-mismatch")


if __name__ == "__main__":
    unittest.main()

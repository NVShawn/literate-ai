"""Lock-native CycloneDX managed-graph projection tests."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.application.component_lock_resolution import (
    ComponentLockResolutionPlan,
    ComponentLockResolver,
    RequirementProviderInput,
)
from literate_ai.contracts import project_component_lock_managed_graph
from literate_ai.contracts.capabilities import CapabilityRequirement, DependencyKind
from literate_ai.contracts.component_locking import AuthoredRepositorySourceDependency
from literate_ai.contracts.repositories import (
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
    RepositorySourceLock,
)
from literate_ai.contracts.sbom import (
    CycloneDxLifecycle,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    component_bom_ref,
    repository_dependency_bom_ref,
    repository_dependency_identity,
)
from tests.support.fixtures_test_component_lock_contracts import component_authoring, identity
from tests.support.fixtures_test_component_lock_resolution import resolved_node


def _requirement(
    requirement_id: str,
    capability: str,
    kind: DependencyKind,
    *,
    optional: bool = False,
) -> CapabilityRequirement:
    return CapabilityRequirement(
        requirement_id,
        capability,
        ">=1,<2",
        kind,
        optional=optional,
    )


def _repository_lock(
    kind: DependencyKind,
    *,
    optional: bool,
    commit: str = "a" * 40,
) -> tuple[AuthoredRepositorySourceDependency, RepositorySourceLock]:
    selector = RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "stable")
    authored = AuthoredRepositorySourceDependency(
        "shared-engine",
        "https://example.invalid/shared-engine.git",
        selector,
        kind,
        optional,
    )
    dependency = RepositorySourceDependency(
        authored.dependency_id,
        authored.repository_url,
        selector,
        kind,
        optional,
    )
    return authored, RepositorySourceLock(
        dependency,
        commit,
        identity(f"shared-snapshot-{commit}"),
        identity(f"shared-tree-{commit}"),
        identity("repository-lock-resolver"),
    )


def _diamond_lock(*, conflicting_repository_locks: bool = False):
    root = component_authoring(
        "diamond-root",
        requirements=(
            _requirement("left", "left-api", DependencyKind.GENERATION),
            _requirement("right", "right-api", DependencyKind.RUNTIME),
        ),
    )
    left_source, left_lock = _repository_lock(DependencyKind.BUILD, optional=False)
    left = replace(
        component_authoring(
            "diamond-left",
            interface=("left-api", "left-interface"),
            requirements=(_requirement("leaf", "leaf-api", DependencyKind.GENERATION),),
        ),
        source_dependencies=(left_source,),
    )
    right_source, right_lock = _repository_lock(
        DependencyKind.RUNTIME,
        optional=True,
        commit="b" * 40 if conflicting_repository_locks else "a" * 40,
    )
    right = replace(
        component_authoring(
            "diamond-right",
            interface=("right-api", "right-interface"),
            requirements=(_requirement("leaf", "leaf-api", DependencyKind.VALIDATION),),
        ),
        source_dependencies=(right_source,),
    )
    leaf = component_authoring("diamond-leaf", interface=("leaf-api", "leaf-interface"))

    root_node = resolved_node(root, selected="rust", rejected="python")
    left_node = resolved_node(
        left,
        selected="python",
        rejected="rust",
        repository_sources=(left_lock,),
    )
    right_node = resolved_node(
        right,
        selected="javascript",
        rejected="rust",
        repository_sources=(right_lock,),
    )
    leaf_node = resolved_node(leaf, selected="cpp", rejected="rust")
    plan = ComponentLockResolutionPlan(
        target_name="host",
        target_profile_identity=identity("diamond-target-profile"),
        selection_policy_identity=identity("diamond-selection-policy"),
        resolver_identity=identity("diamond-component-lock-resolver"),
        catalog_identity=identity("diamond-catalog"),
        root_authoring_identity=root.identity,
        nodes=(root_node, left_node, right_node, leaf_node),
        requirement_providers=(
            RequirementProviderInput(root.identity, "left", left.identity),
            RequirementProviderInput(root.identity, "right", right.identity),
            RequirementProviderInput(left.identity, "leaf", leaf.identity),
            RequirementProviderInput(right.identity, "leaf", leaf.identity),
        ),
    )
    return (
        ComponentLockResolver()
        .resolve(plan, expected_input_evidence_identity=plan.identity)
        .lock
    )


class ComponentLockSbomTests(unittest.TestCase):
    def test_projects_exact_diamond_lock_into_source_and_resolved_boms(self) -> None:
        lock = _diamond_lock()
        managed = CycloneDxManagedGraph.from_component_lock(lock)

        self.assertEqual(managed.resolved_graph_identity, lock.identity)
        self.assertEqual(managed.root_ref, component_bom_ref(lock.root_revision))
        self.assertEqual(len(managed.components), 5)
        self.assertEqual(len(managed.edges), 6)

        by_revision = {node.revision.coordinate.name: node for node in lock.nodes}
        projected_by_name = {item.name: item for item in managed.components}
        self.assertEqual(
            projected_by_name["component://samples/diamond-left"].scopes,
            ("generation",),
        )
        self.assertEqual(
            projected_by_name["component://samples/diamond-right"].scopes,
            ("runtime",),
        )
        self.assertEqual(
            projected_by_name["component://samples/diamond-leaf"].scopes,
            ("generation", "validation"),
        )
        for edge in lock.edges:
            with self.subTest(requirement=edge.requirement_id):
                self.assertIn(
                    CycloneDxManagedEdge(
                        component_bom_ref(edge.consumer_revision),
                        component_bom_ref(edge.provider_revision),
                        edge.kind,
                        edge.optional,
                        edge.identity,
                    ),
                    managed.edges,
                )
                if edge.kind is DependencyKind.GENERATION:
                    provider = next(
                        node
                        for node in lock.nodes
                        if node.revision.identity == edge.provider_revision
                    )
                    self.assertIn(
                        edge.public_interface_identity,
                        tuple(
                            binding.interface_identity
                            for binding in provider.interface_bindings
                        ),
                    )

        repository_nodes = tuple(
            item
            for item in managed.components
            if item.kind is ManagedComponentKind.REPOSITORY_SOURCE
        )
        self.assertEqual(len(repository_nodes), 1)
        repository = repository_nodes[0]
        self.assertEqual(repository.version, "a" * 40)
        self.assertIsNone(repository.version_range)
        self.assertEqual(repository.scopes, ("build", "runtime"))
        for owner_name in ("diamond-left", "diamond-right"):
            source_lock = by_revision[owner_name].revision.repository_sources[0]
            dependency = source_lock.dependency
            dependency_ref = repository_dependency_bom_ref(
                repository_dependency_identity(dependency)
            )
            self.assertEqual(dependency_ref, repository.bom_ref)
            self.assertIn(
                CycloneDxManagedEdge(
                    component_bom_ref(by_revision[owner_name].revision.identity),
                    dependency_ref,
                    dependency.dependency_kind,
                    dependency.optional,
                    dependency.identity,
                ),
                managed.edges,
            )

        source, source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
        )
        resolved, resolved_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=managed,
            source_bom=source,
        )
        self.assertEqual(source_binding.resolved_graph_identity, lock.identity)
        self.assertEqual(resolved_binding.resolved_graph_identity, lock.identity)
        for content in (source, resolved):
            document = json.loads(content)
            metadata_properties = {
                item["name"]: item["value"]
                for item in document["metadata"]["properties"]
            }
            self.assertEqual(
                metadata_properties["literate-ai:resolved-graph-identity"],
                lock.identity.uri,
            )

    def test_rejects_conflicting_exact_locks_for_shared_repository_node(self) -> None:
        lock = _diamond_lock(conflicting_repository_locks=True)

        with self.assertRaisesRegex(ValueError, "conflicting exact source locks"):
            CycloneDxManagedGraph.from_component_lock(lock)

    def test_non_root_chain_projection_contains_only_reachable_dependencies(
        self,
    ) -> None:
        lock = _diamond_lock()
        by_name = {node.revision.coordinate.name: node for node in lock.nodes}
        left = by_name["diamond-left"].revision.identity
        leaf = by_name["diamond-leaf"].revision.identity

        managed = project_component_lock_managed_graph(lock, left)

        self.assertEqual(managed.resolved_graph_identity, lock.identity)
        self.assertEqual(managed.root_ref, component_bom_ref(left))
        self.assertEqual(managed, CycloneDxManagedGraph.from_dict(managed.to_dict()))
        self.assertEqual(
            {item.name for item in managed.components},
            {
                "component://samples/diamond-left",
                "component://samples/diamond-leaf",
                "shared-engine",
            },
        )
        self.assertNotIn(
            "component://samples/diamond-root",
            {item.name for item in managed.components},
        )
        self.assertNotIn(
            "component://samples/diamond-right",
            {item.name for item in managed.components},
        )
        self.assertEqual(
            next(item for item in managed.components if item.identity == left).kind,
            ManagedComponentKind.ROOT,
        )
        self.assertEqual(
            next(item for item in managed.components if item.identity == leaf).scopes,
            ("generation",),
        )
        repository = next(
            item
            for item in managed.components
            if item.kind is ManagedComponentKind.REPOSITORY_SOURCE
        )
        self.assertEqual(repository.scopes, ("build",))
        self.assertEqual(len(managed.edges), 2)
        self.assertEqual(
            managed.components,
            tuple(sorted(managed.components, key=lambda item: item.bom_ref)),
        )
        self.assertEqual(managed.edges, tuple(sorted(managed.edges)))

    def test_sibling_projection_has_its_own_edge_semantics_only(self) -> None:
        lock = _diamond_lock()
        by_name = {node.revision.coordinate.name: node for node in lock.nodes}
        right = by_name["diamond-right"].revision.identity
        leaf = by_name["diamond-leaf"].revision.identity

        managed = project_component_lock_managed_graph(lock, right)

        self.assertEqual(
            {item.name for item in managed.components},
            {
                "component://samples/diamond-right",
                "component://samples/diamond-leaf",
                "shared-engine",
            },
        )
        self.assertEqual(
            next(item for item in managed.components if item.identity == leaf).scopes,
            ("validation",),
        )
        repository = next(
            item
            for item in managed.components
            if item.kind is ManagedComponentKind.REPOSITORY_SOURCE
        )
        self.assertEqual(repository.scopes, ("runtime",))
        self.assertTrue(
            next(
                edge for edge in managed.edges if edge.target_ref == repository.bom_ref
            ).optional
        )

    def test_provider_projection_is_one_private_leaf(self) -> None:
        lock = _diamond_lock()
        leaf = next(
            node.revision.identity
            for node in lock.nodes
            if node.revision.coordinate.name == "diamond-leaf"
        )

        managed = project_component_lock_managed_graph(lock, leaf)

        self.assertEqual(managed.resolved_graph_identity, lock.identity)
        self.assertEqual(managed.root_ref, component_bom_ref(leaf))
        self.assertEqual(len(managed.components), 1)
        self.assertEqual(managed.components[0].identity, leaf)
        self.assertEqual(managed.components[0].kind, ManagedComponentKind.ROOT)
        self.assertEqual(managed.components[0].scopes, ())
        self.assertEqual(managed.edges, ())

    def test_unrelated_conflicting_sibling_lock_does_not_enter_projection(self) -> None:
        lock = _diamond_lock(conflicting_repository_locks=True)
        left = next(
            node.revision.identity
            for node in lock.nodes
            if node.revision.coordinate.name == "diamond-left"
        )

        managed = project_component_lock_managed_graph(lock, left)

        repository = next(
            item
            for item in managed.components
            if item.kind is ManagedComponentKind.REPOSITORY_SOURCE
        )
        self.assertEqual(repository.version, "a" * 40)
        self.assertEqual(repository.scopes, ("build",))

    def test_projection_requires_an_exact_revision_in_the_lock(self) -> None:
        lock = _diamond_lock()
        with self.assertRaisesRegex(TypeError, "ContentIdentity"):
            CycloneDxManagedGraph.from_component_lock(
                lock,
                component_revision=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "absent from the exact ComponentLock"):
            project_component_lock_managed_graph(lock, identity("unknown-revision"))

    def test_requires_a_typed_component_lock(self) -> None:
        with self.assertRaisesRegex(TypeError, "exact ComponentLock"):
            CycloneDxManagedGraph.from_component_lock(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from collections.abc import Callable

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

from literate_ai.adapters.dependencies import (
    CycloneDxBomError,
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from literate_ai.composition import ComponentComposition
from literate_ai.contracts import (
    CYCLONEDX_BOM_BINDING_SCHEMA,
    CYCLONEDX_MANAGED_GRAPH_SCHEMA,
    LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA,
    LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA,
    Capability,
    CapabilityRequirement,
    ComponentCoordinate,
    ComponentRevisionRef,
    CycloneDxBomBinding,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    CycloneDxRepositorySourceResolution,
    DependencyEdge,
    DependencyKind,
    ManagedComponentKind,
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
    canonical_json_bytes,
    component_bom_ref,
    repository_dependency_identity,
)
from literate_ai.contracts.identity import ContentIdentity


def _identity(character: str) -> ContentIdentity:
    return ContentIdentity.parse_uri(f"sha256:{character * 64}")


def _managed_graph(*, with_dependency: bool = True) -> CycloneDxManagedGraph:
    root_identity = _identity("a")
    root_ref = component_bom_ref(root_identity)
    components = [
        CycloneDxManagedComponent(
            root_ref,
            ManagedComponentKind.ROOT,
            root_identity,
            "urn:literate-ai:component:example/app",
            "1.0.0",
            (),
        )
    ]
    edges: list[CycloneDxManagedEdge] = []
    if with_dependency:
        dependency_identity = _identity("b")
        dependency_ref = component_bom_ref(dependency_identity)
        components.append(
            CycloneDxManagedComponent(
                dependency_ref,
                ManagedComponentKind.COMPONENT,
                dependency_identity,
                "urn:literate-ai:component:example/library",
                "2.0.0",
                ("runtime",),
            )
        )
        edges.append(CycloneDxManagedEdge(root_ref, dependency_ref))
    return CycloneDxManagedGraph(
        root_ref,
        tuple(sorted(components, key=lambda item: item.bom_ref)),
        tuple(sorted(edges)),
        _identity("c"),
    )


def _package(ref: str, *, resolved: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "library",
        "bom-ref": ref,
        "name": "portable-package",
        "scope": "required",
        "isExternal": True,
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "package"},
            {"name": "literate-ai:dependency-scope", "value": "runtime"},
        ],
    }
    if resolved:
        result["version"] = "3.2.1"
    else:
        result.update(
            {
                "versionRange": "vers:generic/>=3.0.0|<4.0.0",
            }
        )
    return result


def _mutate(content: bytes, mutation: Callable[[dict[str, object]], None]) -> bytes:
    document = json.loads(content)
    mutation(document)
    return canonical_json_bytes(document)


class CycloneDxSbomTests(unittest.TestCase):
    def test_v2_graph_and_binding_wires_boundedly_read_v1_names(self) -> None:
        managed = _managed_graph(with_dependency=False)
        source, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
        )
        self.assertTrue(source)

        graph_wire = managed.to_dict()
        self.assertEqual(graph_wire["schema"], CYCLONEDX_MANAGED_GRAPH_SCHEMA)
        self.assertIn("resolved_graph_identity", graph_wire)
        self.assertNotIn("composition_identity", graph_wire)
        legacy_graph_wire = dict(graph_wire)
        legacy_graph_wire["schema"] = LEGACY_CYCLONEDX_MANAGED_GRAPH_SCHEMA
        legacy_graph_wire["composition_identity"] = legacy_graph_wire.pop(
            "resolved_graph_identity"
        )
        self.assertEqual(
            CycloneDxManagedGraph.from_dict(legacy_graph_wire),
            managed,
        )

        binding_wire = binding.to_dict()
        self.assertEqual(binding_wire["schema"], CYCLONEDX_BOM_BINDING_SCHEMA)
        self.assertIn("resolved_graph_identity", binding_wire)
        self.assertNotIn("composition_identity", binding_wire)
        legacy_binding_wire = dict(binding_wire)
        legacy_binding_wire["schema"] = LEGACY_CYCLONEDX_BOM_BINDING_SCHEMA
        legacy_binding_wire["composition_identity"] = legacy_binding_wire.pop(
            "resolved_graph_identity"
        )
        self.assertEqual(
            CycloneDxBomBinding.from_dict(legacy_binding_wire),
            binding,
        )

        mixed_graph_wire = dict(legacy_graph_wire)
        mixed_graph_wire["resolved_graph_identity"] = graph_wire[
            "resolved_graph_identity"
        ]
        with self.assertRaisesRegex(ValueError, "legacy records cannot contain"):
            CycloneDxManagedGraph.from_dict(mixed_graph_wire)

    def test_official_17_schema_and_complete_transitive_graph(self) -> None:
        managed = _managed_graph()
        component_ref = next(
            item.bom_ref
            for item in managed.components
            if item.kind is ManagedComponentKind.COMPONENT
        )
        package_ref = "pkg:generic/portable-package"
        content, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
            additional_components=(_package(package_ref),),
            additional_edges=((component_ref, package_ref),),
        )
        self.assertIsNone(
            JsonStrictValidator(SchemaVersion.V1_7).validate_str(
                content.decode(), all_errors=True
            )
        )
        self.assertEqual(binding.component_count, 3)
        self.assertEqual(binding.edge_count, 2)
        self.assertEqual(
            validate_cyclonedx_bom(
                content,
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
            ),
            binding,
        )
        entries = {
            item["ref"]: item["dependsOn"]
            for item in json.loads(content)["dependencies"]
        }
        self.assertEqual(entries[package_ref], [])

    def test_source_may_declare_unresolved_third_party_closure(self) -> None:
        managed = _managed_graph(with_dependency=False)
        package_ref = "pkg:generic/portable-package"
        source, source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
            additional_components=(_package(package_ref),),
            additional_edges=((managed.root_ref, package_ref),),
            composition_aggregate="incomplete_third_party_only",
        )
        source_document = json.loads(source)
        self.assertEqual(
            source_document["compositions"],
            [
                {
                    "aggregate": "incomplete_third_party_only",
                    "dependencies": [managed.root_ref],
                }
            ],
        )
        self.assertEqual(
            validate_cyclonedx_bom(
                source,
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
            ),
            source_binding,
        )

        resolved, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=managed,
            additional_components=(_package(package_ref, resolved=True),),
            additional_edges=((managed.root_ref, package_ref),),
            source_bom=source,
        )
        self.assertEqual(
            json.loads(resolved)["compositions"][0]["aggregate"], "complete"
        )

    def test_resolved_builder_rejects_incomplete_composition(self) -> None:
        managed = _managed_graph(with_dependency=False)
        source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
        )
        with self.assertRaisesRegex(
            CycloneDxBomError, "post-build CycloneDX construction requires"
        ):
            build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=managed,
                source_bom=source,
                composition_aggregate="incomplete_third_party_only",
            )

    def test_resolved_bom_requires_exact_versions(self) -> None:
        managed = _managed_graph()
        package_ref = "pkg:generic/portable-package"
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
            additional_components=(_package(package_ref),),
            additional_edges=((managed.root_ref, package_ref),),
        )
        post_build = _mutate(
            content,
            lambda value: value["metadata"].update(
                {"lifecycles": [{"phase": "post-build"}]}
            ),
        )
        with self.assertRaises(CycloneDxBomError) as caught:
            validate_cyclonedx_bom(
                post_build,
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=managed,
                source_content=content,
            )
        self.assertEqual(caught.exception.code, "sbom.resolved-version-missing")
        resolved, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=managed,
            additional_components=(_package(package_ref, resolved=True),),
            additional_edges=((managed.root_ref, package_ref),),
            source_bom=content,
        )
        self.assertEqual(
            validate_cyclonedx_bom(
                resolved,
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=managed,
                source_content=content,
            ),
            binding,
        )

    def test_standard_and_graph_tampering_fails_closed(self) -> None:
        mutations: tuple[tuple[Callable[[dict[str, object]], None], str], ...] = (
            (
                lambda value: value.update({"specVersion": "1.6"}),
                "sbom.standard-invalid",
            ),
            (
                lambda value: value["metadata"].update(
                    {"lifecycles": [{"phase": "post-build"}]}
                ),
                "sbom.lifecycle-invalid",
            ),
            (
                lambda value: value.update({"compositions": []}),
                "sbom.composition-incomplete",
            ),
            (
                lambda value: value["dependencies"].pop(),
                "sbom.dependency-entry-missing",
            ),
            (
                lambda value: value["dependencies"][0]["dependsOn"].append("unknown"),
                "sbom.dependency-order-invalid",
            ),
        )
        managed = _managed_graph()
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        for mutation, code in mutations:
            with (
                self.subTest(code=code),
                self.assertRaises(CycloneDxBomError) as caught,
            ):
                validate_cyclonedx_bom(
                    _mutate(content, mutation),
                    lifecycle=CycloneDxLifecycle.SOURCE,
                    managed_graph=managed,
                )
            self.assertEqual(caught.exception.code, code)

    def test_managed_component_edge_mismatch_is_rejected(self) -> None:
        managed = _managed_graph()
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )

        def remove_edge(value: dict[str, object]) -> None:
            root = next(
                item
                for item in value["dependencies"]
                if item["ref"] == managed.root_ref
            )
            root["dependsOn"] = []

        with self.assertRaises(CycloneDxBomError) as caught:
            validate_cyclonedx_bom(
                _mutate(content, remove_edge),
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
            )
        self.assertEqual(caught.exception.code, "sbom.component-disconnected")

    def test_no_dependency_application_has_explicit_empty_root_leaf(self) -> None:
        managed = _managed_graph(with_dependency=False)
        source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        content, binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=managed,
            source_bom=source,
        )
        self.assertEqual(
            json.loads(content)["dependencies"],
            [{"dependsOn": [], "ref": managed.root_ref}],
        )
        self.assertEqual(binding.component_count, 1)
        self.assertEqual(binding.edge_count, 0)

    def test_real_composition_and_shared_repository_leaf_are_in_both_boms(self) -> None:
        root_identity = _identity("c")
        dependency_identity = _identity("d")
        root_ref = ComponentRevisionRef(
            ComponentCoordinate("example", "application"),
            "1.0.0",
            root_identity,
        )
        dependency_ref = ComponentRevisionRef(
            ComponentCoordinate("example", "library"),
            "2.0.0",
            dependency_identity,
        )
        requirement = CapabilityRequirement(
            "runtime-library",
            "example.library",
            ">=2,<3",
            DependencyKind.RUNTIME,
        )
        edge = DependencyEdge(
            root_identity,
            dependency_identity,
            requirement,
            Capability("example.library", "2.0.0"),
            _identity("e"),
        )
        composition = ComponentComposition(
            root_identity,
            (root_identity, dependency_identity),
            (edge,),
            (),
            root_ref,
            (root_ref, dependency_ref),
        )
        repository = RepositorySourceDependency(
            "shared-repository",
            "https://example.invalid/shared.git",
            RepositoryRevisionSelector(
                RepositoryRevisionKind.COMMIT,
                "1" * 40,
            ),
            DependencyKind.BUILD,
        )
        managed = CycloneDxManagedGraph.from_component_composition(
            root_ref=composition.root_ref,
            revision_refs=composition.revision_refs,
            edges=composition.edges,
            composition_identity=composition.identity,
            repository_dependencies={
                root_identity.uri: (repository,),
                dependency_identity.uri: (repository,),
            },
        )
        repository_nodes = [
            item
            for item in managed.components
            if item.kind is ManagedComponentKind.REPOSITORY_SOURCE
        ]
        self.assertEqual(len(repository_nodes), 1)
        repository_node = repository_nodes[0]
        self.assertIn(
            CycloneDxManagedEdge(
                component_bom_ref(root_identity),
                repository_node.bom_ref,
                DependencyKind.BUILD,
                False,
                repository.identity,
            ),
            managed.edges,
        )
        self.assertIn(
            CycloneDxManagedEdge(
                component_bom_ref(dependency_identity),
                repository_node.bom_ref,
                DependencyKind.BUILD,
                False,
                repository.identity,
            ),
            managed.edges,
        )
        source_content, source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
        )
        for lifecycle in (CycloneDxLifecycle.SOURCE, CycloneDxLifecycle.RESOLVED):
            with self.subTest(lifecycle=lifecycle):
                if lifecycle is CycloneDxLifecycle.SOURCE:
                    content, binding = source_content, source_binding
                else:
                    content, binding = build_cyclonedx_bom(
                        lifecycle=lifecycle,
                        managed_graph=managed,
                        source_bom=source_content,
                    )
                refs = {item["bom-ref"] for item in json.loads(content)["components"]}
                self.assertEqual(
                    refs,
                    {
                        component_bom_ref(dependency_identity),
                        repository_node.bom_ref,
                    },
                )
                self.assertEqual(binding.component_count, 3)

    def test_noncanonical_json_and_duplicate_keys_are_rejected(self) -> None:
        managed = _managed_graph(with_dependency=False)
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        with self.assertRaises(CycloneDxBomError) as caught:
            validate_cyclonedx_bom(
                content + b"\n",
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
            )
        self.assertEqual(caught.exception.code, "sbom.json-noncanonical")
        duplicate = content.replace(b'{"$schema":', b'{"version":1,"$schema":', 1)
        with self.assertRaises(CycloneDxBomError) as caught:
            validate_cyclonedx_bom(
                duplicate,
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
            )
        self.assertEqual(caught.exception.code, "sbom.json-duplicate-key")

    def test_schema_failure_reports_the_invalid_value_and_location(self) -> None:
        managed = _managed_graph(with_dependency=False)
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )

        def invalidate_version(value: dict[str, object]) -> None:
            value["version"] = "bad-version"

        with self.assertRaises(CycloneDxBomError) as caught:
            validate_cyclonedx_bom(
                _mutate(content, invalidate_version),
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
            )
        self.assertEqual(caught.exception.code, "sbom.schema-invalid")
        self.assertIn("'bad-version' is not of type 'integer'", str(caught.exception))
        self.assertIn("instance['version']", str(caught.exception))

    def test_resolved_transition_preserves_source_inventory_edges_and_binding(
        self,
    ) -> None:
        managed = _managed_graph(with_dependency=False)
        package_ref = "pkg:generic/source-package"
        new_ref = "pkg:generic/resolved-only"
        source, source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
            additional_components=(_package(package_ref),),
            additional_edges=((managed.root_ref, package_ref),),
        )
        new_component = _package(new_ref, resolved=True)
        new_component["name"] = "resolved-only"
        resolved, resolved_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=managed,
            source_bom=source,
            additional_components=(
                _package(package_ref, resolved=True),
                new_component,
            ),
            additional_edges=(
                (managed.root_ref, package_ref),
                (managed.root_ref, new_ref),
                (new_ref, package_ref),
            ),
        )

        checked_source, checked_resolved = validate_resolved_cyclonedx_bom(
            resolved,
            source_content=source,
            source_managed_graph=managed,
        )
        self.assertEqual(checked_source, source_binding)
        self.assertEqual(checked_resolved, resolved_binding)
        self.assertEqual(
            resolved_binding.source_bom_identity, source_binding.bom_identity
        )

        def reparent(value: dict[str, object]) -> None:
            root = next(
                item
                for item in value["dependencies"]
                if item["ref"] == managed.root_ref
            )
            root["dependsOn"].remove(package_ref)

        with self.assertRaises(CycloneDxBomError) as caught:
            validate_resolved_cyclonedx_bom(
                _mutate(resolved, reparent),
                source_content=source,
                source_managed_graph=managed,
            )
        self.assertEqual(caught.exception.code, "sbom.transition-edge-dropped")

        def drop_source_node(value: dict[str, object]) -> None:
            value["components"] = [
                item for item in value["components"] if item["bom-ref"] != package_ref
            ]
            value["dependencies"] = [
                item for item in value["dependencies"] if item["ref"] != package_ref
            ]
            for item in value["dependencies"]:
                item["dependsOn"] = [
                    target for target in item["dependsOn"] if target != package_ref
                ]

        with self.assertRaises(CycloneDxBomError) as caught:
            validate_resolved_cyclonedx_bom(
                _mutate(resolved, drop_source_node),
                source_content=source,
                source_managed_graph=managed,
            )
        self.assertEqual(caught.exception.code, "sbom.transition-inventory-dropped")

    def test_every_bom_binds_one_unchanged_resolved_graph(self) -> None:
        source_managed = _managed_graph(with_dependency=False)
        source, source_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=source_managed,
        )
        resolved, resolved_binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=source_managed,
            source_bom=source,
        )
        for content, binding in (
            (source, source_binding),
            (resolved, resolved_binding),
        ):
            with self.subTest(lifecycle=binding.lifecycle):
                metadata_properties = {
                    item["name"]: item["value"]
                    for item in json.loads(content)["metadata"]["properties"]
                }
                self.assertEqual(
                    metadata_properties["literate-ai:resolved-graph-identity"],
                    source_managed.resolved_graph_identity.uri,
                )
                self.assertEqual(
                    binding.resolved_graph_identity,
                    source_managed.resolved_graph_identity,
                )

                def change_resolved_graph_identity(value: dict[str, object]) -> None:
                    for item in value["metadata"]["properties"]:
                        if item["name"] == "literate-ai:resolved-graph-identity":
                            item["value"] = _identity("f").uri

                with self.assertRaises(CycloneDxBomError) as caught:
                    if binding.lifecycle is CycloneDxLifecycle.SOURCE:
                        validate_cyclonedx_bom(
                            _mutate(content, change_resolved_graph_identity),
                            lifecycle=CycloneDxLifecycle.SOURCE,
                            managed_graph=source_managed,
                        )
                    else:
                        validate_resolved_cyclonedx_bom(
                            _mutate(content, change_resolved_graph_identity),
                            source_content=source,
                            source_managed_graph=source_managed,
                        )
                self.assertEqual(
                    caught.exception.code,
                    "sbom.resolved-graph-binding-invalid",
                )

        def use_legacy_property(value: dict[str, object]) -> None:
            binding = next(
                item
                for item in value["metadata"]["properties"]
                if item["name"] == "literate-ai:resolved-graph-identity"
            )
            binding["name"] = "literate-ai:component-composition-identity"

        legacy_source = _mutate(source, use_legacy_property)
        legacy_binding = validate_cyclonedx_bom(
            legacy_source,
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=source_managed,
        )
        self.assertEqual(
            legacy_binding.resolved_graph_identity,
            source_managed.resolved_graph_identity,
        )

        def mix_v1_and_v2_properties(value: dict[str, object]) -> None:
            value["metadata"]["properties"].append(
                {
                    "name": "literate-ai:component-composition-identity",
                    "value": source_managed.resolved_graph_identity.uri,
                }
            )

        with self.assertRaises(CycloneDxBomError) as caught:
            validate_cyclonedx_bom(
                _mutate(source, mix_v1_and_v2_properties),
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=source_managed,
            )
        self.assertEqual(caught.exception.code, "sbom.resolved-graph-binding-invalid")

        changed_managed = _managed_graph(with_dependency=True)
        with self.assertRaises(CycloneDxBomError) as caught:
            build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=changed_managed,
                source_bom=source,
                source_managed_graph=source_managed,
            )
        self.assertEqual(
            caught.exception.code,
            "sbom.transition-managed-graph-changed",
        )

    def test_edge_kind_and_optionality_are_exact_for_a_shared_diamond(self) -> None:
        root_identity = _identity("1")
        child_identity = _identity("2")
        root = ComponentRevisionRef(
            ComponentCoordinate("diamond", "root"), "1.0.0", root_identity
        )
        child = ComponentRevisionRef(
            ComponentCoordinate("diamond", "child"), "1.0.0", child_identity
        )
        requirement = CapabilityRequirement(
            "child", "diamond.child", ">=1,<2", DependencyKind.RUNTIME
        )
        component_edge = DependencyEdge(
            root_identity,
            child_identity,
            requirement,
            Capability("diamond.child", "1.0.0"),
            _identity("3"),
        )
        shared_build = RepositorySourceDependency(
            "shared",
            "https://example.invalid/shared.git",
            RepositoryRevisionSelector(RepositoryRevisionKind.COMMIT, "4" * 40),
            DependencyKind.BUILD,
        )
        shared_optional_runtime = RepositorySourceDependency(
            "shared",
            "https://example.invalid/shared.git",
            RepositoryRevisionSelector(RepositoryRevisionKind.COMMIT, "4" * 40),
            DependencyKind.RUNTIME,
            optional=True,
        )
        managed = CycloneDxManagedGraph.from_component_composition(
            root_ref=root,
            revision_refs=(root, child),
            edges=(component_edge,),
            composition_identity=_identity("5"),
            repository_dependencies={
                root_identity.uri: (shared_build,),
                child_identity.uri: (shared_optional_runtime,),
            },
        )
        repository_nodes = tuple(
            item
            for item in managed.components
            if item.kind is ManagedComponentKind.REPOSITORY_SOURCE
        )
        self.assertEqual(len(repository_nodes), 1)
        repository_ref = repository_nodes[0].bom_ref
        self.assertIn(
            CycloneDxManagedEdge(
                component_bom_ref(root_identity),
                repository_ref,
                DependencyKind.BUILD,
                False,
                shared_build.identity,
            ),
            managed.edges,
        )
        self.assertIn(
            CycloneDxManagedEdge(
                component_bom_ref(child_identity),
                repository_ref,
                DependencyKind.RUNTIME,
                True,
                shared_optional_runtime.identity,
            ),
            managed.edges,
        )
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        inventory = {
            item["bom-ref"]: item
            for item in (
                json.loads(content)["metadata"]["component"],
                *json.loads(content)["components"],
            )
        }
        root_values = {
            item["value"]
            for item in inventory[component_bom_ref(root_identity)]["properties"]
            if item["name"] == "literate-ai:dependency-edge"
        }
        child_values = {
            item["value"]
            for item in inventory[component_bom_ref(child_identity)]["properties"]
            if item["name"] == "literate-ai:dependency-edge"
        }
        self.assertIn(
            canonical_json_bytes(
                {
                    "kind": "build",
                    "optional": False,
                    "relationship": shared_build.identity.uri,
                    "target": repository_ref,
                }
            ).decode(),
            root_values,
        )
        self.assertIn(
            canonical_json_bytes(
                {
                    "kind": "runtime",
                    "optional": True,
                    "relationship": shared_optional_runtime.identity.uri,
                    "target": repository_ref,
                }
            ).decode(),
            child_values,
        )

    def test_repository_owner_map_is_total_and_exact(self) -> None:
        root = ComponentRevisionRef(
            ComponentCoordinate("owners", "root"), "1.0.0", _identity("5")
        )
        child = ComponentRevisionRef(
            ComponentCoordinate("owners", "child"), "1.0.0", _identity("6")
        )
        with self.assertRaisesRegex(ValueError, "omits a transitive Component"):
            CycloneDxManagedGraph.from_component_composition(
                root_ref=root,
                revision_refs=(root, child),
                edges=(),
                composition_identity=_identity("8"),
                repository_dependencies={root.revision_identity.uri: ()},
            )
        with self.assertRaisesRegex(ValueError, "unknown Component"):
            CycloneDxManagedGraph.from_component_composition(
                root_ref=root,
                revision_refs=(root,),
                edges=(),
                composition_identity=_identity("8"),
                repository_dependencies={
                    root.revision_identity.uri: (),
                    _identity("7").uri: (),
                },
            )

    def test_distinct_component_relationships_do_not_collapse(self) -> None:
        root_identity = _identity("1")
        child_identity = _identity("2")
        root = ComponentRevisionRef(
            ComponentCoordinate("relationships", "root"), "1.0.0", root_identity
        )
        child = ComponentRevisionRef(
            ComponentCoordinate("relationships", "child"), "1.0.0", child_identity
        )
        first = DependencyEdge(
            root_identity,
            child_identity,
            CapabilityRequirement(
                "first-requirement",
                "relationships.child",
                ">=1,<2",
                DependencyKind.RUNTIME,
            ),
            Capability("relationships.child", "1.0.0"),
            _identity("3"),
        )
        second = DependencyEdge(
            root_identity,
            child_identity,
            CapabilityRequirement(
                "second-requirement",
                "relationships.child",
                ">=1,<2",
                DependencyKind.RUNTIME,
            ),
            Capability("relationships.child", "1.0.0"),
            _identity("4"),
        )
        managed = CycloneDxManagedGraph.from_component_composition(
            root_ref=root,
            revision_refs=(root, child),
            edges=(first, second),
            composition_identity=_identity("5"),
            repository_dependencies={root_identity.uri: (), child_identity.uri: ()},
        )

        relationship_ids = {edge.relationship_identity for edge in managed.edges}
        self.assertEqual(relationship_ids, {first.identity, second.identity})
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        document = json.loads(content)
        root_dependencies = next(
            item
            for item in document["dependencies"]
            if item["ref"] == component_bom_ref(root_identity)
        )
        self.assertEqual(
            root_dependencies["dependsOn"], [component_bom_ref(child_identity)]
        )
        relationship_values = {
            item["value"]
            for item in document["metadata"]["component"]["properties"]
            if item["name"] == "literate-ai:dependency-edge"
        }
        self.assertEqual(len(relationship_values), 2)

        def drop_relationship(value: dict[str, object]) -> None:
            properties = value["metadata"]["component"]["properties"]
            removed = False
            retained = []
            for item in properties:
                if item["name"] == "literate-ai:dependency-edge" and not removed:
                    removed = True
                    continue
                retained.append(item)
            value["metadata"]["component"]["properties"] = retained

        with self.assertRaises(CycloneDxBomError) as caught:
            validate_cyclonedx_bom(
                _mutate(content, drop_relationship),
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
            )
        self.assertEqual(caught.exception.code, "sbom.component-graph-mismatch")

    def test_managed_identity_alias_and_unmanaged_managed_kind_are_rejected(
        self,
    ) -> None:
        managed = _managed_graph(with_dependency=False)
        content, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        root_identity = next(
            item.identity
            for item in managed.components
            if item.kind is ManagedComponentKind.ROOT
        )

        def add_forged(value: dict[str, object], *, claim: bool) -> None:
            properties = [
                {"name": "literate-ai:dependency-kind", "value": "component"},
                {"name": "literate-ai:dependency-scope", "value": "runtime"},
            ]
            if claim:
                properties.append(
                    {
                        "name": "literate-ai:component-revision",
                        "value": root_identity.uri,
                    }
                )
            value["components"].append(
                {
                    "bom-ref": "pkg:generic/forged",
                    "name": "forged",
                    "type": "library",
                    "version": "1.0.0",
                    "properties": properties,
                }
            )
            value["dependencies"].append({"ref": "pkg:generic/forged", "dependsOn": []})
            value["dependencies"].sort(key=lambda item: item["ref"])
            root = next(
                item
                for item in value["dependencies"]
                if item["ref"] == managed.root_ref
            )
            root["dependsOn"].append("pkg:generic/forged")
            root["dependsOn"].sort()

        for claim in (False, True):
            with (
                self.subTest(claim=claim),
                self.assertRaises(CycloneDxBomError) as caught,
            ):
                validate_cyclonedx_bom(
                    _mutate(
                        content,
                        lambda value, selected=claim: add_forged(value, claim=selected),
                    ),
                    lifecycle=CycloneDxLifecycle.SOURCE,
                    managed_graph=managed,
                )
            self.assertEqual(caught.exception.code, "sbom.managed-identity-claimed")

    def test_mutable_repository_requires_exact_lock_and_resolution_properties(
        self,
    ) -> None:
        root_identity = _identity("8")
        root = ComponentRevisionRef(
            ComponentCoordinate("mutable", "root"), "1.0.0", root_identity
        )
        dependency = RepositorySourceDependency(
            "mutable-repository",
            "https://example.invalid/mutable.git",
            RepositoryRevisionSelector(RepositoryRevisionKind.BRANCH, "release"),
            DependencyKind.BUILD,
        )
        managed = CycloneDxManagedGraph.from_component_composition(
            root_ref=root,
            revision_refs=(root,),
            edges=(),
            composition_identity=_identity("7"),
            repository_dependencies={root_identity.uri: (dependency,)},
        )
        source, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
        )
        repository = json.loads(source)["components"][0]
        self.assertEqual(repository["versionRange"], "git:branch:release")
        self.assertNotIn("version", repository)

        with self.assertRaises(CycloneDxBomError) as caught:
            build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=managed,
                source_bom=source,
            )
        self.assertEqual(caught.exception.code, "sbom.resolution-missing")

        resolution = CycloneDxRepositorySourceResolution(
            repository_dependency_identity(dependency),
            "9" * 40,
            _identity("a"),
            _identity("b"),
            _identity("c"),
            _identity("d"),
            _identity("e"),
            _identity("f"),
            _identity("0"),
        )
        resolved, _ = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.RESOLVED,
            managed_graph=managed,
            source_bom=source,
            repository_resolutions=(resolution,),
        )
        resolved_repository = json.loads(resolved)["components"][0]
        self.assertEqual(resolved_repository["version"], "9" * 40)
        property_names = {item["name"] for item in resolved_repository["properties"]}
        self.assertTrue(
            {
                "literate-ai:repository-source-lock",
                "literate-ai:repository-source-snapshot",
                "literate-ai:repository-source-tree",
                "literate-ai:repository-source-resolver",
                "literate-ai:repository-source-index",
                "literate-ai:repository-source-admission",
                "literate-ai:repository-source-cache",
            }
            <= property_names
        )


if __name__ == "__main__":
    unittest.main()

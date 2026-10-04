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
)
from literate_ai.contracts import (
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    canonical_json_bytes,
    component_bom_ref,
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


if __name__ == "__main__":
    unittest.main()

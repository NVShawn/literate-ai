"""Project a live SDK's source admission and loader graph into CycloneDX inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.sbom import (
    CycloneDxRepositorySourceResolution,
    component_bom_ref,
    repository_dependency_bom_ref,
    repository_dependency_identity,
)

if TYPE_CHECKING:
    from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInput


@dataclass(frozen=True, slots=True)
class NativeSdkDependencyEvidence:
    """Immutable dependency projection; evidence is not consumer authorization."""

    input_identity: ContentIdentity
    repository_resolution: CycloneDxRepositorySourceResolution
    _components_json: bytes
    edges: tuple[tuple[str, str], ...]

    @property
    def observation(self) -> HostDependencyObservation:
        return HostDependencyObservation(
            tuple(json.loads(self._components_json)), self.edges
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/native-sdk-dependency-evidence@1",
            "input_identity": self.input_identity.to_dict(),
            "repository_resolution": self.repository_resolution.to_dict(),
            "components": json.loads(self._components_json),
            "edges": [list(edge) for edge in self.edges],
        }


def merge_sdk_host_observations(
    sdk: HostDependencyObservation, host: HostDependencyObservation
) -> HostDependencyObservation:
    """Shared images may have both SDK runtime and consumer toolchain scopes."""
    components: dict[str, dict[str, object]] = {}
    for raw in (*sdk.components, *host.components):
        component = json.loads(canonical_json_bytes(raw))
        ref = component["bom-ref"]
        existing = components.get(ref)
        if existing is not None and existing != component:

            def without_scopes(value):
                result = dict(value)
                result["properties"] = sorted(
                    (
                        item
                        for item in value.get("properties", [])
                        if item["name"] != "literate-ai:dependency-scope"
                    ),
                    key=lambda item: (item["name"], item["value"]),
                )
                return result

            if without_scopes(existing) != without_scopes(component):
                raise ValueError("SDK and host dependency observations conflict")
            scopes = {
                item["value"]
                for value in (existing, component)
                for item in value.get("properties", [])
                if item["name"] == "literate-ai:dependency-scope"
            }
            component = without_scopes(component)
            component["properties"].extend(
                {"name": "literate-ai:dependency-scope", "value": scope}
                for scope in sorted(scopes)
            )
        components[ref] = component
    return HostDependencyObservation(
        tuple(components[ref] for ref in sorted(components)),
        tuple(sorted(set((*sdk.edges, *host.edges)))),
    )


def merge_native_sdk_dependencies(
    evidence: tuple[NativeSdkDependencyEvidence, ...],
) -> tuple[HostDependencyObservation, tuple[CycloneDxRepositorySourceResolution, ...]]:
    """Merge a consumer's closure, retaining every SDK's own admission binding.

    A shared repository inventory node carries one canonical source proof. Every
    product still records its own admission and complete resolution identity; only
    proofs of the same exact source bytes may share that repository node.
    """
    observation = HostDependencyObservation((), ())
    resolutions: dict[ContentIdentity, CycloneDxRepositorySourceResolution] = {}
    inputs = set()
    for item in evidence:
        if item.input_identity in inputs:
            raise ValueError("SDK dependency evidence contains duplicate inputs")
        inputs.add(item.input_identity)
        observation = merge_sdk_host_observations(observation, item.observation)
        resolution = item.repository_resolution
        existing = resolutions.get(resolution.dependency_identity)
        if existing is not None:
            if any(
                getattr(existing, field) != getattr(resolution, field)
                for field in (
                    "resolved_commit",
                    "source_snapshot_identity",
                    "source_tree_identity",
                    "resolver_identity",
                )
            ):
                raise ValueError("SDK dependency repository source proofs conflict")
            resolution = min(
                (existing, resolution), key=lambda value: value.identity.uri
            )
        resolutions[resolution.dependency_identity] = resolution
    return (
        observation,
        tuple(
            resolutions[key] for key in sorted(resolutions, key=lambda value: value.uri)
        ),
    )


def project_native_sdk_dependencies(
    binding: NativeSdkConsumerInput, *, root: Path, runtime: dict[str, object]
) -> NativeSdkDependencyEvidence:
    """Project an observed materialization, preserving every native dependency.

    The consumer-input service owns producer and byte verification. This pure
    projection cannot authenticate caller-supplied SDK or runtime records.
    """
    built = binding.build
    sdk = built.product.snapshot
    layout = built.selection.recipe.layout
    if (
        runtime.get("schema") != "literate-ai/native-sdk-runtime-observation@1"
        or runtime.get("snapshot_identity") != sdk.identity.to_dict()
        or runtime.get("target_identity") != sdk.target_identity.to_dict()
        or runtime.get("operating_system") != layout.operating_system
        or runtime.get("architecture") != layout.architecture
    ):
        raise ValueError("SDK dependency observation differs from the bound product")
    raw_components = runtime.get("components")
    raw_edges = runtime.get("edges")
    libraries = runtime.get("native_libraries")
    if not all(
        isinstance(value, list) for value in (raw_components, raw_edges, libraries)
    ):
        raise ValueError("SDK dependency observation must contain a complete graph")
    package_ref = f"urn:literate-ai:native-sdk:{binding.identity.digest}"
    original_root = sdk.identity.uri
    refs = {original_root: package_ref}
    components: dict[str, dict[str, object]] = {}
    relative_by_ref = {}
    files = {item.path: item.blob for item in sdk.files}
    exact_root = root.resolve(strict=True)
    root_prefix = exact_root.as_posix() + "/"
    native_prefix = str(exact_root) + ("\\" if "\\" in str(exact_root) else "/")
    path_property = {
        "linux": "literate-ai:elf-path",
        "macos": "literate-ai:macho-path",
        "windows": "literate-ai:pe-path",
    }[layout.operating_system]
    for raw in raw_components:
        if not isinstance(raw, dict):
            raise ValueError("SDK dependency component must be an object")
        ref = raw.get("bom-ref")
        if not isinstance(ref, str) or not ref or ref in refs:
            raise ValueError("SDK dependency component reference is ambiguous")
        properties = raw.get("properties", [])
        if not isinstance(properties, list) or any(
            not isinstance(item, dict)
            or set(item) != {"name", "value"}
            or not all(isinstance(item[key], str) for key in ("name", "value"))
            for item in properties
        ):
            raise ValueError("SDK dependency properties must be string pairs")
        paths = [item["value"] for item in properties if item["name"] == path_property]
        if len(paths) > 1:
            raise ValueError("SDK dependency image path is ambiguous")
        component = json.loads(canonical_json_bytes(raw))
        relative = None
        if paths and paths[0].replace("\\", "/").startswith(root_prefix):
            relative = paths[0].replace("\\", "/")[len(root_prefix) :]
            blob = files.get(relative)
            if blob is None or {
                "alg": "SHA-256",
                "content": blob.digest,
            } not in raw.get("hashes", []):
                raise ValueError("SDK dependency image differs from captured SDK bytes")
            relative_by_ref[ref] = relative
            image_identity = canonical_identity(
                {
                    "sdk": sdk.identity.to_dict(),
                    "path": relative,
                    "blob": blob.to_dict(),
                }
            )
            component["bom-ref"] = (
                f"urn:literate-ai:native-sdk-image:{image_identity.digest}"
            )
        refs[ref] = component["bom-ref"]
        component["properties"] = [
            {
                "name": item["name"],
                "value": item["value"]
                .replace(native_prefix, "sdk:///")
                .replace(root_prefix, "sdk:///"),
            }
            for item in properties
        ]
        if relative is not None:
            component["properties"] = [
                {"name": item["name"], "value": "runtime"}
                if item["name"] == "literate-ai:dependency-kind"
                else item
                for item in component["properties"]
                if item != {"name": "literate-ai:dependency-scope", "value": "system"}
            ]
            runtime_scope = {"name": "literate-ai:dependency-scope", "value": "runtime"}
            if runtime_scope not in component["properties"]:
                component["properties"].append(runtime_scope)
            component["properties"].append(
                {"name": "literate-ai:native-sdk-relative-path", "value": relative}
            )
        if component["bom-ref"] in components:
            raise ValueError("SDK dependency image references collide after projection")
        components[component["bom-ref"]] = component
    declared = {}
    for library in libraries:
        if not isinstance(library, dict) or set(library) != {"path", "component"}:
            raise ValueError("SDK dependency native library binding is invalid")
        path, ref = library["path"], library["component"]
        if not isinstance(path, str) or not isinstance(ref, str) or path in declared:
            raise ValueError("SDK dependency native library binding is ambiguous")
        if relative_by_ref.get(ref) != path:
            raise ValueError(
                "SDK dependency native library is absent from its snapshot"
            )
        declared[path] = ref
    if set(declared) != set(sdk.native_libraries):
        raise ValueError("SDK dependency native library bindings are incomplete")
    edges = set()
    graph = {ref: set() for ref in refs}
    for edge in raw_edges:
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or any(not isinstance(ref, str) or ref not in refs for ref in edge)
        ):
            raise ValueError("SDK dependency graph contains an unresolved edge")
        source, target = edge
        if target == original_root:
            raise ValueError("SDK dependency graph targets its observation root")
        graph[source].add(target)
        edges.add((refs[source], refs[target]))
    pending, seen = [original_root], set()
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(graph[current] - seen)
    if seen != set(refs):
        raise ValueError("SDK dependency graph contains disconnected components")
    resolution = built.resolution
    lock = resolution.lock
    source_resolution = CycloneDxRepositorySourceResolution(
        repository_dependency_identity(lock.dependency),
        lock.resolved_commit,
        lock.identity,
        lock.source_snapshot,
        lock.source_tree,
        lock.resolver,
        resolution.index_binding.identity,
        resolution.admission.identity,
        resolution.cache_record,
    )
    metadata = {
        "input": binding.identity,
        "snapshot": sdk.identity,
        "recipe": sdk.recipe_identity,
        "target": sdk.target_identity,
        "license": sdk.license_identity,
        "import-surface": sdk.import_surface.identity,
        "source-lock": lock.identity,
        "source-admission": resolution.admission.identity,
        "source-resolution": source_resolution.identity,
    }
    components[package_ref] = {
        "type": "library",
        "bom-ref": package_ref,
        "name": sdk.import_surface.package,
        "version": sdk.identity.uri,
        "hashes": [{"alg": "SHA-256", "content": sdk.identity.digest}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "runtime"},
            {"name": "literate-ai:dependency-scope", "value": "runtime"},
            *(
                {"name": f"literate-ai:native-sdk-{name}", "value": value.uri}
                for name, value in sorted(metadata.items())
            ),
        ],
    }
    edges.add((component_bom_ref(built.selection.component_revision), package_ref))
    edges.add(
        (
            package_ref,
            repository_dependency_bom_ref(source_resolution.dependency_identity),
        )
    )
    return NativeSdkDependencyEvidence(
        binding.identity,
        source_resolution,
        canonical_json_bytes([components[ref] for ref in sorted(components)]),
        tuple(sorted(edges)),
    )

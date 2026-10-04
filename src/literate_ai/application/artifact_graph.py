"""Pure application services for source assembly and artifact graph creation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.cpp_libraries import CppLibraryLayout
from literate_ai.contracts.executable_components.artifacts import (
    ArtifactAssemblyDependency,
    ArtifactBuildGraph,
    ArtifactExport,
    ArtifactMaterializationPlan,
    BuildPrivilege,
    BuildSubActionKind,
    ComponentBuildManifest,
    CompositeBuildRequest,
    ExactLinkPlan,
    GeneratedTextTree,
    OrderedBuildSubAction,
    SourceTreeEntry,
    SourceTreeEntryOrigin,
    SourceTreeManifest,
    authored_asset_identities,
)
from literate_ai.contracts.executable_components.assets import AuthoredBinaryAsset
from literate_ai.contracts.executable_components.packages import (
    PackageEntrypoint,
    PackageFileKind,
    PackageInput,
    PackageKind,
    PackagePlan,
    RuntimeRequirement,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity


class ArtifactAssemblyError(ValueError):
    """An immutable input did not match its declared bytes or authority."""


def create_resource_package_input(
    asset: AuthoredBinaryAsset,
    *,
    destination: str,
) -> PackageInput:
    """Project an authorized arbitrary-byte asset into a package plan."""

    if not isinstance(asset, AuthoredBinaryAsset):
        raise ArtifactAssemblyError("package resource must be an authored binary asset")
    return PackageInput(
        path=destination,
        role=asset.role,
        kind=PackageFileKind.RESOURCE,
        source_identity=asset.identity,
        target_identity=asset.target_identity,
        blob=asset.blob,
    )


def realize_manifest(
    manifest: ComponentBuildManifest,
    exports: Iterable[ArtifactExport],
) -> ComponentBuildManifest:
    """Bind post-build blobs to every authorized output declaration exactly once."""

    realized = tuple(exports)
    if not realized or any(type(item) is not ArtifactExport for item in realized):
        raise ArtifactAssemblyError("realized exports must be typed and non-empty")
    export_ids = tuple(item.export_id for item in realized)
    if export_ids != tuple(sorted(set(export_ids))):
        raise ArtifactAssemblyError("realized exports must use canonical unique IDs")
    if tuple(item.declaration for item in realized) != manifest.export_declarations:
        raise ArtifactAssemblyError(
            "realized exports differ from the exact authorized declarations"
        )
    return replace(manifest, exports=realized)


BlobReader = Callable[[BlobRef], bytes]


def _require_blob(blob: BlobRef, reader: BlobReader, *, text: bool) -> None:
    content = reader(blob)
    if not isinstance(content, bytes):
        raise ArtifactAssemblyError("blob reader must return bytes")
    if len(content) != blob.size:
        raise ArtifactAssemblyError(f"blob size mismatch: {blob.identity}")
    if hashlib.sha256(content).hexdigest() != blob.digest:
        raise ArtifactAssemblyError(f"blob digest mismatch: {blob.identity}")
    if text:
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ArtifactAssemblyError(
                f"generated text is not UTF-8: {blob.identity}"
            ) from exc


def assemble_source_tree(
    generated: GeneratedTextTree,
    authored_assets: Iterable[AuthoredBinaryAsset],
    *,
    read_blob: BlobReader,
) -> SourceTreeManifest:
    """Verify and combine model text with immutable authored binary overlays."""

    assets = tuple(sorted(authored_assets, key=lambda item: item.path))
    generated_paths = {item.path for item in generated.files}
    if len(generated_paths) != len(generated.files):
        raise ArtifactAssemblyError("generated text paths are not unique")
    for item in generated.files:
        _require_blob(item.blob, read_blob, text=True)
    for asset in assets:
        if asset.component_revision != generated.component_revision:
            raise ArtifactAssemblyError("authored asset belongs to another Component")
        if asset.target_identity != generated.target_identity:
            raise ArtifactAssemblyError(
                "authored asset targets another platform or ABI"
            )
        if asset.authorization_identity != generated.authorization_identity:
            raise ArtifactAssemblyError("authored asset has different authorization")
        if asset.path in generated_paths:
            raise ArtifactAssemblyError(
                "model output cannot overwrite an authored asset"
            )
        _require_blob(asset.blob, read_blob, text=False)
    entries = tuple(
        sorted(
            (
                *(
                    SourceTreeEntry(
                        path=item.path,
                        role="generated-source",
                        origin=SourceTreeEntryOrigin.GENERATED_TEXT,
                        blob=item.blob,
                    )
                    for item in generated.files
                ),
                *(
                    SourceTreeEntry(
                        path=item.path,
                        role=item.role,
                        origin=SourceTreeEntryOrigin.AUTHORED_BINARY,
                        blob=item.blob,
                    )
                    for item in assets
                ),
            ),
            key=lambda item: item.path,
        )
    )
    return SourceTreeManifest(
        component_revision=generated.component_revision,
        target_identity=generated.target_identity,
        authorization_identity=generated.authorization_identity,
        generated_text_tree_identity=generated.identity,
        authored_asset_identities=authored_asset_identities(assets),
        entries=entries,
    )


def plan_isolated_materialization(
    manifest: SourceTreeManifest, *, execution_nonce: ContentIdentity
) -> ArtifactMaterializationPlan:
    """Describe exact blob projection into a caller-created fresh empty root."""

    return ArtifactMaterializationPlan(
        source_tree_identity=manifest.identity,
        execution_root_identity=canonical_identity(
            {
                "source_tree_identity": manifest.identity.to_dict(),
                "execution_nonce": execution_nonce.to_dict(),
                "isolation": "fresh-empty-exact-blobs",
            }
        ),
        entries=manifest.entries,
    )


def _link_closure(
    root: str, exports: dict[str, ArtifactExport], additional: dict[str, set[str]]
) -> tuple[str, ...]:
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(uri: str) -> None:
        if uri in visiting:
            raise ArtifactAssemblyError("artifact dependency cycle detected")
        if uri in visited:
            return
        visiting.add(uri)
        artifact = exports.get(uri)
        if artifact is None:
            raise ArtifactAssemblyError(f"dependency artifact is absent: {uri}")
        for dependency in artifact.dependency_artifact_identities:
            visit(dependency.uri)
        for dependency in additional.get(uri, ()):
            visit(dependency)
        visiting.remove(uri)
        visited.add(uri)

    visit(root)
    return tuple(sorted(visited))


def create_artifact_build_graph(
    *,
    build_system_driver_identity: ContentIdentity,
    manifests: Iterable[ComponentBuildManifest],
    link_roots: Iterable[ContentIdentity],
    link_root_groups: Iterable[tuple[ContentIdentity, ...]] = (),
    assembly_dependencies: Iterable[ArtifactAssemblyDependency] = (),
) -> ArtifactBuildGraph:
    """Canonicalize an arbitrary adapter's manifests and derive exact link closures."""

    ordered_manifests = tuple(
        sorted(manifests, key=lambda item: item.component_revision.uri)
    )
    if any(not manifest.exports for manifest in ordered_manifests):
        raise ArtifactAssemblyError(
            "artifact build graph requires realized Component exports"
        )
    exports = {
        export.identity.uri: export
        for manifest in ordered_manifests
        for export in manifest.exports
    }
    dependencies = tuple(assembly_dependencies)
    if len(dependencies) > 16384 or any(
        not isinstance(item, ArtifactAssemblyDependency) for item in dependencies
    ):
        raise ArtifactAssemblyError(
            "assembly dependencies must be bounded typed records"
        )
    dependencies = tuple(sorted(dependencies, key=lambda item: item.identity.uri))
    additional: dict[str, set[str]] = {}
    for dependency in dependencies:
        consumer = dependency.consumer_artifact_identity.uri
        provider = dependency.provider_artifact_identity.uri
        if consumer not in exports or provider not in exports:
            raise ArtifactAssemblyError("assembly endpoint is absent from the graph")
        additional.setdefault(consumer, set()).add(provider)
    links: list[ExactLinkPlan] = []
    for root in sorted(set(link_roots), key=lambda item: item.uri):
        if root.uri not in exports:
            raise ArtifactAssemblyError(f"link root is absent: {root.uri}")
        closure = _link_closure(root.uri, exports, additional)
        links.append(
            ExactLinkPlan(
                root_artifact_identity=root,
                ordered_artifact_identities=tuple(
                    ContentIdentity.parse_uri(uri) for uri in closure
                ),
            )
        )
    for raw_group in link_root_groups:
        group = tuple(raw_group)
        if len(group) < 2:
            raise ArtifactAssemblyError(
                "grouped link roots must contain at least two artifacts"
            )
        primary = group[0]
        canonical_group = tuple(sorted(set(group), key=lambda item: item.uri))
        if len(canonical_group) != len(group):
            raise ArtifactAssemblyError("grouped link roots must be unique")
        missing = tuple(item.uri for item in canonical_group if item.uri not in exports)
        if missing:
            raise ArtifactAssemblyError(f"link root is absent: {missing[0]}")
        closure = tuple(
            sorted(
                {
                    uri
                    for root in canonical_group
                    for uri in _link_closure(root.uri, exports, additional)
                }
            )
        )
        links.append(
            ExactLinkPlan(
                root_artifact_identity=primary,
                ordered_artifact_identities=tuple(
                    ContentIdentity.parse_uri(uri) for uri in closure
                ),
                root_artifact_identities=canonical_group,
            )
        )
    links.sort(key=lambda item: item.root_artifact_identity.uri)
    return ArtifactBuildGraph(
        build_system_driver_identity=build_system_driver_identity,
        manifests=ordered_manifests,
        link_plans=tuple(links),
        assembly_dependencies=dependencies,
    )


def create_package_plan(
    graph: ArtifactBuildGraph,
    *,
    root_component_revision: ContentIdentity,
    component_lock_identity: ContentIdentity,
    target_identity: ContentIdentity,
    root_artifact_identity: ContentIdentity,
    package_kind: PackageKind,
    packager_identity: ContentIdentity,
    destinations: Mapping[str, str],
    entrypoints: Iterable[PackageEntrypoint],
    resource_inputs: Iterable[PackageInput] = (),
    runtime_requirements: Iterable[RuntimeRequirement] = (),
    native_library_root: str | None = None,
    native_library_layout: CppLibraryLayout | None = None,
) -> PackagePlan:
    """Project one exact link closure into a target-specific package plan."""

    exports = {
        export.identity.uri: export
        for manifest in graph.manifests
        for export in manifest.exports
    }
    link = next(
        (
            item
            for item in graph.link_plans
            if item.root_artifact_identity == root_artifact_identity
        ),
        None,
    )
    if link is None:
        raise ArtifactAssemblyError("package root has no exact link plan")
    closure = tuple(item.uri for item in link.ordered_artifact_identities)
    if root_artifact_identity.uri not in destinations:
        raise ArtifactAssemblyError(
            "package destinations must include the linked root artifact"
        )
    if not set(destinations).issubset(closure):
        raise ArtifactAssemblyError(
            "package destinations must reference only exact linked artifacts"
        )
    if exports[root_artifact_identity.uri].component_revision != (
        root_component_revision
    ):
        raise ArtifactAssemblyError(
            "package root Component differs from the linked root artifact"
        )
    if any(exports[item].target_identity != target_identity for item in destinations):
        raise ArtifactAssemblyError("package input targets do not match the package")
    resources = tuple(resource_inputs)
    if any(item.kind is not PackageFileKind.RESOURCE for item in resources):
        raise ArtifactAssemblyError("additional package inputs must be resources")
    inputs = tuple(
        sorted(
            (
                *(
                    PackageInput(
                        path=destinations[uri],
                        role=exports[uri].role,
                        kind=PackageFileKind.ARTIFACT,
                        source_identity=exports[uri].identity,
                        target_identity=exports[uri].target_identity,
                        blob=exports[uri].blob,
                    )
                    for uri in destinations
                ),
                *resources,
            ),
            key=lambda item: item.path,
        )
    )
    return PackagePlan(
        root_component_revision=root_component_revision,
        component_lock_identity=component_lock_identity,
        target_identity=target_identity,
        artifact_graph_identity=graph.identity,
        link_plan_identity=link.identity,
        root_artifact_identity=root_artifact_identity,
        package_kind=package_kind,
        packager_identity=packager_identity,
        inputs=inputs,
        entrypoints=tuple(sorted(entrypoints, key=lambda item: item.name)),
        runtime_requirements=tuple(
            sorted(runtime_requirements, key=lambda item: item.requirement_id)
        ),
        native_library_root=native_library_root,
        native_library_layout=native_library_layout,
    )


def create_composite_build_request(
    manifest: ComponentBuildManifest,
    materialization_plan: ArtifactMaterializationPlan,
    *,
    build_system_resolver_identity: ContentIdentity,
    language_compiler_identity: ContentIdentity,
    language_runtime_identity: ContentIdentity,
    ordered_actions: Iterable[tuple[BuildSubActionKind, str]],
    requested_privileges: Iterable[BuildPrivilege],
) -> CompositeBuildRequest:
    """Bind one manifest to an exact ordered and authorized build invocation."""

    if materialization_plan.source_tree_identity != manifest.source_tree_identity:
        raise ArtifactAssemblyError(
            "materialization plan does not project the manifest source tree"
        )
    actions_by_id = {item.action_id: item for item in manifest.actions}
    order = tuple(ordered_actions)
    ordered_ids = tuple(action_id for _, action_id in order)
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ArtifactAssemblyError("ordered build actions must not repeat an action")
    if set(ordered_ids) != set(actions_by_id) or len(order) != len(actions_by_id):
        raise ArtifactAssemblyError(
            "ordered build actions must include every and only manifest action"
        )
    if any(not isinstance(kind, BuildSubActionKind) for kind, _ in order):
        raise ArtifactAssemblyError("ordered build actions require typed action kinds")

    first = manifest.actions[0]
    if any(
        action.toolchain_identity != language_compiler_identity
        for action in manifest.actions
    ):
        raise ArtifactAssemblyError(
            "language compiler identity does not match every manifest action"
        )
    privileges = tuple(requested_privileges)
    if any(not isinstance(item, BuildPrivilege) for item in privileges):
        raise ArtifactAssemblyError("requested privileges must be typed")
    request = CompositeBuildRequest(
        component_revision=manifest.component_revision,
        component_build_manifest_identity=manifest.identity,
        source_tree_identity=manifest.source_tree_identity,
        materialization_plan_identity=materialization_plan.identity,
        target_identity=first.target_identity,
        build_system_resolver_identity=build_system_resolver_identity,
        build_system_toolchain_identity=manifest.build_system_driver_identity,
        language_compiler_identity=language_compiler_identity,
        language_runtime_identity=language_runtime_identity,
        authorization_identity=first.authorization_identity,
        sub_actions=tuple(
            OrderedBuildSubAction(index, kind, actions_by_id[action_id])
            for index, (kind, action_id) in enumerate(order)
        ),
        requested_privileges=tuple(
            sorted(set(privileges), key=lambda item: item.value)
        ),
        declared_output_ids=tuple(
            item.export_id for item in manifest.export_declarations
        ),
    )
    return validate_composite_build_request(
        request,
        manifest,
        materialization_plan,
        build_system_resolver_identity=build_system_resolver_identity,
        language_compiler_identity=language_compiler_identity,
        language_runtime_identity=language_runtime_identity,
    )


def validate_composite_build_request(
    request: CompositeBuildRequest,
    manifest: ComponentBuildManifest,
    materialization_plan: ArtifactMaterializationPlan,
    *,
    build_system_resolver_identity: ContentIdentity,
    language_compiler_identity: ContentIdentity,
    language_runtime_identity: ContentIdentity,
) -> CompositeBuildRequest:
    """Revalidate a deserialized composite request against its exact authorities."""

    exact = {
        "component revision": (
            request.component_revision,
            manifest.component_revision,
        ),
        "Component build manifest": (
            request.component_build_manifest_identity,
            manifest.identity,
        ),
        "source tree": (request.source_tree_identity, manifest.source_tree_identity),
        "materialization plan": (
            request.materialization_plan_identity,
            materialization_plan.identity,
        ),
        "build-system toolchain": (
            request.build_system_toolchain_identity,
            manifest.build_system_driver_identity,
        ),
        "build-system resolver": (
            request.build_system_resolver_identity,
            build_system_resolver_identity,
        ),
        "language compiler": (
            request.language_compiler_identity,
            language_compiler_identity,
        ),
        "language runtime": (
            request.language_runtime_identity,
            language_runtime_identity,
        ),
    }
    for label, (actual, expected) in exact.items():
        if actual != expected:
            raise ArtifactAssemblyError(
                f"composite build request {label} identity mismatch"
            )
    if materialization_plan.source_tree_identity != manifest.source_tree_identity:
        raise ArtifactAssemblyError(
            "materialization plan does not project the manifest source tree"
        )
    requested_actions = tuple(
        sorted(
            (item.action for item in request.sub_actions),
            key=lambda item: item.action_id,
        )
    )
    if requested_actions != manifest.actions:
        raise ArtifactAssemblyError(
            "composite build request actions differ from the exact manifest"
        )
    expected_outputs = tuple(item.export_id for item in manifest.export_declarations)
    if request.declared_output_ids != expected_outputs:
        raise ArtifactAssemblyError(
            "composite build request outputs differ from the exact manifest"
        )
    return request


__all__ = [
    "ArtifactAssemblyError",
    "assemble_source_tree",
    "create_artifact_build_graph",
    "create_composite_build_request",
    "create_package_plan",
    "create_resource_package_input",
    "plan_isolated_materialization",
    "validate_composite_build_request",
    "realize_manifest",
]

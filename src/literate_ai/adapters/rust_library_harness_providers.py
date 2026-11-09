"""Exact native-provider graph joins; these values grant no host execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from literate_ai.adapters.lifecycle.native_cargo_projection import (
    NativeCargoProviderSource,
    original_native_cargo_manifest,
    project_native_cargo_graph,
)
from literate_ai.contracts.executable_components import ArtifactExport
from literate_ai.contracts.native_cargo_provider_graph import NativeCargoProviderGraph


@dataclass(frozen=True, slots=True)
class RustQualifiedProviderContext:
    """Caller-joined current exports/material, not a decoded admission token."""

    root_export: ArtifactExport
    graph: NativeCargoProviderGraph
    sources: tuple[NativeCargoProviderSource, ...]

    @classmethod
    def bind(cls, *, root_export, graph, sources, files, manifest):
        if (
            type(root_export) is not ArtifactExport
            or root_export.role != "library"
            or type(graph) is not NativeCargoProviderGraph
            or graph.root_revision != root_export.component_revision
            or type(sources) is not tuple
            or tuple(source.node for source in sources) != graph.nodes
        ):
            raise ValueError("rust.provider.current_graph")
        original = original_native_cargo_manifest(files, manifest, graph.root_revision)
        rewritten, projected = project_native_cargo_graph(
            graph=graph,
            root_manifest=original,
            root_manifest_path=manifest,
            sources=sources,
        )
        actual = {row.path: row.content for row in files}
        reserved = ".literate/native-cargo/"
        if (
            len(actual) != len(files)
            or actual.get(manifest) != rewritten
            or {name: raw for name, raw in actual.items() if name.startswith(reserved)}
            != {row.path: row.content for row in projected}
        ):
            raise ValueError("rust.provider.current_material")
        return cls(root_export, graph, sources)

    def selections(self, declaration):
        authored = declaration.qualified_providers
        if not authored or {row.component_revision for row in authored} != {
            row.component_revision for row in self.graph.nodes
        }:
            raise ValueError("rust.provider.authored_graph")
        nodes = {row.component_revision: row for row in self.graph.nodes}
        sources = {row.node.component_revision: row for row in self.sources}
        result = []
        for row in authored:
            node = nodes[row.component_revision]
            source = sources[row.component_revision]
            if (node.import_surface_identity, node.package, node.version) != (
                row.import_surface_identity,
                row.package,
                row.version,
            ):
                raise ValueError("rust.provider.authored_interface")
            package = next(
                item
                for item in declaration.resolution.packages
                if item.scope == row.scope
            )
            if package.manifest != source.manifest:
                raise ValueError("rust.provider.authored_manifest")
            result.append(
                (
                    row,
                    source,
                    PurePosixPath(".literate/native-cargo/providers")
                    / node.projection_identity.digest
                    / source.manifest,
                )
            )
        return tuple(result)

    def observation(self, declaration):
        return {
            "root_export_identity": self.root_export.identity.uri,
            "graph_identity": self.graph.identity.uri,
            "providers": [
                {
                    "scope": row.scope,
                    "alias": row.alias,
                    "projection_identity": source.node.projection_identity.uri,
                    "manifest": str(manifest),
                    **source.node.to_dict(),
                }
                for row, source, manifest in self.selections(declaration)
            ],
        }


def reopen_rust_qualified_providers(
    reader, *, root, component_lock, current_commands, lifecycle
):
    """Reconstruct per-run bindings from independent locked command authority.

    The caller must first reopen the complete lifecycle/build/source/command
    chain. Historical values never become a live owner or execution grant.
    """
    from literate_ai.adapters.directory_artifacts import read_directory_export
    from literate_ai.adapters.lifecycle.native_cargo_projection import (
        MAX_PROJECTION_BYTES,
        MAX_PROJECTION_FILES,
    )
    from literate_ai.adapters.lifecycle.standard_cargo import StandardCargoTarget
    from literate_ai.application.standard_project_lifecycle import (
        StandardProjectLifecycleResult,
    )
    from literate_ai.contracts.identity import ContentIdentity

    if (
        type(lifecycle) is not StandardProjectLifecycleResult
        or current_commands is None
        or lifecycle.root_integration != root
        or root.component_lock_identity != component_lock.identity
        or lifecycle.project_build_plan is None
        or lifecycle.project_build_plan.identity
        != lifecycle.project_build_plan_identity
        or lifecycle.project_build_plan_identity != root.project_build_plan_identity
    ):
        raise ValueError("rust.provider.replay_context")
    from literate_ai.contracts.executable_components import ComponentCommandContract

    if any(
        type(value) is not ComponentCommandContract or value.component_revision != key
        for key, value in current_commands.items()
    ):
        raise ValueError("rust.provider.replay_contracts")
    commands = {key.uri: value for key, value in current_commands.items()}
    if set(commands) != {node.revision.identity.uri for node in component_lock.nodes}:
        raise ValueError("rust.provider.replay_contracts")
    exports = {
        export.identity.uri: export
        for node in lifecycle.node_results
        for export in node.exports
    }
    plans = {plan.identity: plan for plan in lifecycle.project_build_plan.components}
    selected = tuple(
        node
        for node in lifecycle.node_results
        if node.component_revision == component_lock.root_revision
    )
    if len(selected) != 1:
        raise ValueError("rust.provider.replay_root")
    plan = plans[selected[0].build_plan_identity]
    if plan.component_revision != component_lock.root_revision:
        raise ValueError("rust.provider.replay_root")
    root_export = exports[root.package_plan.root_artifact_identity.uri]
    if (
        root_export not in selected[0].exports
        or root_export.component_revision != plan.component_revision
        or root_export.role != "library"
        or root_export.target_identity != root.package_plan.target_identity
        or root_export.dependency_artifact_identities
        != plan.provider_artifact_identities
    ):
        raise ValueError("rust.provider.replay_root")
    from literate_ai.adapters.qualification_native_cargo import (
        reopen_native_cargo_inputs,
    )

    document = reader.read_json(
        commands[plan.component_revision.uri].locked_build_authority_identity
    )
    identity = ContentIdentity.parse_uri
    target = StandardCargoTarget(
        component_revision=identity(document["component_revision"]),
        build_system_resolver_identity=identity(
            document["build_system_resolver_identity"]
        ),
        build_system_toolchain_identity=identity(
            document["build_system_toolchain_identity"]
        ),
        language_compiler_identity=identity(document["language_compiler_identity"]),
        manifest=document["manifest"],
        binary=document["binary"],
        rustc_command=tuple(document["rustc_command"]),
        library=document.get("library", False),
    )
    if target.identity_document() != document:
        raise ValueError("rust.provider.replay_target")
    native = reopen_native_cargo_inputs(
        reader,
        plan=plan,
        component_lock=component_lock,
        current_commands=current_commands,
        provider_exports={export.identity: export for export in exports.values()},
        target=target,
    )
    if native is None:
        raise ValueError("rust.provider.replay_graph")

    def read_blob(blob):
        return reader.read_bytes(ContentIdentity.parse_uri("sha256:" + blob.digest))

    files = read_directory_export(
        read_blob(root_export.blob),
        root_export.blob,
        max_bytes=MAX_PROJECTION_BYTES,
        max_entries=MAX_PROJECTION_FILES,
    )
    return RustQualifiedProviderContext.bind(
        root_export=root_export,
        graph=native[0],
        sources=native[1],
        files=files,
        manifest=target.manifest,
    )

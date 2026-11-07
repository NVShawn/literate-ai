"""Join native Cargo projection data to current locked export/contract inputs."""

from __future__ import annotations

import hashlib
import json
import tomllib

from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.contracts import (
    ArtifactExport,
    ComponentCommandContract,
    LibraryConsumerBinding,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_cargo_provider_graph import (
    MAX_PROVIDER_NODES,
    NativeCargoProviderEdge,
    NativeCargoProviderGraph,
    NativeCargoProviderGraphError,
    NativeCargoProviderNode,
)

from .native_cargo_projection import (
    MAX_MANIFEST_BYTES,
    MAX_PROJECTION_BYTES,
    MAX_PROJECTION_FILES,
    NativeCargoProviderSource,
    original_native_cargo_manifest,
)


def _require(condition, code):
    if not condition:
        raise NativeCargoProviderGraphError("cargo.provider." + code)


def validate_native_cargo_provider_locks(
    *, graph, sources, consumer_manifest=None, consumer_lock=None
):
    """Preserve each qualified provider's exact native and registry resolution.

    A fresh solver may otherwise choose newer registry packages while rebuilding
    unchanged qualified source. Refuse that change before compilation rather
    than treating the earlier provider qualification as covering a new graph.
    This joins inert lock values only; it never grants Cargo execution.
    """
    from literate_ai.adapters.dependencies.acquisition import _cargo_lock_projection

    def label(package):
        if package.managed_ref is not None:
            return ("native", package.managed_ref)
        return (
            "registry",
            package.coordinate,
            package.version,
            package.integrity_algorithm,
            package.integrity_digest,
            package.key.split("|", 1)[1],
        )

    def normalized(projection):
        records, edges = projection
        packages = {row.key: label(row) for row in records}
        return set(packages.values()), {(packages[a], packages[b]) for a, b in edges}

    current = None
    _require(
        (consumer_manifest is None) == (consumer_lock is None), "provider_lock_context"
    )
    if consumer_lock is not None:
        current = normalized(
            _cargo_lock_projection(
                "Cargo.lock",
                consumer_lock.decode(),
                consumer_manifest.decode(),
                native_cargo_graph=graph,
            )
        )
    evidence = []
    for source in sources:
        members = {source.node.component_revision}
        pending = list(members)
        while pending:
            consumer = pending.pop()
            for edge in graph.edges:
                if (
                    edge.consumer_revision == consumer
                    and edge.provider_revision not in members
                ):
                    members.add(edge.provider_revision)
                    pending.append(edge.provider_revision)
        old_graph = NativeCargoProviderGraph(
            source.node.component_revision,
            tuple(
                node
                for node in graph.nodes
                if node.component_revision in members
                and node.component_revision != source.node.component_revision
            ),
            tuple(edge for edge in graph.edges if edge.consumer_revision in members),
        )
        lock_path = source.manifest.removesuffix("Cargo.toml") + "Cargo.lock"
        lock = next(
            (row.content for row in source.files if row.path == lock_path), None
        )
        _require(
            type(lock) is bytes and len(lock) <= MAX_PROJECTION_BYTES,
            "provider_lock_missing",
        )
        previous = normalized(
            _cargo_lock_projection(
                lock_path,
                lock.decode(),
                source.original_manifest.decode(),
                native_cargo_graph=old_graph,
            )
        )
        if current is not None:
            _require(previous[0] <= current[0], "provider_registry_resolution_changed")
            _require(
                {edge for edge in current[1] if edge[0] in previous[0]} == previous[1],
                "provider_registry_resolution_changed",
            )
        evidence.append(
            {
                "component_revision": source.node.component_revision.uri,
                "lock_identity": "sha256:" + hashlib.sha256(lock).hexdigest(),
            }
        )
    return sorted(evidence, key=lambda row: row["component_revision"])


def bind_native_cargo_inputs(
    *,
    consumer_revision,
    target_identity,
    providers,
    available_exports,
    contracts,
    manifest_paths,
    current_bindings,
    read_blob,
):
    """Resolve immutable exact blob bytes after caller's current BUILD admission.

    The caller supplies bindings from that finalized plan's current intent, not
    an aggregate of older intents sharing the same Component revision. No path
    supplied by an environment variable or package manifest selects a provider.
    This function neither invokes host tools nor admits a new export.
    """
    _require(
        type(providers) is tuple
        and all(type(item) is ArtifactExport for item in providers),
        "exports",
    )
    _require(
        type(current_bindings) is tuple
        and all(type(row) is LibraryConsumerBinding for row in current_bindings),
        "bindings",
    )

    def library(artifact):
        contract = contracts.get(artifact.component_revision.uri)
        _require(type(contract) is ComponentCommandContract, "contract")
        return contract.is_library

    direct = tuple(item for item in providers if library(item))
    if not direct:
        return None
    _require(
        {
            row.artifact_identity
            for row in current_bindings
            if row.consumer_component_revision == consumer_revision
        }
        == {row.identity for row in direct},
        "current_bindings",
    )
    direct_by_identity = {row.identity: row for row in direct}
    for binding in current_bindings:
        _require(
            binding.consumer_component_revision == consumer_revision, "current_bindings"
        )
        artifact = direct_by_identity.get(binding.artifact_identity)
        _require(artifact is not None, "current_bindings")
        surface = contracts[artifact.component_revision.uri].library_import_surface
        _require(
            surface is not None
            and binding.provider_component_revision == artifact.component_revision
            and binding.import_surface_identity == surface.identity
            and binding.target_identity == artifact.target_identity
            and binding.dependency_artifact_identities
            == artifact.dependency_artifact_identities,
            "current_bindings",
        )
        try:
            capability = surface.capability(binding.capability)
        except ValueError as error:
            raise NativeCargoProviderGraphError(
                "cargo.provider.current_bindings"
            ) from error
        _require(
            binding.interface_identity == capability.interface_identity,
            "current_bindings",
        )
    pending = list(direct)
    exports = {}
    parsed = {}
    aliases = {}
    old_graphs = {}
    total_bytes = 0
    total_files = 0
    while pending:
        artifact = pending.pop()
        if artifact.identity in exports:
            continue
        _require(len(exports) < MAX_PROVIDER_NODES, "nodes")
        _require(
            available_exports.get(artifact.identity.uri) == artifact, "current_export"
        )
        contract = contracts[artifact.component_revision.uri]
        shape = contract.artifact_export
        surface = contract.library_import_surface
        _require(
            surface is not None
            and surface.language == "rust"
            and artifact.target_identity == target_identity
            and (
                artifact.export_id,
                artifact.role,
                artifact.abi_identity,
                artifact.target_identity,
                artifact.producer_identity,
                artifact.toolchain_identity,
            )
            == (
                shape.export_id,
                shape.role,
                shape.abi_identity,
                shape.target_identity,
                shape.producer_identity,
                contract.language_compiler_identity,
            ),
            "export_contract",
        )
        _require(artifact.component_revision.uri in manifest_paths, "cargo_target")
        total_bytes += artifact.blob.size
        _require(total_bytes <= MAX_PROJECTION_BYTES, "source_closure_bound")
        content = read_blob(artifact.blob)
        files = read_directory_export(
            content,
            artifact.blob,
            max_bytes=MAX_PROJECTION_BYTES,
            max_entries=MAX_PROJECTION_FILES,
        )
        total_files += len(files)
        _require(total_files <= MAX_PROJECTION_FILES, "source_closure_bound")
        manifest_path = manifest_paths[artifact.component_revision.uri]
        original = original_native_cargo_manifest(
            files, manifest_path, artifact.component_revision
        )
        _require(len(original) <= MAX_MANIFEST_BYTES, "manifest_bound")
        try:
            package = tomllib.loads(original.decode("utf-8"))["package"]
            name, version = package["name"], package["version"]
        except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as error:
            raise NativeCargoProviderGraphError(
                "cargo.provider.package_manifest"
            ) from error
        node = NativeCargoProviderNode(
            artifact.component_revision,
            artifact.identity,
            surface.identity,
            artifact.source_tree_identity,
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(original).hexdigest()),
            name,
            version,
        )
        source = NativeCargoProviderSource(node, manifest_path, original, files)
        _require(node.component_revision not in parsed, "duplicate_provider")
        parsed[node.component_revision] = source
        aliases[node.component_revision] = surface.package
        exports[artifact.identity] = artifact
        record = next(
            (
                row.content
                for row in files
                if row.path == ".literate/native-cargo/projection.json"
            ),
            None,
        )
        # Source validation already performs closed, duplicate-key-aware decoding.
        old_graphs[node.component_revision] = (
            NativeCargoProviderGraph.from_dict(json.loads(record)["graph"])
            if record is not None
            else None
        )
        for dependency in artifact.dependency_artifact_identities:
            child = available_exports.get(dependency.uri)
            _require(
                type(child) is ArtifactExport and child.identity == dependency,
                "missing_provider",
            )
            if library(child):
                pending.append(child)
    edges = [
        NativeCargoProviderEdge(
            consumer_revision, row.component_revision, aliases[row.component_revision]
        )
        for row in direct
    ]
    for artifact in exports.values():
        for dependency in artifact.dependency_artifact_identities:
            child = available_exports[dependency.uri]
            if child.identity in exports:
                edges.append(
                    NativeCargoProviderEdge(
                        artifact.component_revision,
                        child.component_revision,
                        aliases[child.component_revision],
                    )
                )

    def order(row):
        return row.consumer_revision.uri, row.alias, row.provider_revision.uri

    graph = NativeCargoProviderGraph(
        consumer_revision,
        tuple(
            sorted(
                (source.node for source in parsed.values()),
                key=lambda row: row.component_revision.uri,
            )
        ),
        tuple(sorted(edges, key=order)),
    )
    nodes = {row.component_revision: row for row in graph.nodes}
    for revision, old in old_graphs.items():
        reachable = set()
        pending = [
            edge.provider_revision
            for edge in graph.edges
            if edge.consumer_revision == revision
        ]
        while pending:
            child = pending.pop()
            if child not in reachable:
                reachable.add(child)
                pending.extend(
                    edge.provider_revision
                    for edge in graph.edges
                    if edge.consumer_revision == child
                )
        expected = NativeCargoProviderGraph(
            revision,
            tuple(
                sorted(
                    (nodes[item] for item in reachable),
                    key=lambda row: row.component_revision.uri,
                )
            ),
            tuple(
                edge
                for edge in graph.edges
                if edge.consumer_revision in reachable | {revision}
            ),
        )
        _require(
            (old is None and not reachable) or old == expected,
            "qualified_projection_graph",
        )
    return graph, tuple(parsed[row.component_revision] for row in graph.nodes)

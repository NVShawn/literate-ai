"""Join a derived Cargo lock to an already admitted native Component graph.

These checks are inert. A lock or decoded graph never authorizes acquisition,
host execution or a new provider. The Standard owner must supply the current
graph and separately validate Cargo metadata against its disposable paths.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from literate_ai.contracts.native_cargo_provider_graph import NativeCargoProviderGraph
from literate_ai.contracts.sbom import (
    CycloneDxManagedGraph,
    ManagedComponentKind,
    component_bom_ref,
)

from .types import DependencyObservationError, _normalized_package_name


def _require(condition, detail):
    if not condition:
        raise DependencyObservationError(
            "dependencies.rust-native-graph-invalid", detail
        )


def native_cargo_lock_refs(*, graph, manifest, packages):
    """Return exact source-less package -> managed-reference bindings.

    Registry packages retain the existing registry/hash admission. No additional
    source-less package is allowed, and a registry package cannot impersonate a
    native name. Cargo's lock does not retain aliases; the metadata checker owns
    that additional check. Here native dependency endpoints must match exactly.
    """
    _require(type(graph) is NativeCargoProviderGraph, "native graph is not typed")
    root = manifest.get("package")
    _require(isinstance(root, Mapping), "native root package is missing")
    root_name, root_version = root.get("name"), root.get("version")
    _require(
        isinstance(root_name, str) and isinstance(root_version, str),
        "native root package lacks literal name/version",
    )
    expected = {
        (node.package, node.version): node.component_revision for node in graph.nodes
    }
    names = {_normalized_package_name(name) for name, _ in expected}
    _require(len(names) == len(expected), "native package names are ambiguous")
    _require(
        _normalized_package_name(root_name) not in names, "native root name collides"
    )
    expected[root_name, root_version] = graph.root_revision
    names.add(_normalized_package_name(root_name))
    _require(isinstance(packages, list), "native Cargo packages are missing")
    observed = {}
    by_name = {}
    records = {}
    for raw in packages:
        _require(isinstance(raw, Mapping), "invalid Cargo package")
        name, version = raw.get("name"), raw.get("version")
        _require(
            isinstance(name, str) and isinstance(version, str), "invalid Cargo identity"
        )
        _require(
            "source" not in raw or isinstance(raw["source"], str),
            "invalid Cargo source",
        )
        key = name, version, raw.get("source")
        _require(key not in records, "duplicate Cargo package")
        records[key] = raw
        by_name.setdefault(name, []).append(key)
        _require("replace" not in raw, "Cargo replacement is unsupported")
        if "source" in raw:
            _require(
                _normalized_package_name(name) not in names,
                "registry package collides with a native package",
            )
            continue
        _require(
            (name, version) in expected and "checksum" not in raw,
            "unbound or hashed native Cargo package",
        )
        observed[name, version] = expected[name, version]
    _require(set(observed) == set(expected), "native Cargo inventory is incomplete")
    edges = set()
    for key, raw in records.items():
        dependencies = raw.get("dependencies", [])
        _require(isinstance(dependencies, list), "invalid Cargo dependencies")
        seen = set()
        for dependency in dependencies:
            _require(isinstance(dependency, str), "invalid Cargo dependency")
            match = re.fullmatch(
                r"([^\s]+)(?: ([^\s]+)(?: \(([^\r\n]+)\))?)?", dependency
            )
            _require(match is not None, "invalid Cargo dependency selector")
            name, version, source = match.groups()
            candidates = [
                row
                for row in by_name.get(name, ())
                if (version is None or row[1] == version)
                and (source is None or row[2] == source)
            ]
            _require(len(candidates) == 1, "ambiguous Cargo dependency")
            target = candidates[0]
            _require(target not in seen, "duplicate Cargo dependency")
            seen.add(target)
            if target[2] is None:
                _require(key[2] is None, "registry dependency reaches a native package")
                edges.add((expected[key[:2]], expected[target[:2]]))
    _require(
        edges
        == {(edge.consumer_revision, edge.provider_revision) for edge in graph.edges},
        "Cargo native edges differ from the admitted graph",
    )
    return {pair: component_bom_ref(revision) for pair, revision in observed.items()}


def require_native_cargo_managed_graph(graph, managed_graph):
    """Refuse lock mappings that would fabricate or redirect managed BOM nodes."""
    _require(type(graph) is NativeCargoProviderGraph, "native graph is not typed")
    _require(type(managed_graph) is CycloneDxManagedGraph, "managed graph is not typed")
    refs = {item.bom_ref: item for item in managed_graph.components}
    _require(
        managed_graph.root_ref == component_bom_ref(graph.root_revision),
        "native graph root differs from the managed root",
    )
    for node in graph.nodes:
        actual = refs.get(component_bom_ref(node.component_revision))
        _require(
            actual is not None
            and actual.kind is ManagedComponentKind.COMPONENT
            and actual.identity == node.component_revision,
            "native provider is missing from the managed BOM graph",
        )
    edges = {(row.source_ref, row.target_ref) for row in managed_graph.edges}
    _require(
        all(
            (
                component_bom_ref(row.consumer_revision),
                component_bom_ref(row.provider_revision),
            )
            in edges
            for row in graph.edges
        ),
        "native edge is missing from the managed BOM graph",
    )

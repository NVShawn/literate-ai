"""Compare Cargo's observed local graph with an independently reviewed expectation.

This internal check neither invokes Cargo nor establishes artifact qualification,
filesystem custody, locked-command provenance, or toolchain authenticity.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai.contracts.cargo_workspace import (
    CargoDependencyExpectation as CargoDependencyExpectation,
)
from literate_ai.contracts.cargo_workspace import (
    CargoPackageExpectation as CargoPackageExpectation,
)
from literate_ai.contracts.cargo_workspace import (
    CargoTargetExpectation as CargoTargetExpectation,
)
from literate_ai.contracts.cargo_workspace import (
    CargoWorkspaceExpectation as CargoWorkspaceExpectation,
)
from literate_ai.contracts.cargo_workspace import (
    CargoWorkspaceGraphError as CargoWorkspaceGraphError,
)
from literate_ai.contracts.cargo_workspace import (
    _require,
    _text,
)


def _array(value: Any) -> list[Any]:
    _require(isinstance(value, list), "malformed_metadata")
    return value


def _mapping(value: Any) -> dict[str, Any]:
    _require(isinstance(value, dict), "malformed_metadata")
    return value


def _set(value: Any) -> set[str]:
    items = _array(value)
    _require(all(_text(item) for item in items), "malformed_metadata")
    result = set(items)
    _require(len(result) == len(items), "duplicate_metadata_entry")
    return result


def _path(root: Path, relative: str) -> str:
    return str(root.joinpath(*PurePosixPath(relative).parts))


def _targets(
    package: dict[str, Any], expected: CargoPackageExpectation, root: Path
) -> None:
    actual = []
    for item in _array(package.get("targets")):
        target = _mapping(item)
        _require(
            _text(target.get("name"))
            and _text(target.get("src_path"))
            and _text(target.get("edition"))
            and type(target.get("test")) is bool
            and type(target.get("doctest")) is bool,
            "malformed_target",
        )
        actual.append(
            (
                target["name"],
                frozenset(_set(target.get("kind"))),
                frozenset(_set(target.get("crate_types"))),
                target["src_path"],
                target["edition"],
                target["test"],
                target["doctest"],
                frozenset(_set(target.get("required-features", []))),
            )
        )
    wanted = {
        (
            t.name,
            frozenset(t.kinds),
            frozenset(t.crate_types),
            _path(root, t.source),
            t.edition,
            t.test,
            t.doctest,
            frozenset(t.required_features),
        )
        for t in expected.targets
    }
    _require(len(actual) == len(set(actual)) and set(actual) == wanted, "target_drift")


def verify_cargo_workspace_graph(
    metadata: dict[str, Any],
    *,
    workspace_root: Path,
    expected: CargoWorkspaceExpectation,
) -> None:
    """Require all local packages and edges, including inactive declarations.

    Callers must separately bind the reviewed manifests/lock, Cargo invocation,
    toolchain, target and features, and maintain package custody before/after build.
    This function does no I/O. Unknown additive Cargo metadata fields are tolerated.
    """
    _require(isinstance(expected, CargoWorkspaceExpectation), "invalid_expectation")
    _require(
        isinstance(workspace_root, Path)
        and workspace_root.is_absolute()
        and ".." not in workspace_root.parts,
        "invalid_workspace_root",
    )
    data = _mapping(metadata)
    _require(
        type(data.get("version")) is int and data["version"] == 1, "metadata_version"
    )
    _require(data.get("workspace_root") == str(workspace_root), "workspace_root_drift")
    _require(
        data.get("target_directory")
        == _path(workspace_root, expected.output_directory),
        "output_directory_drift",
    )
    _require(
        data.get("build_directory", data["target_directory"])
        == data["target_directory"],
        "unreviewed_build_directory",
    )
    packages: dict[str, dict[str, Any]] = {}
    locals_by_manifest = {}
    for item in _array(data.get("packages")):
        package = _mapping(item)
        identity = package.get("id")
        _require(
            _text(identity) and identity not in packages, "duplicate_or_invalid_package"
        )
        _require("source" in package, "malformed_package")
        packages[identity] = package
        if package["source"] is None:
            manifest = package.get("manifest_path")
            _require(
                _text(manifest) and manifest not in locals_by_manifest,
                "duplicate_or_invalid_manifest",
            )
            locals_by_manifest[manifest] = identity
        else:
            _require(_text(package["source"]), "malformed_package")
    expected_by_root = {p.root: p for p in expected.packages}
    manifests = {
        _path(workspace_root, p.root + "/Cargo.toml"): p.root for p in expected.packages
    }
    _require(set(locals_by_manifest) == set(manifests), "local_package_set_drift")
    roots_by_id = {
        identity: manifests[path] for path, identity in locals_by_manifest.items()
    }
    ids_by_root = {root: identity for identity, root in roots_by_id.items()}
    for identity, root in roots_by_id.items():
        package, wanted = packages[identity], expected_by_root[root]
        _require(
            (package.get("name"), package.get("version"))
            == (wanted.name, wanted.version),
            "package_identity_drift",
        )
        _targets(package, wanted, workspace_root)
    for field, wanted in (
        ("workspace_members", expected.members),
        ("workspace_default_members", expected.default_members),
    ):
        _require(
            _set(data.get(field)) == {ids_by_root[r] for r in wanted},
            "workspace_membership_drift",
        )

    declarations = []
    for identity, consumer in roots_by_id.items():
        for item in _array(packages[identity].get("dependencies")):
            dep = _mapping(item)
            if "path" not in dep:
                # Ordinary external dependencies retain Cargo.lock policy.
                continue
            path = dep["path"]
            _require(
                {
                    "name",
                    "source",
                    "rename",
                    "kind",
                    "target",
                    "req",
                    "optional",
                    "uses_default_features",
                    "features",
                }
                <= dep.keys(),
                "malformed_dependency",
            )
            _require(_text(path), "malformed_dependency")
            provider_id = locals_by_manifest.get(str(Path(path) / "Cargo.toml"))
            _require(
                provider_id is not None and dep.get("source") is None,
                "unbound_path_dependency",
            )
            provider = roots_by_id[provider_id]
            _require(
                dep.get("name") == expected_by_root[provider].name,
                "dependency_name_drift",
            )
            _require(
                (dep["rename"] is None or _text(dep["rename"]))
                and dep["kind"] in (None, "dev", "build")
                and (dep["target"] is None or _text(dep["target"]))
                and _text(dep["req"]),
                "malformed_dependency",
            )
            _require(
                type(dep.get("uses_default_features")) is bool
                and type(dep.get("optional")) is bool,
                "malformed_dependency",
            )
            declarations.append(
                (
                    consumer,
                    provider,
                    dep.get("rename") or dep.get("name"),
                    dep.get("kind"),
                    dep.get("target"),
                    frozenset(_set(dep.get("features"))),
                    dep["uses_default_features"],
                    dep["optional"],
                    dep.get("req"),
                )
            )
    wanted_declarations = [
        (
            d.consumer,
            d.provider,
            d.alias,
            d.kind,
            d.target,
            frozenset(d.features),
            d.uses_default_features,
            d.optional,
            d.version_requirement,
        )
        for d in expected.dependencies
    ]
    _require(
        len(declarations) == len(wanted_declarations)
        and len(set(declarations)) == len(declarations)
        and set(declarations) == set(wanted_declarations),
        "dependency_declaration_drift",
    )
    resolution = _mapping(data.get("resolve"))
    _require("root" in resolution, "malformed_resolution")
    _require(
        resolution.get("root")
        == (
            None
            if expected.root_package is None
            else ids_by_root[expected.root_package]
        ),
        "root_package_drift",
    )
    nodes = {}
    edges = []
    for item in _array(resolution.get("nodes")):
        node = _mapping(item)
        identity = node.get("id")
        _require(
            _text(identity) and identity in packages and identity not in nodes,
            "duplicate_or_invalid_node",
        )
        nodes[identity] = node
        features = _set(node.get("features"))
        consumer = roots_by_id.get(identity)
        if consumer is not None:
            _require(
                features == set(expected_by_root[consumer].features),
                "resolved_feature_drift",
            )
        dependency_ids = set()
        for item in _array(node.get("deps")):
            dep = _mapping(item)
            provider_id = dep.get("pkg")
            _require(
                _text(provider_id)
                and provider_id in packages
                and _text(dep.get("name")),
                "unknown_resolved_dependency",
            )
            dependency_ids.add(provider_id)
            kinds = _array(dep.get("dep_kinds"))
            _require(bool(kinds), "missing_dependency_kind")
            for item in kinds:
                kind = _mapping(item)
                _require({"kind", "target"} <= kind.keys(), "malformed_dependency_kind")
                _require(
                    kind.get("kind") in (None, "build", "dev")
                    and (kind.get("target") is None or _text(kind["target"])),
                    "malformed_dependency_kind",
                )
                provider = roots_by_id.get(provider_id)
                if provider is not None:
                    _require(consumer is not None, "external_to_local_dependency")
                    edges.append(
                        (
                            consumer,
                            provider,
                            dep["name"],
                            kind.get("kind"),
                            kind.get("target"),
                        )
                    )
        _require(
            _set(node.get("dependencies")) == dependency_ids, "inconsistent_resolution"
        )
    _require(set(roots_by_id) <= set(nodes), "missing_local_resolution")
    _require(
        all(dep in nodes for node in nodes.values() for dep in node["dependencies"]),
        "incomplete_resolution",
    )
    wanted_edges = [
        (d.consumer, d.provider, d.resolved_name, d.kind, d.target)
        for d in expected.dependencies
        if d.resolved
    ]
    _require(
        len(edges) == len(wanted_edges)
        and len(set(edges)) == len(edges)
        and set(edges) == set(wanted_edges),
        "resolved_edge_drift",
    )

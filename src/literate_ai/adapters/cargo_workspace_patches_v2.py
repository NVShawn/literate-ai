"""Join reviewed registry patches to manifest/lock bytes and native Cargo edges.

No command runs here. The receiving owner must guard all manifest/source bytes,
current registry configuration, measured Cargo invocation and metadata custody.
This validation grants no provider, execution or admission authority.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from literate_ai.adapters.cargo_workspace_graph import (
    _array,
    _mapping,
    _set,
    _verify_cargo_workspace_graph,
)
from literate_ai.contracts.cargo_workspace import _require, _text
from literate_ai.contracts.retained_cargo_patches import CargoWorkspacePatchConsumer
from literate_ai.contracts.retained_cargo_workspace_v2 import (
    RetainedCargoWorkspacePlanV2,
)

_CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"


def _document(content, reference, limit):
    _require(
        reference is not None
        and isinstance(content, bytes)
        and len(content) <= limit
        and len(content) == reference.size
        and hashlib.sha256(content).hexdigest() == reference.digest,
        "patch_document_bytes_refused",
    )
    try:
        return tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("cargo.workspace.patch_document_invalid") from None


def verify_cargo_workspace_patches_v2(
    metadata: dict,
    *,
    workspace_root: Path,
    plan: RetainedCargoWorkspacePlanV2,
    workspace_manifest: bytes,
    lockfile: bytes,
    registry_sources: Mapping[str, str] | None = None,
    maximum_document_bytes: int = 16 * 1024 * 1024,
) -> None:
    """Allow only exact resolved edges explained by independently reviewed patches.

    registry_sources supplies named-registry resolutions from the receiving
    owner's current captured Cargo configuration. URI keys are literal source
    locations; crates-io has its canonical native identity. A caller must not
    derive these bindings from the candidate metadata or patch declaration.
    Unpatched external-to-local and undeclared local packages still refuse.
    """
    _require(type(plan) is RetainedCargoWorkspacePlanV2, "v2_plan_required")
    _require(
        type(maximum_document_bytes) is int
        and 0 < maximum_document_bytes <= 256 * 1024 * 1024,
        "patch_document_limit_invalid",
    )
    _require(
        isinstance(workspace_root, Path)
        and workspace_root.is_absolute()
        and ".." not in workspace_root.parts,
        "invalid_workspace_root",
    )
    sources = {} if registry_sources is None else registry_sources
    _require(
        isinstance(sources, Mapping)
        and len(sources) <= 128
        and all(_text(key) and _text(value) for key, value in sources.items()),
        "registry_bindings_invalid",
    )
    manifests = {item.path: item.after for item in plan.common.manifests}
    root = _document(
        workspace_manifest, manifests["Cargo.toml"], maximum_document_bytes
    )
    locked = _document(lockfile, manifests["Cargo.lock"], maximum_document_bytes)
    patches = plan.graph.registry_patches
    tables = root.get("patch", {})
    _require(isinstance(tables, dict), "patch_table_invalid")
    rows = {}
    for registry, table in tables.items():
        _require(isinstance(table, dict), "patch_table_invalid")
        for name, row in table.items():
            rows[registry, name] = row
    _require(set(rows) == {patch.key for patch in patches}, "patch_table_drift")
    for patch in patches:
        row = rows[patch.key]
        source = (
            _CRATES_IO
            if patch.registry_key == "crates-io"
            else sources.get(patch.registry_key)
        )
        if source is None and patch.registry_key.startswith(
            ("https://", "http://", "file://", "sparse+https://")
        ):
            source = (
                patch.registry_key
                if patch.registry_key.startswith("sparse+")
                else "registry+" + patch.registry_key
            )
        _require(source == patch.registry_source, "patch_registry_source_drift")
        _require(
            isinstance(row, dict)
            and {"path"} <= row.keys()
            and row.keys() <= {"path", "package", "version"}
            and row["path"] == patch.provider_root
            and row.get("package", patch.patch_name) == patch.package_name
            and ("version" not in row or _text(row["version"])),
            "patch_provider_declaration_drift",
        )

    lock_packages = {}
    for row in _array(locked.get("package")):
        package = _mapping(row)
        source = package.get("source")
        _require(
            _text(package.get("name"))
            and _text(package.get("version"))
            and (source is None or _text(source)),
            "patch_lock_invalid",
        )
        key = package["name"], package["version"], source
        _require(key not in lock_packages, "patch_lock_ambiguous")
        if source is None:
            _require("checksum" not in package, "patch_local_checksum_invalid")
        elif source.startswith(("registry+", "sparse+")):
            _require(
                isinstance(package.get("checksum"), str)
                and re.fullmatch(r"[0-9a-f]{64}", package["checksum"]) is not None,
                "patch_registry_checksum_invalid",
            )
        lock_packages[key] = package

    packages = []
    for value in _array(_mapping(metadata).get("packages")):
        package = _mapping(value)
        _require(
            _text(package.get("id"))
            and _text(package.get("name"))
            and _text(package.get("version"))
            and "source" in package
            and (package["source"] is None or _text(package["source"])),
            "patch_package_invalid",
        )
        _require(
            (package["name"], package["version"], package["source"]) in lock_packages,
            "patch_lock_package_missing",
        )
        packages.append(package)

    def local_package(coordinate):
        wanted = str(workspace_root / coordinate / "Cargo.toml")
        matches = [
            p
            for p in packages
            if p["source"] is None and p.get("manifest_path") == wanted
        ]
        _require(len(matches) == 1, "patch_local_package_ambiguous")
        return matches[0]

    explained = set()
    for patch in patches:
        provider = local_package(patch.provider_root)
        _require(
            (provider["name"], provider["version"])
            == (patch.package_name, patch.version),
            "patch_provider_identity_drift",
        )
        for edge in patch.edges:
            if isinstance(edge.consumer, CargoWorkspacePatchConsumer):
                consumer = local_package(edge.consumer.root)
            else:
                matches = [
                    p
                    for p in packages
                    if (p["source"], p["name"], p["version"])
                    == (edge.consumer.source, edge.consumer.name, edge.consumer.version)
                ]
                _require(len(matches) == 1, "patch_external_consumer_ambiguous")
                consumer = matches[0]
            declarations = []
            for value in _array(consumer.get("dependencies")):
                dep = _mapping(value)
                if (
                    (dep.get("rename") or dep.get("name")),
                    dep.get("kind"),
                    dep.get("target"),
                ) == (edge.alias, edge.kind, edge.target):
                    declarations.append(dep)
            _require(len(declarations) == 1, "patch_dependency_declaration_ambiguous")
            dep = declarations[0]
            _require(
                dep.get("path") is None
                and dep.get("name") == patch.package_name
                and dep.get("source") == patch.registry_source
                and dep.get("req") == edge.version_requirement
                and type(dep.get("optional")) is bool
                and dep["optional"] == edge.optional
                and type(dep.get("uses_default_features")) is bool
                and dep["uses_default_features"] == edge.uses_default_features
                and _set(dep.get("features")) == set(edge.features),
                "patch_dependency_declaration_drift",
            )
            key = (
                consumer["id"],
                provider["id"],
                edge.resolved_name,
                edge.kind,
                edge.target,
            )
            _require(key not in explained, "patch_edge_ambiguous")
            explained.add(key)
    _verify_cargo_workspace_graph(
        metadata,
        workspace_root=workspace_root,
        expected=plan.graph.common,
        reviewed_patch_edges=frozenset(explained),
    )

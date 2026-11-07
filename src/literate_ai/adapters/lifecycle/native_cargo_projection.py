"""Pure portable Cargo projection from already-qualified library package bytes.

These values are not qualification or host-execution grants. The Standard
adapter must join every node to current locked contracts and exact export blobs
before calling this module; only its existing authorized builder runs Cargo.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from literate_ai.adapters.directory_artifacts import DirectoryExportFile
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderGraph,
    NativeCargoProviderGraphError,
    NativeCargoProviderNode,
)
from literate_ai.contracts.paths import canonical_relative_posix_paths

_RESERVED = ".literate/native-cargo"
MAX_PROJECTION_FILES = 10000
MAX_PROJECTION_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024


def _require(condition, code):
    if not condition:
        raise NativeCargoProviderGraphError("cargo.provider." + code)


def _digest(content):
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest())


def _path(raw):
    _require(type(raw) is str and len(raw) <= 1024, "manifest_path")
    try:
        canonical_relative_posix_paths([raw], label="Cargo provider manifest")
    except (TypeError, ValueError) as error:
        raise NativeCargoProviderGraphError("cargo.provider.manifest_path") from error
    _require(PurePosixPath(raw).name == "Cargo.toml", "manifest_path")
    return raw


def _manifest(raw):
    _require(
        type(raw) is bytes and 0 < len(raw) <= MAX_MANIFEST_BYTES, "manifest_bound"
    )
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise NativeCargoProviderGraphError("cargo.provider.manifest") from error
    _require(type(value.get("package")) is dict, "package_manifest")
    _require(
        not any(
            key in value
            for key in (
                "workspace",
                "patch",
                "replace",
                "target",
                "dev-dependencies",
                "build-dependencies",
            )
        ),
        "unsupported_manifest",
    )
    _require(value["package"].get("build", False) is False, "build_script")
    dependencies = value.get("dependencies", {})
    _require(type(dependencies) is dict, "dependencies")
    for dependency in dependencies.values():
        _require(type(dependency) in (str, dict), "dependency")
        if type(dependency) is dict:
            _require(
                not any(key in dependency for key in ("path", "git", "workspace")),
                "undeclared_provider_path",
            )
    for key in ("lib", "bin", "test", "example", "bench"):
        rows = value.get(key, [])
        rows = [rows] if type(rows) is dict else rows
        _require(
            type(rows) is list and all(type(row) is dict for row in rows), "target"
        )
        for row in rows:
            if "path" in row:
                try:
                    canonical_relative_posix_paths([row["path"]], label="Cargo target")
                except (TypeError, ValueError) as error:
                    raise NativeCargoProviderGraphError(
                        "cargo.provider.target_path"
                    ) from error
    return value


def _toml(value):
    """Encode parsed Cargo values as TOML inline tables, with a roundtrip check."""
    if type(value) is dict:
        _require(all(type(key) is str for key in value), "manifest_key")
        return (
            "{"
            + ",".join(
                _toml(key) + "=" + _toml(item) for key, item in sorted(value.items())
            )
            + "}"
        )
    if type(value) is list:
        return "[" + ",".join(_toml(item) for item in value) + "]"
    _require(type(value) in (str, bool, int, float), "manifest_value")
    _require(type(value) is not float or math.isfinite(value), "manifest_value")
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace(
        "\x7f", "\\u007f"
    )


def _encode(value):
    raw = (
        "\n".join(_toml(key) + "=" + _toml(item) for key, item in sorted(value.items()))
        + "\n"
    ).encode()
    _require(
        len(raw) <= MAX_MANIFEST_BYTES and tomllib.loads(raw.decode()) == value,
        "manifest_roundtrip",
    )
    return raw


@dataclass(frozen=True, slots=True)
class NativeCargoProviderSource:
    """Qualified-node projection inputs, not an input-admission capability."""

    node: NativeCargoProviderNode
    manifest: str
    original_manifest: bytes
    files: tuple[DirectoryExportFile, ...]

    def __post_init__(self):
        _require(type(self.node) is NativeCargoProviderNode, "source_node")
        _path(self.manifest)
        _require(
            _digest(self.original_manifest) == self.node.original_manifest_identity,
            "original_manifest_identity",
        )
        document = _manifest(self.original_manifest)
        _require(
            document["package"].get("name") == self.node.package
            and document["package"].get("version") == self.node.version,
            "package_identity",
        )
        _require(
            type(self.files) is tuple
            and len(self.files) <= MAX_PROJECTION_FILES
            and all(type(row) is DirectoryExportFile for row in self.files),
            "source_files",
        )
        _require(
            sum(len(row.content) for row in self.files) <= MAX_PROJECTION_BYTES,
            "source_bytes",
        )
        canonical_relative_posix_paths(
            [row.path for row in self.files], label="Cargo provider package"
        )
        _require(self.manifest in {row.path for row in self.files}, "missing_manifest")
        package = str(PurePosixPath(self.manifest).parent)
        _require(
            posixpath.join(package, "build.rs") not in {row.path for row in self.files},
            "build_script",
        )


def project_native_cargo_graph(*, graph, root_manifest, root_manifest_path, sources):
    """Return a replacement root manifest and new files for a disposable tree.

    One shared canonical provider directory is used for every incoming edge.
    Provider packages are copied from immutable decoded bytes; their embedded
    older projection is discarded and rebuilt from this exact admitted closure.
    Existing admitted source/package directories are never edited here.
    """
    _require(type(graph) is NativeCargoProviderGraph, "graph")
    _path(root_manifest_path)
    document = _manifest(root_manifest)
    _require(
        type(sources) is tuple
        and all(type(row) is NativeCargoProviderSource for row in sources),
        "sources",
    )
    by_revision = {row.node.component_revision: row for row in sources}
    _require(
        len(by_revision) == len(sources)
        and set(by_revision) == {node.component_revision for node in graph.nodes}
        and all(
            by_revision[node.component_revision].node == node for node in graph.nodes
        ),
        "source_graph",
    )
    root_package = document["package"].get("name")
    _require(
        type(root_package) is str
        and root_package.replace("-", "_")
        not in {node.package.replace("-", "_") for node in graph.nodes},
        "root_package_collision",
    )
    if not graph.nodes:
        return root_manifest, ()
    directories = {
        node.component_revision: _RESERVED
        + "/providers/"
        + node.artifact_identity.digest
        for node in graph.nodes
    }
    manifests = {
        revision: directories[revision] + "/" + source.manifest
        for revision, source in by_revision.items()
    }
    manifests[graph.root_revision] = root_manifest_path

    def inject(original, revision):
        value = _manifest(original)
        dependencies = value.setdefault("dependencies", {})
        _require(type(dependencies) is dict, "dependencies")
        for edge in graph.edges:
            if edge.consumer_revision != revision:
                continue
            _require(
                edge.alias not in dependencies
                and edge.alias.replace("-", "_")
                not in {key.replace("-", "_") for key in dependencies},
                "conflicting_dependency",
            )
            provider = by_revision[edge.provider_revision].node
            relative = posixpath.relpath(
                posixpath.dirname(manifests[edge.provider_revision]),
                posixpath.dirname(manifests[revision]) or ".",
            )
            dependencies[edge.alias] = {
                "package": provider.package,
                "version": "=" + provider.version,
                "path": relative,
            }
        return _encode(value)

    output = []
    for node in graph.nodes:
        source = by_revision[node.component_revision]
        for row in source.files:
            if row.path == _RESERVED or row.path.startswith(_RESERVED + "/"):
                continue
            if PurePosixPath(row.path).name == "Cargo.lock":
                continue
            content = (
                inject(source.original_manifest, node.component_revision)
                if row.path == source.manifest
                else row.content
            )
            output.append(
                DirectoryExportFile(
                    directories[node.component_revision] + "/" + row.path,
                    content,
                    row.mode,
                )
            )
    record = {
        "schema": "literate-ai/native-cargo-projection@1",
        "root_manifest": root_manifest_path,
        "root_original_manifest_identity": _digest(root_manifest).uri,
        "graph": graph.to_dict(),
    }
    output.extend(
        (
            DirectoryExportFile(
                _RESERVED + "/original-manifest.toml", root_manifest, 0o644
            ),
            DirectoryExportFile(
                _RESERVED + "/projection.json",
                (
                    json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode(),
                0o644,
            ),
        )
    )
    _require(
        len(output) <= MAX_PROJECTION_FILES
        and sum(len(row.content) for row in output) <= MAX_PROJECTION_BYTES,
        "projection_bound",
    )
    canonical_relative_posix_paths(
        [row.path for row in output], label="Cargo provider projection"
    )
    return inject(root_manifest, graph.root_revision), tuple(output)

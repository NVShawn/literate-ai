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

from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
)
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
    package = value["package"]
    _require(
        "workspace" not in package
        and not any(
            type(item) is dict and "workspace" in item
            for key, item in package.items()
            if key != "metadata"
        ),
        "ambient_workspace",
    )
    _require(
        not (type(value.get("lints")) is dict and "workspace" in value["lints"]),
        "ambient_workspace",
    )
    for name in ("readme", "license-file", "license_file"):
        if name not in package or (name == "readme" and type(package[name]) is bool):
            continue
        try:
            canonical_relative_posix_paths([package[name]], label="Cargo package file")
        except (TypeError, ValueError) as error:
            raise NativeCargoProviderGraphError(
                "cargo.provider.package_path"
            ) from error

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


def original_native_cargo_manifest(files, manifest, revision):
    """Read original bytes from a qualified export, never from an ambient path."""
    _path(manifest)
    for row in files:
        path = row.path.casefold()
        if path == _RESERVED or path.startswith(_RESERVED + "/"):
            _require(
                row.path == _RESERVED or row.path.startswith(_RESERVED + "/"),
                "reserved_namespace_alias",
            )
    by_path = {row.path: row.content for row in files}
    _require(len(by_path) == len(files) and manifest in by_path, "source_files")
    record_path = _RESERVED + "/projection.json"
    original_path = _RESERVED + "/original-manifest.toml"
    if record_path not in by_path:
        _require(
            not any(
                path == _RESERVED or path.startswith(_RESERVED + "/")
                for path in by_path
            ),
            "projection_record_missing",
        )
        return by_path[manifest]
    _require(
        original_path in by_path and len(by_path[record_path]) <= MAX_MANIFEST_BYTES,
        "projection_record",
    )

    def unique(rows):
        result = dict(rows)
        _require(len(result) == len(rows), "projection_record")
        return result

    try:
        record = json.loads(by_path[record_path], object_pairs_hook=unique)
    except (UnicodeError, ValueError) as error:
        raise NativeCargoProviderGraphError(
            "cargo.provider.projection_record"
        ) from error
    _require(
        type(record) is dict
        and set(record)
        == {"schema", "root_manifest", "root_original_manifest_identity", "graph"},
        "projection_record",
    )
    _require(
        record["schema"] == "literate-ai/native-cargo-projection@1"
        and record["root_manifest"] == manifest,
        "projection_record",
    )
    graph = NativeCargoProviderGraph.from_dict(record["graph"])
    _require(graph.root_revision == revision, "projection_revision")
    original = by_path[original_path]
    _require(
        _digest(original).uri == record["root_original_manifest_identity"],
        "original_manifest_identity",
    )
    return original


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
            type(self.original_manifest) is bytes
            and 0 < len(self.original_manifest) <= MAX_MANIFEST_BYTES,
            "manifest_bound",
        )
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
            and all(
                type(row) is DirectoryExportFile
                and type(row.content) is bytes
                and type(row.mode) is int
                and 0 <= row.mode <= 0o777
                for row in self.files
            ),
            "source_files",
        )
        _require(
            sum(len(row.content) for row in self.files) <= MAX_PROJECTION_BYTES,
            "source_bytes",
        )
        canonical_relative_posix_paths(
            [row.path for row in self.files], label="Cargo provider package"
        )
        try:
            encode_directory_export(
                self.files,
                max_bytes=MAX_PROJECTION_BYTES,
                max_entries=MAX_PROJECTION_FILES,
            )
        except ValueError as error:
            raise NativeCargoProviderGraphError(
                "cargo.provider.source_files"
            ) from error
        _require(self.manifest in {row.path for row in self.files}, "missing_manifest")
        _require(
            original_native_cargo_manifest(
                self.files, self.manifest, self.node.component_revision
            )
            == self.original_manifest,
            "original_manifest_identity",
        )
        _require(
            not any(
                ".cargo" in [part.casefold() for part in PurePosixPath(row.path).parts]
                for row in self.files
            ),
            "package_cargo_configuration",
        )
        package = str(PurePosixPath(self.manifest).parent)
        _require(
            posixpath.join(package, "build.rs").casefold()
            not in {row.path.casefold() for row in self.files},
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
    _require(
        sum(len(row.files) for row in sources) <= MAX_PROJECTION_FILES
        and sum(len(file.content) for row in sources for file in row.files)
        <= MAX_PROJECTION_BYTES,
        "source_closure_bound",
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
        + node.projection_identity.digest
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
            if row.path == str(PurePosixPath(source.manifest).with_name("Cargo.lock")):
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
    rewritten = inject(root_manifest, graph.root_revision)
    try:
        encode_directory_export(
            (DirectoryExportFile(root_manifest_path, rewritten, 0o644), *output),
            max_bytes=MAX_PROJECTION_BYTES,
            max_entries=MAX_PROJECTION_FILES,
        )
    except ValueError as error:
        raise NativeCargoProviderGraphError(
            "cargo.provider.projection_bound"
        ) from error
    return rewritten, tuple(output)


def validate_native_cargo_metadata(
    *, graph, root_manifest, root_manifest_path, sources, projection_root, metadata
):
    """Join actual Cargo path packages and aliases to the admitted shared graph.

    ``projection_root`` is the builder-owned disposable directory, not a value
    accepted from metadata. Registry nodes remain subject to normal lock/SBOM
    validation; this function never admits an additional path package.
    """
    from pathlib import Path

    _require(type(graph) is NativeCargoProviderGraph, "graph")
    _require(
        isinstance(projection_root, Path) and projection_root.is_absolute(),
        "projection_root",
    )
    _path(root_manifest_path)
    root = _manifest(root_manifest)["package"]
    by_revision = {row.node.component_revision: row for row in sources}
    _require(
        len(by_revision) == len(sources)
        and set(by_revision) == {node.component_revision for node in graph.nodes},
        "source_graph",
    )
    expected = {
        graph.root_revision: (
            root.get("name"),
            root.get("version"),
            projection_root / root_manifest_path,
        ),
    }
    for node in graph.nodes:
        source = by_revision[node.component_revision]
        _require(source.node == node, "source_graph")
        expected[node.component_revision] = (
            node.package,
            node.version,
            projection_root
            / _RESERVED
            / "providers"
            / node.projection_identity.digest
            / source.manifest,
        )
    _require(
        type(metadata) is dict
        and type(metadata.get("packages")) is list
        and len(metadata["packages"]) <= MAX_PROJECTION_FILES,
        "metadata_packages",
    )
    by_id = {}
    native_ids = {}
    for row in metadata["packages"]:
        _require(
            type(row) is dict and type(row.get("id")) is str and row["id"] not in by_id,
            "metadata_package",
        )
        by_id[row["id"]] = row
        if row.get("source") is not None:
            _require(
                row.get("name") not in {node.package for node in graph.nodes},
                "metadata_native_package",
            )
            continue
        matches = [
            revision
            for revision, (name, version, path) in expected.items()
            if (row.get("name"), row.get("version"), row.get("manifest_path"))
            == (name, version, str(path))
        ]
        _require(
            len(matches) == 1 and matches[0] not in native_ids,
            "metadata_native_package",
        )
        native_ids[matches[0]] = row["id"]
    _require(set(native_ids) == set(expected), "metadata_native_package")
    resolve = metadata.get("resolve")
    _require(
        type(resolve) is dict
        and type(resolve.get("nodes")) is list
        and len(resolve["nodes"]) <= MAX_PROJECTION_FILES,
        "metadata_resolve",
    )
    _require(resolve.get("root") == native_ids[graph.root_revision], "metadata_root")
    by_native_id = {value: key for key, value in native_ids.items()}
    observed = set()
    seen = set()
    for row in resolve["nodes"]:
        _require(
            type(row) is dict
            and row.get("id") in by_id
            and row["id"] not in seen
            and type(row.get("deps")) is list
            and len(row["deps"]) <= MAX_PROJECTION_FILES,
            "metadata_resolve",
        )
        seen.add(row["id"])
        for dependency in row["deps"]:
            _require(
                type(dependency) is dict
                and dependency.get("pkg") in by_id
                and type(dependency.get("name")) is str,
                "metadata_dependency",
            )
            if dependency["pkg"] not in by_native_id:
                continue
            _require(
                row["id"] in by_native_id
                and dependency.get("dep_kinds") == [{"kind": None, "target": None}],
                "metadata_native_edge",
            )
            edge = (
                by_native_id[row["id"]],
                by_native_id[dependency["pkg"]],
                dependency["name"],
            )
            _require(edge not in observed, "metadata_native_edge")
            observed.add(edge)
    _require(set(native_ids.values()).issubset(seen), "metadata_resolve")
    wanted = {
        (edge.consumer_revision, edge.provider_revision, edge.alias.replace("-", "_"))
        for edge in graph.edges
    }
    _require(observed == wanted, "metadata_native_edge")
    return {
        "schema": "literate-ai/native-cargo-metadata-binding@1",
        "graph_identity": graph.identity.uri,
        "packages": [
            {"component_revision": revision.uri, "cargo_package_id": package}
            for revision, package in sorted(
                native_ids.items(), key=lambda item: item[0].uri
            )
        ],
    }

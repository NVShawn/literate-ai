"""Provider-neutral bindings for derived source-intelligence artifacts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .identity import ContentIdentity, canonical_identity, canonical_json_bytes
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths

SOURCE_INTELLIGENCE_ARTIFACT_SCHEMA = (
    "urn:literate-ai:schema:v1:source-intelligence-artifact"
)
LEGACY_GENERATED_SOURCE_INDEX_SCHEMA = (
    "urn:literate-ai:schema:v1:generated-source-index-binding"
)
LEGACY_PORTABLE_INDEX_SCHEMA = (
    "urn:literate-ai:schema:v1:portable-codegraph-index-binding"
)
PORTABLE_SOURCE_INTELLIGENCE_SCHEMA = (
    "urn:literate-ai:schema:v1:portable-source-intelligence"
)
SOURCE_INTELLIGENCE_ARTIFACT_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v1:source-intelligence-artifact-binding"
)

_PORTABLE_TOKEN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_MEDIA_TYPE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_PROPERTY = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?=[^\r\n]{1,512}$")


def generated_source_tree_identity(files: Mapping[str, bytes]) -> str:
    """Canonical source-only tree identity shared by every intelligence provider."""

    normalized = _normalized_source_files(files)
    manifest = _generated_source_manifest(normalized)
    return f"sha256:{hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()}"


def generated_source_snapshot_identity(files: Mapping[str, bytes]) -> str:
    """Canonical exact-file snapshot identity for one generated source tree."""

    normalized = _normalized_source_files(files)
    tree_identity = generated_source_tree_identity(normalized)
    return canonical_identity(
        {
            "schema": "urn:literate-ai:schema:v1:generated-source-snapshot",
            "source_tree_identity": tree_identity,
            "files": [
                {
                    "path": path,
                    "identity": f"sha256:{hashlib.sha256(content).hexdigest()}",
                }
                for path, content in sorted(normalized.items())
            ],
        }
    ).uri


def _normalized_source_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not files:
        raise ValueError("generated source tree must not be empty")
    paths = canonical_relative_posix_paths(files.keys(), label="generated source path")
    normalized: dict[str, bytes] = {}
    for path, content in zip(paths, files.values(), strict=True):
        if path.parts[0] in {".codegraph", ".source-intelligence"} or (
            path.as_posix()
            in {
                ".literate-tree.json",
                ".literate-source-index.json",
                ".literate-source-intelligence.json",
            }
        ):
            raise ValueError(
                "generated source path collides with intelligence metadata"
            )
        if not isinstance(content, bytes):
            raise TypeError("generated source content must be bytes")
        normalized[path.as_posix()] = content
    return normalized


def _generated_source_manifest(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    return [
        {
            "path": path,
            "size": len(content),
            "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
        }
        for path, content in sorted(files.items())
    ]


def _canonical_tokens(values: Sequence[str], *, label: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(
        not isinstance(value, str) or _PORTABLE_TOKEN.fullmatch(value) is None
        for value in result
    ):
        raise ValueError(f"source intelligence {label} must use portable tokens")
    if result != tuple(sorted(set(result))):
        raise ValueError(f"source intelligence {label} must be unique and sorted")
    return result


def _canonical_properties(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(values)
    if any(
        not isinstance(value, str) or _PROPERTY.fullmatch(value) is None
        for value in result
    ):
        raise ValueError(
            "source intelligence provider properties must use canonical key=value text"
        )
    if result != tuple(sorted(set(result))):
        raise ValueError(
            "source intelligence provider properties must be unique and sorted"
        )
    keys = tuple(value.partition("=")[0] for value in result)
    if len(keys) != len(set(keys)):
        raise ValueError("source intelligence provider property keys must be unique")
    return result


@dataclass(frozen=True, slots=True)
class SourceIntelligenceArtifact:
    """One provider observation of exact source plus its rebuildable artifact.

    This contract intentionally knows nothing about a source-graph indexer or
    SQLite. Provider and artifact versions identify an observation, never the
    source tree it describes.
    """

    provider_id: str
    provider_version: str
    runtime_version: str
    executable_identity: str
    source_tree_identity: str
    source_snapshot_identity: str
    capabilities: tuple[str, ...]
    provider_properties: tuple[str, ...]
    logical_intelligence_identity: str
    artifact_binding: str
    intelligence_identity: str
    artifact_path: str
    artifact_media_type: str
    artifact_identity: str
    document_count: int
    symbol_count: int
    relationship_count: int
    unresolved_relationship_count: int
    warning_count: int

    def __post_init__(self) -> None:
        for name in ("provider_id", "provider_version", "runtime_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"source intelligence {name} must not be empty")
        if _PORTABLE_TOKEN.fullmatch(self.provider_id) is None:
            raise ValueError("source intelligence provider_id must be portable")
        for name in (
            "source_tree_identity",
            "source_snapshot_identity",
            "logical_intelligence_identity",
            "artifact_binding",
            "intelligence_identity",
            "artifact_identity",
            "executable_identity",
        ):
            ContentIdentity.parse_uri(getattr(self, name))
        _canonical_tokens(self.capabilities, label="capabilities")
        _canonical_properties(self.provider_properties)
        canonical_relative_posix_path(
            self.artifact_path, label="source intelligence artifact path"
        )
        if _MEDIA_TYPE.fullmatch(self.artifact_media_type) is None:
            raise ValueError("source intelligence artifact media type is invalid")
        for name in (
            "document_count",
            "symbol_count",
            "relationship_count",
            "unresolved_relationship_count",
            "warning_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"source intelligence {name} must be non-negative")
        if self.document_count == 0 and any(
            (
                self.symbol_count,
                self.relationship_count,
                self.unresolved_relationship_count,
            )
        ):
            raise ValueError("empty source intelligence has inconsistent graph counts")
        if canonical_identity(self.logical_identity_material()).uri != (
            self.logical_intelligence_identity
        ):
            raise ValueError("source intelligence logical identity does not match")
        expected_binding = canonical_identity(
            {
                "schema": SOURCE_INTELLIGENCE_ARTIFACT_BINDING_SCHEMA,
                "logical_intelligence_identity": self.logical_intelligence_identity,
                "source_tree_identity": self.source_tree_identity,
                "artifact_identity": self.artifact_identity,
            }
        ).uri
        if self.artifact_binding != expected_binding:
            raise ValueError("source intelligence artifact binding does not match")
        if (
            canonical_identity(self.identity_material()).uri
            != self.intelligence_identity
        ):
            raise ValueError("source intelligence identity does not match")

    def logical_identity_material(self) -> dict[str, object]:
        """Location-independent observation semantics, excluding artifact bytes."""

        return {
            "schema": PORTABLE_SOURCE_INTELLIGENCE_SCHEMA,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "runtime_version": self.runtime_version,
            "executable_identity": self.executable_identity,
            "source_tree_identity": self.source_tree_identity,
            "source_snapshot_identity": self.source_snapshot_identity,
            "capabilities": list(self.capabilities),
            "provider_properties": list(self.provider_properties),
            "document_count": self.document_count,
            "symbol_count": self.symbol_count,
            "relationship_count": self.relationship_count,
            "unresolved_relationship_count": self.unresolved_relationship_count,
            "warning_count": self.warning_count,
        }

    def identity_material(self) -> dict[str, object]:
        return {
            "schema": SOURCE_INTELLIGENCE_ARTIFACT_SCHEMA,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "runtime_version": self.runtime_version,
            "executable_identity": self.executable_identity,
            "source_tree_identity": self.source_tree_identity,
            "source_snapshot_identity": self.source_snapshot_identity,
            "capabilities": list(self.capabilities),
            "provider_properties": list(self.provider_properties),
            "logical_intelligence_identity": self.logical_intelligence_identity,
            "artifact_binding": self.artifact_binding,
            "artifact_path": self.artifact_path,
            "artifact_media_type": self.artifact_media_type,
            "artifact_identity": self.artifact_identity,
            "document_count": self.document_count,
            "symbol_count": self.symbol_count,
            "relationship_count": self.relationship_count,
            "unresolved_relationship_count": self.unresolved_relationship_count,
            "warning_count": self.warning_count,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.identity_material(),
            "intelligence_identity": self.intelligence_identity,
        }

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        provider_version: str,
        runtime_version: str,
        executable_identity: str,
        source_tree_identity: str,
        source_snapshot_identity: str,
        artifact_path: str,
        artifact_media_type: str,
        artifact_identity: str,
        document_count: int,
        symbol_count: int,
        relationship_count: int,
        unresolved_relationship_count: int,
        warning_count: int,
        capabilities: tuple[str, ...] = (),
        provider_properties: tuple[str, ...] = (),
    ) -> SourceIntelligenceArtifact:
        logical_material = {
            "schema": PORTABLE_SOURCE_INTELLIGENCE_SCHEMA,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "runtime_version": runtime_version,
            "executable_identity": executable_identity,
            "source_tree_identity": source_tree_identity,
            "source_snapshot_identity": source_snapshot_identity,
            "capabilities": list(capabilities),
            "provider_properties": list(provider_properties),
            "document_count": document_count,
            "symbol_count": symbol_count,
            "relationship_count": relationship_count,
            "unresolved_relationship_count": unresolved_relationship_count,
            "warning_count": warning_count,
        }
        logical_identity = canonical_identity(logical_material).uri
        artifact_binding = canonical_identity(
            {
                "schema": SOURCE_INTELLIGENCE_ARTIFACT_BINDING_SCHEMA,
                "logical_intelligence_identity": logical_identity,
                "source_tree_identity": source_tree_identity,
                "artifact_identity": artifact_identity,
            }
        ).uri
        identity_material = {
            "schema": SOURCE_INTELLIGENCE_ARTIFACT_SCHEMA,
            "provider_id": provider_id,
            "provider_version": provider_version,
            "runtime_version": runtime_version,
            "executable_identity": executable_identity,
            "source_tree_identity": source_tree_identity,
            "source_snapshot_identity": source_snapshot_identity,
            "capabilities": list(capabilities),
            "provider_properties": list(provider_properties),
            "logical_intelligence_identity": logical_identity,
            "artifact_binding": artifact_binding,
            "artifact_path": artifact_path,
            "artifact_media_type": artifact_media_type,
            "artifact_identity": artifact_identity,
            "document_count": document_count,
            "symbol_count": symbol_count,
            "relationship_count": relationship_count,
            "unresolved_relationship_count": unresolved_relationship_count,
            "warning_count": warning_count,
        }
        return cls(
            provider_id=provider_id,
            provider_version=provider_version,
            runtime_version=runtime_version,
            executable_identity=executable_identity,
            source_tree_identity=source_tree_identity,
            source_snapshot_identity=source_snapshot_identity,
            capabilities=capabilities,
            provider_properties=provider_properties,
            logical_intelligence_identity=logical_identity,
            artifact_binding=artifact_binding,
            intelligence_identity=canonical_identity(identity_material).uri,
            artifact_path=artifact_path,
            artifact_media_type=artifact_media_type,
            artifact_identity=artifact_identity,
            document_count=document_count,
            symbol_count=symbol_count,
            relationship_count=relationship_count,
            unresolved_relationship_count=unresolved_relationship_count,
            warning_count=warning_count,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceIntelligenceArtifact:
        if (
            isinstance(value, Mapping)
            and value.get("schema") == LEGACY_GENERATED_SOURCE_INDEX_SCHEMA
        ):
            return cls._from_legacy_generated_index(value)
        expected = {
            "schema",
            "provider_id",
            "provider_version",
            "runtime_version",
            "executable_identity",
            "source_tree_identity",
            "source_snapshot_identity",
            "capabilities",
            "provider_properties",
            "logical_intelligence_identity",
            "artifact_binding",
            "intelligence_identity",
            "artifact_path",
            "artifact_media_type",
            "artifact_identity",
            "document_count",
            "symbol_count",
            "relationship_count",
            "unresolved_relationship_count",
            "warning_count",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value.get("schema") != SOURCE_INTELLIGENCE_ARTIFACT_SCHEMA
        ):
            raise ValueError("source intelligence document shape is invalid")
        capabilities = value["capabilities"]
        properties = value["provider_properties"]
        if not isinstance(capabilities, list) or not isinstance(properties, list):
            raise ValueError("source intelligence arrays are invalid")
        return cls(
            provider_id=value["provider_id"],
            provider_version=value["provider_version"],
            runtime_version=value["runtime_version"],
            executable_identity=value["executable_identity"],
            source_tree_identity=value["source_tree_identity"],
            source_snapshot_identity=value["source_snapshot_identity"],
            capabilities=tuple(capabilities),
            provider_properties=tuple(properties),
            logical_intelligence_identity=value["logical_intelligence_identity"],
            artifact_binding=value["artifact_binding"],
            intelligence_identity=value["intelligence_identity"],
            artifact_path=value["artifact_path"],
            artifact_media_type=value["artifact_media_type"],
            artifact_identity=value["artifact_identity"],
            document_count=value["document_count"],
            symbol_count=value["symbol_count"],
            relationship_count=value["relationship_count"],
            unresolved_relationship_count=value["unresolved_relationship_count"],
            warning_count=value["warning_count"],
        )

    @classmethod
    def _from_legacy_generated_index(
        cls, value: Mapping[str, Any]
    ) -> SourceIntelligenceArtifact:
        expected = {
            "schema",
            "provider_id",
            "provider_version",
            "runtime_version",
            "executable_identity",
            "source_tree_identity",
            "source_snapshot_identity",
            "logical_index_identity",
            "index_binding",
            "index_identity",
            "index_path",
            "built_with_version",
            "extraction_version",
            "database_identity",
            "document_count",
            "node_count",
            "edge_count",
        }
        if set(value) != expected or value.get("index_path") != (
            ".codegraph/codegraph.db"
        ):
            raise ValueError("legacy generated source index shape is invalid")
        logical_material = {
            "schema": LEGACY_PORTABLE_INDEX_SCHEMA,
            "provider_id": value["provider_id"],
            "provider_version": value["provider_version"],
            "runtime_version": value["runtime_version"],
            "executable_identity": value["executable_identity"],
            "built_with_version": value["built_with_version"],
            "extraction_version": value["extraction_version"],
            "source_tree_identity": value["source_tree_identity"],
            "source_snapshot_identity": value["source_snapshot_identity"],
            "document_count": value["document_count"],
            "node_count": value["node_count"],
            "edge_count": value["edge_count"],
        }
        logical_identity = canonical_identity(logical_material).uri
        expected_binding = canonical_identity(
            {
                "schema": "urn:literate-ai:schema:v1:codegraph-database-binding",
                "logical_index_identity": logical_identity,
                "source_tree_identity": value["source_tree_identity"],
                "database_identity": value["database_identity"],
            }
        ).uri
        old_identity_material = {
            key: value[key] for key in expected if key != "index_identity"
        }
        if (
            value["logical_index_identity"] != logical_identity
            or value["index_binding"] != expected_binding
            or value["index_identity"] != canonical_identity(old_identity_material).uri
        ):
            raise ValueError("legacy generated source index identity is invalid")
        built_with = value["built_with_version"]
        extraction = value["extraction_version"]
        properties = tuple(
            sorted(
                item
                for item in (
                    (
                        None
                        if built_with is None
                        else f"built-with-version={built_with}"
                    ),
                    (
                        None
                        if extraction is None
                        else f"extraction-version={extraction}"
                    ),
                )
                if item is not None
            )
        )
        return cls.create(
            provider_id=value["provider_id"],
            provider_version=value["provider_version"],
            runtime_version=value["runtime_version"],
            executable_identity=value["executable_identity"],
            source_tree_identity=value["source_tree_identity"],
            source_snapshot_identity=value["source_snapshot_identity"],
            capabilities=("call-graph", "declarations", "references"),
            provider_properties=properties,
            artifact_path=value["index_path"],
            artifact_media_type="application/vnd.sqlite3",
            artifact_identity=value["database_identity"],
            document_count=value["document_count"],
            symbol_count=value["node_count"],
            relationship_count=value["edge_count"],
            unresolved_relationship_count=0,
            warning_count=0,
        )


__all__ = [
    "PORTABLE_SOURCE_INTELLIGENCE_SCHEMA",
    "SOURCE_INTELLIGENCE_ARTIFACT_BINDING_SCHEMA",
    "SOURCE_INTELLIGENCE_ARTIFACT_SCHEMA",
    "SourceIntelligenceArtifact",
    "generated_source_snapshot_identity",
    "generated_source_tree_identity",
]

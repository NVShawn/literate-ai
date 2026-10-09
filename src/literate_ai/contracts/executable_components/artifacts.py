"""Adapter-neutral generated-source, artifact, action, and link contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    list_value,
    parse_tuple,
    string_value,
)
from ..blobs import BlobRef
from ..identity import ContentIdentity, canonical_identity, contract_identity
from ..paths import canonical_relative_posix_path
from ._common import canonical_identities, identity, portable_name, tuple_value
from .assets import AuthoredBinaryAsset

GENERATED_TEXT_TREE_SCHEMA = "urn:literate-ai:schema:v2:generated-text-tree"
SOURCE_TREE_MANIFEST_SCHEMA = "urn:literate-ai:schema:v2:assembled-source-tree-manifest"
ARTIFACT_EXPORT_SCHEMA = "urn:literate-ai:schema:v2:artifact-export"
ARTIFACT_EXPORT_DECLARATION_SCHEMA = (
    "urn:literate-ai:schema:v2:artifact-export-declaration"
)
BUILD_ACTION_REQUEST_SCHEMA = "urn:literate-ai:schema:v2:component-build-action-request"
COMPONENT_BUILD_MANIFEST_SCHEMA = "urn:literate-ai:schema:v2:component-build-manifest"
EXACT_LINK_PLAN_SCHEMA = "urn:literate-ai:schema:v2:exact-link-plan"
ARTIFACT_MATERIALIZATION_PLAN_SCHEMA = (
    "urn:literate-ai:schema:v2:artifact-materialization-plan"
)
COMPOSITE_BUILD_REQUEST_SCHEMA = "urn:literate-ai:schema:v2:composite-build-request"
ARTIFACT_BUILD_GRAPH_SCHEMA = "urn:literate-ai:schema:v2:artifact-build-graph"


def _path(value: str, label: str) -> str:
    try:
        return canonical_relative_posix_path(value, label=label)
    except (TypeError, ValueError) as exc:
        fail(label, str(exc))


def _blob(value: object, label: str) -> BlobRef:
    if not isinstance(value, BlobRef):
        fail(label, "must be a BlobRef")
    return value


def _canonical_by(values: object, label: str, key: object) -> tuple[object, ...]:
    items = tuple_value(values, label)
    if len(items) > 16384:
        fail(label, "must contain at most 16384 values")
    keys = tuple(key(item) for item in items)  # type: ignore[operator]
    if len(keys) != len(set(keys)):
        fail(label, "must not contain duplicates")
    if keys != tuple(sorted(keys)):
        fail(label, "must use canonical order")
    return items


@dataclass(frozen=True, slots=True)
class GeneratedTextFile:
    """One UTF-8 model-produced file, represented only by immutable bytes."""

    path: str
    blob: BlobRef

    def __post_init__(self) -> None:
        _path(self.path, "GeneratedTextFile.path")
        _blob(self.blob, "GeneratedTextFile.blob")
        if not (
            self.blob.media_type.startswith("text/")
            or self.blob.media_type
            in {
                "application/json",
                "application/javascript",
                "application/xml",
                "application/yaml",
            }
        ):
            fail("GeneratedTextFile.blob.media_type", "must describe textual content")

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "blob": self.blob.to_dict()}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "GeneratedTextFile"
    ) -> GeneratedTextFile:
        data = fields(value, path=path, required=frozenset({"path", "blob"}))
        return cls(
            path=string_value(data["path"], f"{path}.path"),
            blob=BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
        )


@dataclass(frozen=True, slots=True)
class GeneratedTextTree:
    """Complete model output; authored binary bytes are deliberately absent."""

    component_revision: ContentIdentity
    target_identity: ContentIdentity
    authorization_identity: ContentIdentity
    files: tuple[GeneratedTextFile, ...]

    SCHEMA: ClassVar[str] = GENERATED_TEXT_TREE_SCHEMA

    def __post_init__(self) -> None:
        identity(self.component_revision, "GeneratedTextTree.component_revision")
        identity(self.target_identity, "GeneratedTextTree.target_identity")
        identity(
            self.authorization_identity, "GeneratedTextTree.authorization_identity"
        )
        files = _canonical_by(
            self.files, "GeneratedTextTree.files", lambda item: item.path
        )
        if any(not isinstance(item, GeneratedTextFile) for item in files):
            fail("GeneratedTextTree.files", "must contain GeneratedTextFile values")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "authorization_identity": self.authorization_identity.to_dict(),
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "GeneratedTextTree"
    ) -> GeneratedTextTree:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "target_identity",
                    "authorization_identity",
                    "files",
                }
            ),
        )
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            authorization_identity=ContentIdentity.from_dict(
                data["authorization_identity"], path=f"{path}.authorization_identity"
            ),
            files=parse_tuple(
                data["files"], f"{path}.files", GeneratedTextFile.from_dict
            ),
        )


class SourceTreeEntryOrigin(StrEnum):
    GENERATED_TEXT = "generated-text"
    AUTHORED_BINARY = "authored-binary"


@dataclass(frozen=True, slots=True)
class SourceTreeEntry:
    path: str
    role: str
    origin: SourceTreeEntryOrigin
    blob: BlobRef

    def __post_init__(self) -> None:
        _path(self.path, "SourceTreeEntry.path")
        portable_name(self.role, "SourceTreeEntry.role")
        if not isinstance(self.origin, SourceTreeEntryOrigin):
            fail("SourceTreeEntry.origin", "must be a SourceTreeEntryOrigin")
        _blob(self.blob, "SourceTreeEntry.blob")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "role": self.role,
            "origin": self.origin.value,
            "blob": self.blob.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "SourceTreeEntry") -> SourceTreeEntry:
        data = fields(
            value,
            path=path,
            required=frozenset({"path", "role", "origin", "blob"}),
        )
        return cls(
            path=string_value(data["path"], f"{path}.path"),
            role=string_value(data["role"], f"{path}.role"),
            origin=enum_value(SourceTreeEntryOrigin, data["origin"], f"{path}.origin"),
            blob=BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
        )


@dataclass(frozen=True, slots=True)
class SourceTreeManifest:
    """Arbitrary-byte source tree assembled after model generation."""

    component_revision: ContentIdentity
    target_identity: ContentIdentity
    authorization_identity: ContentIdentity
    generated_text_tree_identity: ContentIdentity
    authored_asset_identities: tuple[ContentIdentity, ...]
    entries: tuple[SourceTreeEntry, ...]

    SCHEMA: ClassVar[str] = SOURCE_TREE_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        identity(self.component_revision, "SourceTreeManifest.component_revision")
        identity(self.target_identity, "SourceTreeManifest.target_identity")
        identity(
            self.authorization_identity, "SourceTreeManifest.authorization_identity"
        )
        identity(
            self.generated_text_tree_identity,
            "SourceTreeManifest.generated_text_tree_identity",
        )
        canonical_identities(
            self.authored_asset_identities,
            "SourceTreeManifest.authored_asset_identities",
        )
        entries = _canonical_by(
            self.entries, "SourceTreeManifest.entries", lambda item: item.path
        )
        if any(not isinstance(item, SourceTreeEntry) for item in entries):
            fail("SourceTreeManifest.entries", "must contain SourceTreeEntry values")
        observed_assets = sum(
            item.origin is SourceTreeEntryOrigin.AUTHORED_BINARY for item in entries
        )
        if observed_assets != len(self.authored_asset_identities):
            fail(
                "SourceTreeManifest.authored_asset_identities",
                "must identify every and only authored binary entry",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "authorization_identity": self.authorization_identity.to_dict(),
            "generated_text_tree_identity": self.generated_text_tree_identity.to_dict(),
            "authored_asset_identities": [
                item.to_dict() for item in self.authored_asset_identities
            ],
            "entries": [item.to_dict() for item in self.entries],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceTreeManifest"
    ) -> SourceTreeManifest:
        names = frozenset(
            {
                "component_revision",
                "target_identity",
                "authorization_identity",
                "generated_text_tree_identity",
                "authored_asset_identities",
                "entries",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            authorization_identity=ContentIdentity.from_dict(
                data["authorization_identity"], path=f"{path}.authorization_identity"
            ),
            generated_text_tree_identity=ContentIdentity.from_dict(
                data["generated_text_tree_identity"],
                path=f"{path}.generated_text_tree_identity",
            ),
            authored_asset_identities=parse_tuple(
                data["authored_asset_identities"],
                f"{path}.authored_asset_identities",
                ContentIdentity.from_dict,
            ),
            entries=parse_tuple(
                data["entries"], f"{path}.entries", SourceTreeEntry.from_dict
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactExportDeclaration:
    """Authorized output shape known before a compiler realizes its bytes."""

    export_id: str
    component_revision: ContentIdentity
    role: str
    abi_identity: ContentIdentity
    target_identity: ContentIdentity
    media_type: str
    producer_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    toolchain_identity: ContentIdentity
    authorization_identity: ContentIdentity
    dependency_artifact_identities: tuple[ContentIdentity, ...]
    SCHEMA: ClassVar[str] = ARTIFACT_EXPORT_DECLARATION_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.export_id, "ArtifactExport.export_id")
        portable_name(self.role, "ArtifactExport.role")
        for name in (
            "component_revision",
            "abi_identity",
            "target_identity",
            "producer_identity",
            "source_tree_identity",
            "toolchain_identity",
            "authorization_identity",
        ):
            identity(getattr(self, name), f"ArtifactExport.{name}")
        string_value(self.media_type, "ArtifactExport.media_type", max_length=255)
        canonical_identities(
            self.dependency_artifact_identities,
            "ArtifactExport.dependency_artifact_identities",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "export_id": self.export_id,
            "component_revision": self.component_revision.to_dict(),
            "role": self.role,
            "abi_identity": self.abi_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "media_type": self.media_type,
            "producer_identity": self.producer_identity.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "toolchain_identity": self.toolchain_identity.to_dict(),
            "authorization_identity": self.authorization_identity.to_dict(),
            "dependency_artifact_identities": [
                item.to_dict() for item in self.dependency_artifact_identities
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ArtifactExportDeclaration"
    ) -> ArtifactExportDeclaration:
        names = frozenset(
            {
                "export_id",
                "component_revision",
                "role",
                "abi_identity",
                "target_identity",
                "media_type",
                "producer_identity",
                "source_tree_identity",
                "toolchain_identity",
                "authorization_identity",
                "dependency_artifact_identities",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        identities = {
            name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
            for name in (
                "component_revision",
                "abi_identity",
                "target_identity",
                "producer_identity",
                "source_tree_identity",
                "toolchain_identity",
                "authorization_identity",
            )
        }
        return cls(
            export_id=string_value(data["export_id"], f"{path}.export_id"),
            role=string_value(data["role"], f"{path}.role"),
            media_type=string_value(data["media_type"], f"{path}.media_type"),
            dependency_artifact_identities=parse_tuple(
                data["dependency_artifact_identities"],
                f"{path}.dependency_artifact_identities",
                ContentIdentity.from_dict,
            ),
            **identities,
        )


@dataclass(frozen=True, slots=True)
class ArtifactExport(ArtifactExportDeclaration):
    """One realized exact Component output consumable by another build action."""

    blob: BlobRef

    SCHEMA: ClassVar[str] = ARTIFACT_EXPORT_SCHEMA

    def __post_init__(self) -> None:
        super(ArtifactExport, self).__post_init__()
        _blob(self.blob, "ArtifactExport.blob")
        if self.blob.media_type != self.media_type:
            fail("ArtifactExport.media_type", "must match the exported blob media type")

    @property
    def declaration(self) -> ArtifactExportDeclaration:
        return ArtifactExportDeclaration(
            self.export_id,
            self.component_revision,
            self.role,
            self.abi_identity,
            self.target_identity,
            self.media_type,
            self.producer_identity,
            self.source_tree_identity,
            self.toolchain_identity,
            self.authorization_identity,
            self.dependency_artifact_identities,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **super(ArtifactExport, self).to_dict(),
            "schema": self.SCHEMA,
            "blob": self.blob.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "ArtifactExport") -> ArtifactExport:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "export_id",
                    "component_revision",
                    "role",
                    "abi_identity",
                    "target_identity",
                    "media_type",
                    "producer_identity",
                    "source_tree_identity",
                    "toolchain_identity",
                    "authorization_identity",
                    "dependency_artifact_identities",
                    "blob",
                }
            ),
        )
        declaration = ArtifactExportDeclaration.from_dict(
            {key: value for key, value in data.items() if key != "blob"}
            | {"schema": ArtifactExportDeclaration.SCHEMA},
            path=path,
        )
        return cls(
            declaration.export_id,
            declaration.component_revision,
            declaration.role,
            declaration.abi_identity,
            declaration.target_identity,
            declaration.media_type,
            declaration.producer_identity,
            declaration.source_tree_identity,
            declaration.toolchain_identity,
            declaration.authorization_identity,
            declaration.dependency_artifact_identities,
            BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
        )


@dataclass(frozen=True, slots=True)
class BuildActionRequest:
    """Adapter-neutral, fully authorized invocation of one build action."""

    action_id: str
    component_revision: ContentIdentity
    role: str
    abi_identity: ContentIdentity
    target_identity: ContentIdentity
    media_type: str
    producer_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    toolchain_identity: ContentIdentity
    authorization_identity: ContentIdentity
    dependency_artifacts: tuple[ArtifactExport, ...]
    declared_output_ids: tuple[str, ...]
    package_dependency_artifacts: tuple[ArtifactExport, ...] = ()
    output_declarations: tuple[ArtifactExportDeclaration, ...] = ()

    SCHEMA: ClassVar[str] = BUILD_ACTION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.action_id, "BuildActionRequest.action_id")
        portable_name(self.role, "BuildActionRequest.role")
        for name in (
            "component_revision",
            "abi_identity",
            "target_identity",
            "producer_identity",
            "source_tree_identity",
            "toolchain_identity",
            "authorization_identity",
        ):
            identity(getattr(self, name), f"BuildActionRequest.{name}")
        string_value(self.media_type, "BuildActionRequest.media_type", max_length=255)
        dependencies = _canonical_by(
            self.dependency_artifacts,
            "BuildActionRequest.dependency_artifacts",
            lambda item: item.identity.uri,
        )
        if any(not isinstance(item, ArtifactExport) for item in dependencies):
            fail(
                "BuildActionRequest.dependency_artifacts",
                "must contain ArtifactExport values",
            )
        package_dependencies = _canonical_by(
            self.package_dependency_artifacts,
            "BuildActionRequest.package_dependency_artifacts",
            lambda item: item.identity.uri,
        )
        if any(not isinstance(item, ArtifactExport) for item in package_dependencies):
            fail(
                "BuildActionRequest.package_dependency_artifacts",
                "must contain ArtifactExport values",
            )
        outputs = tuple_value(
            self.declared_output_ids, "BuildActionRequest.declared_output_ids"
        )
        if not outputs:
            fail("BuildActionRequest.declared_output_ids", "must not be empty")
        for output in outputs:
            portable_name(output, "BuildActionRequest.declared_output_ids")
        if outputs != tuple(sorted(set(outputs))):
            fail("BuildActionRequest.declared_output_ids", "must be unique and sorted")
        declarations = _canonical_by(
            self.output_declarations,
            "BuildActionRequest.output_declarations",
            lambda item: item.export_id,
        )
        if any(type(item) is not ArtifactExportDeclaration for item in declarations):
            fail(
                "BuildActionRequest.output_declarations",
                "must contain ArtifactExportDeclaration values",
            )
        if declarations:
            if tuple(item.export_id for item in declarations) != outputs:
                fail(
                    "BuildActionRequest.output_declarations",
                    "must exactly describe every declared output",
                )
            dependency_identities = tuple(
                sorted(
                    {
                        item.identity.uri
                        for item in (*dependencies, *package_dependencies)
                    }
                )
            )
            for declaration in declarations:
                if (
                    declaration.component_revision != self.component_revision
                    or declaration.source_tree_identity != self.source_tree_identity
                    or declaration.toolchain_identity != self.toolchain_identity
                    or declaration.authorization_identity != self.authorization_identity
                ):
                    fail(
                        "BuildActionRequest.output_declarations",
                        "must share the action's Component, source, toolchain, "
                        "and authorization",
                    )
                if (
                    tuple(
                        item.uri for item in declaration.dependency_artifact_identities
                    )
                    != dependency_identities
                ):
                    fail(
                        "BuildActionRequest.output_declarations",
                        "dependency closure must match the producing action",
                    )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": self.SCHEMA,
            "action_id": self.action_id,
            "component_revision": self.component_revision.to_dict(),
            "role": self.role,
            "abi_identity": self.abi_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "media_type": self.media_type,
            "producer_identity": self.producer_identity.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "toolchain_identity": self.toolchain_identity.to_dict(),
            "authorization_identity": self.authorization_identity.to_dict(),
            "dependency_artifacts": [
                item.to_dict() for item in self.dependency_artifacts
            ],
            "declared_output_ids": list(self.declared_output_ids),
        }
        if self.package_dependency_artifacts:
            result["package_dependency_artifacts"] = [
                item.to_dict() for item in self.package_dependency_artifacts
            ]
        if self.output_declarations:
            result["output_declarations"] = [
                item.to_dict() for item in self.output_declarations
            ]
        return result

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "BuildActionRequest"
    ) -> BuildActionRequest:
        names = frozenset(
            {
                "action_id",
                "component_revision",
                "role",
                "abi_identity",
                "target_identity",
                "media_type",
                "producer_identity",
                "source_tree_identity",
                "toolchain_identity",
                "authorization_identity",
                "dependency_artifacts",
                "declared_output_ids",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names,
            optional=frozenset({"package_dependency_artifacts", "output_declarations"}),
        )
        identities = {
            name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
            for name in (
                "component_revision",
                "abi_identity",
                "target_identity",
                "producer_identity",
                "source_tree_identity",
                "toolchain_identity",
                "authorization_identity",
            )
        }
        outputs = data["declared_output_ids"]
        if not isinstance(outputs, list):
            fail(f"{path}.declared_output_ids", "must be an array")
        return cls(
            action_id=string_value(data["action_id"], f"{path}.action_id"),
            role=string_value(data["role"], f"{path}.role"),
            media_type=string_value(data["media_type"], f"{path}.media_type"),
            dependency_artifacts=parse_tuple(
                data["dependency_artifacts"],
                f"{path}.dependency_artifacts",
                ArtifactExport.from_dict,
            ),
            declared_output_ids=tuple(
                string_value(item, f"{path}.declared_output_ids[{index}]")
                for index, item in enumerate(outputs)
            ),
            package_dependency_artifacts=(
                parse_tuple(
                    data["package_dependency_artifacts"],
                    f"{path}.package_dependency_artifacts",
                    ArtifactExport.from_dict,
                )
                if "package_dependency_artifacts" in data
                else ()
            ),
            output_declarations=(
                parse_tuple(
                    data["output_declarations"],
                    f"{path}.output_declarations",
                    ArtifactExportDeclaration.from_dict,
                )
                if "output_declarations" in data
                else ()
            ),
            **identities,
        )


@dataclass(frozen=True, slots=True)
class ComponentBuildManifest:
    component_revision: ContentIdentity
    source_tree_identity: ContentIdentity
    build_system_driver_identity: ContentIdentity
    actions: tuple[BuildActionRequest, ...]
    exports: tuple[ArtifactExport, ...] = ()
    export_declarations: tuple[ArtifactExportDeclaration, ...] = ()

    SCHEMA: ClassVar[str] = COMPONENT_BUILD_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        identity(self.component_revision, "ComponentBuildManifest.component_revision")
        identity(
            self.source_tree_identity, "ComponentBuildManifest.source_tree_identity"
        )
        identity(
            self.build_system_driver_identity,
            "ComponentBuildManifest.build_system_driver_identity",
        )
        actions = _canonical_by(
            self.actions, "ComponentBuildManifest.actions", lambda item: item.action_id
        )
        exports = _canonical_by(
            self.exports, "ComponentBuildManifest.exports", lambda item: item.export_id
        )
        declarations = self.export_declarations
        if not declarations and exports:
            declarations = tuple(item.declaration for item in exports)
            object.__setattr__(self, "export_declarations", declarations)
        declarations = _canonical_by(
            declarations,
            "ComponentBuildManifest.export_declarations",
            lambda item: item.export_id,
        )
        if not actions or not declarations:
            fail(
                "ComponentBuildManifest",
                "must contain actions and export declarations",
            )
        if any(not isinstance(item, BuildActionRequest) for item in actions):
            fail(
                "ComponentBuildManifest.actions",
                "must contain BuildActionRequest values",
            )
        if any(type(item) is not ArtifactExport for item in exports):
            fail("ComponentBuildManifest.exports", "must contain ArtifactExport values")
        if any(type(item) is not ArtifactExportDeclaration for item in declarations):
            fail(
                "ComponentBuildManifest.export_declarations",
                "must contain ArtifactExportDeclaration values",
            )
        for item in (*actions, *declarations, *exports):
            if item.component_revision != self.component_revision:
                fail(
                    "ComponentBuildManifest", "all values must belong to the Component"
                )
            if item.source_tree_identity != self.source_tree_identity:
                fail("ComponentBuildManifest", "all values must bind the source tree")
        declared = {
            output for action in actions for output in action.declared_output_ids
        }
        if declared != {item.export_id for item in declarations}:
            fail(
                "ComponentBuildManifest.export_declarations",
                "must exactly match declared outputs",
            )
        declarations_by_id = {item.export_id: item for item in declarations}
        for action in actions:
            dependency_identities = tuple(
                sorted(
                    {
                        item.identity.uri
                        for item in (
                            *action.dependency_artifacts,
                            *action.package_dependency_artifacts,
                        )
                    }
                )
            )
            for output_id in action.declared_output_ids:
                export = declarations_by_id[output_id]
                if action.output_declarations:
                    described = {
                        item.export_id: item for item in action.output_declarations
                    }[output_id]
                    if export != described:
                        fail(
                            f"ComponentBuildManifest.exports.{output_id}",
                            "must match its producing action's output declaration",
                        )
                    continue
                for field_name in (
                    "role",
                    "abi_identity",
                    "target_identity",
                    "media_type",
                    "producer_identity",
                    "toolchain_identity",
                    "authorization_identity",
                ):
                    if getattr(export, field_name) != getattr(action, field_name):
                        fail(
                            f"ComponentBuildManifest.exports.{output_id}",
                            f"{field_name} must match its producing action",
                        )
                if (
                    tuple(item.uri for item in export.dependency_artifact_identities)
                    != dependency_identities
                ):
                    fail(
                        f"ComponentBuildManifest.exports.{output_id}",
                        "dependency closure must match its producing action",
                    )
        if exports and tuple(item.declaration for item in exports) != declarations:
            fail(
                "ComponentBuildManifest.exports",
                "realized exports must exactly satisfy their declarations",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "build_system_driver_identity": self.build_system_driver_identity.to_dict(),
            "actions": [item.to_dict() for item in self.actions],
            "export_declarations": [
                item.to_dict() for item in self.export_declarations
            ],
            "exports": [item.to_dict() for item in self.exports],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentBuildManifest"
    ) -> ComponentBuildManifest:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "source_tree_identity",
                    "build_system_driver_identity",
                    "actions",
                    "exports",
                }
            ),
            optional=frozenset({"export_declarations"}),
        )
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            source_tree_identity=ContentIdentity.from_dict(
                data["source_tree_identity"], path=f"{path}.source_tree_identity"
            ),
            build_system_driver_identity=ContentIdentity.from_dict(
                data["build_system_driver_identity"],
                path=f"{path}.build_system_driver_identity",
            ),
            actions=parse_tuple(
                data["actions"], f"{path}.actions", BuildActionRequest.from_dict
            ),
            export_declarations=(
                parse_tuple(
                    data["export_declarations"],
                    f"{path}.export_declarations",
                    ArtifactExportDeclaration.from_dict,
                )
                if "export_declarations" in data
                else ()
            ),
            exports=parse_tuple(
                data["exports"], f"{path}.exports", ArtifactExport.from_dict
            ),
        )


@dataclass(frozen=True, slots=True)
class ExactLinkPlan:
    root_artifact_identity: ContentIdentity
    ordered_artifact_identities: tuple[ContentIdentity, ...]
    root_artifact_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = EXACT_LINK_PLAN_SCHEMA

    def __post_init__(self) -> None:
        identity(self.root_artifact_identity, "ExactLinkPlan.root_artifact_identity")
        canonical_identities(
            self.ordered_artifact_identities,
            "ExactLinkPlan.ordered_artifact_identities",
            required=True,
        )
        if self.root_artifact_identity not in self.ordered_artifact_identities:
            fail("ExactLinkPlan", "must include its root artifact")
        if self.root_artifact_identities:
            canonical_identities(
                self.root_artifact_identities,
                "ExactLinkPlan.root_artifact_identities",
                required=True,
            )
            if len(self.root_artifact_identities) < 2:
                fail(
                    "ExactLinkPlan.root_artifact_identities",
                    "must be absent for a single root",
                )
            if self.root_artifact_identity not in self.root_artifact_identities:
                fail(
                    "ExactLinkPlan.root_artifact_identities",
                    "must include the primary root artifact",
                )
            if any(
                item not in self.ordered_artifact_identities
                for item in self.root_artifact_identities
            ):
                fail(
                    "ExactLinkPlan.root_artifact_identities",
                    "every grouped root must be in the artifact closure",
                )

    @property
    def resolved_root_artifact_identities(self) -> tuple[ContentIdentity, ...]:
        return self.root_artifact_identities or (self.root_artifact_identity,)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": self.SCHEMA,
            "root_artifact_identity": self.root_artifact_identity.to_dict(),
            "ordered_artifact_identities": [
                item.to_dict() for item in self.ordered_artifact_identities
            ],
        }
        if self.root_artifact_identities:
            result["root_artifact_identities"] = [
                item.to_dict() for item in self.root_artifact_identities
            ]
        return result

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "ExactLinkPlan") -> ExactLinkPlan:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"root_artifact_identity", "ordered_artifact_identities"}
            ),
            optional=frozenset({"root_artifact_identities"}),
        )
        return cls(
            root_artifact_identity=ContentIdentity.from_dict(
                data["root_artifact_identity"], path=f"{path}.root_artifact_identity"
            ),
            ordered_artifact_identities=parse_tuple(
                data["ordered_artifact_identities"],
                f"{path}.ordered_artifact_identities",
                ContentIdentity.from_dict,
            ),
            root_artifact_identities=(
                parse_tuple(
                    data["root_artifact_identities"],
                    f"{path}.root_artifact_identities",
                    ContentIdentity.from_dict,
                )
                if "root_artifact_identities" in data
                else ()
            ),
        )


class MaterializationIsolation(StrEnum):
    FRESH_EMPTY_EXACT_BLOBS = "fresh-empty-exact-blobs"


@dataclass(frozen=True, slots=True)
class ArtifactMaterializationPlan:
    source_tree_identity: ContentIdentity
    execution_root_identity: ContentIdentity
    entries: tuple[SourceTreeEntry, ...]
    isolation: MaterializationIsolation = (
        MaterializationIsolation.FRESH_EMPTY_EXACT_BLOBS
    )

    native_sdk_input_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = ARTIFACT_MATERIALIZATION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.source_tree_identity,
            "ArtifactMaterializationPlan.source_tree_identity",
        )
        identity(
            self.execution_root_identity,
            "ArtifactMaterializationPlan.execution_root_identity",
        )
        entries = _canonical_by(
            self.entries, "ArtifactMaterializationPlan.entries", lambda item: item.path
        )
        if any(not isinstance(item, SourceTreeEntry) for item in entries):
            fail(
                "ArtifactMaterializationPlan.entries",
                "must contain SourceTreeEntry values",
            )
        if len(self.native_sdk_input_identities) > 16384:
            fail(
                "ArtifactMaterializationPlan.native_sdk_input_identities",
                "must contain at most 16384 values",
            )
        canonical_identities(
            self.native_sdk_input_identities,
            "ArtifactMaterializationPlan.native_sdk_input_identities",
        )
        if self.isolation is not MaterializationIsolation.FRESH_EMPTY_EXACT_BLOBS:
            fail(
                "ArtifactMaterializationPlan.isolation", "must require an isolated root"
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "execution_root_identity": self.execution_root_identity.to_dict(),
            "entries": [item.to_dict() for item in self.entries],
            "isolation": self.isolation.value,
        }
        if self.native_sdk_input_identities:
            value["native_sdk_input_identities"] = [
                item.to_dict() for item in self.native_sdk_input_identities
            ]
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ArtifactMaterializationPlan"
    ) -> ArtifactMaterializationPlan:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "source_tree_identity",
                    "execution_root_identity",
                    "entries",
                    "isolation",
                }
            ),
            optional=frozenset({"native_sdk_input_identities"}),
        )
        return cls(
            source_tree_identity=ContentIdentity.from_dict(
                data["source_tree_identity"], path=f"{path}.source_tree_identity"
            ),
            execution_root_identity=ContentIdentity.from_dict(
                data["execution_root_identity"], path=f"{path}.execution_root_identity"
            ),
            entries=parse_tuple(
                data["entries"], f"{path}.entries", SourceTreeEntry.from_dict
            ),
            native_sdk_input_identities=parse_tuple(
                data.get("native_sdk_input_identities", []),
                f"{path}.native_sdk_input_identities",
                ContentIdentity.from_dict,
            ),
            isolation=enum_value(
                MaterializationIsolation, data["isolation"], f"{path}.isolation"
            ),
        )


class BuildSubActionKind(StrEnum):
    RESOLVE_DEPENDENCIES = "resolve-dependencies"
    GENERATE_BUILD_METADATA = "generate-build-metadata"
    COMPILE = "compile"
    LINK = "link"
    PACKAGE = "package"


class BuildPrivilege(StrEnum):
    READ_MATERIALIZED_SOURCE = "read-materialized-source"
    WRITE_OBJECT_DIRECTORY = "write-object-directory"
    EXECUTE_BUILD_TOOLS = "execute-build-tools"
    READ_DECLARED_ENVIRONMENT = "read-declared-environment"
    NETWORK_ACCESS = "network-access"


@dataclass(frozen=True, slots=True)
class OrderedBuildSubAction:
    index: int
    kind: BuildSubActionKind
    action: BuildActionRequest

    def __post_init__(self) -> None:
        int_value(self.index, "OrderedBuildSubAction.index", maximum=16383)
        if not isinstance(self.kind, BuildSubActionKind):
            fail("OrderedBuildSubAction.kind", "must be a BuildSubActionKind")
        if not isinstance(self.action, BuildActionRequest):
            fail("OrderedBuildSubAction.action", "must be a BuildActionRequest")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "kind": self.kind.value,
            "action": self.action.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "OrderedBuildSubAction"
    ) -> OrderedBuildSubAction:
        data = fields(
            value,
            path=path,
            required=frozenset({"index", "kind", "action"}),
        )
        return cls(
            index=int_value(data["index"], f"{path}.index", maximum=16383),
            kind=enum_value(BuildSubActionKind, data["kind"], f"{path}.kind"),
            action=BuildActionRequest.from_dict(data["action"], path=f"{path}.action"),
        )


@dataclass(frozen=True, slots=True)
class CompositeBuildRequest:
    """One exact, fully typed build invocation above adapter-specific actions."""

    component_revision: ContentIdentity
    component_build_manifest_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    materialization_plan_identity: ContentIdentity
    target_identity: ContentIdentity
    build_system_resolver_identity: ContentIdentity
    build_system_toolchain_identity: ContentIdentity
    language_compiler_identity: ContentIdentity
    language_runtime_identity: ContentIdentity
    authorization_identity: ContentIdentity
    sub_actions: tuple[OrderedBuildSubAction, ...]
    requested_privileges: tuple[BuildPrivilege, ...]
    declared_output_ids: tuple[str, ...]

    SCHEMA: ClassVar[str] = COMPOSITE_BUILD_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "component_build_manifest_identity",
            "source_tree_identity",
            "materialization_plan_identity",
            "target_identity",
            "build_system_resolver_identity",
            "build_system_toolchain_identity",
            "language_compiler_identity",
            "language_runtime_identity",
            "authorization_identity",
        ):
            identity(getattr(self, name), f"CompositeBuildRequest.{name}")

        actions = tuple_value(self.sub_actions, "CompositeBuildRequest.sub_actions")
        if (
            not actions
            or len(actions) > 16384
            or any(not isinstance(item, OrderedBuildSubAction) for item in actions)
        ):
            fail(
                "CompositeBuildRequest.sub_actions",
                "must contain at most 16384 typed sub-actions",
            )
        if tuple(item.index for item in actions) != tuple(range(len(actions))):
            fail(
                "CompositeBuildRequest.sub_actions",
                "must preserve contiguous execution order from zero",
            )
        action_ids = tuple(item.action.action_id for item in actions)
        if len(set(action_ids)) != len(action_ids):
            fail("CompositeBuildRequest.sub_actions", "must not repeat an action")
        for item in actions:
            action = item.action
            exact_bindings = {
                "component_revision": self.component_revision,
                "source_tree_identity": self.source_tree_identity,
                "target_identity": self.target_identity,
                "authorization_identity": self.authorization_identity,
                "toolchain_identity": self.language_compiler_identity,
            }
            for name, expected in exact_bindings.items():
                if getattr(action, name) != expected:
                    fail(
                        f"CompositeBuildRequest.sub_actions[{item.index}].action.{name}",
                        "must match the composite build request",
                    )

        privileges = tuple_value(
            self.requested_privileges, "CompositeBuildRequest.requested_privileges"
        )
        if any(not isinstance(item, BuildPrivilege) for item in privileges):
            fail(
                "CompositeBuildRequest.requested_privileges",
                "must contain BuildPrivilege values",
            )
        privilege_values = tuple(item.value for item in privileges)
        if privilege_values != tuple(sorted(set(privilege_values))):
            fail(
                "CompositeBuildRequest.requested_privileges",
                "must be unique and canonically ordered",
            )
        if BuildPrivilege.EXECUTE_BUILD_TOOLS not in privileges:
            fail(
                "CompositeBuildRequest.requested_privileges",
                "must explicitly request build-tool execution",
            )

        outputs = tuple_value(
            self.declared_output_ids, "CompositeBuildRequest.declared_output_ids"
        )
        for output in outputs:
            portable_name(output, "CompositeBuildRequest.declared_output_ids")
        expected_outputs = tuple(
            sorted(
                {
                    output
                    for item in actions
                    for output in item.action.declared_output_ids
                }
            )
        )
        if outputs != expected_outputs:
            fail(
                "CompositeBuildRequest.declared_output_ids",
                "must exactly declare every sub-action output",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "component_build_manifest_identity": (
                self.component_build_manifest_identity.to_dict()
            ),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "materialization_plan_identity": (
                self.materialization_plan_identity.to_dict()
            ),
            "target_identity": self.target_identity.to_dict(),
            "build_system_resolver_identity": (
                self.build_system_resolver_identity.to_dict()
            ),
            "build_system_toolchain_identity": (
                self.build_system_toolchain_identity.to_dict()
            ),
            "language_compiler_identity": self.language_compiler_identity.to_dict(),
            "language_runtime_identity": self.language_runtime_identity.to_dict(),
            "authorization_identity": self.authorization_identity.to_dict(),
            "sub_actions": [item.to_dict() for item in self.sub_actions],
            "requested_privileges": [item.value for item in self.requested_privileges],
            "declared_output_ids": list(self.declared_output_ids),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CompositeBuildRequest"
    ) -> CompositeBuildRequest:
        identity_names = (
            "component_revision",
            "component_build_manifest_identity",
            "source_tree_identity",
            "materialization_plan_identity",
            "target_identity",
            "build_system_resolver_identity",
            "build_system_toolchain_identity",
            "language_compiler_identity",
            "language_runtime_identity",
            "authorization_identity",
        )
        required = frozenset(
            {
                *identity_names,
                "sub_actions",
                "requested_privileges",
                "declared_output_ids",
            }
        )
        data = contract_fields(
            value, path=path, schema_uri=cls.SCHEMA, required=required
        )
        privileges = list_value(
            data["requested_privileges"], f"{path}.requested_privileges"
        )
        outputs = list_value(data["declared_output_ids"], f"{path}.declared_output_ids")
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            sub_actions=parse_tuple(
                data["sub_actions"],
                f"{path}.sub_actions",
                OrderedBuildSubAction.from_dict,
            ),
            requested_privileges=tuple(
                enum_value(
                    BuildPrivilege,
                    item,
                    f"{path}.requested_privileges[{index}]",
                )
                for index, item in enumerate(privileges)
            ),
            declared_output_ids=tuple(
                string_value(item, f"{path}.declared_output_ids[{index}]")
                for index, item in enumerate(outputs)
            ),
        )


def artifact_driver_composition_identity(
    drivers: tuple[ContentIdentity, ...],
) -> ContentIdentity:
    """Bind exact measured drivers without replacing Component build authority."""
    canonical_identities(drivers, "ArtifactBuildGraph.driver_composition")
    if not 2 <= len(drivers) <= 16384:
        fail("ArtifactBuildGraph.driver_composition", "requires 2 to 16384 drivers")
    return canonical_identity(
        {
            "schema": "literate-ai/artifact-driver-composition@1",
            "drivers": [item.to_dict() for item in drivers],
        }
    )


@dataclass(frozen=True, slots=True)
class ArtifactBuildGraph:
    """Build-system-neutral graph; driver selection is data, never a default here."""

    build_system_driver_identity: ContentIdentity
    manifests: tuple[ComponentBuildManifest, ...]
    link_plans: tuple[ExactLinkPlan, ...]
    driver_composition: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = ARTIFACT_BUILD_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.build_system_driver_identity,
            "ArtifactBuildGraph.build_system_driver_identity",
        )
        manifests = _canonical_by(
            self.manifests,
            "ArtifactBuildGraph.manifests",
            lambda item: item.component_revision.uri,
        )
        links = _canonical_by(
            self.link_plans,
            "ArtifactBuildGraph.link_plans",
            lambda item: item.root_artifact_identity.uri,
        )
        if any(not isinstance(item, ComponentBuildManifest) for item in manifests):
            fail(
                "ArtifactBuildGraph.manifests",
                "must contain ComponentBuildManifest values",
            )
        if any(not isinstance(item, ExactLinkPlan) for item in links):
            fail("ArtifactBuildGraph.link_plans", "must contain ExactLinkPlan values")
        canonical_identities(
            self.driver_composition, "ArtifactBuildGraph.driver_composition"
        )
        if self.driver_composition:
            composition = artifact_driver_composition_identity(self.driver_composition)
            measured = tuple(
                sorted(
                    {item.build_system_driver_identity for item in manifests},
                    key=lambda item: item.uri,
                )
            )
            if (
                self.driver_composition != measured
                or self.build_system_driver_identity != composition
            ):
                fail(
                    "ArtifactBuildGraph.driver_composition",
                    "must bind every and only measured manifest driver",
                )
        elif any(
            item.build_system_driver_identity != self.build_system_driver_identity
            for item in manifests
        ):
            fail(
                "ArtifactBuildGraph.manifests", "must use the selected driver identity"
            )
        exports = {
            export.identity.uri: export
            for manifest in manifests
            for export in manifest.exports
        }
        if len(exports) != sum(len(item.exports) for item in manifests):
            fail("ArtifactBuildGraph.manifests", "must not repeat artifact identities")
        for export in exports.values():
            if any(
                item.uri not in exports
                for item in export.dependency_artifact_identities
            ):
                fail(
                    "ArtifactBuildGraph", "artifact dependency is absent from the graph"
                )
        for export in exports.values():
            _artifact_closure(export, exports)
        for manifest in manifests:
            for action in manifest.actions:
                if any(
                    item.identity.uri not in exports
                    for item in (
                        *action.dependency_artifacts,
                        *action.package_dependency_artifacts,
                    )
                ):
                    fail(
                        "ArtifactBuildGraph",
                        "action dependency is absent from the graph",
                    )
        for link in links:
            if any(
                root.uri not in exports
                for root in link.resolved_root_artifact_identities
            ):
                fail(
                    "ArtifactBuildGraph.link_plans",
                    "link root is absent from the graph",
                )
            expected = tuple(
                sorted(
                    {
                        uri
                        for root in link.resolved_root_artifact_identities
                        for uri in _artifact_closure(exports[root.uri], exports)
                    }
                )
            )
            if tuple(item.uri for item in link.ordered_artifact_identities) != expected:
                fail(
                    "ArtifactBuildGraph.link_plans",
                    "must contain the exact artifact closure",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        result = {
            "schema": self.SCHEMA,
            "build_system_driver_identity": self.build_system_driver_identity.to_dict(),
            "manifests": [item.to_dict() for item in self.manifests],
            "link_plans": [item.to_dict() for item in self.link_plans],
        }
        if self.driver_composition:
            result["driver_composition"] = [
                item.to_dict() for item in self.driver_composition
            ]
        return result

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ArtifactBuildGraph"
    ) -> ArtifactBuildGraph:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"build_system_driver_identity", "manifests", "link_plans"}
            ),
            optional=frozenset({"driver_composition"}),
        )
        if "driver_composition" in data and not data["driver_composition"]:
            fail(f"{path}.driver_composition", "requires multiple drivers")
        return cls(
            build_system_driver_identity=ContentIdentity.from_dict(
                data["build_system_driver_identity"],
                path=f"{path}.build_system_driver_identity",
            ),
            manifests=parse_tuple(
                data["manifests"],
                f"{path}.manifests",
                ComponentBuildManifest.from_dict,
            ),
            link_plans=parse_tuple(
                data["link_plans"], f"{path}.link_plans", ExactLinkPlan.from_dict
            ),
            driver_composition=(
                parse_tuple(
                    data["driver_composition"],
                    f"{path}.driver_composition",
                    ContentIdentity.from_dict,
                )
                if "driver_composition" in data
                else ()
            ),
        )


def _artifact_closure(
    root: ArtifactExport, exports: dict[str, ArtifactExport]
) -> tuple[str, ...]:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(uri: str) -> None:
        if uri in visiting:
            fail("ArtifactBuildGraph", "artifact dependency cycle detected")
        if uri in visited:
            return
        visiting.add(uri)
        for dependency in exports[uri].dependency_artifact_identities:
            visit(dependency.uri)
        visiting.remove(uri)
        visited.add(uri)

    visit(root.identity.uri)
    return tuple(sorted(visited))


def authored_asset_identities(
    assets: tuple[AuthoredBinaryAsset, ...],
) -> tuple[ContentIdentity, ...]:
    """Canonical helper shared by assembly without exposing asset bytes."""

    return tuple(sorted((item.identity for item in assets), key=lambda item: item.uri))


__all__ = [
    "ARTIFACT_BUILD_GRAPH_SCHEMA",
    "ARTIFACT_EXPORT_DECLARATION_SCHEMA",
    "ARTIFACT_EXPORT_SCHEMA",
    "ARTIFACT_MATERIALIZATION_PLAN_SCHEMA",
    "BUILD_ACTION_REQUEST_SCHEMA",
    "COMPOSITE_BUILD_REQUEST_SCHEMA",
    "COMPONENT_BUILD_MANIFEST_SCHEMA",
    "EXACT_LINK_PLAN_SCHEMA",
    "GENERATED_TEXT_TREE_SCHEMA",
    "SOURCE_TREE_MANIFEST_SCHEMA",
    "ArtifactBuildGraph",
    "ArtifactExport",
    "ArtifactExportDeclaration",
    "ArtifactMaterializationPlan",
    "BuildPrivilege",
    "BuildSubActionKind",
    "BuildActionRequest",
    "CompositeBuildRequest",
    "ComponentBuildManifest",
    "ExactLinkPlan",
    "GeneratedTextFile",
    "GeneratedTextTree",
    "OrderedBuildSubAction",
    "MaterializationIsolation",
    "SourceTreeEntry",
    "SourceTreeEntryOrigin",
    "SourceTreeManifest",
    "authored_asset_identities",
]

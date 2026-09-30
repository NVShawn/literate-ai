"""Exact package plans and release artifact closures for Component roots."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    fields,
    parse_tuple,
    string_value,
)
from ..blobs import BlobRef
from ..cpp_libraries import CppLibraryLayout
from ..identity import (
    ComponentRevisionRef,
    ContentIdentity,
    canonical_json_bytes,
    contract_identity,
)
from ..paths import canonical_relative_posix_path
from ._common import canonical_identities, identity, portable_name, tuple_value

PACKAGE_PLAN_SCHEMA = "urn:literate-ai:schema:v2:package-plan"
PACKAGE_RESULT_SCHEMA = "urn:literate-ai:schema:v2:package-result"
RELEASE_ARTIFACT_SET_SCHEMA = "urn:literate-ai:schema:v2:release-artifact-set"
RELEASE_EVIDENCE_MANIFEST_SCHEMA = "literate-ai/standard-release-evidence-manifest@1"
GENERATED_SOURCE_TREE_RECORD_SCHEMA = "literate-ai/generated-source-tree-record@1"


def release_evidence_manifest_bytes(
    evidence: Iterable[ContentIdentity],
) -> bytes:
    """Return the canonical compact manifest for one semantic evidence closure."""

    values = tuple(evidence)
    if not values or any(not isinstance(item, ContentIdentity) for item in values):
        raise TypeError("release evidence must contain ContentIdentity values")
    values = tuple(sorted(values, key=lambda item: item.uri))
    if len(set(values)) != len(values):
        raise ValueError("release evidence identities must be unique")
    return canonical_json_bytes(
        {
            "schema": RELEASE_EVIDENCE_MANIFEST_SCHEMA,
            "evidence_identities": [item.uri for item in values],
        }
    )


@dataclass(frozen=True, slots=True)
class SourceBundleFile:
    """One exact generated source path and its retrievable immutable bytes."""

    path: str
    blob: BlobRef

    def __post_init__(self) -> None:
        _path(self.path, "SourceBundleFile.path")
        if not isinstance(self.blob, BlobRef):
            fail("SourceBundleFile.blob", "must be a BlobRef")

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "blob": self.blob.to_dict()}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceBundleFile"
    ) -> SourceBundleFile:
        data = fields(value, path=path, required=frozenset({"path", "blob"}))
        return cls(
            string_value(data["path"], f"{path}.path"),
            BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
        )


@dataclass(frozen=True, slots=True)
class SourceBundleClosure:
    """Typed tree-record root plus every generated source blob it references."""

    root: BlobRef
    tree_identity: ContentIdentity
    files: tuple[SourceBundleFile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.root, BlobRef):
            fail("SourceBundleClosure.root", "must be a BlobRef")
        identity(self.tree_identity, "SourceBundleClosure.tree_identity")
        values = _canonical(
            self.files,
            "SourceBundleClosure.files",
            key=lambda item: item.path,
            required=True,
        )
        if any(not isinstance(item, SourceBundleFile) for item in values):
            fail(
                "SourceBundleClosure.files",
                "must contain SourceBundleFile values",
            )
        content = self.canonical_root_bytes
        if self.root.digest != hashlib.sha256(
            content
        ).hexdigest() or self.root.size != len(content):
            fail(
                "SourceBundleClosure.root",
                "must identify the canonical tree record for every source file",
            )

    @property
    def canonical_root_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "schema": GENERATED_SOURCE_TREE_RECORD_SCHEMA,
                "tree_identity": self.tree_identity.to_dict(),
                "files": [item.to_dict() for item in self.files],
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root.to_dict(),
            "tree_identity": self.tree_identity.to_dict(),
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceBundleClosure"
    ) -> SourceBundleClosure:
        data = fields(
            value,
            path=path,
            required=frozenset({"root", "tree_identity", "files"}),
        )
        return cls(
            BlobRef.from_dict(data["root"], path=f"{path}.root"),
            ContentIdentity.from_dict(
                data["tree_identity"], path=f"{path}.tree_identity"
            ),
            parse_tuple(data["files"], f"{path}.files", SourceBundleFile.from_dict),
        )


def _path(value: str, label: str) -> str:
    try:
        return canonical_relative_posix_path(value, label=label)
    except (TypeError, ValueError) as exc:
        fail(label, str(exc))


def _canonical(
    values: object,
    label: str,
    *,
    key: object,
    required: bool = False,
    maximum: int = 16384,
) -> tuple[object, ...]:
    items = tuple_value(values, label)
    if required and not items:
        fail(label, "must not be empty")
    if len(items) > maximum:
        fail(label, f"must contain at most {maximum} values")
    keys = tuple(key(item) for item in items)  # type: ignore[operator]
    if len(keys) != len(set(keys)):
        fail(label, "must not contain duplicates")
    if keys != tuple(sorted(keys)):
        fail(label, "must use canonical order")
    return items


class PackageKind(StrEnum):
    """Truthful target-selected package shape, not a universal binary promise."""

    STANDALONE_EXECUTABLE = "standalone-executable"
    RUNTIME_BUNDLE = "runtime-bundle"
    DIRECTORY = "directory"
    ARCHIVE = "archive"
    INSTALLER = "installer"
    CONTAINER_IMAGE = "container-image"


_DIRECTORY_SHAPED_PACKAGE_KINDS = frozenset(
    {
        PackageKind.STANDALONE_EXECUTABLE,
        PackageKind.RUNTIME_BUNDLE,
        PackageKind.DIRECTORY,
    }
)


class PackageFileKind(StrEnum):
    """The authority represented by one package-relative or outer file."""

    ARTIFACT = "artifact"
    RESOURCE = "resource"
    PACKAGE_OUTPUT = "package-output"


class RuntimeRequirementKind(StrEnum):
    INTERPRETER = "interpreter"
    SHARED_LIBRARY = "shared-library"
    SYSTEM_SERVICE = "system-service"
    DEVICE = "device"
    ENVIRONMENT = "environment"


@dataclass(frozen=True, slots=True)
class RuntimeRequirement:
    """One exact runtime dependency and whether package bytes satisfy it."""

    requirement_id: str
    kind: RuntimeRequirementKind
    name: str
    requirement_identity: ContentIdentity
    supplied_by_package: bool

    def __post_init__(self) -> None:
        portable_name(self.requirement_id, "RuntimeRequirement.requirement_id")
        if not isinstance(self.kind, RuntimeRequirementKind):
            fail("RuntimeRequirement.kind", "must be a RuntimeRequirementKind")
        string_value(self.name, "RuntimeRequirement.name", max_length=255)
        identity(
            self.requirement_identity,
            "RuntimeRequirement.requirement_identity",
        )
        bool_value(
            self.supplied_by_package,
            "RuntimeRequirement.supplied_by_package",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind.value,
            "name": self.name,
            "requirement_identity": self.requirement_identity.to_dict(),
            "supplied_by_package": self.supplied_by_package,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RuntimeRequirement"
    ) -> RuntimeRequirement:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "requirement_id",
                    "kind",
                    "name",
                    "requirement_identity",
                    "supplied_by_package",
                }
            ),
        )
        return cls(
            requirement_id=string_value(
                data["requirement_id"], f"{path}.requirement_id"
            ),
            kind=enum_value(RuntimeRequirementKind, data["kind"], f"{path}.kind"),
            name=string_value(data["name"], f"{path}.name"),
            requirement_identity=ContentIdentity.from_dict(
                data["requirement_identity"],
                path=f"{path}.requirement_identity",
            ),
            supplied_by_package=bool_value(
                data["supplied_by_package"],
                f"{path}.supplied_by_package",
            ),
        )


@dataclass(frozen=True, slots=True)
class PackageInput:
    """One exact artifact or resource projected into a package-relative path."""

    path: str
    role: str
    kind: PackageFileKind
    source_identity: ContentIdentity
    target_identity: ContentIdentity
    blob: BlobRef
    executable: bool = False

    def __post_init__(self) -> None:
        _path(self.path, "PackageInput.path")
        portable_name(self.role, "PackageInput.role")
        if self.kind not in {PackageFileKind.ARTIFACT, PackageFileKind.RESOURCE}:
            fail(
                "PackageInput.kind",
                "must identify an artifact or resource input",
            )
        identity(self.source_identity, "PackageInput.source_identity")
        identity(self.target_identity, "PackageInput.target_identity")
        if not isinstance(self.blob, BlobRef):
            fail("PackageInput.blob", "must be a BlobRef")

        bool_value(self.executable, "PackageInput.executable")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "path": self.path,
            "role": self.role,
            "kind": self.kind.value,
            "source_identity": self.source_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "blob": self.blob.to_dict(),
        }
        if self.executable:
            value["executable"] = True
        return value

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "PackageInput") -> PackageInput:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "path",
                    "role",
                    "kind",
                    "source_identity",
                    "target_identity",
                    "blob",
                }
            ),
            optional=frozenset({"executable"}),
        )
        return cls(
            path=string_value(data["path"], f"{path}.path"),
            role=string_value(data["role"], f"{path}.role"),
            kind=enum_value(PackageFileKind, data["kind"], f"{path}.kind"),
            source_identity=ContentIdentity.from_dict(
                data["source_identity"], path=f"{path}.source_identity"
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            blob=BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
            executable=bool_value(data.get("executable", False), f"{path}.executable"),
        )


@dataclass(frozen=True, slots=True)
class PackageEntrypoint:
    name: str
    kind: str
    path: str
    source_identity: ContentIdentity
    deployment_unit: str | None = None

    def __post_init__(self) -> None:
        portable_name(self.name, "PackageEntrypoint.name")
        portable_name(self.kind, "PackageEntrypoint.kind")
        _path(self.path, "PackageEntrypoint.path")
        identity(self.source_identity, "PackageEntrypoint.source_identity")
        if self.deployment_unit is not None:
            string_value(
                self.deployment_unit,
                "PackageEntrypoint.deployment_unit",
                max_length=4096,
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "source_identity": self.source_identity.to_dict(),
        }
        if self.deployment_unit is not None:
            result["deployment_unit"] = self.deployment_unit
        return result

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "PackageEntrypoint"
    ) -> PackageEntrypoint:
        data = fields(
            value,
            path=path,
            required=frozenset({"name", "kind", "path", "source_identity"}),
            optional=frozenset({"deployment_unit"}),
        )
        return cls(
            name=string_value(data["name"], f"{path}.name"),
            kind=string_value(data["kind"], f"{path}.kind"),
            path=string_value(data["path"], f"{path}.path"),
            source_identity=ContentIdentity.from_dict(
                data["source_identity"], path=f"{path}.source_identity"
            ),
            deployment_unit=(
                string_value(data["deployment_unit"], f"{path}.deployment_unit")
                if "deployment_unit" in data
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PackagePlan:
    """Provider-neutral package intent over one exact root artifact closure."""

    root_component_revision: ContentIdentity
    component_lock_identity: ContentIdentity
    target_identity: ContentIdentity
    artifact_graph_identity: ContentIdentity
    link_plan_identity: ContentIdentity
    root_artifact_identity: ContentIdentity
    package_kind: PackageKind
    packager_identity: ContentIdentity
    inputs: tuple[PackageInput, ...]
    entrypoints: tuple[PackageEntrypoint, ...]
    runtime_requirements: tuple[RuntimeRequirement, ...]
    native_library_root: str | None = None
    native_library_layout: CppLibraryLayout | None = None

    SCHEMA: ClassVar[str] = PACKAGE_PLAN_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "root_component_revision",
            "component_lock_identity",
            "target_identity",
            "artifact_graph_identity",
            "link_plan_identity",
            "root_artifact_identity",
            "packager_identity",
        ):
            identity(getattr(self, name), f"PackagePlan.{name}")
        if not isinstance(self.package_kind, PackageKind):
            fail("PackagePlan.package_kind", "must be a PackageKind")
        inputs = _canonical(
            self.inputs,
            "PackagePlan.inputs",
            key=lambda item: item.path,
            required=True,
        )
        if any(not isinstance(item, PackageInput) for item in inputs):
            fail("PackagePlan.inputs", "must contain PackageInput values")
        entrypoints = _canonical(
            self.entrypoints,
            "PackagePlan.entrypoints",
            key=lambda item: item.name,
            required=(
                self.package_kind is not PackageKind.DIRECTORY
                and self.native_library_layout is None
            ),
            maximum=256,
        )
        if any(not isinstance(item, PackageEntrypoint) for item in entrypoints):
            fail(
                "PackagePlan.entrypoints",
                "must contain PackageEntrypoint values",
            )
        requirements = _canonical(
            self.runtime_requirements,
            "PackagePlan.runtime_requirements",
            key=lambda item: item.requirement_id,
            maximum=1024,
        )
        if any(not isinstance(item, RuntimeRequirement) for item in requirements):
            fail(
                "PackagePlan.runtime_requirements",
                "must contain RuntimeRequirement values",
            )
        if any(item.target_identity != self.target_identity for item in inputs):
            fail("PackagePlan.inputs", "must match the package target")
        artifact_inputs = {
            item.source_identity
            for item in inputs
            if item.kind is PackageFileKind.ARTIFACT
        }
        input_paths = {item.path: item for item in inputs}
        if (self.native_library_root is None) != (self.native_library_layout is None):
            fail(
                "PackagePlan.native_library_layout",
                "requires both the native library root and layout",
            )
        if self.native_library_root is not None:
            root = canonical_relative_posix_path(
                self.native_library_root,
                label="PackagePlan.native_library_root",
            )
            root_path = root.as_posix()
            if root_path != self.native_library_root:
                fail(
                    "PackagePlan.native_library_root",
                    "must be a canonical relative path",
                )
            if self.package_kind not in {PackageKind.DIRECTORY, PackageKind.ARCHIVE}:
                fail(
                    "PackagePlan.native_library_layout",
                    "requires a directory or archive package",
                )
            if entrypoints:
                fail(
                    "PackagePlan.entrypoints",
                    "native library packages do not declare application entrypoints",
                )
            native_root = input_paths.get(root_path)
            if (
                native_root is None
                or native_root.kind is not PackageFileKind.ARTIFACT
                or native_root.source_identity != self.root_artifact_identity
            ):
                fail(
                    "PackagePlan.native_library_root",
                    "must bind the exact root library artifact input",
                )
        if self.root_artifact_identity not in artifact_inputs:
            fail("PackagePlan.inputs", "must include the root linked artifact")
        for item in entrypoints:
            packaged = input_paths.get(item.path)
            if packaged is None or packaged.source_identity != item.source_identity:
                fail(
                    "PackagePlan.entrypoints",
                    "must bind an exact package input path and artifact",
                )
        if self.package_kind is PackageKind.STANDALONE_EXECUTABLE and any(
            not item.supplied_by_package for item in requirements
        ):
            fail(
                "PackagePlan.runtime_requirements",
                "a standalone executable cannot require an external runtime",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def standalone(self) -> bool:
        return self.package_kind is PackageKind.STANDALONE_EXECUTABLE and all(
            item.supplied_by_package for item in self.runtime_requirements
        )

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema": self.SCHEMA,
            "root_component_revision": self.root_component_revision.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "artifact_graph_identity": self.artifact_graph_identity.to_dict(),
            "link_plan_identity": self.link_plan_identity.to_dict(),
            "root_artifact_identity": self.root_artifact_identity.to_dict(),
            "package_kind": self.package_kind.value,
            "packager_identity": self.packager_identity.to_dict(),
            "inputs": [item.to_dict() for item in self.inputs],
            "entrypoints": [item.to_dict() for item in self.entrypoints],
            "runtime_requirements": [
                item.to_dict() for item in self.runtime_requirements
            ],
        }
        if self.native_library_root is not None:
            value["native_library_root"] = self.native_library_root
            assert self.native_library_layout is not None
            value["native_library_layout"] = self.native_library_layout.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "PackagePlan") -> PackagePlan:
        identity_names = (
            "root_component_revision",
            "component_lock_identity",
            "target_identity",
            "artifact_graph_identity",
            "link_plan_identity",
            "root_artifact_identity",
            "packager_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    *identity_names,
                    "package_kind",
                    "inputs",
                    "entrypoints",
                    "runtime_requirements",
                }
            ),
            optional=frozenset({"native_library_root", "native_library_layout"}),
        )
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            package_kind=enum_value(
                PackageKind, data["package_kind"], f"{path}.package_kind"
            ),
            inputs=parse_tuple(
                data["inputs"], f"{path}.inputs", PackageInput.from_dict
            ),
            entrypoints=parse_tuple(
                data["entrypoints"],
                f"{path}.entrypoints",
                PackageEntrypoint.from_dict,
            ),
            runtime_requirements=parse_tuple(
                data["runtime_requirements"],
                f"{path}.runtime_requirements",
                RuntimeRequirement.from_dict,
            ),
            native_library_root=(
                string_value(data["native_library_root"], f"{path}.native_library_root")
                if "native_library_root" in data
                else None
            ),
            native_library_layout=(
                CppLibraryLayout.from_dict(
                    data["native_library_layout"],
                    path=f"{path}.native_library_layout",
                )
                if "native_library_layout" in data
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PackagedFile:
    path: str
    role: str
    kind: PackageFileKind
    source_identity: ContentIdentity
    target_identity: ContentIdentity
    blob: BlobRef
    executable: bool

    def __post_init__(self) -> None:
        _path(self.path, "PackagedFile.path")
        portable_name(self.role, "PackagedFile.role")
        if not isinstance(self.kind, PackageFileKind):
            fail("PackagedFile.kind", "must be a PackageFileKind")
        identity(self.source_identity, "PackagedFile.source_identity")
        identity(self.target_identity, "PackagedFile.target_identity")
        if not isinstance(self.blob, BlobRef):
            fail("PackagedFile.blob", "must be a BlobRef")
        bool_value(self.executable, "PackagedFile.executable")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "role": self.role,
            "kind": self.kind.value,
            "source_identity": self.source_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "blob": self.blob.to_dict(),
            "executable": self.executable,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "PackagedFile") -> PackagedFile:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "path",
                    "role",
                    "kind",
                    "source_identity",
                    "target_identity",
                    "blob",
                    "executable",
                }
            ),
        )
        return cls(
            path=string_value(data["path"], f"{path}.path"),
            role=string_value(data["role"], f"{path}.role"),
            kind=enum_value(PackageFileKind, data["kind"], f"{path}.kind"),
            source_identity=ContentIdentity.from_dict(
                data["source_identity"], path=f"{path}.source_identity"
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            blob=BlobRef.from_dict(data["blob"], path=f"{path}.blob"),
            executable=bool_value(data["executable"], f"{path}.executable"),
        )


@dataclass(frozen=True, slots=True)
class PackageResult:
    """Exact package output with an independently checkable runtime-closure claim."""

    package_plan_identity: ContentIdentity
    root_component_revision: ContentIdentity
    component_lock_identity: ContentIdentity
    target_identity: ContentIdentity
    artifact_graph_identity: ContentIdentity
    package_kind: PackageKind
    packager_identity: ContentIdentity
    files: tuple[PackagedFile, ...]
    artifacts: tuple[PackagedFile, ...]
    entrypoints: tuple[PackageEntrypoint, ...]
    runtime_requirements: tuple[RuntimeRequirement, ...]
    native_library_root: str | None = None
    native_library_layout: CppLibraryLayout | None = None

    SCHEMA: ClassVar[str] = PACKAGE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "package_plan_identity",
            "root_component_revision",
            "component_lock_identity",
            "target_identity",
            "artifact_graph_identity",
            "packager_identity",
        ):
            identity(getattr(self, name), f"PackageResult.{name}")
        if not isinstance(self.package_kind, PackageKind):
            fail("PackageResult.package_kind", "must be a PackageKind")
        files = _canonical(
            self.files,
            "PackageResult.files",
            key=lambda item: item.path,
            required=True,
        )
        if any(not isinstance(item, PackagedFile) for item in files):
            fail("PackageResult.files", "must contain PackagedFile values")
        if any(item.kind is PackageFileKind.PACKAGE_OUTPUT for item in files):
            fail(
                "PackageResult.files",
                "logical package files cannot be outer package outputs",
            )
        if any(item.target_identity != self.target_identity for item in files):
            fail("PackageResult.files", "must match the package target")
        artifacts = _canonical(
            self.artifacts,
            "PackageResult.artifacts",
            key=lambda item: item.path,
            required=True,
            maximum=(
                16384 if self.package_kind in _DIRECTORY_SHAPED_PACKAGE_KINDS else 256
            ),
        )
        if any(not isinstance(item, PackagedFile) for item in artifacts):
            fail("PackageResult.artifacts", "must contain PackagedFile values")
        if any(item.target_identity != self.target_identity for item in artifacts):
            fail("PackageResult.artifacts", "must match the package target")
        entrypoints = _canonical(
            self.entrypoints,
            "PackageResult.entrypoints",
            key=lambda item: item.name,
            required=(
                self.package_kind is not PackageKind.DIRECTORY
                and self.native_library_layout is None
            ),
            maximum=256,
        )
        requirements = _canonical(
            self.runtime_requirements,
            "PackageResult.runtime_requirements",
            key=lambda item: item.requirement_id,
            maximum=1024,
        )
        if any(not isinstance(item, PackageEntrypoint) for item in entrypoints):
            fail(
                "PackageResult.entrypoints",
                "must contain PackageEntrypoint values",
            )
        if any(not isinstance(item, RuntimeRequirement) for item in requirements):
            fail(
                "PackageResult.runtime_requirements",
                "must contain RuntimeRequirement values",
            )
        by_path = {item.path: item for item in files}
        if (self.native_library_root is None) != (self.native_library_layout is None):
            fail(
                "PackageResult.native_library_layout",
                "requires both the native library root and layout",
            )
        if self.native_library_root is not None:
            root = canonical_relative_posix_path(
                self.native_library_root,
                label="PackageResult.native_library_root",
            )
            root_path = root.as_posix()
            if root_path != self.native_library_root or root_path not in by_path:
                fail(
                    "PackageResult.native_library_root",
                    "must bind an exact logical package file",
                )
            if self.package_kind not in {PackageKind.DIRECTORY, PackageKind.ARCHIVE}:
                fail(
                    "PackageResult.native_library_layout",
                    "requires a directory or archive package",
                )
            if entrypoints:
                fail(
                    "PackageResult.entrypoints",
                    "native library packages do not declare application entrypoints",
                )
        for item in entrypoints:
            packaged = by_path.get(item.path)
            if packaged is None or packaged.source_identity != item.source_identity:
                fail(
                    "PackageResult.entrypoints",
                    "must bind an exact packaged file and source artifact",
                )
        if self.package_kind is PackageKind.STANDALONE_EXECUTABLE:
            if any(not item.supplied_by_package for item in requirements):
                fail(
                    "PackageResult.runtime_requirements",
                    "a standalone executable cannot require an external runtime",
                )
            if not any(item.executable for item in artifacts):
                fail(
                    "PackageResult.artifacts",
                    "a standalone executable must contain executable bytes",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def standalone(self) -> bool:
        return self.package_kind is PackageKind.STANDALONE_EXECUTABLE and all(
            item.supplied_by_package for item in self.runtime_requirements
        )

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema": self.SCHEMA,
            "package_plan_identity": self.package_plan_identity.to_dict(),
            "root_component_revision": self.root_component_revision.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "artifact_graph_identity": self.artifact_graph_identity.to_dict(),
            "package_kind": self.package_kind.value,
            "packager_identity": self.packager_identity.to_dict(),
            "files": [item.to_dict() for item in self.files],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "entrypoints": [item.to_dict() for item in self.entrypoints],
            "runtime_requirements": [
                item.to_dict() for item in self.runtime_requirements
            ],
            "standalone": self.standalone,
        }
        if self.native_library_root is not None:
            value["native_library_root"] = self.native_library_root
            assert self.native_library_layout is not None
            value["native_library_layout"] = self.native_library_layout.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "PackageResult") -> PackageResult:
        identity_names = (
            "package_plan_identity",
            "root_component_revision",
            "component_lock_identity",
            "target_identity",
            "artifact_graph_identity",
            "packager_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    *identity_names,
                    "package_kind",
                    "files",
                    "artifacts",
                    "entrypoints",
                    "runtime_requirements",
                    "standalone",
                }
            ),
            optional=frozenset({"native_library_root", "native_library_layout"}),
        )
        result = cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            package_kind=enum_value(
                PackageKind, data["package_kind"], f"{path}.package_kind"
            ),
            files=parse_tuple(data["files"], f"{path}.files", PackagedFile.from_dict),
            artifacts=parse_tuple(
                data["artifacts"], f"{path}.artifacts", PackagedFile.from_dict
            ),
            entrypoints=parse_tuple(
                data["entrypoints"],
                f"{path}.entrypoints",
                PackageEntrypoint.from_dict,
            ),
            runtime_requirements=parse_tuple(
                data["runtime_requirements"],
                f"{path}.runtime_requirements",
                RuntimeRequirement.from_dict,
            ),
            native_library_root=(
                string_value(data["native_library_root"], f"{path}.native_library_root")
                if "native_library_root" in data
                else None
            ),
            native_library_layout=(
                CppLibraryLayout.from_dict(
                    data["native_library_layout"],
                    path=f"{path}.native_library_layout",
                )
                if "native_library_layout" in data
                else None
            ),
        )
        claimed = bool_value(data["standalone"], f"{path}.standalone")
        if claimed != result.standalone:
            fail(f"{path}.standalone", "does not match the exact runtime closure")
        return result


@dataclass(frozen=True, slots=True)
class ReleaseArtifactSet:
    """All package variants and release evidence for one exact target lock."""

    root_component_ref: ComponentRevisionRef
    root_component_revision: ContentIdentity
    root_source_bundle: SourceBundleClosure
    component_lock_identity: ContentIdentity
    target_identity: ContentIdentity
    artifact_graph_identity: ContentIdentity
    accepted_workspace_identities: tuple[ContentIdentity, ...]
    release_declaration_identities: tuple[ContentIdentity, ...]
    packages: tuple[PackageResult, ...]
    resource_identities: tuple[ContentIdentity, ...]
    evidence_identities: tuple[ContentIdentity, ...]
    evidence_manifest: BlobRef

    SCHEMA: ClassVar[str] = RELEASE_ARTIFACT_SET_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.root_component_ref, ComponentRevisionRef):
            fail(
                "ReleaseArtifactSet.root_component_ref",
                "must be a ComponentRevisionRef",
            )
        if self.root_component_ref.revision_identity != self.root_component_revision:
            fail(
                "ReleaseArtifactSet.root_component_ref",
                "must bind the exact root Component revision",
            )
        for name in (
            "root_component_revision",
            "component_lock_identity",
            "target_identity",
            "artifact_graph_identity",
        ):
            identity(getattr(self, name), f"ReleaseArtifactSet.{name}")
        if not isinstance(self.root_source_bundle, SourceBundleClosure):
            fail(
                "ReleaseArtifactSet.root_source_bundle",
                "must be a SourceBundleClosure",
            )
        canonical_identities(
            self.accepted_workspace_identities,
            "ReleaseArtifactSet.accepted_workspace_identities",
            required=True,
        )
        release_declarations = canonical_identities(
            self.release_declaration_identities,
            "ReleaseArtifactSet.release_declaration_identities",
            required=True,
        )
        packages = _canonical(
            self.packages,
            "ReleaseArtifactSet.packages",
            key=lambda item: item.package_plan_identity.uri,
            required=True,
            maximum=256,
        )
        if any(not isinstance(item, PackageResult) for item in packages):
            fail("ReleaseArtifactSet.packages", "must contain PackageResult values")
        if len(release_declarations) != len(packages):
            fail(
                "ReleaseArtifactSet.release_declaration_identities",
                "must identify exactly one declaration for every package result",
            )
        canonical_identities(
            self.resource_identities,
            "ReleaseArtifactSet.resource_identities",
        )
        evidence_identities = canonical_identities(
            self.evidence_identities,
            "ReleaseArtifactSet.evidence_identities",
            required=True,
        )
        if not isinstance(self.evidence_manifest, BlobRef):
            fail("ReleaseArtifactSet.evidence_manifest", "must be a BlobRef")
        expected_evidence_manifest = release_evidence_manifest_bytes(
            evidence_identities
        )
        if self.evidence_manifest.digest != hashlib.sha256(
            expected_evidence_manifest
        ).hexdigest() or self.evidence_manifest.size != len(expected_evidence_manifest):
            fail(
                "ReleaseArtifactSet.evidence_manifest",
                "must index every and only declared evidence identity",
            )
        for item in packages:
            exact = {
                "root_component_revision": self.root_component_revision,
                "component_lock_identity": self.component_lock_identity,
                "target_identity": self.target_identity,
                "artifact_graph_identity": self.artifact_graph_identity,
            }
            for name, expected in exact.items():
                if getattr(item, name) != expected:
                    fail(
                        f"ReleaseArtifactSet.packages.{name}",
                        "must match the release artifact set",
                    )
        packaged_resources = {
            file.source_identity
            for package in packages
            for file in package.files
            if file.kind is PackageFileKind.RESOURCE
        }
        if packaged_resources != set(self.resource_identities):
            fail(
                "ReleaseArtifactSet.resource_identities",
                "must identify every and only packaged runtime resource",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def root_source_bundle_identity(self) -> ContentIdentity:
        return ContentIdentity.parse_uri(self.root_source_bundle.root.identity)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "root_component_ref": self.root_component_ref.to_dict(),
            "root_component_revision": self.root_component_revision.to_dict(),
            "root_source_bundle": self.root_source_bundle.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "artifact_graph_identity": self.artifact_graph_identity.to_dict(),
            "accepted_workspace_identities": [
                item.to_dict() for item in self.accepted_workspace_identities
            ],
            "release_declaration_identities": [
                item.to_dict() for item in self.release_declaration_identities
            ],
            "packages": [item.to_dict() for item in self.packages],
            "resource_identities": [
                item.to_dict() for item in self.resource_identities
            ],
            "evidence_identities": [
                item.to_dict() for item in self.evidence_identities
            ],
            "evidence_manifest": self.evidence_manifest.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ReleaseArtifactSet"
    ) -> ReleaseArtifactSet:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "root_component_ref",
                    "root_component_revision",
                    "root_source_bundle",
                    "component_lock_identity",
                    "target_identity",
                    "artifact_graph_identity",
                    "accepted_workspace_identities",
                    "release_declaration_identities",
                    "packages",
                    "resource_identities",
                    "evidence_identities",
                    "evidence_manifest",
                }
            ),
        )
        return cls(
            root_component_ref=ComponentRevisionRef.from_dict(
                data["root_component_ref"]
            ),
            root_component_revision=ContentIdentity.from_dict(
                data["root_component_revision"],
                path=f"{path}.root_component_revision",
            ),
            root_source_bundle=SourceBundleClosure.from_dict(
                data["root_source_bundle"],
                path=f"{path}.root_source_bundle",
            ),
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"],
                path=f"{path}.component_lock_identity",
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            artifact_graph_identity=ContentIdentity.from_dict(
                data["artifact_graph_identity"],
                path=f"{path}.artifact_graph_identity",
            ),
            accepted_workspace_identities=parse_tuple(
                data["accepted_workspace_identities"],
                f"{path}.accepted_workspace_identities",
                ContentIdentity.from_dict,
            ),
            release_declaration_identities=parse_tuple(
                data["release_declaration_identities"],
                f"{path}.release_declaration_identities",
                ContentIdentity.from_dict,
            ),
            packages=parse_tuple(
                data["packages"], f"{path}.packages", PackageResult.from_dict
            ),
            resource_identities=parse_tuple(
                data["resource_identities"],
                f"{path}.resource_identities",
                ContentIdentity.from_dict,
            ),
            evidence_identities=parse_tuple(
                data["evidence_identities"],
                f"{path}.evidence_identities",
                ContentIdentity.from_dict,
            ),
            evidence_manifest=BlobRef.from_dict(data["evidence_manifest"]),
        )


__all__ = [
    "GENERATED_SOURCE_TREE_RECORD_SCHEMA",
    "PACKAGE_PLAN_SCHEMA",
    "PACKAGE_RESULT_SCHEMA",
    "RELEASE_EVIDENCE_MANIFEST_SCHEMA",
    "RELEASE_ARTIFACT_SET_SCHEMA",
    "PackageEntrypoint",
    "PackageFileKind",
    "PackageInput",
    "PackageKind",
    "PackagePlan",
    "PackageResult",
    "PackagedFile",
    "ReleaseArtifactSet",
    "RuntimeRequirement",
    "RuntimeRequirementKind",
    "SourceBundleClosure",
    "SourceBundleFile",
    "release_evidence_manifest_bytes",
]

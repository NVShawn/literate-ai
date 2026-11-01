"""Retained-origin unit definitions and plans; decoding grants no live authority.

Definitions precede the ownership/dependency graph. Plans bind that completed
graph, and results bind produced archives, avoiding recursive artifact identities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields
from .blobs import BlobRef
from .executable_components._common import portable_name
from .identity import ComponentCoordinate, ContentIdentity, contract_identity
from .native_tree_shards import MAX_DESCRIPTOR_BYTES, MAX_INDEX_BYTES
from .paths import canonical_relative_posix_path


def _json_reference(value):
    if (
        not isinstance(value, BlobRef)
        or value.media_type != "application/json"
        or not 0 < value.size <= MAX_DESCRIPTOR_BYTES
    ):
        raise ValueError("retained.unit.bounded-json-reference-required")


def _identities(value, names):
    if any(not isinstance(getattr(value, name), ContentIdentity) for name in names):
        raise TypeError("retained.unit.typed-provenance-required")


@dataclass(frozen=True, slots=True)
class RetainedUnitPackageDefinition:
    """Local, non-circular provenance; the live owner must verify every binding."""

    unit_name: str
    component: ComponentCoordinate
    request_identity: ContentIdentity
    retained_plan_identity: ContentIdentity
    receipt_identity: ContentIdentity
    retained_source_identity: ContentIdentity
    payload_identity: ContentIdentity
    component_authoring_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    recipe_identity: ContentIdentity
    target_identity: ContentIdentity
    source_bom_identity: ContentIdentity
    resolved_bom_identity: ContentIdentity
    license_identity: ContentIdentity
    provider_revision: ContentIdentity
    provider_parameters: BlobRef
    abi: str
    version: str
    provider: str = "pkg"

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-definition@1"
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "request_identity",
        "retained_plan_identity",
        "receipt_identity",
        "retained_source_identity",
        "payload_identity",
        "component_authoring_identity",
        "component_lock_identity",
        "recipe_identity",
        "target_identity",
        "source_bom_identity",
        "resolved_bom_identity",
        "license_identity",
        "provider_revision",
    )

    def __post_init__(self):
        portable_name(self.unit_name, "RetainedUnitPackageDefinition.unit_name")
        if not isinstance(self.component, ComponentCoordinate):
            raise TypeError("retained.unit.canonical-component-required")
        _identities(self, self.IDENTITY_FIELDS)
        _json_reference(self.provider_parameters)
        for name in ("abi", "version"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 255
                or any(c in value for c in "\r\n\x00")
            ):
                raise ValueError("retained.unit.exact-abi-version-binding-required")
        if self.provider != "pkg":
            raise ValueError("retained.unit.native-pkg-provider-required")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "release_authorized": False,
            "unit_name": self.unit_name,
            "component": self.component.to_dict(),
            "provider": self.provider,
            "abi": self.abi,
            "version": self.version,
            "provider_parameters": self.provider_parameters.to_dict(),
            **{name: getattr(self, name).uri for name in self.IDENTITY_FIELDS},
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "authority_mode",
                    "release_authorized",
                    "unit_name",
                    "component",
                    "provider",
                    "abi",
                    "version",
                    "provider_parameters",
                    *cls.IDENTITY_FIELDS,
                }
            ),
        )
        if (
            data["authority_mode"] != "retained-project"
            or data["release_authorized"] is not False
        ):
            raise ValueError("retained.unit.original-authority-required")
        return cls(
            data["unit_name"],
            ComponentCoordinate.from_dict(data["component"]),
            *(ContentIdentity.parse_uri(data[name]) for name in cls.IDENTITY_FIELDS),
            BlobRef.from_dict(data["provider_parameters"]),
            data["abi"],
            data["version"],
            data["provider"],
        )


@dataclass(frozen=True, slots=True)
class RetainedUnitOwnership:
    """One explicit payload owner; shared structural ancestors are not payload."""

    path: str
    kind: str
    unit_identity: ContentIdentity

    def __post_init__(self):
        canonical_relative_posix_path(self.path, label="retained unit ownership")
        if self.kind not in {"file", "symlink", "hardlink", "empty-directory"}:
            raise ValueError("retained.unit.explicit-payload-kind-required")
        _identities(self, ("unit_identity",))

    def to_dict(self):
        return {
            "path": self.path,
            "kind": self.kind,
            "unit_identity": self.unit_identity.uri,
        }

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {"path", "kind", "unit_identity"}:
            raise ValueError("retained.unit.ownership-fields-invalid")
        return cls(
            value["path"],
            value["kind"],
            ContentIdentity.parse_uri(value["unit_identity"]),
        )


@dataclass(frozen=True, slots=True)
class RetainedUnitDependency:
    source_unit_identity: ContentIdentity
    target_unit_identity: ContentIdentity
    kind: str
    target_identity: ContentIdentity
    required_abi: str | None = None
    required_version: str | None = None

    def __post_init__(self):
        _identities(
            self, ("source_unit_identity", "target_unit_identity", "target_identity")
        )
        if self.kind not in {"build-input", "runtime", "tree-link"}:
            raise ValueError("retained.unit.typed-dependency-kind-required")
        if self.source_unit_identity == self.target_unit_identity:
            raise ValueError("retained.unit.self-dependency-refused")
        for name in ("required_abi", "required_version"):
            value = getattr(self, name)
            if value is None and self.kind == "tree-link":
                continue
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 255
                or any(c in value for c in "\r\n\x00")
            ):
                raise ValueError("retained.unit.exact-abi-version-binding-required")

    def to_dict(self):
        return {
            "source_unit_identity": self.source_unit_identity.uri,
            "target_unit_identity": self.target_unit_identity.uri,
            "kind": self.kind,
            "target_identity": self.target_identity.uri,
            "required_abi": self.required_abi,
            "required_version": self.required_version,
        }

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {
            "source_unit_identity",
            "target_unit_identity",
            "kind",
            "target_identity",
            "required_abi",
            "required_version",
        }:
            raise ValueError("retained.unit.dependency-fields-invalid")
        return cls(
            ContentIdentity.parse_uri(value["source_unit_identity"]),
            ContentIdentity.parse_uri(value["target_unit_identity"]),
            value["kind"],
            ContentIdentity.parse_uri(value["target_identity"]),
            value["required_abi"],
            value["required_version"],
        )


@dataclass(frozen=True, slots=True)
class RetainedUnitPackagePlan:
    definition: RetainedUnitPackageDefinition
    package_set_plan_identity: ContentIdentity
    ownership_index: BlobRef
    dependency_index: BlobRef
    packager_identity: ContentIdentity
    tool_identity: ContentIdentity

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-plan@1"

    def __post_init__(self):
        if not isinstance(self.definition, RetainedUnitPackageDefinition):
            raise TypeError("retained.unit.typed-definition-required")
        _identities(
            self, ("package_set_plan_identity", "packager_identity", "tool_identity")
        )
        _json_reference(self.ownership_index)
        _json_reference(self.dependency_index)

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "release_authorized": False,
            "definition": self.definition.to_dict(),
            "package_set_plan_identity": self.package_set_plan_identity.uri,
            "ownership_index": self.ownership_index.to_dict(),
            "dependency_index": self.dependency_index.to_dict(),
            "packager_identity": self.packager_identity.uri,
            "tool_identity": self.tool_identity.uri,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "authority_mode",
                    "release_authorized",
                    "definition",
                    "package_set_plan_identity",
                    "ownership_index",
                    "dependency_index",
                    "packager_identity",
                    "tool_identity",
                }
            ),
        )
        if (
            data["authority_mode"] != "retained-project"
            or data["release_authorized"] is not False
        ):
            raise ValueError("retained.unit.original-authority-required")
        return cls(
            RetainedUnitPackageDefinition.from_dict(data["definition"]),
            ContentIdentity.parse_uri(data["package_set_plan_identity"]),
            BlobRef.from_dict(data["ownership_index"]),
            BlobRef.from_dict(data["dependency_index"]),
            ContentIdentity.parse_uri(data["packager_identity"]),
            ContentIdentity.parse_uri(data["tool_identity"]),
        )


@dataclass(frozen=True, slots=True)
class RetainedUnitPackageSetPlan:
    """Bind local graph definitions before unit plans or produced artifact IDs."""

    retained_plan_identity: ContentIdentity
    receipt_identity: ContentIdentity
    target_identity: ContentIdentity
    graph_index: BlobRef
    tree_index: BlobRef
    provider_policy_identity: ContentIdentity
    packager_identity: ContentIdentity
    tool_identity: ContentIdentity
    max_concurrent_units: int
    max_staging_bytes: int
    max_transport_bytes: int
    max_archive_bytes: int
    max_total_archive_bytes: int

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-set-plan@1"
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "retained_plan_identity",
        "receipt_identity",
        "target_identity",
        "provider_policy_identity",
        "packager_identity",
        "tool_identity",
    )
    BUDGET_FIELDS: ClassVar[tuple[str, ...]] = (
        "max_concurrent_units",
        "max_staging_bytes",
        "max_transport_bytes",
        "max_archive_bytes",
        "max_total_archive_bytes",
    )

    def __post_init__(self):
        _identities(self, self.IDENTITY_FIELDS)
        for ref in (self.graph_index, self.tree_index):
            _json_reference(ref)
            if ref.size > MAX_INDEX_BYTES:
                raise ValueError("retained.unit.bounded-set-index-required")
        for name in self.BUDGET_FIELDS:
            value = getattr(self, name)
            maximum = 1_000_000 if name == "max_concurrent_units" else 2**63 - 1
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("retained.unit.finite-set-resource-policy-required")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "release_authorized": False,
            **{name: getattr(self, name).uri for name in self.IDENTITY_FIELDS},
            "graph_index": self.graph_index.to_dict(),
            "tree_index": self.tree_index.to_dict(),
            **{name: getattr(self, name) for name in self.BUDGET_FIELDS},
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "authority_mode",
                    "release_authorized",
                    "graph_index",
                    "tree_index",
                    *cls.IDENTITY_FIELDS,
                    *cls.BUDGET_FIELDS,
                }
            ),
        )
        if (
            data["authority_mode"] != "retained-project"
            or data["release_authorized"] is not False
        ):
            raise ValueError("retained.unit.original-authority-required")
        return cls(
            *(
                ContentIdentity.parse_uri(data[name])
                for name in cls.IDENTITY_FIELDS[:3]
            ),
            BlobRef.from_dict(data["graph_index"]),
            BlobRef.from_dict(data["tree_index"]),
            *(
                ContentIdentity.parse_uri(data[name])
                for name in cls.IDENTITY_FIELDS[3:]
            ),
            *(data[name] for name in cls.BUDGET_FIELDS),
        )


@dataclass(frozen=True, slots=True)
class RetainedUnitPackageResult:
    unit_plan_identity: ContentIdentity
    package_set_plan_identity: ContentIdentity
    definition_identity: ContentIdentity
    graph_identity: ContentIdentity
    packager_identity: ContentIdentity
    tool_identity: ContentIdentity
    archive: BlobRef

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-result@1"
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "unit_plan_identity",
        "package_set_plan_identity",
        "definition_identity",
        "graph_identity",
        "packager_identity",
        "tool_identity",
    )

    def __post_init__(self):
        _identities(self, self.IDENTITY_FIELDS)
        if (
            not isinstance(self.archive, BlobRef)
            or self.archive.size < 1
            or self.archive.size >= 2**63
            or self.archive.media_type != "application/vnd.freebsd.pkg"
        ):
            raise ValueError("retained.unit.exact-native-archive-reference-required")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "release_authorized": False,
            **{name: getattr(self, name).uri for name in self.IDENTITY_FIELDS},
            "archive": self.archive.to_dict(),
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "authority_mode",
                    "release_authorized",
                    "archive",
                    *cls.IDENTITY_FIELDS,
                }
            ),
        )
        if (
            data["authority_mode"] != "retained-project"
            or data["release_authorized"] is not False
        ):
            raise ValueError("retained.unit.original-authority-required")
        return cls(
            *(ContentIdentity.parse_uri(data[name]) for name in cls.IDENTITY_FIELDS),
            BlobRef.from_dict(data["archive"]),
        )

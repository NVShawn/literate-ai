"""Reviewed registry replacement relations, including external Cargo consumers.

A declaration is not a resolved graph or a dependency exemption. Native receiving
must join each edge to its exact manifest declaration, lock and Cargo package
identity under current input custody. Nothing here reads, fetches or executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import (
    contract_fields,
    fail,
    fields,
    list_value,
    object_value,
    string_value,
)
from .cargo_workspace import CargoWorkspaceExpectation
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_path


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    text = string_value(value, label, max_length=maximum)
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        fail(label, "requires bounded printable text")
    return text


def _root(value: Any, label: str) -> str:
    text = _text(value, label, 128)
    if text != ".":
        canonical_relative_posix_path(text, label=label)
    return text


def _source(value: Any, label: str) -> str:
    text = _text(value, label)
    # Source IDs are opaque native Cargo values, including Git revision fragments.
    # Credentials belong to the operator, never to a reviewed package identity.
    try:
        parsed = urlsplit(text)
    except ValueError:
        fail(label, "requires a native source identity")
    if parsed.password is not None or (
        parsed.username is not None and parsed.scheme not in {"ssh", "git+ssh"}
    ):
        fail(label, "source identity cannot embed credentials")
    return text


@dataclass(frozen=True, slots=True)
class CargoWorkspacePatchConsumer:
    root: str

    def __post_init__(self) -> None:
        _root(self.root, "CargoWorkspacePatchConsumer.root")

    @property
    def key(self):
        return "workspace", self.root, "", ""

    def to_dict(self):
        return {"kind": "workspace", "root": self.root}


@dataclass(frozen=True, slots=True)
class CargoExternalPatchConsumer:
    source: str
    name: str
    version: str

    def __post_init__(self) -> None:
        _source(self.source, "CargoExternalPatchConsumer.source")
        _text(self.name, "CargoExternalPatchConsumer.name", 256)
        _text(self.version, "CargoExternalPatchConsumer.version", 128)

    @property
    def key(self):
        return "external", self.source, self.name, self.version

    def to_dict(self):
        return dict(
            kind="external", source=self.source, name=self.name, version=self.version
        )


def _consumer(value):
    data = object_value(value, "CargoPatchConsumer")
    if data.get("kind") == "workspace":
        data = fields(
            data,
            path="CargoWorkspacePatchConsumer",
            required=frozenset({"kind", "root"}),
        )
        return CargoWorkspacePatchConsumer(data["root"])
    if data.get("kind") == "external":
        data = fields(
            data,
            path="CargoExternalPatchConsumer",
            required=frozenset({"kind", "source", "name", "version"}),
        )
        return CargoExternalPatchConsumer(data["source"], data["name"], data["version"])
    fail(
        "CargoPatchConsumer.kind", "requires an explicit workspace or external consumer"
    )


@dataclass(frozen=True, slots=True)
class CargoPatchedDependencyExpectation:
    consumer: CargoWorkspacePatchConsumer | CargoExternalPatchConsumer
    alias: str
    resolved_name: str
    kind: str | None
    target: str | None
    features: tuple[str, ...]
    uses_default_features: bool
    optional: bool
    version_requirement: str

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "consumer",
            "alias",
            "resolved_name",
            "kind",
            "target",
            "features",
            "uses_default_features",
            "optional",
            "version_requirement",
        }
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.consumer, (CargoWorkspacePatchConsumer, CargoExternalPatchConsumer)
        ):
            fail(self.__class__.__name__, "requires an exact typed consumer")
        for name in ("alias", "resolved_name"):
            _text(getattr(self, name), self.__class__.__name__ + "." + name, 256)
        if self.kind not in (None, "dev", "build"):
            fail(self.__class__.__name__ + ".kind", "unsupported dependency kind")
        if self.target is not None:
            _text(self.target, self.__class__.__name__ + ".target")
        _text(
            self.version_requirement,
            self.__class__.__name__ + ".version_requirement",
            1024,
        )
        if (
            type(self.uses_default_features) is not bool
            or type(self.optional) is not bool
        ):
            fail(self.__class__.__name__, "requires explicit dependency switches")
        if not isinstance(self.features, tuple) or len(self.features) > 4096:
            fail(self.__class__.__name__ + ".features", "requires bounded features")
        for feature in self.features:
            _text(feature, self.__class__.__name__ + ".feature", 256)
        if self.features != tuple(sorted(set(self.features))):
            fail(
                self.__class__.__name__ + ".features",
                "requires ordered distinct features",
            )

    @property
    def key(self):
        return (*self.consumer.key, self.alias, self.kind or "", self.target or "")

    def to_dict(self):
        return dict(
            consumer=self.consumer.to_dict(),
            alias=self.alias,
            resolved_name=self.resolved_name,
            kind=self.kind,
            target=self.target,
            features=list(self.features),
            uses_default_features=self.uses_default_features,
            optional=self.optional,
            version_requirement=self.version_requirement,
        )

    @classmethod
    def from_dict(cls, value: Any):
        data = fields(value, path=cls.__name__, required=cls.FIELDS)
        features = list_value(data["features"], cls.__name__ + ".features")
        if len(features) > 4096:
            fail(cls.__name__, "feature count exceeds allowance")
        return cls(
            _consumer(data["consumer"]),
            data["alias"],
            data["resolved_name"],
            data["kind"],
            data["target"],
            tuple(features),
            data["uses_default_features"],
            data["optional"],
            data["version_requirement"],
        )


@dataclass(frozen=True, slots=True)
class CargoRegistryPatchExpectation:
    registry_key: str
    patch_name: str
    registry_source: str
    package_name: str
    version: str
    provider_root: str
    edges: tuple[CargoPatchedDependencyExpectation, ...]

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "registry_key",
            "patch_name",
            "registry_source",
            "package_name",
            "version",
            "provider_root",
            "edges",
        }
    )

    def __post_init__(self) -> None:
        _source(self.registry_key, self.__class__.__name__ + ".registry_key")
        _source(self.registry_source, self.__class__.__name__ + ".registry_source")
        for name in ("patch_name", "package_name"):
            _text(getattr(self, name), self.__class__.__name__ + "." + name, 256)
        _text(self.version, self.__class__.__name__ + ".version", 128)
        _root(self.provider_root, self.__class__.__name__ + ".provider_root")
        if (
            not isinstance(self.edges, tuple)
            or not 1 <= len(self.edges) <= 4096
            or any(
                not isinstance(x, CargoPatchedDependencyExpectation) for x in self.edges
            )
        ):
            fail(self.__class__.__name__, "requires bounded typed affected edges")
        keys = tuple(edge.key for edge in self.edges)
        if keys != tuple(sorted(set(keys))):
            fail(self.__class__.__name__, "requires ordered distinct affected edges")

    @property
    def key(self):
        return self.registry_key, self.patch_name

    def to_dict(self):
        return dict(
            registry_key=self.registry_key,
            patch_name=self.patch_name,
            registry_source=self.registry_source,
            package_name=self.package_name,
            version=self.version,
            provider_root=self.provider_root,
            edges=[x.to_dict() for x in self.edges],
        )

    @classmethod
    def from_dict(cls, value: Any):
        data = fields(value, path=cls.__name__, required=cls.FIELDS)
        edges = list_value(data["edges"], cls.__name__ + ".edges")
        if not 1 <= len(edges) <= 4096:
            fail(cls.__name__, "affected edge count exceeds allowance")
        return cls(
            data["registry_key"],
            data["patch_name"],
            data["registry_source"],
            data["package_name"],
            data["version"],
            data["provider_root"],
            tuple(CargoPatchedDependencyExpectation.from_dict(x) for x in edges),
        )


@dataclass(frozen=True, slots=True)
class CargoWorkspaceExpectationV2:
    common: CargoWorkspaceExpectation
    registry_patches: tuple[CargoRegistryPatchExpectation, ...]

    SCHEMA: ClassVar[str] = "literate-ai/cargo-workspace-expectation@2"
    COMMON_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "packages",
            "dependencies",
            "members",
            "default_members",
            "root_package",
            "output_directory",
        }
    )

    def __post_init__(self) -> None:
        if type(self.common) is not CargoWorkspaceExpectation:
            fail(self.__class__.__name__, "requires exact common Cargo graph values")
        if len(self.common.packages) > 4096 or len(self.common.dependencies) > 4096:
            fail(self.__class__.__name__, "graph allowance exceeded")
        if (
            not isinstance(self.registry_patches, tuple)
            or len(self.registry_patches) > 4096
            or any(
                not isinstance(x, CargoRegistryPatchExpectation)
                for x in self.registry_patches
            )
        ):
            fail(self.__class__.__name__, "requires bounded typed patches")
        keys = tuple(x.key for x in self.registry_patches)
        if keys != tuple(sorted(set(keys))):
            fail(
                self.__class__.__name__, "requires ordered distinct patch declarations"
            )
        if sum(len(x.edges) for x in self.registry_patches) > 4096:
            fail(self.__class__.__name__, "affected edge allowance exceeded")
        packages = {p.root: p for p in self.common.packages}
        path_edges = {
            (d.consumer, d.alias, d.kind or "", d.target or "")
            for d in self.common.dependencies
        }
        explained = set()
        replacements = set()
        for patch in self.registry_patches:
            provider = packages.get(patch.provider_root)
            if provider is None or (provider.name, provider.version) != (
                patch.package_name,
                patch.version,
            ):
                fail(
                    self.__class__.__name__,
                    "patch provider differs from the exact local package",
                )
            replacement = patch.registry_source, patch.package_name, patch.version
            if replacement in replacements:
                fail(
                    self.__class__.__name__,
                    "ambiguous replacement for one source package",
                )
            replacements.add(replacement)
            for edge in patch.edges:
                if edge.key in explained:
                    fail(
                        self.__class__.__name__,
                        "one dependency edge has multiple patch explanations",
                    )
                explained.add(edge.key)
                if isinstance(edge.consumer, CargoWorkspacePatchConsumer):
                    if (
                        edge.consumer.root not in packages
                        or (
                            edge.consumer.root,
                            edge.alias,
                            edge.kind or "",
                            edge.target or "",
                        )
                        in path_edges
                    ):
                        fail(
                            self.__class__.__name__,
                            "registry patches cannot invent path declarations",
                        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self):
        result = self.common.to_dict()
        result.update(
            schema=self.SCHEMA,
            registry_patches=[x.to_dict() for x in self.registry_patches],
        )
        return result

    @classmethod
    def from_dict(cls, value: Any):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=cls.COMMON_FIELDS | {"registry_patches"},
        )
        patches = list_value(
            data["registry_patches"], cls.__name__ + ".registry_patches"
        )
        if len(patches) > 4096:
            fail(cls.__name__, "patch count exceeds allowance")
        # Reject the aggregate raw shape before constructing any edge DTOs.
        # Per-patch limits alone permit 4096 squared conversions before refusal.
        edge_count = 0
        for patch in patches:
            raw_patch = fields(
                patch,
                path="CargoRegistryPatchExpectation",
                required=CargoRegistryPatchExpectation.FIELDS,
            )
            edges = list_value(
                raw_patch["edges"], "CargoRegistryPatchExpectation.edges"
            )
            edge_count += len(edges)
            if edge_count > 4096:
                fail(cls.__name__, "aggregate affected edge count exceeds allowance")
        common = CargoWorkspaceExpectation.from_dict(
            {key: data[key] for key in cls.COMMON_FIELDS}
        )
        return cls(
            common, tuple(CargoRegistryPatchExpectation.from_dict(x) for x in patches)
        )

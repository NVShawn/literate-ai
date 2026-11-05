"""Reviewed Rust verifier inputs; these values grant no execution permission.

The independent-acceptance owner must reopen current verifier authority, capture
the complete declared roots, bind native tools and compare locked Cargo resolution.
A library candidate or decoded declaration cannot construct that live owner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import bool_value, fail, fields, int_value, list_value, string_value
from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_path
from .retained_cargo_custody import RetainedCargoCustodyPolicy
from .retained_cargo_patches import _source as native_cargo_source
from .versioning import semantic_version

MAX_DIRECT_DEPENDENCIES = 128
MAX_SELECTED_FEATURES = 4096
MAX_LOCKFILE_BYTES = 16 * 1024 * 1024
MAX_ORACLE_BYTES = 1024 * 1024
# These structural guards cannot narrow any record that fits the oracle byte cap.
MAX_RESOLVED_PACKAGES = MAX_ORACLE_BYTES // 2
MAX_RESOLVED_EDGES = MAX_ORACLE_BYTES // 2
_ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
_PACKAGE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}\Z")


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    value = string_value(value, label, max_length=maximum)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        fail(label, "requires bounded printable text")
    return value


def _document_budget(value: Any, label: str) -> None:
    """Charge the existing oracle allowance before building nested records.

    The actual file reader bounds raw bytes first. Incremental encoding also
    prevents a programmatic decoder call from constructing a second unbounded
    typed graph; no encoded aggregate document is retained here.
    """
    used = 0
    try:
        for chunk in json.JSONEncoder(
            ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).iterencode(value):
            if len(chunk) > MAX_ORACLE_BYTES:
                fail(label, "declaration exceeds the oracle byte allowance")
            used += len(chunk.encode("utf-8"))
            if used > MAX_ORACLE_BYTES:
                fail(label, "declaration exceeds the oracle byte allowance")
    except (TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError("Rust verifier declaration is not bounded JSON") from exc


def rust_dependency_alias(value: Any, label: str) -> str:
    value = _text(value, label, 64)
    if _ALIAS.fullmatch(value) is None:
        fail(label, "requires one Cargo dependency alias")
    return value


def rust_registry_source(value: Any, label: str) -> str:
    value = _text(value, label)
    prefix = next(
        (prefix for prefix in ("registry+", "sparse+") if value.startswith(prefix)),
        None,
    )
    if prefix is None:
        fail(label, "requires an exact registry source identity")
    try:
        url = urlsplit(value[len(prefix) :])
        invalid = (
            url.scheme not in {"https", "http", "file", "ssh"}
            or url.password is not None
            or (url.username is not None and url.scheme != "ssh")
            or bool(url.query or url.fragment)
            or (url.scheme != "file" and not url.hostname)
            or (url.scheme == "file" and not url.path.startswith("/"))
        )
    except ValueError:
        invalid = True
    if invalid:
        fail(label, "requires a credential-free fixed registry identity")
    return value


def rust_resolved_source(value: Any, label: str) -> str | None:
    """Keep the library's exact owned-path and locked native Git resolution.

    Extra harness declarations remain registry-only. A source coordinate is
    data: native observation still has to bind every manifest to a captured
    owner and compare the complete graph. No source URL grants traversal.
    """
    if value is None:
        return None
    value = native_cargo_source(value, label)
    if value.startswith(("registry+", "sparse+")):
        return rust_registry_source(value, label)
    if (
        not value.startswith("git+")
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", urlsplit(value).fragment) is None
    ):
        fail(label, "requires an exact locked native Cargo source identity")
    return value


def rust_features(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > MAX_SELECTED_FEATURES:
        fail(label, "requires a bounded tuple of selected features")
    for feature in value:
        _text(feature, label, 256)
    if value != tuple(sorted(set(value))):
        fail(label, "requires uniquely sorted selected features")
    return value


@dataclass(frozen=True, slots=True)
class RustHarnessRegistryDependency:
    alias: str
    package: str
    version: str
    registry: str
    registry_source: str
    default_features: bool
    features: tuple[str, ...]

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "alias",
            "package",
            "version",
            "registry",
            "registry_source",
            "default_features",
            "features",
        }
    )

    def __post_init__(self) -> None:
        rust_dependency_alias(self.alias, "RustHarnessRegistryDependency.alias")
        package = _text(self.package, "RustHarnessRegistryDependency.package", 64)
        if _PACKAGE.fullmatch(package) is None:
            fail("RustHarnessRegistryDependency.package", "requires one package name")
        semantic_version(self.version, "RustHarnessRegistryDependency.version")
        rust_dependency_alias(self.registry, "RustHarnessRegistryDependency.registry")
        rust_registry_source(
            self.registry_source, "RustHarnessRegistryDependency.registry_source"
        )
        bool_value(
            self.default_features, "RustHarnessRegistryDependency.default_features"
        )
        rust_features(self.features, "RustHarnessRegistryDependency.features")

    def to_dict(self) -> dict[str, Any]:
        return {
            **{name: getattr(self, name) for name in self.FIELDS - {"features"}},
            "features": list(self.features),
        }

    @classmethod
    def from_dict(cls, value: Any) -> RustHarnessRegistryDependency:
        data = fields(value, path=cls.__name__, required=cls.FIELDS)
        features = list_value(data["features"], cls.__name__ + ".features")
        if len(features) > MAX_SELECTED_FEATURES:
            fail(cls.__name__ + ".features", "selected feature limit exceeded")
        return cls(
            data["alias"],
            data["package"],
            data["version"],
            data["registry"],
            data["registry_source"],
            data["default_features"],
            tuple(features),
        )


@dataclass(frozen=True, slots=True)
class RustHarnessLockfile:
    file: str
    identity: ContentIdentity
    size: int

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.file, label="RustHarnessLockfile.file")
        if "/" in self.file:
            fail("RustHarnessLockfile.file", "requires one sibling lockfile")
        if type(self.identity) is not ContentIdentity:
            fail("RustHarnessLockfile.identity", "requires an exact content identity")
        int_value(
            self.size, "RustHarnessLockfile.size", minimum=1, maximum=MAX_LOCKFILE_BYTES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "identity": self.identity.to_dict(),
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, value: Any) -> RustHarnessLockfile:
        data = fields(
            value, path=cls.__name__, required=frozenset({"file", "identity", "size"})
        )
        return cls(
            data["file"], ContentIdentity.from_dict(data["identity"]), data["size"]
        )


@dataclass(frozen=True, slots=True)
class RustHarnessInputSources:
    """Portable verifier-relative roots and exact complete streaming custody.

    Paths resolve only below the current verifier authority directory. Physical
    overlap, topology, unchanged bytes and current authority remain owner checks.
    No root is inferred from ambient Cargo configuration or a candidate manifest.
    """

    roots: tuple[tuple[str, str], ...]
    custody: RetainedCargoCustodyPolicy
    owners_identity: ContentIdentity
    maximum_metadata_bytes: int

    def __post_init__(self) -> None:
        if type(self.custody) is not RetainedCargoCustodyPolicy:
            fail("RustHarnessInputSources.custody", "requires exact typed custody")
        if (
            not isinstance(self.roots, tuple)
            or len(self.roots) != len(self.custody.owners)
            or any(not isinstance(row, tuple) or len(row) != 2 for row in self.roots)
        ):
            fail(
                "RustHarnessInputSources.roots", "requires the exact reviewed owner set"
            )
        if tuple(scope for scope, _ in self.roots) != tuple(
            owner.scope for owner in self.custody.owners
        ):
            fail("RustHarnessInputSources.roots", "owner scopes differ from custody")
        for _, path in self.roots:
            canonical_relative_posix_path(path, label="RustHarnessInputSources.root")
        if len({path for _, path in self.roots}) != len(self.roots):
            fail("RustHarnessInputSources.roots", "owner roots must be distinct")
        if type(self.owners_identity) is not ContentIdentity:
            fail("RustHarnessInputSources.owners_identity", "requires exact identity")
        int_value(
            self.maximum_metadata_bytes,
            "RustHarnessInputSources.maximum_metadata_bytes",
            minimum=16 * 1024 * 1024 * len(self.roots),
            maximum=16 * 1024**3,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": [{"scope": scope, "path": path} for scope, path in self.roots],
            "custody": self.custody.to_dict(),
            "owners_identity": self.owners_identity.to_dict(),
            "maximum_metadata_bytes": self.maximum_metadata_bytes,
        }

    @classmethod
    def from_dict(cls, value: Any) -> RustHarnessInputSources:
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset(
                {"roots", "custody", "owners_identity", "maximum_metadata_bytes"}
            ),
        )
        roots = list_value(data["roots"], cls.__name__ + ".roots")
        if not 2 <= len(roots) <= RetainedCargoCustodyPolicy.MAXIMUM_OWNERS:
            fail(cls.__name__ + ".roots", "owner count exceeds its finite limit")
        selected = []
        for value in roots:
            row = fields(
                value,
                path=cls.__name__ + ".root",
                required=frozenset({"scope", "path"}),
            )
            selected.append((row["scope"], row["path"]))
        return cls(
            tuple(selected),
            RetainedCargoCustodyPolicy.from_dict(data["custody"]),
            ContentIdentity.from_dict(data["owners_identity"]),
            data["maximum_metadata_bytes"],
        )


@dataclass(frozen=True, slots=True)
class RustHarnessResolvedDependency:
    alias: str
    provider: ContentIdentity
    kind: str | None
    target: str | None

    def __post_init__(self) -> None:
        rust_dependency_alias(self.alias, "RustHarnessResolvedDependency.alias")
        if type(self.provider) is not ContentIdentity:
            fail("RustHarnessResolvedDependency.provider", "requires exact identity")
        if self.kind not in {None, "build", "dev"}:
            fail("RustHarnessResolvedDependency.kind", "requires a native Cargo kind")
        if self.target is not None:
            _text(self.target, "RustHarnessResolvedDependency.target", 1024)

    @property
    def key(self):
        return self.alias, self.provider.uri, self.kind or "", self.target or ""

    def to_dict(self):
        return {
            "alias": self.alias,
            "provider": self.provider.to_dict(),
            "kind": self.kind,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset({"alias", "provider", "kind", "target"}),
        )
        return cls(
            data["alias"],
            ContentIdentity.from_dict(data["provider"]),
            data["kind"],
            data["target"],
        )


@dataclass(frozen=True, slots=True)
class RustHarnessResolvedPackage:
    """Path-portable projection of one actually resolved Cargo package.

    The observer must independently map manifest paths to the sealed library,
    fresh owned harness or a complete current provisioned input owner. Native
    Cargo IDs alone are not path custody or evidence of selected features.
    """

    scope: str
    manifest: str
    name: str
    version: str
    source: str | None
    features: tuple[str, ...]
    dependencies: tuple[RustHarnessResolvedDependency, ...]

    def __post_init__(self) -> None:
        scope = _text(self.scope, "RustHarnessResolvedPackage.scope", 256)
        if scope not in {"harness", "library"} and not scope.startswith("input:"):
            fail("RustHarnessResolvedPackage.scope", "requires an explicit owner")
        canonical_relative_posix_path(
            self.manifest, label="RustHarnessResolvedPackage.manifest"
        )
        if self.manifest.rsplit("/", 1)[-1] != "Cargo.toml":
            fail("RustHarnessResolvedPackage.manifest", "requires a Cargo manifest")
        name = _text(self.name, "RustHarnessResolvedPackage.name", 64)
        if _PACKAGE.fullmatch(name) is None:
            fail("RustHarnessResolvedPackage.name", "requires one package name")
        semantic_version(self.version, "RustHarnessResolvedPackage.version")
        if scope in {"harness", "library"}:
            if self.source is not None:
                fail(
                    "RustHarnessResolvedPackage.source", "sealed local package required"
                )
        else:
            rust_resolved_source(self.source, "RustHarnessResolvedPackage.source")
        rust_features(self.features, "RustHarnessResolvedPackage.features")
        if (
            not isinstance(self.dependencies, tuple)
            or len(self.dependencies) > MAX_RESOLVED_PACKAGES
            or any(
                type(row) is not RustHarnessResolvedDependency
                for row in self.dependencies
            )
        ):
            fail(
                "RustHarnessResolvedPackage.dependencies",
                "bounded typed edges required",
            )
        keys = tuple(row.key for row in self.dependencies)
        if keys != tuple(sorted(set(keys))):
            fail(
                "RustHarnessResolvedPackage.dependencies",
                "uniquely sorted edges required",
            )

    @property
    def identity(self):
        return canonical_identity(
            {
                "scope": self.scope,
                "manifest": self.manifest,
                "name": self.name,
                "version": self.version,
                "source": self.source,
            }
        )

    def to_dict(self):
        return {
            "scope": self.scope,
            "manifest": self.manifest,
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "features": list(self.features),
            "dependencies": [row.to_dict() for row in self.dependencies],
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset(
                {
                    "scope",
                    "manifest",
                    "name",
                    "version",
                    "source",
                    "features",
                    "dependencies",
                }
            ),
        )
        features = list_value(data["features"], cls.__name__ + ".features")
        dependencies = list_value(data["dependencies"], cls.__name__ + ".dependencies")
        if (
            len(features) > MAX_SELECTED_FEATURES
            or len(dependencies) > MAX_RESOLVED_PACKAGES
        ):
            fail(cls.__name__, "resolved declaration limit exceeded")
        return cls(
            data["scope"],
            data["manifest"],
            data["name"],
            data["version"],
            data["source"],
            tuple(features),
            tuple(RustHarnessResolvedDependency.from_dict(row) for row in dependencies),
        )


@dataclass(frozen=True, slots=True)
class RustHarnessResolution:
    root: ContentIdentity
    library: ContentIdentity
    packages: tuple[RustHarnessResolvedPackage, ...]

    def __post_init__(self) -> None:
        if (
            type(self.root) is not ContentIdentity
            or type(self.library) is not ContentIdentity
        ):
            fail("RustHarnessResolution", "requires exact selected package identities")
        if (
            not isinstance(self.packages, tuple)
            or not 2 <= len(self.packages) <= MAX_RESOLVED_PACKAGES
            or any(type(row) is not RustHarnessResolvedPackage for row in self.packages)
        ):
            fail("RustHarnessResolution.packages", "requires bounded typed packages")
        identities = tuple(row.identity for row in self.packages)
        if identities != tuple(sorted(set(identities), key=lambda row: row.uri)):
            fail("RustHarnessResolution.packages", "requires uniquely sorted packages")
        selected = {row.identity: row for row in self.packages}
        if self.root not in selected or self.library not in selected:
            fail("RustHarnessResolution", "selected package is absent")
        harness = selected[self.root]
        if (
            harness.scope != "harness"
            or harness.manifest != "Cargo.toml"
            or harness.name != "litai-library-acceptance"
            or harness.version != "0.0.0"
            or sum(row.scope == "harness" for row in self.packages) != 1
            or selected[self.library].scope != "library"
        ):
            fail(
                "RustHarnessResolution", "exact verifier and library selection required"
            )
        if sum(len(row.features) for row in self.packages) > MAX_SELECTED_FEATURES:
            fail("RustHarnessResolution", "aggregate selected feature limit exceeded")
        if sum(len(row.dependencies) for row in self.packages) > MAX_RESOLVED_EDGES:
            fail("RustHarnessResolution", "aggregate resolved edge limit exceeded")
        if any(
            edge.provider not in selected
            for row in self.packages
            for edge in row.dependencies
        ):
            fail("RustHarnessResolution", "resolved dependency provider is absent")

    def to_dict(self):
        return {
            "root": self.root.to_dict(),
            "library": self.library.to_dict(),
            "packages": [row.to_dict() for row in self.packages],
        }

    @classmethod
    def from_dict(cls, value):
        _document_budget(value, cls.__name__)
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset({"root", "library", "packages"}),
        )
        packages = list_value(data["packages"], cls.__name__ + ".packages")
        if not 2 <= len(packages) <= MAX_RESOLVED_PACKAGES:
            fail(cls.__name__ + ".packages", "resolved package limit exceeded")
        feature_count = edge_count = 0
        for row in packages:
            package = fields(
                row,
                path="RustHarnessResolvedPackage",
                required=frozenset(
                    {
                        "scope",
                        "manifest",
                        "name",
                        "version",
                        "source",
                        "features",
                        "dependencies",
                    }
                ),
            )
            feature_count += len(
                list_value(package["features"], "RustHarnessResolvedPackage.features")
            )
            edge_count += len(
                list_value(
                    package["dependencies"], "RustHarnessResolvedPackage.dependencies"
                )
            )
            if feature_count > MAX_SELECTED_FEATURES or edge_count > MAX_RESOLVED_EDGES:
                fail(cls.__name__, "aggregate resolved declaration limit exceeded")
        return cls(
            ContentIdentity.from_dict(data["root"]),
            ContentIdentity.from_dict(data["library"]),
            tuple(RustHarnessResolvedPackage.from_dict(row) for row in packages),
        )


@dataclass(frozen=True, slots=True)
class RustLibraryHarnessDependencies:
    dependencies: tuple[RustHarnessRegistryDependency, ...]
    lockfile: RustHarnessLockfile
    inputs: RustHarnessInputSources
    resolution: RustHarnessResolution
    cargo_toolchain: ContentIdentity
    rust_toolchain: ContentIdentity
    timeout_seconds: int
    stdout_limit_bytes: int
    stderr_limit_bytes: int

    SCHEMA: ClassVar[str] = "literate-ai/rust-library-harness-dependencies@1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "dependencies",
            "lockfile",
            "inputs",
            "resolution",
            "cargo_toolchain",
            "rust_toolchain",
            "timeout_seconds",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        }
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.dependencies, tuple)
            or len(self.dependencies) > MAX_DIRECT_DEPENDENCIES
            or any(
                type(row) is not RustHarnessRegistryDependency
                for row in self.dependencies
            )
        ):
            fail(
                "RustLibraryHarnessDependencies.dependencies",
                "bounded exact registry declarations required",
            )
        aliases = tuple(row.alias for row in self.dependencies)
        if aliases != tuple(sorted(set(aliases))) or len(
            {alias.replace("-", "_") for alias in aliases}
        ) != len(aliases):
            fail(
                "RustLibraryHarnessDependencies.dependencies",
                "unique canonical Rust aliases required",
            )
        if sum(len(row.features) for row in self.dependencies) > MAX_SELECTED_FEATURES:
            fail(
                "RustLibraryHarnessDependencies.dependencies",
                "aggregate selected feature limit exceeded",
            )
        if (
            type(self.lockfile) is not RustHarnessLockfile
            or type(self.inputs) is not RustHarnessInputSources
            or type(self.resolution) is not RustHarnessResolution
        ):
            fail(
                "RustLibraryHarnessDependencies",
                "exact lock, input and resolution declarations required",
            )
        registries = {}
        for dependency in self.dependencies:
            previous = registries.setdefault(
                dependency.registry, dependency.registry_source
            )
            if previous != dependency.registry_source:
                fail(
                    "RustLibraryHarnessDependencies.dependencies",
                    "registry name has competing source identities",
                )
        owner_scopes = {"input:" + scope for scope, _ in self.inputs.roots}
        if any(
            row.scope not in {"harness", "library"} | owner_scopes
            for row in self.resolution.packages
        ):
            fail(
                "RustLibraryHarnessDependencies.resolution",
                "resolved package has no declared source owner",
            )
        for name in ("cargo_toolchain", "rust_toolchain"):
            if type(getattr(self, name)) is not ContentIdentity:
                fail(
                    "RustLibraryHarnessDependencies." + name,
                    "exact native tool identity required",
                )
        int_value(
            self.timeout_seconds,
            "RustLibraryHarnessDependencies.timeout_seconds",
            minimum=1,
            maximum=300,
        )
        for name in ("stdout_limit_bytes", "stderr_limit_bytes"):
            int_value(
                getattr(self, name),
                "RustLibraryHarnessDependencies." + name,
                minimum=1,
                maximum=16 * 1024 * 1024,
            )

    def require_library_alias(self, alias: str) -> None:
        """Join declared root edges; no native graph or currentness is inferred."""
        rust_dependency_alias(alias, "RustLibraryHarnessDependencies.library_alias")
        library_alias = alias.replace("-", "_")
        if library_alias in {row.alias.replace("-", "_") for row in self.dependencies}:
            fail(
                "RustLibraryHarnessDependencies.dependencies",
                "dependency alias collides with the qualified library",
            )
        selected = {row.identity: row for row in self.resolution.packages}
        root = selected[self.resolution.root]
        edges = {row.alias: row for row in root.dependencies}
        if (
            len(edges) != len(root.dependencies)
            or set(edges)
            != {
                library_alias,
                *(row.alias.replace("-", "_") for row in self.dependencies),
            }
            or edges[library_alias].provider != self.resolution.library
            or any(
                edge.kind is not None or edge.target is not None
                for edge in root.dependencies
            )
        ):
            fail(
                "RustLibraryHarnessDependencies.resolution",
                "exact generated verifier dependency edges required",
            )
        for dependency in self.dependencies:
            package = selected[edges[dependency.alias.replace("-", "_")].provider]
            if (
                package.name != dependency.package
                or package.version != dependency.version
                or package.source != dependency.registry_source
                or not set(dependency.features) <= set(package.features)
            ):
                fail(
                    "RustLibraryHarnessDependencies.resolution",
                    "reviewed registry dependency resolution differs",
                )

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "dependencies": [row.to_dict() for row in self.dependencies],
            "lockfile": self.lockfile.to_dict(),
            "inputs": self.inputs.to_dict(),
            "resolution": self.resolution.to_dict(),
            "cargo_toolchain": self.cargo_toolchain.to_dict(),
            "rust_toolchain": self.rust_toolchain.to_dict(),
            "timeout_seconds": self.timeout_seconds,
            "stdout_limit_bytes": self.stdout_limit_bytes,
            "stderr_limit_bytes": self.stderr_limit_bytes,
        }

    @classmethod
    def from_dict(cls, value):
        _document_budget(value, cls.__name__)
        data = fields(value, path=cls.__name__, required=cls.FIELDS | {"schema"})
        if data["schema"] != cls.SCHEMA:
            fail(cls.__name__, "exact Rust verifier declaration schema required")
        dependencies = list_value(data["dependencies"], cls.__name__ + ".dependencies")
        if len(dependencies) > MAX_DIRECT_DEPENDENCIES:
            fail(cls.__name__ + ".dependencies", "direct dependency limit exceeded")
        # Charge direct feature rows before constructing another nested declaration.
        selected_features = 0
        for row in dependencies:
            declaration = fields(
                row,
                path="RustHarnessRegistryDependency",
                required=RustHarnessRegistryDependency.FIELDS,
            )
            selected_features += len(
                list_value(
                    declaration["features"], "RustHarnessRegistryDependency.features"
                )
            )
            if selected_features > MAX_SELECTED_FEATURES:
                fail(
                    cls.__name__ + ".dependencies",
                    "aggregate selected feature limit exceeded",
                )
        return cls(
            tuple(RustHarnessRegistryDependency.from_dict(row) for row in dependencies),
            RustHarnessLockfile.from_dict(data["lockfile"]),
            RustHarnessInputSources.from_dict(data["inputs"]),
            RustHarnessResolution.from_dict(data["resolution"]),
            ContentIdentity.from_dict(data["cargo_toolchain"]),
            ContentIdentity.from_dict(data["rust_toolchain"]),
            data["timeout_seconds"],
            data["stdout_limit_bytes"],
            data["stderr_limit_bytes"],
        )

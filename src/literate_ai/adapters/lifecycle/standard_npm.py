"""Typed npm target authority for the local Standard lifecycle."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.contracts import ContentIdentity, SemanticVersion, canonical_identity
from literate_ai.storage import canonical_json_bytes

_DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)
_UNSUPPORTED_ROOT_FIELDS = (
    "bundleDependencies",
    "bundledDependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
    "scripts",
    "workspaces",
)
_MAX_NPM_DOCUMENT_BYTES = 16 * 1024 * 1024


class StandardNpmLifecycleError(RuntimeError):
    """A generated npm authority or its installed projection is not exact."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _package_path(value: str, *, name: str, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError(f"npm {label} path must be nonempty")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.name != name
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValueError(f"npm {label} must be a canonical relative {name} path")
    return path


@dataclass(frozen=True, slots=True)
class StandardNpmTarget:
    """Exact package manifest, lock, npm CLI, and Node authority for one build."""

    component_revision: ContentIdentity
    packaging_flavor_revision_identity: ContentIdentity
    packaging_profile_identity: ContentIdentity
    build_system_resolver_identity: ContentIdentity
    build_system_toolchain_identity: ContentIdentity
    node_toolchain_identity: ContentIdentity
    manifest: str
    lockfile: str
    npm_command: tuple[str, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.component_revision, "Component revision"),
            (self.packaging_flavor_revision_identity, "packaging Flavor revision"),
            (self.packaging_profile_identity, "packaging profile"),
            (self.build_system_resolver_identity, "resolver"),
            (self.build_system_toolchain_identity, "npm toolchain"),
            (self.node_toolchain_identity, "Node.js toolchain"),
        ):
            if not isinstance(value, ContentIdentity):
                raise TypeError(f"npm target {label} must be an identity")
        manifest = _package_path(self.manifest, name="package.json", label="manifest")
        lockfile = _package_path(
            self.lockfile, name="package-lock.json", label="lockfile"
        )
        if manifest.parent != lockfile.parent:
            raise ValueError("npm manifest and lockfile must share one directory")
        if (
            not isinstance(self.npm_command, tuple)
            or not self.npm_command
            or any(not isinstance(item, str) or not item for item in self.npm_command)
        ):
            raise ValueError("npm target requires one exact npm command")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """The existing identity payload, available for bounded evidence retention."""
        return {
            "schema": "literate-ai/standard-npm-target@1",
            "component_revision": self.component_revision.uri,
            "packaging_flavor_revision_identity": (
                self.packaging_flavor_revision_identity.uri
            ),
            "packaging_profile_identity": self.packaging_profile_identity.uri,
            "build_system_resolver_identity": (self.build_system_resolver_identity.uri),
            "build_system_toolchain_identity": (
                self.build_system_toolchain_identity.uri
            ),
            "node_toolchain_identity": self.node_toolchain_identity.uri,
            "manifest": self.manifest,
            "lockfile": self.lockfile,
            "npm_command": list(self.npm_command),
        }


@dataclass(frozen=True, slots=True)
class StandardNpmLockedPackage:
    """One unique package in the deliberately narrow npm lock subset."""

    name: str
    version: str
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ValueError("locked npm package requires a name and version")
        if self.dependencies != tuple(sorted(set(self.dependencies))):
            raise ValueError("locked npm dependencies must be sorted and unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class StandardNpmSourceAuthority:
    """Exact package.json/package-lock.json authority admitted before npm runs."""

    manifest_path: str
    lockfile_path: str
    manifest_identity: ContentIdentity
    lockfile_identity: ContentIdentity
    root_name: str
    root_version: str
    root_dependencies: tuple[str, ...]
    packages: tuple[StandardNpmLockedPackage, ...]

    def __post_init__(self) -> None:
        _package_path(self.manifest_path, name="package.json", label="manifest")
        _package_path(self.lockfile_path, name="package-lock.json", label="lockfile")
        if not isinstance(self.manifest_identity, ContentIdentity) or not isinstance(
            self.lockfile_identity, ContentIdentity
        ):
            raise TypeError("npm source authority requires content identities")
        if (
            not self.root_name
            or not self.root_version
            or not self.root_dependencies
            or not self.packages
        ):
            raise ValueError("npm source authority requires one nonempty package graph")
        if self.root_dependencies != tuple(sorted(set(self.root_dependencies))):
            raise ValueError("npm root dependencies must be sorted and unique")
        names = tuple(item.name for item in self.packages)
        if names != tuple(sorted(set(names))):
            raise ValueError("npm source authority package names must be unique")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """The existing identity payload, available for bounded evidence retention."""
        return {
            "schema": "literate-ai/standard-npm-source-authority@1",
            "manifest_path": self.manifest_path,
            "lockfile_path": self.lockfile_path,
            "manifest_identity": self.manifest_identity.uri,
            "lockfile_identity": self.lockfile_identity.uri,
            "root_name": self.root_name,
            "root_version": self.root_version,
            "root_dependencies": list(self.root_dependencies),
            "packages": [item.to_dict() for item in self.packages],
        }


@dataclass(frozen=True, slots=True)
class StandardNpmDependencyEvidence:
    """Typed evidence retained with one installed npm artifact projection."""

    authorization_identity: ContentIdentity
    component_revision: ContentIdentity
    build_plan_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    packaging_flavor_revision_identity: ContentIdentity
    packaging_profile_identity: ContentIdentity
    npm_target_identity: ContentIdentity
    npm_toolchain_identity: ContentIdentity
    node_toolchain_identity: ContentIdentity
    source_authority_identity: ContentIdentity
    manifest_identity: ContentIdentity
    lockfile_identity: ContentIdentity
    install_process_identity: ContentIdentity
    inventory_process_identity: ContentIdentity
    inventory_identity: ContentIdentity
    installed_tree_identity: ContentIdentity
    files: tuple[tuple[str, ContentIdentity], ...]

    def __post_init__(self) -> None:
        identity_fields = (
            self.authorization_identity,
            self.component_revision,
            self.build_plan_identity,
            self.source_tree_identity,
            self.packaging_flavor_revision_identity,
            self.packaging_profile_identity,
            self.npm_target_identity,
            self.npm_toolchain_identity,
            self.node_toolchain_identity,
            self.source_authority_identity,
            self.manifest_identity,
            self.lockfile_identity,
            self.install_process_identity,
            self.inventory_process_identity,
            self.inventory_identity,
            self.installed_tree_identity,
        )
        if any(not isinstance(item, ContentIdentity) for item in identity_fields):
            raise TypeError("npm dependency evidence requires content identities")
        paths = tuple(path for path, _identity in self.files)
        if paths != tuple(sorted(set(paths))) or any(
            not isinstance(identity, ContentIdentity) for _path, identity in self.files
        ):
            raise ValueError("npm evidence files must be sorted unique identity pairs")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "urn:literate-ai:schema:v1:standard-npm-dependency-evidence",
            "authorization_identity": self.authorization_identity.uri,
            "component_revision": self.component_revision.uri,
            "build_plan_identity": self.build_plan_identity.uri,
            "source_tree_identity": self.source_tree_identity.uri,
            "packaging_flavor_revision_identity": (
                self.packaging_flavor_revision_identity.uri
            ),
            "packaging_profile_identity": self.packaging_profile_identity.uri,
            "npm_target_identity": self.npm_target_identity.uri,
            "npm_toolchain_identity": self.npm_toolchain_identity.uri,
            "node_toolchain_identity": self.node_toolchain_identity.uri,
            "source_authority_identity": self.source_authority_identity.uri,
            "manifest_identity": self.manifest_identity.uri,
            "lockfile_identity": self.lockfile_identity.uri,
            "install_process_identity": self.install_process_identity.uri,
            "inventory_process_identity": self.inventory_process_identity.uri,
            "inventory_identity": self.inventory_identity.uri,
            "installed_tree_identity": self.installed_tree_identity.uri,
            "files": [
                {"path": path, "identity": identity.uri}
                for path, identity in self.files
            ],
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())


def _sha256(content: bytes) -> ContentIdentity:
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest())


def _read_bounded(path: Path, *, label: str) -> bytes:
    if path_is_link_or_reparse(path) or not path.is_file():
        raise StandardNpmLifecycleError(
            "standard_npm.source_file_missing", f"npm {label} is not a regular file"
        )
    content = path.read_bytes()
    if len(content) > _MAX_NPM_DOCUMENT_BYTES:
        raise StandardNpmLifecycleError(
            "standard_npm.source_file_oversized", f"npm {label} exceeds the byte limit"
        )
    return content


def _json_document(content: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StandardNpmLifecycleError(
            "standard_npm.document_invalid", f"npm {label} is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise StandardNpmLifecycleError(
            "standard_npm.document_invalid", f"npm {label} root must be an object"
        )
    return value


def _dependency_map(value: object, *, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        for name, version in value.items()
    ):
        raise StandardNpmLifecycleError(
            "standard_npm.dependencies_invalid",
            f"npm {label} dependencies must be a string map",
        )
    return {str(name): str(version) for name, version in value.items()}


def _exact_version(value: str, *, label: str) -> str:
    try:
        parsed = SemanticVersion.parse(value)
    except ValueError as exc:
        raise StandardNpmLifecycleError(
            "standard_npm.dependency_not_exact",
            f"npm {label} must use one exact semantic version",
        ) from exc
    if str(parsed) != value:
        raise StandardNpmLifecycleError(
            "standard_npm.dependency_not_exact",
            f"npm {label} must use one canonical exact semantic version",
        )
    return value


def _peer_dependencies(
    package: Mapping[str, object], *, label: str
) -> tuple[Mapping[str, str], frozenset[str]]:
    peers = _dependency_map(package.get("peerDependencies"), label=label)
    metadata = package.get("peerDependenciesMeta", {})
    if not isinstance(metadata, Mapping):
        raise StandardNpmLifecycleError(
            "standard_npm.peer_metadata_invalid",
            f"locked npm package {label!r} has invalid peer dependency metadata",
        )
    optional: set[str] = set()
    for raw_name, raw_rule in metadata.items():
        if (
            not isinstance(raw_name, str)
            or raw_name not in peers
            or not isinstance(raw_rule, Mapping)
            or any(key != "optional" for key in raw_rule)
            or (
                raw_rule.get("optional") is not True
                and raw_rule.get("optional") is not False
                and raw_rule.get("optional") is not None
            )
        ):
            raise StandardNpmLifecycleError(
                "standard_npm.peer_metadata_invalid",
                f"locked npm package {label!r} has invalid peer dependency metadata",
            )
        if raw_rule.get("optional") is True:
            optional.add(raw_name)
    return peers, frozenset(optional)


def dependency_bearing_package_manifests(source_root: Path) -> tuple[str, ...]:
    """Locate package manifests that actually request an npm dependency closure."""

    manifests: list[str] = []
    for path in sorted(source_root.rglob("package.json")):
        relative = path.relative_to(source_root).as_posix()
        document = _json_document(_read_bounded(path, label=relative), label=relative)
        if any(
            _dependency_map(document.get(field), label=relative)
            for field in _DEPENDENCY_FIELDS
        ):
            manifests.append(relative)
    return tuple(manifests)


def _integrity(value: object, *, package: str) -> None:
    if not isinstance(value, str) or " " in value:
        raise StandardNpmLifecycleError(
            "standard_npm.integrity_missing",
            f"locked npm package {package!r} lacks one integrity digest",
        )
    algorithm, separator, encoded = value.partition("-")
    if not separator or algorithm not in {"sha256", "sha384", "sha512"}:
        raise StandardNpmLifecycleError(
            "standard_npm.integrity_unsupported",
            f"locked npm package {package!r} uses unsupported integrity",
        )
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise StandardNpmLifecycleError(
            "standard_npm.integrity_invalid",
            f"locked npm package {package!r} has invalid integrity",
        ) from exc
    expected_size = {"sha256": 32, "sha384": 48, "sha512": 64}[algorithm]
    if len(decoded) != expected_size:
        raise StandardNpmLifecycleError(
            "standard_npm.integrity_invalid",
            f"locked npm package {package!r} has invalid integrity length",
        )


def _registry_resolution(value: object, *, package: str, version: str) -> None:
    if not isinstance(value, str):
        raise StandardNpmLifecycleError(
            "standard_npm.resolution_missing",
            f"locked npm package {package!r} lacks one registry resolution",
        )
    try:
        resolved = urlsplit(value)
        username = resolved.username
        password = resolved.password
    except ValueError as exc:
        raise StandardNpmLifecycleError(
            "standard_npm.resolution_unsupported",
            f"locked npm package {package!r} has an invalid registry resolution",
        ) from exc
    archive_name = package.rsplit("/", 1)[-1]
    expected_path = f"/{package}/-/{archive_name}-{version}.tgz"
    if (
        resolved.scheme != "https"
        or resolved.netloc != "registry.npmjs.org"
        or username is not None
        or password is not None
        or resolved.query
        or resolved.fragment
        or resolved.path != expected_path
    ):
        raise StandardNpmLifecycleError(
            "standard_npm.resolution_unsupported",
            f"locked npm package {package!r} does not name its canonical "
            "registry.npmjs.org archive",
        )


def _resolve_locked_dependency(
    package_path: str, dependency: str, packages: Mapping[str, Mapping[str, object]]
) -> str | None:
    cursor = PurePosixPath(package_path)
    while True:
        candidate = str(cursor / "node_modules" / dependency).lstrip("./")
        if candidate in packages:
            return candidate
        if str(cursor) in {"", "."}:
            return None
        parts = cursor.parts
        if len(parts) >= 2 and parts[-2] == "node_modules":
            cursor = PurePosixPath(*parts[:-2])
        elif (
            len(parts) >= 3
            and parts[-3] == "node_modules"
            and parts[-2].startswith("@")
        ):
            cursor = PurePosixPath(*parts[:-3])
        else:
            cursor = cursor.parent


def _locked_package_name(package_path: str) -> str:
    """Infer npm's package name from one canonical lockfile package location."""

    parts = PurePosixPath(package_path).parts
    node_modules = tuple(
        index for index, part in enumerate(parts) if part == "node_modules"
    )
    if not node_modules:
        raise StandardNpmLifecycleError(
            "standard_npm.lock_path_invalid",
            f"package-lock.json contains invalid package path {package_path!r}",
        )
    tail = parts[node_modules[-1] + 1 :]
    if len(tail) == 1 and not tail[0].startswith("@"):
        return tail[0]
    if len(tail) == 2 and tail[0].startswith("@") and len(tail[0]) > 1:
        return "/".join(tail)
    raise StandardNpmLifecycleError(
        "standard_npm.lock_path_invalid",
        f"package-lock.json contains invalid package path {package_path!r}",
    )


def load_npm_source_authority(
    source_root: Path, target: StandardNpmTarget
) -> StandardNpmSourceAuthority:
    """Admit the narrow exact npm subset before any package command executes."""

    manifests = dependency_bearing_package_manifests(source_root)
    if manifests != (target.manifest,):
        raise StandardNpmLifecycleError(
            "standard_npm.package_root_unsupported",
            "package-npm requires exactly one dependency-bearing package root at "
            f"{target.manifest}",
        )
    manifest_path = source_root.joinpath(*PurePosixPath(target.manifest).parts)
    lockfile_path = source_root.joinpath(*PurePosixPath(target.lockfile).parts)
    manifest_bytes = _read_bounded(manifest_path, label=target.manifest)
    lockfile_bytes = _read_bounded(lockfile_path, label=target.lockfile)
    return parse_npm_source_authority(manifest_bytes, lockfile_bytes, target)


def parse_npm_source_authority(
    manifest_bytes: bytes, lockfile_bytes: bytes, target: StandardNpmTarget
) -> StandardNpmSourceAuthority:
    """Parse bounded exact npm authority bytes without opening paths or running tools.

    The filesystem loader separately owns package-root discovery. This parser
    proves only the supplied manifest/lock graph, not its membership in a tree.
    """

    if not isinstance(target, StandardNpmTarget):
        raise TypeError("npm authority requires a StandardNpmTarget")
    for content, label in (
        (manifest_bytes, target.manifest),
        (lockfile_bytes, target.lockfile),
    ):
        if not isinstance(content, bytes):
            raise StandardNpmLifecycleError(
                "standard_npm.document_invalid", f"npm {label} must be immutable bytes"
            )
        if len(content) > _MAX_NPM_DOCUMENT_BYTES:
            raise StandardNpmLifecycleError(
                "standard_npm.source_file_oversized",
                f"npm {label} exceeds the byte limit",
            )
    manifest = _json_document(manifest_bytes, label=target.manifest)
    lock = _json_document(lockfile_bytes, label=target.lockfile)

    unsupported = tuple(
        field
        for field in _UNSUPPORTED_ROOT_FIELDS
        if manifest.get(field) not in (None, {}, [])
    )
    if unsupported:
        raise StandardNpmLifecycleError(
            "standard_npm.dependency_kind_unsupported",
            "package-npm currently supports ordinary runtime dependencies only: "
            + ", ".join(unsupported),
        )
    name, version = manifest.get("name"), manifest.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str):
        raise StandardNpmLifecycleError(
            "standard_npm.manifest_invalid",
            "package-npm requires package.json name and exact version",
        )
    _exact_version(version, label="package version")
    dependencies = _dependency_map(manifest.get("dependencies"), label=target.manifest)
    if not dependencies:
        raise StandardNpmLifecycleError(
            "standard_npm.dependencies_missing",
            "package-npm target has no ordinary dependency to resolve",
        )
    for dependency, requirement in dependencies.items():
        _exact_version(requirement, label=f"dependency {dependency!r}")

    if lock.get("lockfileVersion") not in {2, 3}:
        raise StandardNpmLifecycleError(
            "standard_npm.lock_invalid",
            "package-lock.json must use lockfileVersion 2 or 3",
        )
    raw_packages = lock.get("packages")
    if not isinstance(raw_packages, Mapping) or "" not in raw_packages:
        raise StandardNpmLifecycleError(
            "standard_npm.lock_invalid",
            "package-lock.json must contain its complete packages graph",
        )
    package_records: dict[str, Mapping[str, object]] = {}
    package_names: dict[str, str] = {}
    package_names_by_path: dict[str, str] = {}
    for raw_path, raw_record in raw_packages.items():
        if not isinstance(raw_path, str) or not isinstance(raw_record, Mapping):
            raise StandardNpmLifecycleError(
                "standard_npm.lock_invalid",
                "package-lock.json contains an invalid package",
            )
        portable = PurePosixPath(raw_path)
        if raw_path and (
            portable.is_absolute()
            or portable.as_posix() != raw_path
            or any(part in {"", ".", ".."} for part in portable.parts)
            or not raw_path.startswith("node_modules/")
        ):
            raise StandardNpmLifecycleError(
                "standard_npm.lock_path_invalid",
                f"package-lock.json contains unsafe package path {raw_path!r}",
            )
        if raw_record.get("link") is True:
            raise StandardNpmLifecycleError(
                "standard_npm.lock_link_unsupported",
                "package-lock.json contains a mutable linked package",
            )
        declared_name = raw_record.get("name")
        record_name = name if not raw_path else _locked_package_name(raw_path)
        record_version = raw_record.get("version")
        if (
            (not raw_path and declared_name is None)
            or (declared_name is not None and declared_name != record_name)
            or not isinstance(record_version, str)
        ):
            raise StandardNpmLifecycleError(
                "standard_npm.lock_invalid",
                f"locked npm package {raw_path!r} has inconsistent name/version",
            )
        _exact_version(record_version, label=f"locked package {record_name!r}")
        if raw_path:
            _integrity(raw_record.get("integrity"), package=record_name)
            _registry_resolution(
                raw_record.get("resolved"),
                package=record_name,
                version=record_version,
            )
            if raw_record.get("hasInstallScript") not in (
                None,
                False,
            ) or raw_record.get("gypfile") not in (None, False):
                raise StandardNpmLifecycleError(
                    "standard_npm.native_build_unsupported",
                    f"locked npm package {record_name!r} requires an install/native "
                    "build lifecycle",
                )
            if any(
                raw_record.get(field) not in (None, False)
                for field in ("dev", "devOptional", "inBundle", "peer")
            ):
                raise StandardNpmLifecycleError(
                    "standard_npm.dependency_kind_unsupported",
                    f"locked npm package {record_name!r} is not an ordinary "
                    "runtime dependency",
                )
        if raw_record.get("devDependencies") not in (None, {}):
            raise StandardNpmLifecycleError(
                "standard_npm.lock_dependency_kind_unsupported",
                f"locked npm package {record_name!r} uses unsupported devDependencies",
            )
        _dependency_map(raw_record.get("dependencies"), label=f"locked {record_name}")
        _dependency_map(
            raw_record.get("optionalDependencies"), label=f"locked {record_name}"
        )
        _peer_dependencies(raw_record, label=record_name)
        if record_name in package_names:
            raise StandardNpmLifecycleError(
                "standard_npm.duplicate_package_name_unsupported",
                f"package-lock.json resolves multiple copies of {record_name!r}",
            )
        package_records[raw_path] = raw_record
        package_names[record_name] = raw_path
        package_names_by_path[raw_path] = record_name

    root = package_records[""]
    if (
        root.get("name") != name
        or root.get("version") != version
        or _dependency_map(root.get("dependencies"), label="locked root")
        != dependencies
    ):
        raise StandardNpmLifecycleError(
            "standard_npm.lock_manifest_mismatch",
            "package-lock.json root differs from package.json",
        )
    graph: dict[str, tuple[str, ...]] = {}
    for package_path, record in package_records.items():
        record_name = package_names_by_path[package_path]
        children: set[str] = set()
        for field in ("dependencies", "optionalDependencies"):
            for dependency in _dependency_map(
                record.get(field), label=f"locked {record_name}"
            ):
                child = _resolve_locked_dependency(
                    package_path, dependency, package_records
                )
                if child is None:
                    raise StandardNpmLifecycleError(
                        "standard_npm.lock_edge_missing",
                        f"package-lock.json cannot resolve dependency {dependency!r}",
                    )
                children.add(child)
        peer_dependencies, optional_peers = _peer_dependencies(
            record, label=record_name
        )
        for dependency in peer_dependencies:
            child = _resolve_locked_dependency(
                package_path, dependency, package_records
            )
            if child is None:
                if dependency in optional_peers:
                    continue
                raise StandardNpmLifecycleError(
                    "standard_npm.lock_peer_missing",
                    "package-lock.json cannot resolve required peer dependency "
                    f"{dependency!r}",
                )
            children.add(child)
        graph[package_path] = tuple(sorted(children))
    reachable = {""}
    pending = [""]
    while pending:
        current = pending.pop()
        for child in graph[current]:
            if child not in reachable:
                reachable.add(child)
                pending.append(child)
    if reachable != set(package_records):
        raise StandardNpmLifecycleError(
            "standard_npm.lock_orphan_unsupported",
            "package-lock.json contains packages outside the root dependency closure",
        )
    packages = tuple(
        sorted(
            (
                StandardNpmLockedPackage(
                    package_names_by_path[package_path],
                    str(record["version"]),
                    tuple(
                        sorted(
                            package_names_by_path[child]
                            for child in graph[package_path]
                        )
                    ),
                )
                for package_path, record in package_records.items()
                if package_path
            ),
            key=lambda item: item.name,
        )
    )
    return StandardNpmSourceAuthority(
        target.manifest,
        target.lockfile,
        _sha256(manifest_bytes),
        _sha256(lockfile_bytes),
        name,
        version,
        tuple(sorted(dependencies)),
        packages,
    )


def validate_npm_inventory(
    content: bytes, authority: StandardNpmSourceAuthority
) -> bytes:
    """Normalize npm ls output and prove it equals the admitted lock graph."""

    document = _json_document(content, label="installed package inventory")
    if document.get("problems") not in (None, []):
        raise StandardNpmLifecycleError(
            "standard_npm.inventory_problem", "npm reported an invalid installed tree"
        )
    actual_versions: dict[str, str] = {}
    actual_dependencies: dict[str, set[str]] = {}

    def visit(name: str, value: object, ancestors: frozenset[int]) -> None:
        if not isinstance(value, Mapping) or id(value) in ancestors:
            raise StandardNpmLifecycleError(
                "standard_npm.inventory_invalid",
                "npm inventory contains an invalid graph",
            )
        if any(
            value.get(field) not in (None, False, [])
            for field in ("missing", "invalid", "extraneous", "problems")
        ):
            raise StandardNpmLifecycleError(
                "standard_npm.inventory_problem",
                f"npm reported invalid package {name!r}",
            )
        version = value.get("version")
        dependencies = value.get("dependencies", {})
        if not isinstance(version, str) or not isinstance(dependencies, Mapping):
            raise StandardNpmLifecycleError(
                "standard_npm.inventory_invalid",
                f"npm inventory package {name!r} is incomplete",
            )
        existing_version = actual_versions.get(name)
        if existing_version is not None and existing_version != version:
            raise StandardNpmLifecycleError(
                "standard_npm.inventory_duplicate_conflict",
                f"npm inventory reports conflicting views of package {name!r}",
            )
        actual_versions[name] = version
        actual_dependencies.setdefault(name, set())
        next_ancestors = ancestors | {id(value)}
        for child_name, child in dependencies.items():
            if not isinstance(child_name, str) or not child_name:
                raise StandardNpmLifecycleError(
                    "standard_npm.inventory_invalid",
                    "npm inventory has an invalid package name",
                )
            if isinstance(child, Mapping) and not child:
                continue
            actual_dependencies.setdefault(name, set()).add(child_name)
            visit(child_name, child, next_ancestors)

    root_dependencies = document.get("dependencies", {})
    if (
        document.get("name") != authority.root_name
        or document.get("version") != authority.root_version
        or not isinstance(root_dependencies, Mapping)
    ):
        raise StandardNpmLifecycleError(
            "standard_npm.inventory_root_mismatch",
            "npm inventory root differs from its lock",
        )
    for dependency_name, dependency in root_dependencies.items():
        if not isinstance(dependency_name, str):
            raise StandardNpmLifecycleError(
                "standard_npm.inventory_invalid",
                "npm inventory has an invalid root dependency",
            )
        visit(dependency_name, dependency, frozenset())
    if tuple(sorted(str(name) for name in root_dependencies)) != (
        authority.root_dependencies
    ):
        raise StandardNpmLifecycleError(
            "standard_npm.inventory_root_mismatch",
            "installed npm root dependencies differ from package-lock.json",
        )
    expected = {item.name: item for item in authority.packages}
    actual = {
        name: StandardNpmLockedPackage(
            name,
            version,
            tuple(sorted(actual_dependencies[name])),
        )
        for name, version in actual_versions.items()
    }
    if actual != expected:
        raise StandardNpmLifecycleError(
            "standard_npm.inventory_lock_mismatch",
            "installed npm package graph differs from package-lock.json",
        )
    return normalized_npm_inventory(authority)


def normalized_npm_inventory(authority: StandardNpmSourceAuthority) -> bytes:
    """Canonical retained installed-graph bytes for one admitted lock authority."""

    normalized = {
        "schema": "literate-ai/standard-npm-installed-graph@1",
        "root": {
            "name": authority.root_name,
            "version": authority.root_version,
            "dependencies": list(authority.root_dependencies),
        },
        "packages": [item.to_dict() for item in authority.packages],
    }
    return canonical_json_bytes(normalized)


__all__ = [
    "StandardNpmDependencyEvidence",
    "StandardNpmLifecycleError",
    "StandardNpmLockedPackage",
    "StandardNpmSourceAuthority",
    "StandardNpmTarget",
    "dependency_bearing_package_manifests",
    "load_npm_source_authority",
    "normalized_npm_inventory",
    "validate_npm_inventory",
]

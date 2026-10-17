"""Lock-graph acquisition from generated package and Cargo locks."""

from __future__ import annotations

import base64
import binascii
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

from .admission import (
    _cargo_dependencies,
    _cargo_manifest,
    _package_json_dependencies,
    _pyproject_dependencies,
    _python_requirements,
)
from .mix_lock import MixLock
from .mix_source import MixSourceAuthority, prepare_mix_source_authority
from .python_lock import PythonWheelLock
from .python_source import PythonSourceAuthority, prepare_python_source_authority
from .types import DependencyObservationError, _normalized_package_name


@dataclass(frozen=True, slots=True)
class _LockedPackage:
    key: str
    ecosystem: str
    coordinate: str
    version: str
    integrity_algorithm: str
    integrity_digest: str
    root: bool


@dataclass(frozen=True, slots=True)
class _LockProjection:
    packages: tuple[_LockedPackage, ...]
    edges: tuple[tuple[str, str], ...]


def _python_wheel_lock_projection(lock: PythonWheelLock) -> _LockProjection:
    """Project inert wheel intent into the existing complete SBOM graph checker.

    This is not acquisition evidence. Standard admission must still reject
    Python locks until target-bound acquisition and installed-tree custody are
    integrated; a parsed hash is not proof that wheel bytes were obtained.
    """
    prefix = "python-wheel-lock:"
    root = _LockedPackage(prefix + "@root", "pypi", "", "", "", "", True)
    packages = tuple(
        _LockedPackage(
            prefix + package.name,
            "pypi",
            package.name,
            package.version,
            "SHA-256",
            package.sha256,
            False,
        )
        for package in lock.packages
    )
    return _LockProjection(
        (root, *packages),
        tuple((prefix + parent, prefix + child) for parent, child in lock.edges),
    )


def _mix_lock_projection(lock: MixLock) -> _LockProjection:
    """Project a lifecycle-verified native lock; never prove acquisition here."""
    prefix = "mix-lock:"
    return _LockProjection(
        (
            _LockedPackage(prefix + "@root", "hex", "", "", "", "", True),
            *(
                _LockedPackage(
                    prefix + package.name,
                    "hex",
                    package.name,
                    package.version,
                    "SHA-256",
                    package.outer_sha256,
                    False,
                )
                for package in lock.packages
            ),
        ),
        tuple((prefix + parent, prefix + child) for parent, child in lock.edges),
    )


def _matching_locked_package(
    source: Mapping[str, object], projection: _LockProjection
) -> _LockedPackage | None:
    package_identity = _source_package_identity(source)
    if package_identity is None:
        return None
    ecosystem, coordinate = package_identity
    matches: list[_LockedPackage] = []
    for package in projection.packages:
        if package.root or (package.ecosystem, package.coordinate) != package_identity:
            continue
        source_version = source.get("version")
        if isinstance(source_version, str):
            if package.version == source_version:
                matches.append(package)
            continue
        try:
            from .resolution import _require_version_in_range

            _require_version_in_range(source.get("versionRange"), package.version)
        except DependencyObservationError as exc:
            if exc.code == "dependencies.source-range-mismatch":
                continue
            raise
        matches.append(package)
    if len(matches) > 1:
        raise DependencyObservationError(
            "dependencies.lock-resolution-ambiguous",
            f"lock data has multiple {ecosystem} versions for {coordinate}",
        )
    return matches[0] if matches else None


def _source_package_identity(
    component: Mapping[str, object],
) -> tuple[str, str] | None:
    purl = component.get("purl")
    if not isinstance(purl, str) or not purl.startswith("pkg:"):
        return None
    body = purl[4:].split("?", 1)[0].split("#", 1)[0]
    ecosystem, separator, coordinate = body.partition("/")
    if not separator or ecosystem not in {"cargo", "generic", "hex", "npm", "pypi"}:
        return None
    version_separator = coordinate.rfind("@")
    if version_separator > coordinate.rfind("/"):
        coordinate = coordinate[:version_separator]
    return ecosystem, _lock_coordinate(ecosystem, unquote(coordinate))


def _lock_coordinate(ecosystem: str, coordinate: str) -> str:
    if ecosystem in {"cargo", "pypi"}:
        return _normalized_package_name(coordinate)
    return coordinate.casefold()


def _require_lock_graph_covered(
    projection: _LockProjection,
    *,
    package_ref_by_lock_key: Mapping[str, str],
    root_ref: str,
    source_edges: set[tuple[str, str]],
) -> None:
    missing = sorted(
        package.key
        for package in projection.packages
        if not package.root and package.key not in package_ref_by_lock_key
    )
    if missing:
        raise DependencyObservationError(
            "dependencies.lock-inventory-missing",
            "source BOM omits exact locked packages: " + ", ".join(missing),
        )
    packages = {package.key: package for package in projection.packages}
    for source_key, target_key in projection.edges:
        source = packages[source_key]
        target = packages[target_key]
        source_ref = root_ref if source.root else package_ref_by_lock_key[source_key]
        target_ref = root_ref if target.root else package_ref_by_lock_key[target_key]
        if source_ref != target_ref and (source_ref, target_ref) not in source_edges:
            raise DependencyObservationError(
                "dependencies.lock-edge-missing",
                "source BOM omits exact lock dependency edge "
                f"{source.coordinate}@{source.version} -> "
                f"{target.coordinate}@{target.version}",
            )


def _generated_lock_projection(
    files: Mapping[str, object],
    *,
    allow_missing_cargo_lock: bool = False,
    python_source_authority: PythonSourceAuthority | None = None,
    mix_source_authority: MixSourceAuthority | None = None,
) -> _LockProjection:
    text_files = {
        path: content
        for path, content in files.items()
        if isinstance(path, str) and isinstance(content, str)
    }
    from .resolution import _bazel_module_intent

    _bazel_module_intent(text_files)
    python_locks = [
        path
        for path in files
        if isinstance(path, str)
        and PurePosixPath(path).name == "python-wheel-lock.json"
    ]
    if python_locks:
        if len(python_locks) != 1:
            raise DependencyObservationError(
                "dependencies.python-source-invalid",
                "Python wheel admission requires one selected package root",
            )
        lock_path = python_locks[0]
        root = PurePosixPath(lock_path).parent
        manifests = [
            str(root / name)
            for name in ("requirements.txt", "requirements.in", "pyproject.toml")
            if str(root / name) in files
        ]
        if len(manifests) != 1:
            raise DependencyObservationError(
                "dependencies.python-source-invalid",
                "Python wheel lock requires exactly one corresponding manifest",
            )
        authority = prepare_python_source_authority(
            files, manifest_path=manifests[0], lock_path=lock_path
        )
        # Source consistency is not a substitute for independently observed
        # target, acquired bytes and installed environment custody.
        if python_source_authority is None:
            raise DependencyObservationError(
                "dependencies.python-acquisition-evidence-missing",
                "Python source lock is consistent but lacks target-bound acquisition "
                "and installed-closure evidence",
            )
        if authority != python_source_authority:
            raise DependencyObservationError(
                "dependencies.python-source-invalid",
                "Python source differs from the selected lifecycle authority",
            )
    elif python_source_authority is not None:
        raise DependencyObservationError(
            "dependencies.python-source-invalid",
            "Selected Python lifecycle authority is absent from source",
        )
    mix_paths = [
        path
        for path in files
        if isinstance(path, str)
        and PurePosixPath(path).name.casefold()
        in {"mix-project.json", "mix.lock", "mix.exs", ".iex.exs"}
    ]
    if mix_source_authority is not None:
        if (
            not isinstance(mix_source_authority, MixSourceAuthority)
            or prepare_mix_source_authority(files) != mix_source_authority
        ):
            raise DependencyObservationError(
                "dependencies.mix-source-invalid",
                "Mix source differs from its selected lifecycle authority",
            )
    elif mix_paths:
        raise DependencyObservationError(
            "dependencies.mix-acquisition-evidence-missing",
            "Mix dependency authority requires its lifecycle-owned lock, "
            "acquired archive and retained runtime payload evidence",
        )
    for path, content in text_files.items():
        name = PurePosixPath(path).name
        if name in {"mix.lock", "mix.exs"}:
            raise DependencyObservationError(
                "dependencies.mix-acquisition-evidence-missing",
                "Mix dependency authority requires its lifecycle-owned lock, "
                "acquired archive and retained runtime payload evidence",
            )
        if name in {"pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock"}:
            raise DependencyObservationError(
                "dependencies.lock-authority-unsupported",
                f"generated dependency authority {name} is not yet supported",
            )
        if name in {"conanfile.py", "conanfile.txt"}:
            raise DependencyObservationError(
                "dependencies.lock-authority-unsupported",
                f"generated dependency authority {name} is not yet supported",
            )
        if name == "CMakeLists.txt" and re.search(
            r"\b(?:find_package|FetchContent_Declare|ExternalProject_Add)\s*\(",
            content,
            re.IGNORECASE,
        ):
            raise DependencyObservationError(
                "dependencies.lock-authority-unsupported",
                "external CMake dependency declarations are not yet supported",
            )
    if python_source_authority is None and any(
        _python_requirements(content)
        for path, content in text_files.items()
        if PurePosixPath(path).name in {"requirements.txt", "requirements.in"}
    ):
        raise DependencyObservationError(
            "dependencies.python-lock-unsupported",
            "Python requirements lack a complete typed parent-edge lock projection",
        )
    for path, content in text_files.items():
        if PurePosixPath(path).name != "pyproject.toml":
            continue
        if python_source_authority is None and _pyproject_dependencies(content):
            raise DependencyObservationError(
                "dependencies.python-lock-unsupported",
                "pyproject dependencies lack a complete typed lock projection",
            )

    packages: list[_LockedPackage] = []
    edges: list[tuple[str, str]] = []
    if python_source_authority is not None:
        python_projection = _python_wheel_lock_projection(python_source_authority.lock)
        packages.extend(python_projection.packages)
        edges.extend(python_projection.edges)
    package_locks = {
        path: content
        for path, content in text_files.items()
        if PurePosixPath(path).name == "package-lock.json"
    }
    package_manifests = {
        str(PurePosixPath(path).parent / "package.json") for path in package_locks
    }
    for path, content in text_files.items():
        if PurePosixPath(path).name == "package.json":
            dependencies = _package_json_dependencies(content)
            if dependencies and path not in package_manifests:
                raise DependencyObservationError(
                    "dependencies.javascript-lock-missing",
                    f"generated package manifest {path} lacks package-lock.json",
                )
    for path, content in sorted(package_locks.items()):
        lock_packages, lock_edges = _package_lock_projection(path, content)
        packages.extend(lock_packages)
        edges.extend(lock_edges)

    cargo_locks = {
        path: content
        for path, content in text_files.items()
        if PurePosixPath(path).name == "Cargo.lock"
    }
    cargo_manifests = {
        str(PurePosixPath(path).parent / "Cargo.toml") for path in cargo_locks
    }
    for path, content in text_files.items():
        if PurePosixPath(path).name != "Cargo.toml":
            continue
        manifest = _cargo_manifest(content)
        dependencies = manifest.get("dependencies", {})
        if not isinstance(dependencies, Mapping) or any(
            not isinstance(requirement, (str, Mapping))
            or (
                isinstance(requirement, Mapping)
                and any(key in requirement for key in ("path", "git", "workspace"))
            )
            for requirement in dependencies.values()
        ):
            raise DependencyObservationError(
                "dependencies.rust-authority-unsupported",
                "generated Cargo manifest contains a non-registry dependency",
            )
        unsupported = {
            key
            for key in manifest
            if key in {"build-dependencies", "dev-dependencies", "workspace"}
            or str(key).startswith("target")
        }
        if unsupported:
            raise DependencyObservationError(
                "dependencies.rust-authority-unsupported",
                "generated Cargo manifest uses unsupported dependency authority: "
                + ", ".join(sorted(unsupported)),
            )
        if (
            _cargo_dependencies(content)
            and path not in cargo_manifests
            and not allow_missing_cargo_lock
        ):
            raise DependencyObservationError(
                "dependencies.rust-lock-missing",
                f"generated Cargo manifest {path} lacks Cargo.lock",
            )
    for path, content in sorted(cargo_locks.items()):
        manifest_path = str(PurePosixPath(path).parent / "Cargo.toml")
        manifest_content = text_files.get(manifest_path)
        if manifest_content is None:
            raise DependencyObservationError(
                "dependencies.rust-manifest-missing",
                f"generated lock {path} lacks its Cargo.toml",
            )
        lock_packages, lock_edges = _cargo_lock_projection(
            path, content, manifest_content
        )
        packages.extend(lock_packages)
        edges.extend(lock_edges)
    return _LockProjection(tuple(packages), tuple(sorted(set(edges))))


def _package_lock_projection(
    path: str, content: str
) -> tuple[tuple[_LockedPackage, ...], tuple[tuple[str, str], ...]]:
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.javascript-lock-invalid", f"generated {path} is invalid"
        ) from exc
    if not isinstance(document, Mapping) or document.get("lockfileVersion") not in {
        2,
        3,
    }:
        raise DependencyObservationError(
            "dependencies.javascript-lock-invalid",
            f"generated {path} must use package-lock format 2 or 3",
        )
    raw_packages = document.get("packages")
    if not isinstance(raw_packages, Mapping) or "" not in raw_packages:
        raise DependencyObservationError(
            "dependencies.javascript-lock-invalid",
            f"generated {path} omits its exact packages graph",
        )
    prefix = f"npm:{PurePosixPath(path).parent}:"
    packages: dict[str, _LockedPackage] = {}
    manifests: dict[str, Mapping[str, object]] = {}
    for package_path, raw in raw_packages.items():
        if not isinstance(package_path, str) or not isinstance(raw, Mapping):
            raise DependencyObservationError(
                "dependencies.javascript-lock-invalid",
                f"generated {path} contains an invalid package",
            )
        if raw.get("link") is True:
            raise DependencyObservationError(
                "dependencies.javascript-lock-unsupported",
                f"generated {path} contains a mutable linked package",
            )
        root = package_path == ""
        name = raw.get("name") if root else _package_lock_name(package_path)
        declared_name = raw.get("name")
        version = raw.get("version")
        if (
            not isinstance(name, str)
            or (declared_name is not None and declared_name != name)
            or not isinstance(version, str)
        ):
            raise DependencyObservationError(
                "dependencies.javascript-lock-invalid",
                f"generated {path} package {package_path!r} has an invalid "
                "name/version",
            )
        algorithm, digest = ("SHA-256", "0" * 64)
        if not root:
            algorithm, digest = _npm_integrity(raw.get("integrity"), path=path)
        key = prefix + package_path
        packages[key] = _LockedPackage(
            key,
            "npm",
            _lock_coordinate("npm", name),
            version,
            algorithm,
            digest,
            root,
        )
        manifests[package_path] = raw
    edges: set[tuple[str, str]] = set()
    for package_path, raw in manifests.items():
        source_key = prefix + package_path
        optional = raw.get("optionalDependencies", {})
        optional_peers = _package_lock_optional_peers(
            raw, path=path, package_path=package_path
        )
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            dependencies = raw.get(field, {})
            if not isinstance(dependencies, Mapping):
                raise DependencyObservationError(
                    "dependencies.javascript-lock-invalid",
                    f"generated {path} package dependency map is invalid",
                )
            for dependency in dependencies:
                target_path = _resolve_package_lock_dependency(
                    package_path, str(dependency), manifests
                )
                if target_path is None and field == "optionalDependencies":
                    continue
                if (
                    target_path is None
                    and field == "peerDependencies"
                    and dependency in optional_peers
                ):
                    continue
                if (
                    target_path is None
                    and isinstance(optional, Mapping)
                    and dependency in optional
                ):
                    continue
                if target_path is None:
                    raise DependencyObservationError(
                        "dependencies.javascript-lock-edge-missing",
                        f"generated {path} cannot resolve locked dependency "
                        f"{dependency}",
                    )
                edges.add((source_key, prefix + target_path))
    return tuple(packages[key] for key in sorted(packages)), tuple(sorted(edges))


def _package_lock_name(package_path: str) -> str:
    parts = PurePosixPath(package_path).parts
    locations = tuple(
        index for index, part in enumerate(parts) if part == "node_modules"
    )
    if not locations:
        return ""
    tail = parts[locations[-1] + 1 :]
    if len(tail) == 1 and not tail[0].startswith("@"):
        return tail[0]
    if len(tail) == 2 and tail[0].startswith("@") and len(tail[0]) > 1:
        return "/".join(tail)
    return ""


def _package_lock_optional_peers(
    package: Mapping[str, object], *, path: str, package_path: str
) -> frozenset[str]:
    peers = package.get("peerDependencies", {})
    metadata = package.get("peerDependenciesMeta", {})
    if not isinstance(peers, Mapping) or not isinstance(metadata, Mapping):
        raise DependencyObservationError(
            "dependencies.javascript-lock-invalid",
            f"generated {path} package {package_path!r} has invalid peer metadata",
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
            raise DependencyObservationError(
                "dependencies.javascript-lock-invalid",
                f"generated {path} package {package_path!r} has invalid peer metadata",
            )
        if raw_rule.get("optional") is True:
            optional.add(raw_name)
    return frozenset(optional)


def _resolve_package_lock_dependency(
    package_path: str,
    dependency: str,
    packages: Mapping[str, Mapping[str, object]],
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


def _npm_integrity(value: object, *, path: str) -> tuple[str, str]:
    if not isinstance(value, str) or " " in value:
        raise DependencyObservationError(
            "dependencies.javascript-integrity-missing",
            f"generated {path} package lacks one exact integrity digest",
        )
    algorithm, separator, encoded = value.partition("-")
    algorithm_names = {"sha256": "SHA-256", "sha384": "SHA-384", "sha512": "SHA-512"}
    if not separator or algorithm not in algorithm_names:
        raise DependencyObservationError(
            "dependencies.javascript-integrity-unsupported",
            f"generated {path} package integrity algorithm is unsupported",
        )
    try:
        digest = base64.b64decode(encoded, validate=True).hex()
    except (ValueError, binascii.Error) as exc:
        raise DependencyObservationError(
            "dependencies.javascript-integrity-invalid",
            f"generated {path} package integrity digest is invalid",
        ) from exc
    return algorithm_names[algorithm], digest


def _cargo_lock_projection(
    path: str, content: str, manifest_content: str
) -> tuple[tuple[_LockedPackage, ...], tuple[tuple[str, str], ...]]:
    try:
        document = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DependencyObservationError(
            "dependencies.rust-lock-invalid", f"generated {path} is invalid"
        ) from exc
    raw_packages = document.get("package")
    if not isinstance(raw_packages, list):
        raise DependencyObservationError(
            "dependencies.rust-lock-invalid", f"generated {path} omits packages"
        )
    manifest = _cargo_manifest(manifest_content)
    root_manifest = manifest.get("package")
    root_name = (
        root_manifest.get("name") if isinstance(root_manifest, Mapping) else None
    )
    root_version = (
        root_manifest.get("version") if isinstance(root_manifest, Mapping) else None
    )
    prefix = f"cargo:{PurePosixPath(path).parent}:"
    packages: dict[str, _LockedPackage] = {}
    records: dict[str, Mapping[str, object]] = {}
    by_name: dict[str, list[str]] = {}
    for raw in raw_packages:
        if not isinstance(raw, Mapping):
            raise DependencyObservationError(
                "dependencies.rust-lock-invalid", f"generated {path} package is invalid"
            )
        name, version, source = raw.get("name"), raw.get("version"), raw.get("source")
        if not isinstance(name, str) or not isinstance(version, str):
            raise DependencyObservationError(
                "dependencies.rust-lock-invalid",
                f"generated {path} package lacks name/version",
            )
        root = name == root_name and version == root_version and source is None
        checksum = raw.get("checksum")
        if not root and (
            not isinstance(source, str)
            or not source.startswith("registry+")
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        ):
            raise DependencyObservationError(
                "dependencies.rust-lock-unsupported",
                f"generated {path} contains a non-registry or unhashed package",
            )
        key = prefix + name + "@" + version + "|" + str(source or "root")
        packages[key] = _LockedPackage(
            key,
            "cargo",
            _lock_coordinate("cargo", name),
            version,
            "SHA-256",
            checksum if isinstance(checksum, str) else "0" * 64,
            root,
        )
        records[key] = raw
        by_name.setdefault(name, []).append(key)
    roots = [package for package in packages.values() if package.root]
    if len(roots) != 1:
        raise DependencyObservationError(
            "dependencies.rust-lock-root-invalid",
            f"generated {path} must identify exactly one root package",
        )
    edges: set[tuple[str, str]] = set()
    for source_key, raw in records.items():
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise DependencyObservationError(
                "dependencies.rust-lock-invalid",
                f"generated {path} package dependencies are invalid",
            )
        for dependency in dependencies:
            if not isinstance(dependency, str):
                raise DependencyObservationError(
                    "dependencies.rust-lock-invalid",
                    f"generated {path} package dependency is invalid",
                )
            dependency_name = dependency.split(" ", 1)[0]
            candidates = by_name.get(dependency_name, [])
            version_match = re.match(r"^[^ ]+\s+([^ ]+)", dependency)
            if version_match:
                candidates = [
                    key
                    for key in candidates
                    if packages[key].version == version_match.group(1)
                ]
            if len(candidates) != 1:
                raise DependencyObservationError(
                    "dependencies.rust-lock-edge-ambiguous",
                    f"generated {path} dependency {dependency!r} is ambiguous",
                )
            edges.add((source_key, candidates[0]))
    return tuple(packages[key] for key in sorted(packages)), tuple(sorted(edges))

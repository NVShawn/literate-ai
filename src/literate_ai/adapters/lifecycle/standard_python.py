"""Retained Python dependency evidence for the Standard artifact boundary.

The caller owns build authorization and artifact sealing. Neither an untrusted
manifest nor a matching wheel lock can grant permission to execute packages.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, fields
from pathlib import Path

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.builders.python import PythonToolchain
from literate_ai.adapters.dependencies.python_install import (
    PIP_INSTALLER,
    InstalledPythonWheels,
)
from literate_ai.adapters.dependencies.python_installed import _snapshot
from literate_ai.adapters.dependencies.python_lock import _strict_object
from literate_ai.adapters.dependencies.python_resolution import PythonDependencyEvidence
from literate_ai.adapters.dependencies.python_source import PythonSourceAuthority
from literate_ai.adapters.dependencies.python_target import (
    PythonWheelTarget,
    observe_python_wheel_target,
)
from literate_ai.adapters.dependencies.types import (
    DependencyObservationError,
    HostDependencyObservation,
)
from literate_ai.contracts import (
    ContentIdentity,
    StandardPythonWheelCommandProfile,
    canonical_identity,
)
from literate_ai.storage import canonical_json_bytes

_PAYLOAD = "python-runtime"
_MANIFEST = "python-dependencies.json"
_SCHEMA = "literate-ai/standard-python-dependency-evidence@1"
_MAX_MANIFEST = 64 * 1024**2


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.python-artifact-invalid", message)


@dataclass(frozen=True, slots=True)
class StandardPythonTarget:
    """Locked Python packaging selection, distinct from dependency-free Python."""

    component_revision: ContentIdentity
    packaging_flavor_revision_identity: ContentIdentity
    packaging_profile_identity: ContentIdentity
    build_system_resolver_identity: ContentIdentity
    build_system_toolchain_identity: ContentIdentity
    python_toolchain_identity: ContentIdentity
    manifest: str
    lockfile: str
    python_command: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name.endswith("identity") or field.name == "component_revision":
                if not isinstance(getattr(self, field.name), ContentIdentity):
                    raise TypeError("Python target requires exact content identities")
        profile = StandardPythonWheelCommandProfile(
            "pip", "python", self.manifest, self.lockfile
        )
        if profile.identity != self.packaging_profile_identity:
            raise ValueError("Python target paths differ from the locked profile")
        if self.build_system_toolchain_identity != self.python_toolchain_identity:
            raise ValueError("Python wheel build must use the selected interpreter")
        if (
            not isinstance(self.python_command, tuple)
            or not self.python_command
            or any(
                not isinstance(item, str) or not item for item in self.python_command
            )
        ):
            raise ValueError("Python target requires one exact interpreter command")

    def identity_document(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/standard-python-wheel-target@1",
            **{
                field.name: getattr(self, field.name).uri
                for field in fields(self)
                if field.name.endswith("identity") or field.name == "component_revision"
            },
            "manifest": self.manifest,
            "lockfile": self.lockfile,
            "python_command": list(self.python_command),
            "installer_filename": PIP_INSTALLER.filename,
            "installer_sha256": PIP_INSTALLER.sha256,
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())


@dataclass(frozen=True)
class StandardPythonBuildBinding:
    """Expected identities supplied by the authorized Standard build plan."""

    authorization_identity: ContentIdentity
    component_revision: ContentIdentity
    build_plan_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    command_contract_identity: ContentIdentity
    packaging_flavor_revision_identity: ContentIdentity
    packaging_profile_identity: ContentIdentity
    python_toolchain_identity: ContentIdentity

    def __post_init__(self) -> None:
        if any(
            not isinstance(getattr(self, field.name), ContentIdentity)
            for field in fields(self)
        ):
            raise TypeError("Python build binding requires content identities")

    def to_dict(self) -> dict[str, str]:
        return {field.name: getattr(self, field.name).uri for field in fields(self)}

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())


@dataclass(frozen=True)
class StandardPythonDependencyEvidence:
    identity: ContentIdentity
    payload_root: Path
    observation: HostDependencyObservation
    installed_tree_identity: ContentIdentity
    target_identity: ContentIdentity
    installer_identity: ContentIdentity
    install_process_identity: ContentIdentity


@dataclass(frozen=True)
class StandardPythonDependencyObserver:
    """Fresh resolver callback bound to sealed artifact and build authority.

    Construct only from lifecycle-owned records, never artifact-local metadata.
    Re-observe the selected interpreter and re-hash retained files on each call.
    """

    artifact_root: Path
    expected_identity: ContentIdentity
    binding: StandardPythonBuildBinding
    source: PythonSourceAuthority
    toolchain: PythonToolchain

    def __call__(self) -> PythonDependencyEvidence:
        verified = verify_standard_python_dependencies(
            self.artifact_root,
            expected_identity=self.expected_identity,
            binding=self.binding,
            source=self.source,
            target=observe_python_wheel_target(self.toolchain),
        )
        return PythonDependencyEvidence(
            self.source, verified.identity, verified.observation
        )


def _source_document(source: PythonSourceAuthority) -> dict[str, str]:
    return {
        "manifest_path": source.manifest_path,
        "lock_path": source.lock_path,
        "manifest_sha256": source.manifest_sha256,
        "lock_sha256": source.lock_sha256,
    }


def _require_root(root: Path) -> None:
    if path_is_link_or_reparse(root) or not root.is_dir():
        _fail("Python artifact root is not an owned directory")


def _read_manifest(path: Path) -> bytes:
    if path_is_link_or_reparse(path) or not path.is_file() or path.stat().st_nlink != 1:
        _fail("Python dependency evidence manifest is missing or unsafe")
    with path.open("rb") as stream:
        content = stream.read(_MAX_MANIFEST + 1)
    if len(content) > _MAX_MANIFEST:
        _fail("Python dependency evidence manifest exceeds its bound")
    return content


def _executable_paths(root: Path, paths: list[str]) -> list[str]:
    # Windows executable behavior is bound by payload bytes and target identity;
    # POSIX script entry points additionally require their execute permissions.
    return sorted(
        path
        for path in paths
        if os.name != "nt" and (root / path).stat().st_mode & 0o111
    )


def retain_standard_python_dependencies(
    artifact_root: Path,
    *,
    binding: StandardPythonBuildBinding,
    source: PythonSourceAuthority,
    installed: InstalledPythonWheels,
) -> StandardPythonDependencyEvidence:
    """Copy verified payload and retain evidence in two reserved artifact paths.

    Returns the identity the lifecycle must seal outside this manifest. This
    helper never replaces existing artifact data, nor seals an artifact itself.
    """
    _require_root(artifact_root)
    payload = artifact_root / _PAYLOAD
    manifest = artifact_root / _MANIFEST
    if (
        payload.exists()
        or payload.is_symlink()
        or manifest.exists()
        or manifest.is_symlink()
    ):
        _fail("Build output occupied reserved Python dependency evidence paths")
    if (
        source.lock != installed.staged.lock
        or binding.python_toolchain_identity.uri != installed.target.toolchain_identity
    ):
        _fail("Installed Python evidence does not belong to this source or toolchain")
    installed.revalidate()
    copied = False
    written = False
    try:
        # Create the exact destination ourselves so failure cleanup never owns a
        # pre-existing path. The enclosing artifact workspace remains caller-owned.
        payload.mkdir()
        copied = True
        shutil.copytree(installed.directory, payload, dirs_exist_ok=True)
        if _snapshot(payload) != installed.observation.files:
            _fail("Python payload changed while entering artifact custody")
        installed.revalidate()
        file_records = [
            [item.path, item.size, item.sha256] for item in installed.observation.files
        ]
        document = {
            "schema": _SCHEMA,
            "binding": binding.to_dict(),
            "source": _source_document(source),
            "target_identity": installed.target.identity.uri,
            "installer_identity": installed.installer_identity.uri,
            "install_process_identity": installed.install_process_identity.uri,
            "installed_tree_identity": installed.observation.tree_identity.uri,
            "package_schemes": installed.package_schemes,
            "projector_identities": {
                name: value.process_identity.uri
                for name, value in sorted(installed.changes.items())
            },
            "components": list(installed.observation.graph.components),
            "edges": [list(edge) for edge in installed.observation.graph.edges],
            "files": file_records,
            "executable_paths": _executable_paths(
                payload, [item[0] for item in file_records]
            ),
        }
        content = canonical_json_bytes(document)
        if len(content) > _MAX_MANIFEST:
            _fail("Python artifact evidence exceeds its bound")
        with manifest.open("xb") as stream:
            written = True
            stream.write(content)
        return verify_standard_python_dependencies(
            artifact_root,
            expected_identity=canonical_identity(document),
            binding=binding,
            source=source,
            target=installed.target,
        )
    except Exception:
        if written:
            manifest.unlink()
        if copied:
            shutil.rmtree(payload)
        raise


def verify_standard_python_dependencies(
    artifact_root: Path,
    *,
    expected_identity: ContentIdentity,
    binding: StandardPythonBuildBinding,
    source: PythonSourceAuthority,
    target: PythonWheelTarget,
) -> StandardPythonDependencyEvidence:
    """Revalidate a cache entry using an identity from trusted artifact custody.

    Never obtain expected_identity from this manifest or freshly hash a mutable
    cache entry to grant it trust. The caller must also reobserve the selected
    target before calling; a retained target record alone is not current evidence.
    """
    if not isinstance(expected_identity, ContentIdentity):
        raise TypeError(
            "Python cache revalidation requires an external expected identity"
        )
    try:
        _require_root(artifact_root)
        target.require_lock(source.lock)
        if binding.python_toolchain_identity.uri != target.toolchain_identity:
            _fail("Python artifact binding names another selected toolchain")
        manifest = artifact_root / _MANIFEST
        content = _read_manifest(manifest)
        document = json.loads(content, object_pairs_hook=_strict_object)
        if canonical_identity(document) != expected_identity:
            _fail(
                "Python dependency evidence differs from the sealed artifact identity"
            )
        if (
            not isinstance(document, dict)
            or set(document)
            != {
                "schema",
                "binding",
                "source",
                "target_identity",
                "installer_identity",
                "install_process_identity",
                "installed_tree_identity",
                "package_schemes",
                "projector_identities",
                "components",
                "edges",
                "files",
                "executable_paths",
            }
            or document["schema"] != _SCHEMA
        ):
            _fail("Python dependency evidence has an unsupported shape")
        if (document["binding"], document["source"], document["target_identity"]) != (
            binding.to_dict(),
            _source_document(source),
            target.identity.uri,
        ):
            _fail(
                "Python dependency evidence belongs to another build, source or target"
            )
        payload = artifact_root / _PAYLOAD
        observed = _snapshot(payload)
        actual_files = [[item.path, item.size, item.sha256] for item in observed]
        if document["files"] != actual_files:
            _fail("Retained Python dependency payload changed")
        if document["executable_paths"] != _executable_paths(
            payload, [item.path for item in observed]
        ):
            _fail("Retained Python executable permissions changed")
        packages = {p.name: p.version for p in source.lock.packages}
        components = document["components"]
        if not isinstance(components, list) or any(
            not isinstance(item, dict) for item in components
        ):
            _fail("Retained Python dependency graph is malformed")
        if (
            len(components) != len(packages)
            or {item.get("name"): item.get("version") for item in components}
            != packages
        ):
            _fail("Retained Python graph differs from the locked closure")
        edges = [
            [parent if parent == "@root" else "pkg:pypi/" + parent, "pkg:pypi/" + child]
            for parent, child in source.lock.edges
        ]
        if document["edges"] != edges:
            _fail("Retained Python dependency edges differ from the lock")
        if (
            _snapshot(payload) != observed
            or _read_manifest(manifest) != content
            or document["executable_paths"]
            != _executable_paths(payload, [item.path for item in observed])
        ):
            _fail("Retained Python evidence changed during revalidation")
        return StandardPythonDependencyEvidence(
            expected_identity,
            payload,
            HostDependencyObservation(
                tuple(components), tuple(tuple(edge) for edge in edges)
            ),
            ContentIdentity.parse_uri(document["installed_tree_identity"]),
            target.identity,
            ContentIdentity.parse_uri(document["installer_identity"]),
            ContentIdentity.parse_uri(document["install_process_identity"]),
        )
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise DependencyObservationError(
            "dependencies.python-artifact-invalid",
            "Retained Python dependency evidence is unavailable or invalid",
        ) from exc

"""Resolve the Standard lifecycle driver against exact installed framework bytes.

The Standard driver is framework-distributed trusted code.  A project therefore pins
the logical wheel payload and the immutable Standard policy, rather than trusting the
ambient import location or the package version alone.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from literate_ai.contracts import (
    ContentIdentity,
    LifecycleDriverTrust,
    LifecycleDriverTrustBinding,
    ProjectInitializationOrigin,
    StandardLifecyclePolicy,
    StandardProjectLifecycleDriver,
    canonical_identity,
    load_current_standard_lifecycle_policy,
)

INSTALLED_FRAMEWORK_DISTRIBUTION_SCHEMA = (
    "literate-ai/installed-framework-distribution@1"
)
EMBEDDED_DISTRIBUTION_ORIGIN_SCHEMA = "literate-ai/distribution-origin@1"
_FRAMEWORK_DISTRIBUTION_NAME = "literate-ai"
_MAXIMUM_PAYLOAD_FILES = 100_000
_MAXIMUM_PAYLOAD_BYTES = 256 * 1024 * 1024
# Keep aligned with the stdlib-only, independently staged worker bootstrap. Importing
# policy from that file would make the trusted in-process observer depend on a
# transport program, while the bootstrap cannot import this package before trust is
# established; the parity regression covers this deliberate two-surface duplication.
_DIST_INFO_EXCLUSIONS = frozenset(
    {
        "direct_url.json",
        "installer",
        "record",
        "requested",
        "uv_build.json",
        "uv_cache.json",
    }
)
_DIST_INFO_DIRECTORY = re.compile(r"^[A-Za-z0-9_.-]+\.dist-info$")
# Installer-created console launchers live outside the immutable wheel payload.
# Keep this set aligned with `[project.scripts]` and with
# `remote_worker_bootstrap._CONSOLE_LAUNCHER_NAMES`.
_CONSOLE_LAUNCHER_NAMES = frozenset(
    {
        "litai",
        "litai.exe",
        "litai-script.py",
        "litai-mcp",
        "litai-mcp.exe",
        "litai-mcp-script.py",
    }
)


class StandardLifecycleBindingError(ValueError):
    """Stable fail-closed error from Standard driver observation or resolution."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class InstalledFrameworkDistributionMember:
    """One host-path-free member of the installed wheel payload."""

    path: str
    size: int
    digest: str

    def __post_init__(self) -> None:
        candidate = PurePosixPath(self.path)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.as_posix() != self.path
        ):
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_payload_invalid",
                "installed framework payload contains a non-canonical logical path",
            )
        if (
            not isinstance(self.size, int)
            or isinstance(self.size, bool)
            or self.size < 0
        ):
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_payload_invalid",
                "installed framework payload contains an invalid file size",
            )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.digest):
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_payload_invalid",
                "installed framework payload contains an invalid file digest",
            )

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class InstalledFrameworkDistribution:
    """Canonical logical observation of one installed, non-editable wheel."""

    distribution_name: str
    distribution_version: str
    members: tuple[InstalledFrameworkDistributionMember, ...]

    def __post_init__(self) -> None:
        if canonicalize_name(self.distribution_name) != _FRAMEWORK_DISTRIBUTION_NAME:
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_metadata_invalid",
                "installed framework distribution has an unexpected package name",
            )
        try:
            normalized_version = str(Version(self.distribution_version))
        except InvalidVersion as exc:
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_metadata_invalid",
                "installed framework distribution has an invalid package version",
            ) from exc
        if normalized_version != self.distribution_version:
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_metadata_invalid",
                "installed framework distribution version is not canonical",
            )
        paths = tuple(member.path for member in self.members)
        if not paths or paths != tuple(sorted(set(paths))):
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_payload_invalid",
                "installed framework payload must be non-empty, unique, and canonical",
            )

    def identity_material(self) -> dict[str, object]:
        """Return portable material; absolute installation paths are never projected."""

        return {
            "schema": INSTALLED_FRAMEWORK_DISTRIBUTION_SCHEMA,
            "distribution_name": self.distribution_name,
            "distribution_version": self.distribution_version,
            "members": [member.to_dict() for member in self.members],
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_material())


class _DistributionLike(Protocol):
    metadata: Any
    files: Iterable[Any] | None
    version: str

    def locate_file(self, path: Any) -> Path: ...

    def read_text(self, filename: str) -> str | None: ...


DistributionFinder = Callable[[str], Iterable[_DistributionLike]]
PolicyLoader = Callable[[], StandardLifecyclePolicy]
DistributionObserver = Callable[[], InstalledFrameworkDistribution]
OriginObserver = Callable[[str], ProjectInitializationOrigin]


def _default_distribution_finder(name: str) -> Iterable[_DistributionLike]:
    return importlib.metadata.distributions(name=name)


def _distribution_name(distribution: _DistributionLike) -> str:
    try:
        value = distribution.metadata.get("Name")
    except (AttributeError, TypeError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_metadata_invalid",
            "installed framework distribution metadata is unreadable",
        ) from exc
    if not isinstance(value, str) or not value.strip():
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_metadata_invalid",
            "installed framework distribution metadata lacks its package name",
        )
    return canonicalize_name(value)


def _require_non_editable(distribution: _DistributionLike) -> None:
    try:
        direct_url = distribution.read_text("direct_url.json")
    except (OSError, UnicodeError, AttributeError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_metadata_invalid",
            "installed framework direct-origin metadata is unreadable",
        ) from exc
    if direct_url is None:
        return
    try:
        value = json.loads(direct_url)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_metadata_invalid",
            "installed framework direct-origin metadata is invalid",
        ) from exc
    if not isinstance(value, dict):
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_metadata_invalid",
            "installed framework direct-origin metadata is invalid",
        )
    directory = value.get("dir_info")
    if isinstance(directory, dict) and directory.get("editable") is True:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_editable",
            "an editable installation cannot attest the imported framework payload; "
            "install the literate-ai wheel into a clean environment",
        )


def _logical_payload_path(entry: PurePosixPath) -> str | None:
    parts = entry.parts
    if entry.is_absolute() or not parts or any(part in {"", "."} for part in parts):
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_payload_invalid",
            "installed framework distribution contains an invalid recorded path",
        )
    lowered = tuple(part.casefold() for part in parts)
    if entry.suffix.casefold() in {".pyc", ".pyo"}:
        return None
    if (
        any(part.startswith("__editable__") for part in lowered)
        or entry.suffix.casefold() == ".pth"
    ):
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_editable",
            "editable installation machinery cannot attest framework source bytes",
        )
    if "literate_ai" in parts:
        index = parts.index("literate_ai")
        return PurePosixPath(*parts[index:]).as_posix()
    for index in range(len(parts) - 3):
        if lowered[index : index + 3] == ("share", "literate-ai", "schemas"):
            return PurePosixPath(*parts[index:]).as_posix()
    for index, part in enumerate(parts):
        if _DIST_INFO_DIRECTORY.fullmatch(part):
            suffix = parts[index + 1 :]
            if not suffix:
                raise StandardLifecycleBindingError(
                    "standard_binding.distribution_payload_invalid",
                    "installed framework distribution contains an invalid "
                    "metadata path",
                )
            if suffix[-1].casefold() in _DIST_INFO_EXCLUSIONS:
                return None
            return PurePosixPath(part, *suffix).as_posix()
    # Installer-created console launchers are deliberately outside the immutable wheel
    # payload.  Any other unknown data member is rejected rather than silently omitted.
    if entry.name.casefold() in _CONSOLE_LAUNCHER_NAMES and any(
        part.casefold() in {"bin", "scripts"} for part in parts[:-1]
    ):
        return None
    raise StandardLifecycleBindingError(
        "standard_binding.distribution_payload_invalid",
        "installed framework distribution contains an unsupported payload location",
    )


def _read_stable_member(path: Path) -> tuple[int, str]:
    if path.is_symlink():
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_payload_unsafe",
            "installed framework payload contains a symbolic link",
        )
    try:
        before = path.stat()
        if not path.is_file():
            raise OSError
        digest = hashlib.sha256()
        observed_size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                observed_size += len(chunk)
                digest.update(chunk)
        after = path.stat()
    except OSError as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_payload_unavailable",
            "installed framework payload member is unavailable",
        ) from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if observed_size != before.st_size or any(
        getattr(before, field) != getattr(after, field) for field in stable_fields
    ):
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_changed",
            "installed framework payload changed while it was being observed",
        )
    return observed_size, "sha256:" + digest.hexdigest()


def observe_installed_framework_distribution(
    *, distribution_finder: DistributionFinder = _default_distribution_finder
) -> InstalledFrameworkDistribution:
    """Observe one exact non-editable installed literate-ai wheel."""

    try:
        candidates = tuple(distribution_finder(_FRAMEWORK_DISTRIBUTION_NAME))
    except Exception as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_unavailable",
            "the installed literate-ai distribution cannot be discovered",
        ) from exc
    if not candidates:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_unavailable",
            "the installed literate-ai distribution is unavailable",
        )
    if len(candidates) == 1:
        distribution = candidates[0]
    else:
        non_editable = []
        for candidate in candidates:
            try:
                _require_non_editable(candidate)
            except StandardLifecycleBindingError as exc:
                if exc.code != "standard_binding.distribution_editable":
                    raise
            else:
                non_editable.append(candidate)
        if len(non_editable) != 1:
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_ambiguous",
                "exactly one non-editable installed literate-ai distribution is "
                "required",
            )
        distribution = non_editable[0]
    name = _distribution_name(distribution)
    if name != _FRAMEWORK_DISTRIBUTION_NAME:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_metadata_invalid",
            "the discovered distribution is not literate-ai",
        )
    _require_non_editable(distribution)
    try:
        version = str(Version(distribution.version))
    except (AttributeError, InvalidVersion, TypeError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_metadata_invalid",
            "installed framework distribution has an invalid package version",
        ) from exc
    try:
        recorded = tuple(distribution.files or ())
    except (AttributeError, TypeError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_payload_invalid",
            "installed framework distribution lacks a bounded file inventory",
        ) from exc
    if not recorded or len(recorded) > _MAXIMUM_PAYLOAD_FILES:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_payload_invalid",
            "installed framework distribution lacks a bounded file inventory",
        )
    members: list[InstalledFrameworkDistributionMember] = []
    total_size = 0
    for raw_entry in recorded:
        entry = PurePosixPath(str(raw_entry))
        logical = _logical_payload_path(entry)
        if logical is None:
            continue
        try:
            located = Path(distribution.locate_file(raw_entry))
        except (AttributeError, OSError, TypeError) as exc:
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_payload_unavailable",
                "installed framework payload member cannot be located",
            ) from exc
        size, digest = _read_stable_member(located)
        total_size += size
        if total_size > _MAXIMUM_PAYLOAD_BYTES:
            raise StandardLifecycleBindingError(
                "standard_binding.distribution_payload_invalid",
                "installed framework payload exceeds the byte limit",
            )
        members.append(InstalledFrameworkDistributionMember(logical, size, digest))
    logical_paths = {member.path for member in members}
    if (
        "literate_ai/__init__.py" not in logical_paths
        or not any(
            path.startswith("literate_ai/standard_policies/") and path.endswith(".json")
            for path in logical_paths
        )
        or not any(
            path.startswith("share/literate-ai/schemas/") for path in logical_paths
        )
    ):
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_payload_incomplete",
            "installed framework payload omits package, policy, or schema authority",
        )
    return InstalledFrameworkDistribution(
        name, version, tuple(sorted(members, key=lambda item: item.path))
    )


def observe_installed_framework_origin(
    distribution_version: str,
) -> ProjectInitializationOrigin:
    """Read the immutable origin embedded by the wheel build backend.

    ``direct_url.json`` describes an installer's transport and may name a temporary
    checkout. It is not source authority. A reviewed Standard rebind therefore uses
    only the bounded origin record included in the observed wheel payload and rejects
    source checkouts or older distributions that do not carry it.
    """

    try:
        resource = importlib.resources.files("literate_ai").joinpath(
            "_distribution_origin.json"
        )
        if not resource.is_file():
            raise FileNotFoundError
        raw = resource.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_origin_unavailable",
            "installed framework wheel lacks embedded distribution origin evidence",
        ) from exc
    if not raw or len(raw) > 64 * 1024:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_origin_invalid",
            "installed framework wheel has invalid embedded origin evidence",
        )
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_origin_invalid",
            "installed framework wheel has invalid embedded origin evidence",
        ) from exc
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "repository_url",
        "git_revision",
    }:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_origin_invalid",
            "installed framework wheel has invalid embedded origin evidence",
        )
    if value.get("schema") != EMBEDDED_DISTRIBUTION_ORIGIN_SCHEMA:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_origin_invalid",
            "installed framework wheel has unsupported origin evidence",
        )
    try:
        origin = ProjectInitializationOrigin(
            repository_url=value["repository_url"],
            git_revision=value["git_revision"],
            distribution_name=_FRAMEWORK_DISTRIBUTION_NAME,
            distribution_version=distribution_version,
        )
    except (TypeError, ValueError) as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_origin_invalid",
            "installed framework wheel has invalid embedded origin evidence",
        ) from exc
    return origin


@dataclass(frozen=True, slots=True)
class ResolvedStandardProjectLifecycleDriver:
    """A configured Standard binding resolved to exact current installed authority."""

    driver: StandardProjectLifecycleDriver
    distribution: InstalledFrameworkDistribution
    policy: StandardLifecyclePolicy
    _distribution_observer: DistributionObserver = field(repr=False, compare=False)
    _policy_loader: PolicyLoader = field(repr=False, compare=False)

    @property
    def trust_binding(self) -> LifecycleDriverTrustBinding:
        return LifecycleDriverTrustBinding(
            trust=LifecycleDriverTrust.STANDARD,
            driver_identity=self.driver.identity,
            policy_identity=self.policy.identity,
            framework_distribution_identity=self.distribution.identity,
            project_authorization_identity=None,
        )

    def require_unchanged(self) -> None:
        """Reject installed payload or policy drift since initial resolution."""

        try:
            distribution = self._distribution_observer()
            policy = self._policy_loader()
        except Exception as exc:
            raise StandardLifecycleBindingError(
                "standard_binding.changed",
                "Standard lifecycle authority became unavailable after resolution",
            ) from exc
        if distribution != self.distribution or policy != self.policy:
            raise StandardLifecycleBindingError(
                "standard_binding.changed",
                "Standard lifecycle authority changed after resolution",
            )


def resolve_standard_project_lifecycle_driver(
    driver: StandardProjectLifecycleDriver,
    *,
    distribution_observer: DistributionObserver = (
        observe_installed_framework_distribution
    ),
    policy_loader: PolicyLoader = load_current_standard_lifecycle_policy,
) -> ResolvedStandardProjectLifecycleDriver:
    """Resolve a project pin without substituting a different distribution or policy."""

    if not isinstance(driver, StandardProjectLifecycleDriver):
        raise TypeError("driver must be a StandardProjectLifecycleDriver")
    distribution = distribution_observer()
    if distribution.identity != driver.framework_distribution_identity:
        raise StandardLifecycleBindingError(
            "standard_binding.distribution_mismatch",
            "project-pinned framework distribution differs from installed wheel bytes",
        )
    try:
        policy = policy_loader()
    except StandardLifecycleBindingError:
        raise
    except Exception as exc:
        raise StandardLifecycleBindingError(
            "standard_binding.policy_unavailable",
            "the installed Standard lifecycle policy is unavailable",
        ) from exc
    if not isinstance(policy, StandardLifecyclePolicy):
        raise StandardLifecycleBindingError(
            "standard_binding.policy_invalid",
            "the installed Standard lifecycle policy has the wrong type",
        )
    if policy.identity != driver.policy_identity:
        raise StandardLifecycleBindingError(
            "standard_binding.policy_mismatch",
            "project-pinned Standard policy differs from installed policy",
        )
    return ResolvedStandardProjectLifecycleDriver(
        driver,
        distribution,
        policy,
        distribution_observer,
        policy_loader,
    )


__all__ = [
    "INSTALLED_FRAMEWORK_DISTRIBUTION_SCHEMA",
    "EMBEDDED_DISTRIBUTION_ORIGIN_SCHEMA",
    "InstalledFrameworkDistribution",
    "InstalledFrameworkDistributionMember",
    "ResolvedStandardProjectLifecycleDriver",
    "StandardLifecycleBindingError",
    "observe_installed_framework_distribution",
    "observe_installed_framework_origin",
    "resolve_standard_project_lifecycle_driver",
]

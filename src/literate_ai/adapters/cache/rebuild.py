"""Driver-side adapter for the typed ``litai rebuild`` source-cache protocol."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from literate_ai.contracts.generation_cache import (
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceDerivationCacheKey,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.rebuild_cache import (
    RebuildSourceCacheControl,
    RebuildSourceCacheDecision,
    RebuildSourceCacheDecisionItem,
    RebuildSourceCacheDerivationManifest,
    RebuildSourceCacheLifecycleBinding,
    RebuildSourceCacheOutcome,
    RebuildSourceCachePublicationOffer,
)
from literate_ai.projects import LoadedProject

from .filesystem import (
    FinalPathSourceIntelligenceVerifier,
    MaterializedCachedSource,
    SourceCacheMaterializer,
    SourceCacheResolver,
    project_source_cache_protected_paths,
)

SOURCE_CACHE_PROTOCOL_DIRECTORY = ".litai"
SOURCE_CACHE_CONTROL_FILENAME = "source-cache-control.json"
SOURCE_CACHE_DECISION_FILENAME = "source-cache-decision.json"
SOURCE_CACHE_PUBLICATION_FILENAME = "source-cache-publication.json"
SOURCE_CACHE_LIFECYCLE_FILENAME = "source-cache-lifecycle.json"
SOURCE_CACHE_DERIVATION_MANIFEST_FILENAME = "source-cache-derivations.json"
SOURCE_CACHE_CONTROL_ENVIRONMENT = "LITAI_SOURCE_CACHE_CONTROL"
SOURCE_CACHE_CONTROL_IDENTITY_ENVIRONMENT = "LITAI_SOURCE_CACHE_CONTROL_IDENTITY"
SOURCE_CACHE_DECISION_ENVIRONMENT = "LITAI_SOURCE_CACHE_DECISION"
SOURCE_CACHE_DECISION_IDENTITY_ENVIRONMENT = "LITAI_SOURCE_CACHE_DECISION_IDENTITY"
SOURCE_CACHE_PUBLICATION_ENVIRONMENT = "LITAI_SOURCE_CACHE_PUBLICATION"
SOURCE_CACHE_LIFECYCLE_ENVIRONMENT = "LITAI_SOURCE_CACHE_LIFECYCLE"
SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT = "LITAI_SOURCE_CACHE_DERIVATION_MANIFEST"
SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT = (
    "LITAI_SOURCE_CACHE_PLANNING_REQUEST_IDENTITY"
)
SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT = "LITAI_LIFECYCLE_MODE"
SOURCE_CACHE_PLANNING_MODE = "plan-derivations"

_MAXIMUM_PROTOCOL_BYTES = 32 * 1024 * 1024
_T = TypeVar("_T")


class RebuildSourceCacheProtocolError(RuntimeError):
    """Stable failure at the cache-aware lifecycle-driver boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProtocolDirectoryIdentity:
    """Stable host identity of one protocol directory."""

    device: int
    inode: int


def protocol_directory_identity(path: Path) -> ProtocolDirectoryIdentity:
    """Identify one canonical non-symbolic directory."""

    candidate = Path(os.path.abspath(path))
    try:
        observed = os.stat(candidate, follow_symlinks=False)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or candidate.resolve(strict=True) != candidate
        ):
            raise OSError
    except (OSError, RuntimeError) as exc:
        raise RebuildSourceCacheProtocolError(
            "source-cache.protocol-directory-invalid",
            "source-cache protocol directory is unavailable or symbolic",
        ) from exc
    return ProtocolDirectoryIdentity(observed.st_dev, observed.st_ino)


def source_cache_protocol_paths(runtime_root: Path) -> tuple[Path, Path, Path, Path]:
    """Return fixed control, decision, lifecycle, and publication paths."""

    root = Path(runtime_root)
    protocol = root / SOURCE_CACHE_PROTOCOL_DIRECTORY
    return (
        protocol / SOURCE_CACHE_CONTROL_FILENAME,
        protocol / SOURCE_CACHE_DECISION_FILENAME,
        protocol / SOURCE_CACHE_LIFECYCLE_FILENAME,
        protocol / SOURCE_CACHE_PUBLICATION_FILENAME,
    )


def source_cache_derivation_manifest_path(runtime_root: Path) -> Path:
    """Return the fixed outer-planning result path."""

    return (
        Path(runtime_root)
        / SOURCE_CACHE_PROTOCOL_DIRECTORY
        / (SOURCE_CACHE_DERIVATION_MANIFEST_FILENAME)
    )


def read_rebuild_source_cache_control(
    path: Path,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> RebuildSourceCacheControl:
    return _read_contract(
        path,
        RebuildSourceCacheControl.from_dict,
        label="source-cache control",
        directory_identity=directory_identity,
    )


def read_rebuild_source_cache_derivation_manifest(
    path: Path,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> RebuildSourceCacheDerivationManifest:
    return _read_contract(
        path,
        RebuildSourceCacheDerivationManifest.from_dict,
        label="source-cache derivation manifest",
        directory_identity=directory_identity,
    )


def read_rebuild_source_cache_decision(
    path: Path,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> RebuildSourceCacheDecision:
    return _read_contract(
        path,
        RebuildSourceCacheDecision.from_dict,
        label="source-cache decision",
        directory_identity=directory_identity,
    )


def read_rebuild_source_cache_lifecycle(
    path: Path,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> RebuildSourceCacheLifecycleBinding:
    return _read_contract(
        path,
        RebuildSourceCacheLifecycleBinding.from_dict,
        label="source-cache current lifecycle binding",
        directory_identity=directory_identity,
    )


def read_rebuild_source_cache_publication(
    path: Path,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> RebuildSourceCachePublicationOffer:
    return _read_contract(
        path,
        RebuildSourceCachePublicationOffer.from_dict,
        label="source-cache publication offer",
        directory_identity=directory_identity,
    )


def write_rebuild_source_cache_control(
    path: Path,
    control: RebuildSourceCacheControl,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> None:
    if not isinstance(control, RebuildSourceCacheControl):
        raise TypeError("source-cache control must use its public contract")
    _write_new_contract(
        path,
        control.to_dict(),
        label="source-cache control",
        directory_identity=directory_identity,
    )


def write_rebuild_source_cache_derivation_manifest(
    path: Path,
    manifest: RebuildSourceCacheDerivationManifest,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> None:
    if not isinstance(manifest, RebuildSourceCacheDerivationManifest):
        raise TypeError("derivation manifest must use its public contract")
    _write_new_contract(
        path,
        manifest.to_dict(),
        label="source-cache derivation manifest",
        directory_identity=directory_identity,
    )


def write_rebuild_source_cache_decision(
    path: Path,
    decision: RebuildSourceCacheDecision,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> None:
    if not isinstance(decision, RebuildSourceCacheDecision):
        raise TypeError("source-cache decision must use its public contract")
    _write_new_contract(
        path,
        decision.to_dict(),
        label="source-cache decision",
        directory_identity=directory_identity,
    )


def write_rebuild_source_cache_lifecycle(
    path: Path,
    lifecycle: RebuildSourceCacheLifecycleBinding,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> None:
    if not isinstance(lifecycle, RebuildSourceCacheLifecycleBinding):
        raise TypeError("source-cache lifecycle must use its public contract")
    _write_new_contract(
        path,
        lifecycle.to_dict(),
        label="source-cache current lifecycle binding",
        directory_identity=directory_identity,
    )


def write_rebuild_source_cache_publication(
    path: Path,
    offer: RebuildSourceCachePublicationOffer,
    *,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> None:
    if not isinstance(offer, RebuildSourceCachePublicationOffer):
        raise TypeError("source-cache publication offer must use its public contract")
    _write_new_contract(
        path,
        offer.to_dict(),
        label="source-cache publication offer",
        directory_identity=directory_identity,
    )


def source_cache_protocol_path_present(
    path: Path,
    *,
    directory_identity: ProtocolDirectoryIdentity,
) -> bool:
    """Check one protocol namespace entry through the pinned parent directory."""

    candidate = Path(os.path.abspath(path))
    parent_descriptor: int | None = None
    try:
        parent_descriptor, pinned_parent = _open_protocol_parent(
            candidate.parent, expected=directory_identity
        )
        try:
            _stat_protocol_leaf(candidate, parent_descriptor=parent_descriptor)
        except FileNotFoundError:
            present = False
        else:
            present = True
        _require_protocol_parent(
            candidate.parent,
            pinned_parent,
            parent_descriptor=parent_descriptor,
        )
        return present
    except (OSError, RuntimeError) as exc:
        raise RebuildSourceCacheProtocolError(
            "source-cache.protocol-invalid",
            "source-cache protocol namespace changed during inspection",
        ) from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


class RebuildSourceCacheSession:
    """Resolve exact keys for a cache-aware, content-pinned lifecycle driver.

    A returned hit is structurally verified and materialized at its final path.  It is
    deliberately *not* current acceptance evidence; the caller must continue through
    the same validation, authorization, build, test, execution, independent acceptance,
    and workspace-admission phases as newly generated source.
    """

    def __init__(
        self,
        control: RebuildSourceCacheControl,
        *,
        project: LoadedProject,
    ) -> None:
        if not isinstance(control, RebuildSourceCacheControl):
            raise TypeError("cache session requires RebuildSourceCacheControl")
        if not isinstance(project, LoadedProject):
            raise TypeError("cache session requires a loaded project")
        if project.definition.source_cache != control.configuration:
            raise RebuildSourceCacheProtocolError(
                "source-cache.configuration-mismatch",
                "cache control does not match the loaded project configuration",
            )
        self.control = control
        self.project = project
        self._items: dict[str, RebuildSourceCacheDecisionItem] = {}
        self._consumed_entries: set[str] = set()
        self._expected_keys = {
            key.identity.uri: key for key in control.derivation_manifest.cache_keys
        }
        configuration = control.configuration
        self._resolver = (
            None
            if configuration is None or not configuration.mode.can_read
            else SourceCacheResolver.from_configuration(
                SourceCacheConfiguration(
                    SourceCacheMode.READ_ONLY,
                    configuration.targets,
                    write_target_id=None,
                    require_unique=False,
                ),
                project=project,
                operator_roots={
                    item.reference: Path(item.path) for item in control.operator_roots
                },
            )
        )
        self._materializer = SourceCacheMaterializer(
            protected_paths=project_source_cache_protected_paths(project)
        )

    def resolve(
        self,
        key: SourceDerivationCacheKey,
        *,
        destination: Path,
        intelligence_verifier: FinalPathSourceIntelligenceVerifier | None = None,
    ) -> MaterializedCachedSource | None:
        """Resolve one derivation and materialize a selected untrusted hit."""

        if not isinstance(key, SourceDerivationCacheKey):
            raise TypeError("cache resolution requires a SourceDerivationCacheKey")
        key_identity = key.identity.uri
        if (
            key_identity not in self._expected_keys
            or self._expected_keys[key_identity] != key
        ):
            raise RebuildSourceCacheProtocolError(
                "source-cache.key-unplanned",
                "cache resolution key is absent from the outer-owned derivation "
                "manifest",
            )
        if key_identity in self._items:
            raise RebuildSourceCacheProtocolError(
                "source-cache.key-duplicate",
                "one rebuild may resolve each exact derivation key only once",
            )
        configuration = self.control.configuration
        if configuration is None or configuration.mode is SourceCacheMode.OFF:
            raise RebuildSourceCacheProtocolError(
                "source-cache.read-unconfigured",
                "an unconfigured or disabled cache has a key-free fresh decision",
            )
        if self.control.force_regeneration:
            self._items[key_identity] = RebuildSourceCacheDecisionItem(
                key,
                RebuildSourceCacheOutcome.FORCED_REGENERATION,
            )
            return None
        if not configuration.mode.can_read:
            self._items[key_identity] = RebuildSourceCacheDecisionItem(
                key,
                RebuildSourceCacheOutcome.READ_DISABLED,
            )
            return None
        assert self._resolver is not None
        candidates = self._resolver.resolve(key)
        if not candidates:
            self._items[key_identity] = RebuildSourceCacheDecisionItem(
                key,
                RebuildSourceCacheOutcome.MISS,
            )
            return None
        planned_locks = frozenset(
            identity.uri for identity in self.control.component_lock_identities
        )
        if any(
            candidate.entry.derivation.component_lock_identity.uri not in planned_locks
            for candidate in candidates
        ):
            raise RebuildSourceCacheProtocolError(
                "source-cache.component-lock-mismatch",
                "an exact-key cache candidate binds a Component lock outside the "
                "outer-owned rebuild plan",
            )
        requested = {item.uri for item in self.control.requested_entry_identities}
        explicitly_matching = [
            candidate for candidate in candidates if candidate.identity.uri in requested
        ]
        if len(explicitly_matching) > 1:
            raise RebuildSourceCacheProtocolError(
                "source-cache.selection-conflict",
                "multiple explicitly selected entries name one derivation key",
            )
        if explicitly_matching:
            selected = explicitly_matching[0]
        elif len(candidates) == 1:
            selected = candidates[0]
        else:
            raise RebuildSourceCacheProtocolError(
                "source-cache.ambiguous",
                "an exact key has multiple entries; select one with "
                "--source-cache-entry",
            )
        materialized = self._materializer.materialize(
            selected,
            Path(destination),
            intelligence_verifier=intelligence_verifier,
        )
        self._consumed_entries.add(selected.identity.uri)
        self._items[key_identity] = RebuildSourceCacheDecisionItem(
            cache_key=key,
            outcome=RebuildSourceCacheOutcome.HIT,
            candidate_identities=tuple(
                sorted(
                    (item.identity for item in candidates),
                    key=lambda item: item.uri,
                )
            ),
            selected_entry_identity=selected.identity,
            source_tree_identity=selected.entry.derivation.source_tree_identity,
            current_acceptance_trusted=False,
            generation_skipped=True,
        )
        return materialized

    def decision(self) -> RebuildSourceCacheDecision:
        """Finalize after every project derivation was resolved."""

        configuration = self.control.configuration
        if configuration is None or configuration.mode is SourceCacheMode.OFF:
            return RebuildSourceCacheDecision.fresh(self.control)
        requested = {item.uri for item in self.control.requested_entry_identities}
        missing = requested - self._consumed_entries
        if missing:
            raise RebuildSourceCacheProtocolError(
                "source-cache.entry-not-found",
                "one or more explicitly selected entries were not exact candidates",
            )
        items = tuple(self._items[key] for key in sorted(self._items))
        missing_keys = set(self._expected_keys) - set(self._items)
        if missing_keys:
            raise RebuildSourceCacheProtocolError(
                "source-cache.decision-incomplete",
                "cache decision omitted one or more outer-planned derivation keys",
            )
        decision = RebuildSourceCacheDecision(
            control_identity=self.control.identity,
            lifecycle_request_identity=self.control.lifecycle_request_identity,
            configuration_identity=self.control.configuration_identity,
            mode=configuration.mode.value,
            fresh_generation_required=any(
                item.outcome is not RebuildSourceCacheOutcome.HIT for item in items
            ),
            items=items,
        )
        decision.validate_against(self.control)
        return decision


def _read_contract(
    path: Path,
    parser: Callable[..., _T],
    *,
    label: str,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> _T:
    candidate = Path(os.path.abspath(path))
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor, pinned_parent = _open_protocol_parent(
            candidate.parent, expected=directory_identity
        )
        descriptor = _open_protocol_leaf(
            candidate,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            parent_descriptor=parent_descriptor,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAXIMUM_PROTOCOL_BYTES
        ):
            raise ValueError
        chunks: list[bytes] = []
        remaining = _MAXIMUM_PROTOCOL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        named = _stat_protocol_leaf(candidate, parent_descriptor=parent_descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(raw) != before.st_size
            or len(raw) > _MAXIMUM_PROTOCOL_BYTES
            or before_identity != after_identity
            or not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError
        _require_protocol_parent(
            candidate.parent,
            pinned_parent,
            parent_descriptor=parent_descriptor,
        )
        value = json.loads(raw.decode("utf-8"))
        if canonical_json_bytes(value) != raw:
            raise ValueError
        result = parser(value)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise RebuildSourceCacheProtocolError(
            "source-cache.protocol-invalid",
            f"{label} is absent, non-canonical, or invalid",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    return result


def _write_new_contract(
    path: Path,
    value: Any,
    *,
    label: str,
    directory_identity: ProtocolDirectoryIdentity | None = None,
) -> None:
    destination = Path(os.path.abspath(path))
    parent_descriptor: int | None = None
    descriptor: int | None = None
    written_identity: ProtocolDirectoryIdentity | None = None
    try:
        parent_descriptor, pinned_parent = _open_protocol_parent(
            destination.parent, expected=directory_identity
        )
        raw = canonical_json_bytes(value)
        if not raw or len(raw) > _MAXIMUM_PROTOCOL_BYTES:
            raise ValueError
        descriptor = _open_protocol_leaf(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode=0o600,
            parent_descriptor=parent_descriptor,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
            written_identity = ProtocolDirectoryIdentity(written.st_dev, written.st_ino)
            named = _stat_protocol_leaf(
                destination, parent_descriptor=parent_descriptor
            )
            if (
                not stat.S_ISREG(written.st_mode)
                or written.st_size != len(raw)
                or not stat.S_ISREG(named.st_mode)
                or (named.st_dev, named.st_ino) != (written.st_dev, written.st_ino)
            ):
                raise OSError
            _require_protocol_parent(
                destination.parent,
                pinned_parent,
                parent_descriptor=parent_descriptor,
            )
        except Exception:
            _unlink_protocol_leaf_if_identity(
                destination,
                written_identity,
                parent_descriptor=parent_descriptor,
            )
            raise
    except (OSError, RecursionError, RuntimeError, TypeError, ValueError) as exc:
        raise RebuildSourceCacheProtocolError(
            "source-cache.protocol-write-failed",
            f"{label} could not be written exactly once",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _open_protocol_parent(
    parent: Path,
    *,
    expected: ProtocolDirectoryIdentity | None,
) -> tuple[int | None, ProtocolDirectoryIdentity]:
    candidate = Path(os.path.abspath(parent))
    if candidate.resolve(strict=True) != candidate:
        raise OSError
    if os.name != "nt" and all(
        operation in os.supports_dir_fd for operation in (os.open, os.stat, os.unlink)
    ):
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            observed = os.fstat(descriptor)
            named = os.stat(candidate, follow_symlinks=False)
            identity = ProtocolDirectoryIdentity(observed.st_dev, observed.st_ino)
            if (
                not stat.S_ISDIR(observed.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != (identity.device, identity.inode)
                or (expected is not None and identity != expected)
            ):
                raise OSError
            return descriptor, identity
        except BaseException:
            os.close(descriptor)
            raise
    # Python's portable Windows API does not expose a directory handle usable for
    # descriptor-relative, reparse-point-safe leaf I/O. The fallback pins and
    # rechecks the named directory, which catches ordinary replacement but cannot
    # exclude a same-user replace-and-restore race. The protocol directory is
    # therefore part of the same-user TCB on Windows; stronger deployments need a
    # native handle-backed adapter or an externally isolated runtime directory.
    observed = os.stat(candidate, follow_symlinks=False)
    identity = ProtocolDirectoryIdentity(observed.st_dev, observed.st_ino)
    if not stat.S_ISDIR(observed.st_mode) or (
        expected is not None and identity != expected
    ):
        raise OSError
    return None, identity


def _open_protocol_leaf(
    path: Path,
    flags: int,
    *,
    parent_descriptor: int | None,
    mode: int = 0o777,
) -> int:
    if parent_descriptor is not None:
        return os.open(path.name, flags, mode, dir_fd=parent_descriptor)
    return os.open(path, flags, mode)


def _stat_protocol_leaf(path: Path, *, parent_descriptor: int | None) -> os.stat_result:
    if parent_descriptor is not None:
        return os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
    return os.stat(path, follow_symlinks=False)


def _require_protocol_parent(
    path: Path,
    expected: ProtocolDirectoryIdentity,
    *,
    parent_descriptor: int | None,
) -> None:
    observed = (
        os.fstat(parent_descriptor)
        if parent_descriptor is not None
        else os.stat(path, follow_symlinks=False)
    )
    named = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or (observed.st_dev, observed.st_ino) != (expected.device, expected.inode)
        or (named.st_dev, named.st_ino) != (expected.device, expected.inode)
    ):
        raise OSError


def _unlink_protocol_leaf_if_identity(
    path: Path,
    expected: ProtocolDirectoryIdentity | None,
    *,
    parent_descriptor: int | None,
) -> None:
    if expected is None:
        return
    try:
        observed = _stat_protocol_leaf(path, parent_descriptor=parent_descriptor)
        if (observed.st_dev, observed.st_ino) != (
            expected.device,
            expected.inode,
        ):
            return
        if parent_descriptor is not None:
            os.unlink(path.name, dir_fd=parent_descriptor)
        else:
            path.unlink()
    except OSError:
        return


__all__ = [
    "ProtocolDirectoryIdentity",
    "RebuildSourceCacheProtocolError",
    "RebuildSourceCacheSession",
    "SOURCE_CACHE_CONTROL_ENVIRONMENT",
    "SOURCE_CACHE_CONTROL_FILENAME",
    "SOURCE_CACHE_CONTROL_IDENTITY_ENVIRONMENT",
    "SOURCE_CACHE_DECISION_ENVIRONMENT",
    "SOURCE_CACHE_DECISION_FILENAME",
    "SOURCE_CACHE_DECISION_IDENTITY_ENVIRONMENT",
    "SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT",
    "SOURCE_CACHE_DERIVATION_MANIFEST_FILENAME",
    "SOURCE_CACHE_LIFECYCLE_ENVIRONMENT",
    "SOURCE_CACHE_LIFECYCLE_FILENAME",
    "SOURCE_CACHE_PROTOCOL_DIRECTORY",
    "SOURCE_CACHE_PLANNING_MODE",
    "SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT",
    "SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT",
    "SOURCE_CACHE_PUBLICATION_ENVIRONMENT",
    "SOURCE_CACHE_PUBLICATION_FILENAME",
    "read_rebuild_source_cache_control",
    "read_rebuild_source_cache_derivation_manifest",
    "read_rebuild_source_cache_decision",
    "read_rebuild_source_cache_lifecycle",
    "read_rebuild_source_cache_publication",
    "protocol_directory_identity",
    "source_cache_protocol_path_present",
    "source_cache_protocol_paths",
    "source_cache_derivation_manifest_path",
    "write_rebuild_source_cache_control",
    "write_rebuild_source_cache_derivation_manifest",
    "write_rebuild_source_cache_decision",
    "write_rebuild_source_cache_lifecycle",
    "write_rebuild_source_cache_publication",
]

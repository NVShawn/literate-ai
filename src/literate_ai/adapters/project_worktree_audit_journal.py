"""Private immutable input-observation history; not migration/removal authority."""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    HashAlgorithm,
    canonical_identity,
    canonical_json_bytes,
)

from .project_worktree_audit_inputs import (
    AUDIT_INPUT_SCHEMA,
    PreparedWorktreeAuditInputs,
)
from .project_worktrees import ProjectWorktreeError
from .repository_orchestration import _read_document

_MAX_OBJECT_BYTES = 64 * 1024 * 1024
_MAX_STORE_BYTES = 1024 * 1024 * 1024
_MAX_FILES = 8192
_MAX_EVENTS = 4096
_READ_SECONDS = 120
_EVENT_SCHEMA = "literate-ai/worktree-audit-input-event@1"
_HEAD_SCHEMA = "literate-ai/worktree-audit-input-head@1"


@dataclass(frozen=True, slots=True)
class StoredAuditInputObservation:
    event_identity: ContentIdentity
    observation_identity: ContentIdentity
    canonical_bytes: bytes
    history_length: int = 1

    def to_dict(self):
        # Stored JSON never reconstructs the live capture owner or grants retirement.
        return json.loads(self.canonical_bytes)


def _key(identity):
    if (
        not isinstance(identity, ContentIdentity)
        or identity.algorithm is not HashAlgorithm.SHA256
    ):
        raise ProjectWorktreeError("worktree journal requires a SHA-256 identity")
    return identity.digest


def _private(path, *, create=False):
    if create:
        ensure_safe_directory(path)
    else:
        require_safe_directory(path)
    node = path.lstat()
    if os.name != "nt" and (
        node.st_uid != os.getuid() or stat.S_IMODE(node.st_mode) & 0o077
    ):
        raise ProjectWorktreeError("worktree journal directory must be private")
    return node.st_dev, node.st_ino


def _slot(checkout):
    return canonical_identity(
        {"schema": "literate-ai/worktree-audit-slot@1", "checkout": str(checkout)}
    ).digest


def _read_json(path, *, maximum_bytes=_MAX_OBJECT_BYTES):
    raw = _read_document(
        path, label="worktree observation journal", maximum_bytes=maximum_bytes
    )
    node = path.lstat()
    if os.name != "nt" and (
        node.st_uid != os.getuid() or stat.S_IMODE(node.st_mode) & 0o077
    ):
        raise ProjectWorktreeError("worktree journal file must be private")
    document = json.loads(raw)
    if not isinstance(document, dict) or canonical_json_bytes(document) != raw:
        raise ProjectWorktreeError("worktree journal JSON is not canonical")
    return document, raw


def _object(path, expected):
    document, raw = _read_json(path)
    if canonical_identity(document) != expected:
        raise ProjectWorktreeError("worktree journal content identity mismatch")
    return document, raw


def _current(scope, common_identity, checkout):
    head = scope / "heads" / (_slot(checkout) + ".json")
    if not os.path.lexists(head):
        return None
    document, _ = _read_json(head, maximum_bytes=4096)
    if (
        set(document) != {"schema", "repository_identity", "checkout", "event_identity"}
        or document["schema"] != _HEAD_SCHEMA
    ):
        raise ProjectWorktreeError("worktree journal head schema is invalid")
    if document["repository_identity"] != common_identity.to_dict() or document[
        "checkout"
    ] != str(checkout):
        raise ProjectWorktreeError("worktree journal head belongs to different custody")
    current = ContentIdentity.from_dict(document["event_identity"])
    first = current
    seen, observations = set(), set()
    result = None
    deadline = time.monotonic() + _READ_SECONDS
    while current is not None:
        if time.monotonic() >= deadline or len(seen) >= _MAX_EVENTS:
            raise ProjectWorktreeError("worktree journal history exceeds its bound")
        if current in seen:
            raise ProjectWorktreeError("worktree journal event history is cyclic")
        seen.add(current)
        event, _ = _object(scope / "events" / (_key(current) + ".json"), current)
        if set(event) != {
            "schema",
            "repository_identity",
            "checkout",
            "observation_identity",
            "previous_event_identity",
            "audit_complete",
            "retirement_authorized",
        }:
            raise ProjectWorktreeError("worktree journal event fields are invalid")
        if (
            event["schema"] != _EVENT_SCHEMA
            or event["audit_complete"] is not False
            or event["retirement_authorized"] is not False
        ):
            raise ProjectWorktreeError(
                "worktree input journal cannot supply audit or retirement authority"
            )
        if event["repository_identity"] != common_identity.to_dict() or event[
            "checkout"
        ] != str(checkout):
            raise ProjectWorktreeError("worktree journal event custody mismatch")
        observation = ContentIdentity.from_dict(event["observation_identity"])
        if observation not in observations:
            captured, raw = _object(
                scope / "observations" / (_key(observation) + ".json"), observation
            )
            if (
                captured.get("schema") != AUDIT_INPUT_SCHEMA
                or captured.get("audit_complete") is not False
                or captured.get("retirement_authorized") is not False
            ):
                raise ProjectWorktreeError(
                    "stored worktree inputs cannot grant authority"
                )
            if captured.get(
                "common_directory_observation_identity"
            ) != common_identity.to_dict() or captured.get("checkout") != str(checkout):
                raise ProjectWorktreeError("stored worktree input custody mismatch")
            observations.add(observation)
            if result is None:
                result = StoredAuditInputObservation(first, observation, raw)
        previous = event["previous_event_identity"]
        current = None if previous is None else ContentIdentity.from_dict(previous)
    return replace(result, history_length=len(seen))


def _quota(scope, prospective, *, additional_files=0):
    files, size = 0, 0
    if prospective > _MAX_STORE_BYTES:
        raise ProjectWorktreeError("worktree journal byte limit exceeded")
    for name in ("observations", "events", "heads"):
        _private(scope / name)
        with os.scandir(scope / name) as entries:
            for entry in entries:
                files += 1
                if files + additional_files > _MAX_FILES:
                    raise ProjectWorktreeError(
                        "worktree journal file count limit exceeded"
                    )
                node = entry.stat(follow_symlinks=False)
                if stat_is_link_or_reparse(node) or not stat.S_ISREG(node.st_mode):
                    raise ProjectWorktreeError(
                        "worktree journal contains unsafe entries"
                    )
                size += node.st_size
                if size + prospective > _MAX_STORE_BYTES:
                    raise ProjectWorktreeError("worktree journal byte limit exceeded")


def _sync_directory(path):
    if os.name != "nt":
        require_safe_directory(path)
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish(path, content, *, replace=False):
    if len(content) > _MAX_OBJECT_BYTES:
        raise ProjectWorktreeError("worktree journal object exceeds its bound")
    if not replace and os.path.lexists(path):
        _, raw = _read_json(path)
        if raw != content:
            raise ProjectWorktreeError("worktree journal object collision")
        return
    parent_node = _private(path.parent)
    temporary = path.parent / (".pending-" + uuid4().hex + ".json")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    own = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if _private(path.parent) != parent_node:
            raise ProjectWorktreeError("worktree journal directory changed")
        if replace:
            if os.path.lexists(path):
                _read_json(path, maximum_bytes=4096)
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.lexists(temporary):
            node = temporary.lstat()
            if (node.st_dev, node.st_ino) == (own.st_dev, own.st_ino) and stat.S_ISREG(
                node.st_mode
            ):
                temporary.unlink()
        _sync_directory(path.parent)


def record_worktree_audit_inputs(prepared, journal_root: Path):
    """Resume/append immutable observations after fresh live-owner revalidation.

    Interrupted pending files remain quota-accounted evidence. There is no eviction,
    source mutation, completed-audit inference or migration action in this store.
    """
    if type(prepared) is not PreparedWorktreeAuditInputs:
        raise ProjectWorktreeError("worktree journal requires live captured inputs")
    try:
        root = Path(os.path.abspath(journal_root))
        prepared = prepared.require_current()
        document = json.loads(prepared.canonical_bytes)
        common = Path(document["repository"])
        # Refuse both directions: never repurpose a containing source/Git directory.
        for protected in (*prepared.protected_source_roots, prepared.checkout, common):
            if root.is_relative_to(protected) or protected.is_relative_to(root):
                raise ProjectWorktreeError(
                    "worktree journal overlaps captured source or Git metadata"
                )
        common_identity = ContentIdentity.from_dict(
            document["common_directory_observation_identity"]
        )
        root_node = _private(root, create=True)
        scope = root / _key(common_identity)
        scope_node = _private(scope, create=True)
        for name in ("observations", "events", "heads"):
            _private(scope / name, create=True)
        with exclusive_cache_lock(scope / ".write.lock", timeout_seconds=1):
            if _private(root) != root_node or _private(scope) != scope_node:
                raise ProjectWorktreeError("worktree journal directory custody changed")
            previous = _current(scope, common_identity, prepared.checkout)
            if (
                previous is not None
                and previous.observation_identity == prepared.identity
            ):
                return previous
            if previous is not None and previous.history_length >= _MAX_EVENTS:
                raise ProjectWorktreeError("worktree journal history exceeds its bound")
            event = {
                "schema": _EVENT_SCHEMA,
                "repository_identity": common_identity.to_dict(),
                "checkout": str(prepared.checkout),
                "observation_identity": prepared.identity.to_dict(),
                "previous_event_identity": None
                if previous is None
                else previous.event_identity.to_dict(),
                "audit_complete": False,
                "retirement_authorized": False,
            }
            event_identity = canonical_identity(event)
            head = {
                "schema": _HEAD_SCHEMA,
                "repository_identity": common_identity.to_dict(),
                "checkout": str(prepared.checkout),
                "event_identity": event_identity.to_dict(),
            }
            event_bytes, head_bytes = (
                canonical_json_bytes(event),
                canonical_json_bytes(head),
            )
            _quota(
                scope,
                len(prepared.canonical_bytes) + len(event_bytes) + len(head_bytes),
                additional_files=3,
            )
            _publish(
                scope / "observations" / (_key(prepared.identity) + ".json"),
                prepared.canonical_bytes,
            )
            _publish(scope / "events" / (_key(event_identity) + ".json"), event_bytes)
            if _private(root) != root_node or _private(scope) != scope_node:
                raise ProjectWorktreeError("worktree journal directory custody changed")
            _publish(
                scope / "heads" / (_slot(prepared.checkout) + ".json"),
                head_bytes,
                replace=True,
            )
            return StoredAuditInputObservation(
                event_identity,
                prepared.identity,
                prepared.canonical_bytes,
                history_length=1 if previous is None else previous.history_length + 1,
            )
    except ProjectWorktreeError:
        raise
    except (
        OSError,
        ValueError,
        CacheLockError,
        UnsafeFilesystemPathError,
        RecursionError,
    ) as exc:
        raise ProjectWorktreeError(
            "worktree input journal could not retain observations"
        ) from exc


def load_worktree_audit_input_observation(journal_root, common_identity, checkout):
    """Read historical inputs without creating a store or a live capture owner."""
    try:
        root = Path(os.path.abspath(journal_root))
        checkout = Path(os.path.abspath(checkout))
        if not os.path.lexists(root):
            return None
        root_node = _private(root)
        scope = root / _key(common_identity)
        if not os.path.lexists(scope):
            return None
        scope_node = _private(scope)
        if not os.path.lexists(scope / ".write.lock"):
            if os.path.lexists(scope / "heads"):
                _private(scope / "heads")
                if any((scope / "heads").iterdir()):
                    raise ProjectWorktreeError(
                        "worktree journal head has no serialization custody"
                    )
            return None
        with exclusive_cache_lock(scope / ".write.lock", timeout_seconds=1):
            _quota(scope, 0)
            result = _current(scope, common_identity, checkout)
            if _private(root) != root_node or _private(scope) != scope_node:
                raise ProjectWorktreeError("worktree journal directory custody changed")
            return result
    except ProjectWorktreeError:
        raise
    except (
        OSError,
        ValueError,
        CacheLockError,
        UnsafeFilesystemPathError,
        RecursionError,
    ) as exc:
        raise ProjectWorktreeError("worktree input journal could not be read") from exc


__all__ = [
    "StoredAuditInputObservation",
    "record_worktree_audit_inputs",
    "load_worktree_audit_input_observation",
]

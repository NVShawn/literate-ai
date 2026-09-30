"""Explicit local alert-state persistence under cooperative same-host locking."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import UnsafeFilesystemPathError, stat_is_link_or_reparse
from literate_ai.adapters.dependencies.observation import _stat_binding_identity
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_package_tree import _signature
from literate_ai.adapters.worker_storage import _unique_object
from literate_ai.application.worker_alerts import WorkerAlertHistory, alert_transitions
from literate_ai.contracts import canonical_json_bytes
from literate_ai.projects import PinnedInputClosure, ProjectError

MAX_STATE_BYTES = 64 * 1024


class WorkerAlertHistoryError(ValueError):
    pass


def _node(path):
    try:
        node = path.lstat()
    except FileNotFoundError:
        return None
    if (
        stat_is_link_or_reparse(node)
        or not stat.S_ISREG(node.st_mode)
        or node.st_nlink != 1
    ):
        raise ValueError("worker.alert_state_unsafe")
    if os.name != "nt" and (node.st_mode & 0o077 or node.st_uid != os.getuid()):
        raise ValueError("worker.alert_state_not_private")
    return _signature(node)


def record_worker_alerts(
    path,
    assessment,
    observation,
    *,
    additional_findings=(),
    guard=lambda: None,
    lock_timeout_seconds=2,
):
    """Update one already-selected private file, never create its parent tree."""
    destination = Path(path).expanduser()
    try:
        if not destination.is_absolute() or ".." in destination.parts:
            raise ValueError("worker.alert_state_path_invalid")
        parent = directory_node(destination.parent)
        lock = destination.with_name("." + destination.name + ".lock")
        with exclusive_cache_lock(lock, timeout_seconds=lock_timeout_seconds):
            if directory_node(destination.parent) != parent:
                raise ValueError("worker.alert_state_parent_changed")
            original = _node(destination)
            custody = PinnedInputClosure(
                maximum_files=1,
                maximum_file_bytes=MAX_STATE_BYTES,
                maximum_total_bytes=MAX_STATE_BYTES,
            )
            previous = None
            if original is not None:
                content = custody.pin(
                    destination,
                    boundary=destination.parent,
                    label="worker-alert-history",
                )
                previous = WorkerAlertHistory.from_dict(
                    json.loads(content, object_pairs_hook=_unique_object)
                )
            state, events, metadata = alert_transitions(
                assessment,
                observation,
                previous,
                additional_findings=additional_findings,
            )
            payload = canonical_json_bytes(state.to_dict()) + b"\n"
            if len(payload) > MAX_STATE_BYTES:
                raise ValueError("worker.alert_state_oversized")

            def current():
                guard()
                if (
                    directory_node(destination.parent) != parent
                    or _node(destination) != original
                ):
                    raise ValueError("worker.alert_state_changed")
                custody.require_unchanged()

            current()
            descriptor, name = tempfile.mkstemp(
                prefix=".worker-alert-", dir=destination.parent
            )
            temporary = Path(name)
            temporary_node = None
            owner = os.fstat(descriptor)
            owned_identity = owner.st_dev, owner.st_ino, owner.st_mode, owner.st_nlink
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                    opened = os.fstat(stream.fileno())
                    observed = temporary.lstat()
                    if _stat_binding_identity(opened) != _stat_binding_identity(
                        observed
                    ):
                        raise ValueError("worker.alert_stage_changed")
                    temporary_node = _signature(observed)
                current()
                if _signature(temporary.lstat()) != temporary_node:
                    raise ValueError("worker.alert_stage_changed")
                os.replace(temporary, destination)
                published = _node(destination)
                if (
                    directory_node(destination.parent) != parent
                    or published is None
                    or published[:5] + published[6:]
                    != temporary_node[:5] + temporary_node[6:]
                ):
                    raise ValueError("worker.alert_publication_changed")
                if os.name != "nt":
                    directory = os.open(
                        destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                # Only remove this invocation's unchanged staging file in its
                # unchanged parent; never repair or widen cleanup after a race.
                if directory_node(destination.parent) == parent:
                    try:
                        node = temporary.lstat()
                        if (
                            node.st_dev,
                            node.st_ino,
                            node.st_mode,
                            node.st_nlink,
                        ) == owned_identity:
                            temporary.unlink()
                    except FileNotFoundError:
                        pass
            return events, metadata
    except (
        OSError,
        ValueError,
        TypeError,
        RecursionError,
        CacheLockError,
        UnsafeFilesystemPathError,
        ProjectError,
    ) as exc:
        raise WorkerAlertHistoryError(
            "Alert history could not be safely updated; check its private path, "
            "contents and concurrent use."
        ) from exc

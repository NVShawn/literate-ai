"""Acknowledged multi-root writer reservations, before transaction application.

Includes manifest and lifecycle advisory locks without publishing owner diagnostics.
Does not update pins, manifests, child source, receipts, or publication evidence.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from pathlib import Path

from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import PROJECT_FILENAME

from ._write_reservations import (
    WriteReservationSet,
    WriteReservationTarget,
    acquire_write_reservations,
)
from .lifecycle_lock import project_lifecycle_lock_path
from .repository_locks import RepositoryLockStore
from .repository_orchestration import OrchestrationInventoryError
from .repository_refresh import (
    PreparedRepositoryRefresh,
    _local_integer_wire,
    require_repository_refresh_inputs_unchanged,
)


def _custody_wire(value):
    if isinstance(value, bytes):
        return {"content_identity": "sha256:" + hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return _local_integer_wire(value)
    if is_dataclass(value):
        return {
            field.name: _custody_wire(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_custody_wire(item) for item in value]
    return value


def refresh_custody_identity(prepared: PreparedRepositoryRefresh) -> str:
    """Local approval binds exact nodes/bytes, not only portable desired pins."""
    if not isinstance(prepared, PreparedRepositoryRefresh):
        raise TypeError("refresh custody identity requires prepared inputs")
    return canonical_identity(_custody_wire(prepared)).uri


def refresh_reservation_targets(
    prepared: PreparedRepositoryRefresh,
    *,
    framework_locks: bool = False,
) -> tuple[WriteReservationTarget, ...]:
    if not isinstance(prepared, PreparedRepositoryRefresh):
        raise TypeError("refresh reservations require prepared inputs")
    store = RepositoryLockStore(prepared.repository.root)
    targets = [
        WriteReservationTarget(
            store.writer_path, store.directory, prepared.metadata_node
        )
    ]
    for observed in (prepared.root_git, *prepared.children):
        if framework_locks:
            targets.extend(
                WriteReservationTarget(
                    path, observed.root, observed.root_node, advisory=True
                )
                for path in (
                    observed.root / f".{PROJECT_FILENAME}.write.lock",
                    project_lifecycle_lock_path(observed.root),
                )
            )
        targets.extend(
            (
                WriteReservationTarget(
                    observed.git_directory / "HEAD.lock",
                    observed.git_directory,
                    observed.git_node,
                ),
                WriteReservationTarget(
                    observed.index.path.with_name(observed.index.path.name + ".lock"),
                    observed.git_directory,
                    observed.git_node,
                ),
            )
        )
        for reference in observed.references:
            path = reference.file.path
            anchor, node = (
                (observed.git_directory, observed.git_node)
                if path == observed.git_directory / reference.name
                else (observed.common_directory, observed.common_node)
            )
            targets.append(
                WriteReservationTarget(
                    path.with_name(path.name + ".lock"), anchor, node
                )
            )
        targets.append(
            WriteReservationTarget(
                observed.packed_references.path.with_name("packed-refs.lock"),
                observed.common_directory,
                observed.common_node,
            )
        )
    targets.extend(
        WriteReservationTarget(
            item.before.path.with_name(item.before.path.name + ".lock"),
            item.anchor,
            item.anchor_node,
        )
        for item in prepared.reflogs
        if item.append
    )
    return tuple(targets)


@contextmanager
def reserve_refresh_git_inputs(
    prepared: PreparedRepositoryRefresh,
    *,
    expected_custody_identity: str,
    acknowledge: bool,
) -> Iterator[WriteReservationSet]:
    if acknowledge is not True:
        raise OrchestrationInventoryError(
            "refresh_acknowledgement_required",
            "write reservations require explicit acknowledgement",
        )
    if refresh_custody_identity(prepared) != expected_custody_identity:
        raise OrchestrationInventoryError(
            "refresh_plan_stale", "reviewed refresh custody does not match these inputs"
        )
    require_repository_refresh_inputs_unchanged(prepared)
    with acquire_write_reservations(
        refresh_reservation_targets(prepared)
    ) as reservations:
        require_repository_refresh_inputs_unchanged(prepared, reservations=reservations)
        yield reservations


@contextmanager
def reserve_refresh_inputs(
    prepared: PreparedRepositoryRefresh,
    *,
    expected_custody_identity: str,
    acknowledge: bool,
) -> Iterator[WriteReservationSet]:
    """Own Git, repository, manifest and lifecycle writers across observed roots.

    This does not perform publication renewal or apply prospective changes.
    Existing framework lock files are retained; diagnostics are not rewritten.
    """
    if acknowledge is not True:
        raise OrchestrationInventoryError(
            "refresh_acknowledgement_required",
            "write reservations require explicit acknowledgement",
        )
    if refresh_custody_identity(prepared) != expected_custody_identity:
        raise OrchestrationInventoryError(
            "refresh_plan_stale", "reviewed refresh custody does not match these inputs"
        )
    require_repository_refresh_inputs_unchanged(prepared)
    with acquire_write_reservations(
        refresh_reservation_targets(prepared, framework_locks=True)
    ) as reservations:
        require_repository_refresh_inputs_unchanged(prepared, reservations=reservations)
        yield reservations

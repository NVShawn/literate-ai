"""Exact metadata-parent custody, including owner-created logical absence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.contracts.paths import canonical_relative_posix_path

from ._write_reservations import WriteReservationSet
from .lifecycle_lock import project_lifecycle_lock_path
from .repository_orchestration import OrchestrationInventoryError
from .repository_refresh import (
    RefreshFileObservation,
    _file,
    _local_integer_wire,
    _node,
)

_MAX_DIRECTORIES = 8192
_MAX_DEPTH = 64


@dataclass(frozen=True, slots=True)
class RefreshDirectoryObservation:
    path: Path
    node: tuple[int, int, int] | None

    def to_dict(self):
        return {
            "path": str(self.path),
            "node": (
                None
                if self.node is None
                else [_local_integer_wire(item) for item in self.node]
            ),
        }


def observe_metadata_parents(targets, reservations=None):
    """Targets contain (file path, stable ancestor, ancestor node) triples."""
    values = {}

    def record(path, node):
        observed = RefreshDirectoryObservation(path, node)
        if path in values and values[path] != observed:
            raise OrchestrationInventoryError(
                "inputs_changed", "metadata directory changed during observation"
            )
        values[path] = observed
        if len(values) > _MAX_DIRECTORIES:
            raise OrchestrationInventoryError(
                "refresh_directory_limit",
                "metadata directory custody exceeds its bound",
            )

    for path, anchor, expected in targets:
        relative = canonical_relative_posix_path(
            path.relative_to(anchor).as_posix(), label="refresh metadata"
        )
        if len(relative.parts) > _MAX_DEPTH or _node(anchor) != expected:
            raise OrchestrationInventoryError(
                "refresh_directory_invalid",
                "metadata anchor changed or path exceeds its depth bound",
            )
        record(anchor, expected)
        parent = anchor
        missing = False
        for part in relative.parts[:-1]:
            candidate = parent / part
            node = None
            if not missing:
                require_safe_directory(parent)
                WriteReservationSet._no_alias(candidate)
                if os.path.lexists(candidate):
                    actual = _node(candidate)
                    if reservations is None or not reservations.owns_directory(
                        candidate
                    ):
                        node = actual
                else:
                    missing = True
            record(candidate, node)
            parent = candidate
    return tuple(values[path] for path in sorted(values))


def optional_metadata_file(
    path, anchor, anchor_node, *, maximum_bytes, reservations=None
):
    observe_metadata_parents(((path, anchor, anchor_node),), reservations)
    if not os.path.lexists(path.parent):
        return RefreshFileObservation(path, None, None)
    WriteReservationSet._no_alias(path)
    return _file(path, optional=True, maximum_bytes=maximum_bytes)


def refresh_metadata_directory_targets(root_git, children, manifest, lock, reflogs):
    targets = [
        (item.path, root_git.root, root_git.root_node) for item in (manifest, lock)
    ]
    for observed in (root_git, *children):
        targets.extend(
            (item.path, observed.git_directory, observed.git_node)
            for item in (observed.head, observed.index)
        )
        targets.append(
            (
                observed.packed_references.path,
                observed.common_directory,
                observed.common_node,
            )
        )
        targets.append(
            (
                project_lifecycle_lock_path(observed.root),
                observed.root,
                observed.root_node,
            )
        )
        for reference in observed.references:
            anchor, node = (
                (observed.git_directory, observed.git_node)
                if reference.file.path == observed.git_directory / reference.name
                else (observed.common_directory, observed.common_node)
            )
            targets.append((reference.file.path, anchor, node))
    targets.extend(
        (item.before.path, item.anchor, item.anchor_node) for item in reflogs
    )
    return tuple(targets)

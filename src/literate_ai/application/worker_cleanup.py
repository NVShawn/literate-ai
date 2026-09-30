"""Bounded, read-only discovery of task-owned cleanup candidates."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts.identity import canonical_identity


@dataclass(frozen=True, slots=True)
class CleanupRoot:
    alias: str
    path: Path | str
    ownership: str
    recovery: str
    active_markers: tuple[str, ...] = ()
    inactive_markers: tuple[str, ...] = ()
    cleanup_command: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CleanupScanPolicy:
    roots: tuple[CleanupRoot, ...]
    deadline_ms: int
    maximum_entries: int
    maximum_depth: int
    minimum_candidate_bytes: int


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    candidate_id: str
    root: str
    kind: str
    bytes: int
    entries: int
    ownership: str
    active_use: str
    uncertainty: str
    recovery: str

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "root": self.root,
            "kind": self.kind,
            "bytes": self.bytes,
            "entries": self.entries,
            "ownership": self.ownership,
            "active_use": self.active_use,
            "uncertainty": self.uncertainty,
            "recovery": self.recovery,
        }


@dataclass(frozen=True, slots=True)
class CleanupInvestigation:
    status: str
    scanned_entries: int
    skipped_links: int
    candidates: tuple[CleanupCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/worker-cleanup-investigation@1",
            "status": self.status,
            "scanned_entries": self.scanned_entries,
            "skipped_links": self.skipped_links,
            "candidates": [item.to_dict() for item in self.candidates],
            "deletion_authorized": False,
        }


def _candidate_id(root: CleanupRoot, relative: str) -> str:
    return canonical_identity(
        {
            "schema": "literate-ai/worker-cleanup-candidate@1",
            "root": root.alias,
            "binding": hashlib.sha256(os.fsencode(root.path)).hexdigest(),
            "relative": relative,
        }
    ).uri


def _has_link_or_reparse_ancestor(path: Path) -> bool:
    current = path
    while True:
        metadata = current.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
            return True
        if current.parent == current:
            return False
        current = current.parent


def _scan_candidate(
    root: CleanupRoot,
    candidate: Path,
    *,
    deadline: float,
    maximum_entries: int,
    maximum_depth: int,
    monotonic,
) -> tuple[int, int, int, bool, bool, bool, bool]:
    """Return size, entries, links, active/inactive markers, complete and link."""

    total = 0
    entries = 0
    skipped = 0
    active = False
    inactive = False
    stack = [(candidate, 0)]
    while stack:
        if monotonic() >= deadline or entries >= maximum_entries:
            return total, entries, skipped, active, inactive, False, False
        path, depth = stack.pop()
        try:
            metadata = path.lstat()
        except OSError:
            return total, entries, skipped, active, inactive, False, False
        entries += 1
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
            skipped += 1
            if path == candidate:
                return total, entries, skipped, active, inactive, True, True
            continue
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
            if path.name in root.active_markers:
                active = True
            if path.name in root.inactive_markers:
                inactive = True
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        if path.name in root.active_markers:
            active = True
        if path.name in root.inactive_markers:
            inactive = True
        if depth >= maximum_depth:
            return total, entries, skipped, active, inactive, False, False
        try:
            children = tuple(path.iterdir())
        except OSError:
            return total, entries, skipped, active, inactive, False, False
        stack.extend((child, depth + 1) for child in reversed(children))
    return total, entries, skipped, active, inactive, True, False


def investigate_cleanup_candidates(
    policy: CleanupScanPolicy,
    *,
    monotonic=time.monotonic,
) -> CleanupInvestigation:
    """Measure only configured top-level candidates without following links."""

    if not policy.roots:
        return CleanupInvestigation("not-configured", 0, 0, ())
    deadline = monotonic() + policy.deadline_ms / 1000
    scanned = 0
    skipped = 0
    complete = True
    candidates: list[CleanupCandidate] = []
    for root in policy.roots:
        root_path = Path(root.path)
        if monotonic() >= deadline or scanned >= policy.maximum_entries:
            complete = False
            break
        try:
            if _has_link_or_reparse_ancestor(root_path):
                complete = False
                continue
            metadata = root_path.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or attributes & 0x0400
            ):
                complete = False
                continue
            children = tuple(root_path.iterdir())
        except OSError:
            complete = False
            continue
        for child in children:
            if monotonic() >= deadline or scanned >= policy.maximum_entries:
                complete = False
                break
            (
                measured,
                entries,
                links,
                active,
                inactive,
                finished,
                candidate_is_link,
            ) = _scan_candidate(
                root,
                child,
                deadline=deadline,
                maximum_entries=policy.maximum_entries - scanned,
                maximum_depth=policy.maximum_depth,
                monotonic=monotonic,
            )
            scanned += entries
            skipped += links
            complete = complete and finished
            if candidate_is_link or measured < policy.minimum_candidate_bytes:
                continue
            try:
                relative = child.relative_to(root_path).as_posix()
                kind = "directory" if child.is_dir() else "file"
            except (OSError, ValueError):
                complete = False
                continue
            candidates.append(
                CleanupCandidate(
                    _candidate_id(root, relative),
                    root.alias,
                    kind,
                    measured,
                    entries,
                    root.ownership,
                    "active" if active else "inactive" if inactive else "uncertain",
                    (
                        "configured active-use marker present"
                        if active
                        else "configured completed-use marker present"
                        if inactive
                        else "no process ownership proof was collected"
                    ),
                    root.recovery,
                )
            )
    candidates.sort(key=lambda item: (-item.bytes, item.root, item.candidate_id))
    return CleanupInvestigation(
        "complete" if complete else "bounded-partial",
        scanned,
        skipped,
        tuple(candidates),
    )


__all__ = [
    "CleanupCandidate",
    "CleanupInvestigation",
    "CleanupRoot",
    "CleanupScanPolicy",
    "investigate_cleanup_candidates",
]

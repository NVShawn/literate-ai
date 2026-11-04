"""Bounded opaque Git-operation inputs; these never establish idle/audited state."""

from __future__ import annotations

import base64
import hashlib
import os
import stat

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse

from .project_worktrees import ProjectWorktreeError
from .repository_orchestration import _read_document

_MARKERS = (
    "HEAD.lock",
    "index.lock",
    "packed-refs.lock",
    "config.lock",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "BISECT_LOG",
    "BISECT_START",
)
_INPUT_NAMES = tuple(
    sorted(
        set(_MARKERS)
        | {
            "AUTO_MERGE",
            "MERGE_MSG",
            "MERGE_MODE",
            "MERGE_RR",
            "MERGE_AUTOSTASH",
            "SQUASH_MSG",
            "REBASE_HEAD",
            "ORIG_HEAD",
            "FETCH_HEAD",
            "BISECT_NAMES",
            "BISECT_TERMS",
            "BISECT_EXPECTED_REV",
            "BISECT_ANCESTORS_OK",
            "BISECT_RUN",
        }
    )
)


def _signature(path):
    node = path.lstat()
    return (
        node.st_dev,
        node.st_ino,
        node.st_mode,
        node.st_size,
        node.st_mtime_ns,
        node.st_ctime_ns,
        node.st_nlink,
    )


def capture_operation_inputs(custody, observation, budget):
    """Capture known operation/recovery paths, preserving the existing marker view.

    These are opaque bytes, not a complete inventory of every possible Git
    operation or proof that Git/process activity has stopped. Recovery files such
    as ORIG_HEAD can persist after an operation and are never labelled active.
    """
    markers, records = [], []

    def names_at(path):
        names = []
        with os.scandir(path) as entries:
            for entry in entries:
                observation.remaining()
                if len(names) >= budget.limits.maximum_entries:
                    raise ProjectWorktreeError(
                        "worktree operation directory entry limit exceeded"
                    )
                budget.metadata({"operation_entry": entry.name})
                names.append(entry.name)
        return tuple(sorted(names))

    def capture(anchor, initial):
        # Explicit post-order checks avoid Python recursion limits on opaque data.
        stack = [(initial, None, None)]
        while stack:
            path, closing_signature, closing_names = stack.pop()
            observation.remaining()
            require_safe_directory(path.parent)
            if closing_signature is not None:
                require_safe_directory(path)
                if (
                    _signature(path) != closing_signature
                    or names_at(path) != closing_names
                ):
                    raise ProjectWorktreeError(
                        "worktree operation directory membership changed"
                    )
                observation.remember(path)
                continue
            observation.remember(path)
            before = _signature(path)
            mode = before[2]
            if stat_is_link_or_reparse(path.lstat()):
                raise ProjectWorktreeError("worktree operation input is link-like")
            record = {
                "anchor": str(anchor),
                "path": path.relative_to(anchor).as_posix(),
                "signature": before,
            }
            if stat.S_ISREG(mode):
                if before[3] > budget.limits.maximum_file_bytes:
                    raise ProjectWorktreeError(
                        "worktree operation per-file byte limit exceeded"
                    )
                content = _read_document(
                    path,
                    label="worktree operation metadata",
                    maximum_bytes=min(
                        budget.limits.maximum_file_bytes,
                        budget.limits.maximum_metadata_bytes,
                    ),
                )
                record.update(
                    kind="file",
                    bytes=len(content),
                    content_identity="sha256:" + hashlib.sha256(content).hexdigest(),
                    wire_base64=base64.b64encode(content).decode("ascii"),
                )
            elif stat.S_ISDIR(mode):
                require_safe_directory(path)
                names = names_at(path)
                record.update(kind="directory", names=names)
                stack.append((path, before, names))
                stack.extend((path / name, None, None) for name in reversed(names))
            else:
                raise ProjectWorktreeError("worktree operation input is not regular")
            if _signature(path) != before:
                raise ProjectWorktreeError(
                    "worktree operation input changed during capture"
                )
            observation.remember(path)
            observation.remaining()
            budget.entry(record)
            records.append(record)

    for anchor in sorted({custody.administration_directory, custody.common_directory}):
        for name in _INPUT_NAMES:
            observation.remaining()
            if observation.remember(anchor / name):
                if name in _MARKERS:
                    markers.append({"anchor": str(anchor), "name": name})
                capture(anchor, anchor / name)
    budget.metadata(markers)
    markers.sort(key=lambda item: (item["anchor"], _MARKERS.index(item["name"])))
    return markers, sorted(records, key=lambda item: (item["anchor"], item["path"]))

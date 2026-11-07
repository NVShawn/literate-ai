"""Durable, bounded records for an original HTML publication attempt.

These descriptive records are not output ownership or source acceptance. An
interrupted attempt is retained, never reopened here for appending or cleanup.
"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager

from literate_ai.contracts.identity import canonical_json_bytes

from .html_tree_output_initialization import _node, _read_at
from .retained_project_inputs import _root

MAX_JOURNAL_RECORD_BYTES = 4096
MAX_JOURNAL_RECORDS = 4
JOURNAL_STORAGE_RESERVATION = MAX_JOURNAL_RECORD_BYTES * MAX_JOURNAL_RECORDS
JOURNAL_MEMORY_RESERVATION = 16 * JOURNAL_STORAGE_RESERVATION
_PHASES = ("prepared", "output-reserved", "generation-staged", "published")


@contextmanager
def initial_html_tree_journal(path, *, prepared_data, require_current):
    """Create a new private journal, retaining every written byte on failure.

    The caller holds the canonical target lock and charges the fixed reservation
    before entering. Existing journals refuse, even when their bytes look valid.
    Every append closes over original descriptors and checks all previous records
    before writing the next exclusive file. A partially written record is retained
    for diagnosis; it can never be mistaken for a completed publication record.
    """
    if os.name != "posix" or not callable(require_current):
        raise ValueError("html.tree.native-journal-context-required")
    parent = _root(path.parent)
    parent_node = _node(parent.lstat())
    if (
        parent_node[3] != os.geteuid()
        or stat.S_IMODE(parent_node[2]) & ~stat.S_ISGID != 0o700
    ):
        raise ValueError("html.tree.private-journal-parent-required")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(parent, flags)
    descriptor = None
    active = True
    receiver_pid = os.getpid()
    records = []

    def parent_current():
        if not active or os.getpid() != receiver_pid:
            raise ValueError("html.tree.journal-context-ended")
        require_current()
        if (
            _node(_root(parent).lstat()) != parent_node
            or _node(os.fstat(parent_fd)) != parent_node
        ):
            raise ValueError("html.tree.journal-parent-changed")

    def journal_current():
        parent_current()
        if (
            _node(os.fstat(descriptor)) != journal_node
            or _node(os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False))
            != journal_node
        ):
            raise ValueError("html.tree.journal-directory-changed")
        # Count before retaining names; a foreign directory cannot force an
        # unbounded allocation while checking a four-record journal.
        names = set()
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(names) >= MAX_JOURNAL_RECORDS:
                    raise ValueError("html.tree.journal-membership-changed")
                names.add(entry.name)
        if names != {item[0] for item in records}:
            raise ValueError("html.tree.journal-membership-changed")
        for name, signature, content in records:
            current_signature, current_content = _read_at(
                descriptor, name, MAX_JOURNAL_RECORD_BYTES
            )
            if current_signature != signature or current_content != content:
                raise ValueError("html.tree.journal-record-changed")
        parent_current()

    def append(phase, data):
        journal_current()
        index = len(records)
        if index >= MAX_JOURNAL_RECORDS or phase != _PHASES[index]:
            raise ValueError("html.tree.journal-phase-invalid")
        content = canonical_json_bytes(
            {
                "schema": "literate-ai/tree-html-publication-journal@1",
                "sequence": index,
                "phase": phase,
                "previous": None
                if not records
                else "sha256:" + hashlib.sha256(records[-1][2]).hexdigest(),
                "data": data,
                "grants_output_ownership": False,
            }
        )
        if len(content) > MAX_JOURNAL_RECORD_BYTES:
            raise ValueError("html.tree.journal-record-limit")
        name = f"{index:04d}.json"
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=descriptor,
        )
        try:
            original = _node(os.fstat(fd))
            remaining = memoryview(content)
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0:
                    raise OSError("html.tree.journal-write-incomplete")
                remaining = remaining[count:]
            os.fsync(fd)
            signature, reopened = _read_at(descriptor, name, MAX_JOURNAL_RECORD_BYTES)
            if signature[0] != original or reopened != content:
                raise ValueError("html.tree.journal-write-changed")
            records.append((name, signature, content))
        finally:
            os.close(fd)
        os.fsync(descriptor)
        journal_current()

    try:
        parent_current()
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        journal_node = _node(os.fstat(descriptor))
        if (
            journal_node[3] != os.geteuid()
            or stat.S_IMODE(journal_node[2]) & ~stat.S_ISGID != 0o700
        ):
            raise ValueError("html.tree.private-journal-directory-required")
        os.fsync(descriptor)
        os.fsync(parent_fd)
        journal_current()
        append("prepared", prepared_data)
        yield append, journal_current
        journal_current()
    finally:
        active = False
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)

from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_repository_tree``."""

import hashlib
from pathlib import PurePosixPath

from literate_ai.contracts.repository_tree import (
    RepositoryTreeEntry,
    RepositoryTreeSnapshot,
)


def oid(kind, content, width=40):
    return hashlib.new(
        "sha1" if width == 40 else "sha256",
        kind.encode() + b" " + str(len(content)).encode() + b"\0" + content,
    ).hexdigest()


def tree(files, *, width=40, message=b"fixture"):
    entries = {}
    for path, (mode, content) in files.items():
        entries[path] = RepositoryTreeEntry(
            path,
            mode,
            "1" * width if mode == "160000" else oid("blob", content, width),
            None if mode == "160000" else content,
        )
    directories = {
        parent.as_posix() for path in files for parent in PurePosixPath(path).parents
    }
    directories.add(".")
    for directory in sorted(
        directories, key=lambda path: (-len(PurePosixPath(path).parts), path)
    ):
        children = [
            entry
            for entry in entries.values()
            if PurePosixPath(entry.path).parent.as_posix() == directory
        ]
        children.sort(
            key=lambda entry: (
                PurePosixPath(entry.path).name + ("/" if entry.mode == "040000" else "")
            ).encode()
        )
        raw = b"".join(
            entry.mode.lstrip("0").encode()
            + b" "
            + PurePosixPath(entry.path).name.encode()
            + b"\0"
            + bytes.fromhex(entry.object_id)
            for entry in children
        )
        identity = oid("tree", raw, width)
        if directory != ".":
            entries[directory] = RepositoryTreeEntry(directory, "040000", identity)
    commit = b"tree " + identity.encode() + b"\n\n" + message
    return RepositoryTreeSnapshot(
        oid("commit", commit, width),
        commit,
        tuple(sorted(entries.values(), key=lambda entry: entry.path)),
    )

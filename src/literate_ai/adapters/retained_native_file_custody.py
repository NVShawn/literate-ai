"""Bounded streaming custody for large observed native images, without byte copies."""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.dependencies.observation import _stat_binding_identity
from literate_ai.adapters.retained_package_tree import _signature
from literate_ai.contracts.identity import canonical_identity

_CHUNK_BYTES = 1024 * 1024


def _safe_parent(path):
    try:
        require_safe_directory(path.parent)
    except UnsafeFilesystemPathError as exc:
        raise ValueError("retained.native.file-parent-unsafe") from exc


def _stream_descriptor(descriptor, maximum_bytes):
    digest, size = hashlib.sha256(), 0
    while True:
        chunk = os.read(descriptor, min(_CHUNK_BYTES, maximum_bytes - size + 1))
        if not chunk:
            break
        size += len(chunk)
        if size > maximum_bytes:
            raise ValueError("retained.native.file-size-limit")
        digest.update(chunk)
    return digest.hexdigest(), size, os.fstat(descriptor)


def _requires_content_recheck():
    return os.name == "nt"


def native_file_digest(path, maximum_bytes):
    """Hash the same regular descriptor and path under a finite read budget."""
    _safe_parent(path)
    before = path.lstat()
    if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("retained.native.file-unsafe")
    if before.st_size > maximum_bytes:
        raise ValueError(
            f"retained.native.file-size-limit: observed_bytes={before.st_size} "
            f"maximum_bytes={maximum_bytes}"
        )
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            stat_is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _stat_binding_identity(opened) != _stat_binding_identity(before)
        ):
            raise ValueError("retained.native.custody-changed")
        digest, size, streamed = _stream_descriptor(descriptor, maximum_bytes)
    finally:
        os.close(descriptor)
    # Windows may not finalize same-size write timestamps until every open handle
    # closes. Compare the named path only after closing our read descriptor so a
    # concurrent writer cannot hide behind that deferred metadata update.
    after = path.lstat()
    if (
        size != opened.st_size
        or _signature(streamed) != _signature(opened)
        or _signature(after) != _signature(before)
        or _stat_binding_identity(after) != _stat_binding_identity(opened)
    ):
        raise ValueError("retained.native.custody-changed")
    if _requires_content_recheck():
        # Windows can preserve every observable stat field for a same-size
        # overwrite completed while the first descriptor is open. Reopen and
        # stream the named file once more so custody does not depend on deferred
        # metadata finalization.
        verification_descriptor = os.open(path, flags)
        try:
            verification_opened = os.fstat(verification_descriptor)
            if (
                stat_is_link_or_reparse(verification_opened)
                or not stat.S_ISREG(verification_opened.st_mode)
                or _stat_binding_identity(verification_opened)
                != _stat_binding_identity(after)
            ):
                raise ValueError("retained.native.custody-changed")
            verified_digest, verified_size, verification_streamed = _stream_descriptor(
                verification_descriptor, maximum_bytes
            )
        finally:
            os.close(verification_descriptor)
        final = path.lstat()
        if (
            (verified_digest, verified_size) != (digest, size)
            or _signature(verification_streamed) != _signature(verification_opened)
            or _signature(final) != _signature(after)
            or _stat_binding_identity(final)
            != _stat_binding_identity(verification_opened)
        ):
            raise ValueError("retained.native.custody-changed")
    _safe_parent(path)
    return digest, size


@dataclass(frozen=True)
class NativeFileDigest:
    path: Path
    labels: tuple[str, ...]
    digest: str
    size: int


@dataclass(frozen=True)
class NativeFileClosure:
    entries: tuple[NativeFileDigest, ...]

    @property
    def total_bytes(self):
        return sum(entry.size for entry in self.entries)

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/native-file-digest-closure@1",
                "inputs": [
                    {
                        "labels": list(entry.labels),
                        "content_identity": "sha256:" + entry.digest,
                        "bytes": entry.size,
                    }
                    for entry in sorted(self.entries, key=lambda entry: entry.labels)
                ],
            }
        ).uri

    def require_unchanged(self):
        for entry in self.entries:
            if native_file_digest(entry.path, entry.size) != (entry.digest, entry.size):
                raise ValueError("retained.native.custody-changed")

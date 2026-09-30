"""Explicit bounded delivery of canonical retained-library directory bundles.

Delivery proves exact bytes and archive structure, never qualification or importer
trust. HTTPS reuses the fixed-origin evidence CAS protocol and its TLS/auth rules.
No cache lookup, publication, extraction, tool invocation or model call occurs.
"""

from __future__ import annotations

import os
import ssl
import stat
from contextlib import ExitStack
from pathlib import Path

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    read_directory_export,
)
from literate_ai.adapters.evidence_storage import HttpsEvidenceStore
from literate_ai.contracts.blobs import BlobRef
from literate_ai.security.evidence.storage import (
    EvidenceReadLimits,
    EvidenceStorageError,
)


class RetainedBundleDeliveryError(ValueError):
    """Sanitized refusal without URLs, local paths, credentials or bundle bytes."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _opening_metadata(value: os.stat_result) -> tuple[int, ...]:
    if os.name != "nt":
        return _metadata(value)
    # Windows CPython 3.12 pathname stat reports creation time in st_ctime,
    # while fstat reports metadata change time. Compare creation time across
    # APIs, and keep each API's full before/after metadata checks below.
    return _metadata(value)[:-1] + (
        getattr(value, "st_birthtime_ns", value.st_ctime_ns),
    )


def _read_file(path: Path, expected: BlobRef) -> bytes:
    path = Path(path).absolute()
    # Reject traversal before any I/O; do not normalize aliases into accepted paths.
    if ".." in path.parts:
        raise RetainedBundleDeliveryError("retained.delivery.path-unsafe")
    require_safe_directory(path.parent)
    before = path.lstat()
    if stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise RetainedBundleDeliveryError("retained.delivery.path-unsafe")
    if before.st_size != expected.size:
        raise RetainedBundleDeliveryError("retained.delivery.size-mismatch")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with ExitStack() as stack:
        if os.name == "posix":
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(path.anchor, directory_flags)
            stack.callback(os.close, parent)
            for name in path.parts[1:-1]:
                parent = os.open(name, directory_flags, dir_fd=parent)
                stack.callback(os.close, parent)
            descriptor = os.open(path.name, flags, dir_fd=parent)
        else:
            descriptor = os.open(path, flags)
        stack.callback(os.close, descriptor)
        opened = os.fstat(descriptor)
        if _opening_metadata(opened) != _opening_metadata(before):
            raise RetainedBundleDeliveryError("retained.delivery.inputs-changed")
        chunks = []
        remaining = expected.size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                raise RetainedBundleDeliveryError("retained.delivery.size-mismatch")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise RetainedBundleDeliveryError("retained.delivery.size-mismatch")
        require_safe_directory(path.parent)
        if _metadata(os.fstat(descriptor)) != _metadata(opened) or _metadata(
            path.lstat()
        ) != _metadata(before):
            raise RetainedBundleDeliveryError("retained.delivery.inputs-changed")
        return b"".join(chunks)


class RetainedBundleDelivery:
    """Caller chooses offline policy and finite bounds before supplying an artifact.

    HTTPS locations are explicit CAS base URLs. The request path is derived solely
    from the pinned digest; redirects and ambient credentials/proxies are refused by
    HttpsEvidenceStore. Its timeout bounds each network I/O, not total elapsed time.
    """

    def __init__(self, *, offline: bool, max_bytes: int, max_entries: int):
        if (
            not isinstance(offline, bool)
            or type(max_entries) is not int
            or max_entries < 1
        ):
            raise RetainedBundleDeliveryError("retained.delivery.configuration-invalid")
        try:
            self._limits = EvidenceReadLimits(max_bytes, max_bytes, 1)
        except ValueError:
            raise RetainedBundleDeliveryError(
                "retained.delivery.configuration-invalid"
            ) from None
        self._offline = offline
        self._max_entries = max_entries

    def _reference(self, expected: BlobRef) -> None:
        try:
            self._limits.require_reference(expected)
        except EvidenceStorageError:
            raise RetainedBundleDeliveryError(
                "retained.delivery.reference-invalid"
            ) from None

    def _decode(
        self, content: bytes, expected: BlobRef
    ) -> tuple[DirectoryExportFile, ...]:
        try:
            return read_directory_export(
                content,
                expected,
                max_bytes=self._limits.maximum_blob_bytes,
                max_entries=self._max_entries,
            )
        except ValueError:
            raise RetainedBundleDeliveryError(
                "retained.delivery.archive-refused"
            ) from None

    def read_file(
        self, path: Path, expected: BlobRef
    ) -> tuple[DirectoryExportFile, ...]:
        self._reference(expected)
        try:
            content = _read_file(path, expected)
        except RetainedBundleDeliveryError:
            raise
        except (OSError, ValueError, UnsafeFilesystemPathError):
            raise RetainedBundleDeliveryError(
                "retained.delivery.file-refused"
            ) from None
        return self._decode(content, expected)

    def read_https(
        self,
        base_url: str,
        expected: BlobRef,
        *,
        bearer_token: str | None = None,
        timeout_seconds: float = 30,
        tls_context: ssl.SSLContext | None = None,
    ) -> tuple[DirectoryExportFile, ...]:
        # Refuse before even constructing a transport. Offline never falls back.
        if self._offline:
            raise RetainedBundleDeliveryError("retained.delivery.offline")
        self._reference(expected)
        try:
            content = HttpsEvidenceStore(
                base_url,
                bearer_token=bearer_token,
                timeout_seconds=timeout_seconds,
                tls_context=tls_context,
                limits=self._limits,
            ).get_bytes(expected)
        except EvidenceStorageError:
            raise RetainedBundleDeliveryError(
                "retained.delivery.https-refused"
            ) from None
        return self._decode(content, expected)

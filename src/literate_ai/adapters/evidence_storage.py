"""Explicit filesystem, checked-out monorepo and HTTPS immutable evidence stores."""

from __future__ import annotations

import hashlib
import http.client
import math
import re
import ssl
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    require_safe_directory,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.security.evidence.storage import (
    DEFAULT_EVIDENCE_READ_LIMITS,
    EvidenceNotFoundError,
    EvidenceReadLimits,
    EvidenceStorageError,
    verify_evidence_bytes,
)
from literate_ai.storage.cas import BlobNotFoundError, FileSystemCAS, StorageError


def _reference(content: bytes, media_type: str, limits: EvidenceReadLimits) -> BlobRef:
    if not isinstance(content, bytes):
        raise EvidenceStorageError("evidence.storage.content-invalid")
    if len(content) > limits.maximum_blob_bytes:
        raise EvidenceStorageError("evidence.storage.blob-limit")
    reference = BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )
    # Reuse the predicate's canonical media/size validation at the publication edge.
    limits.require_reference(reference)
    return reference


class FileSystemEvidenceStore:
    """Reuse the immutable CAS layout; read-only construction never creates paths."""

    def __init__(
        self,
        root: Path,
        *,
        writable: bool = False,
        limits: EvidenceReadLimits = DEFAULT_EVIDENCE_READ_LIMITS,
    ):
        if not isinstance(writable, bool) or not isinstance(limits, EvidenceReadLimits):
            raise EvidenceStorageError("evidence.storage.configuration-invalid")
        configured = Path(root).absolute()
        try:
            (ensure_safe_directory if writable else require_safe_directory)(configured)
            self._cas = FileSystemCAS(configured, create=writable)
            require_safe_directory(self._cas.blob_root)
        except (OSError, StorageError, UnsafeFilesystemPathError):
            raise EvidenceStorageError("evidence.storage.path-unsafe") from None
        self._writable = writable
        self._limits = limits

    def get_bytes(self, reference: BlobRef) -> bytes:
        self._limits.require_reference(reference)
        try:
            require_safe_directory(self._cas.blob_root)
            parent = self._cas.path_for(reference).parent
            try:
                parent.lstat()
            except FileNotFoundError:
                raise EvidenceNotFoundError() from None
            require_safe_directory(parent)
            return self._cas.get_bytes(reference)
        except EvidenceStorageError:
            raise
        except BlobNotFoundError:
            raise EvidenceNotFoundError() from None
        except (OSError, StorageError, UnsafeFilesystemPathError):
            raise EvidenceStorageError("evidence.storage.read-failed") from None

    def put_bytes(self, content: bytes, *, media_type: str) -> BlobRef:
        if not self._writable:
            raise EvidenceStorageError("evidence.storage.read-only")
        reference = _reference(content, media_type, self._limits)
        try:
            require_safe_directory(self._cas.blob_root)
            self._cas.put_bytes(content, media_type=media_type)
            verify_evidence_bytes(reference, self.get_bytes(reference))
        except EvidenceStorageError:
            raise
        except (OSError, StorageError, UnsafeFilesystemPathError):
            raise EvidenceStorageError("evidence.storage.write-failed") from None
        return reference


class MonorepoEvidenceStore(FileSystemEvidenceStore):
    """CAS under a configured repository prefix, without Git side effects."""

    def __init__(
        self,
        repository: Path,
        *,
        prefix: str = "verification/evidence",
        writable: bool = False,
        limits: EvidenceReadLimits = DEFAULT_EVIDENCE_READ_LIMITS,
    ):
        if (
            not isinstance(prefix, str)
            or not prefix
            or len(prefix) > 256
            or any(
                not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", part)
                or part.endswith(".")
                or part.split(".", 1)[0].casefold()
                in {
                    "con",
                    "prn",
                    "aux",
                    "nul",
                    *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10)),
                }
                for part in prefix.split("/")
            )
        ):
            raise EvidenceStorageError("evidence.storage.prefix-invalid")
        root = Path(repository).absolute()
        try:
            require_safe_directory(root)
        except (OSError, UnsafeFilesystemPathError):
            raise EvidenceStorageError("evidence.storage.path-unsafe") from None
        super().__init__(
            root.joinpath(*PurePosixPath(prefix).parts),
            writable=writable,
            limits=limits,
        )


class HttpsEvidenceStore:
    """Fixed-origin CAS HTTP protocol, without redirects, proxies or ambient auth.

    GET requires an exact Content-Type and Content-Length with no transfer/content
    encoding. PUT uses If-None-Match: * and always rereads the object. Each network
    I/O uses the configured timeout; this is not a whole-operation wall-clock budget.
    Credentials come only from the explicitly configured bearer token.
    """

    def __init__(
        self,
        base_url: str,
        *,
        writable: bool = False,
        bearer_token: str | None = None,
        timeout_seconds: float = 30,
        tls_context: ssl.SSLContext | None = None,
        limits: EvidenceReadLimits = DEFAULT_EVIDENCE_READ_LIMITS,
    ):
        if (
            not isinstance(base_url, str)
            or len(base_url) > 2048
            or base_url.strip() != base_url
            or any(ord(c) < 32 for c in base_url)
        ):
            raise EvidenceStorageError("evidence.storage.endpoint-invalid")
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except (TypeError, ValueError):
            raise EvidenceStorageError("evidence.storage.endpoint-invalid") from None
        if (
            parsed.scheme != "https"
            or port == 0
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*/?", parsed.path)
        ):
            raise EvidenceStorageError("evidence.storage.endpoint-invalid")
        if (
            not isinstance(writable, bool)
            or not isinstance(limits, EvidenceReadLimits)
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 300
        ):
            raise EvidenceStorageError("evidence.storage.configuration-invalid")
        if bearer_token is not None and (
            not isinstance(bearer_token, str)
            or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", bearer_token)
            or len(bearer_token) > 8192
        ):
            raise EvidenceStorageError("evidence.storage.credentials-invalid")
        context = (
            tls_context if tls_context is not None else ssl.create_default_context()
        )
        if (
            not isinstance(context, ssl.SSLContext)
            or not context.check_hostname
            or context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise EvidenceStorageError("evidence.storage.tls-invalid")
        self._host, self._port = parsed.hostname, port
        self._prefix = parsed.path.rstrip("/")
        self._token = bearer_token
        self._writable = writable
        self._timeout = timeout_seconds
        self._context = context
        self._limits = limits

    def _request(self, reference: BlobRef, content: bytes | None = None) -> bytes:
        self._limits.require_reference(reference)
        # Recheck a caller-owned context before every connection.
        if (
            not self._context.check_hostname
            or self._context.verify_mode != ssl.CERT_REQUIRED
        ):
            raise EvidenceStorageError("evidence.storage.tls-invalid")
        connection = http.client.HTTPSConnection(
            self._host, self._port, timeout=self._timeout, context=self._context
        )
        path = f"{self._prefix}/blobs/sha256/{reference.digest[:2]}/{reference.digest}"
        headers = {"Accept-Encoding": "identity", "Accept": reference.media_type}
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token
        if content is not None:
            headers.update({"If-None-Match": "*", "Content-Type": reference.media_type})
        response = None
        try:
            connection.request(
                "GET" if content is None else "PUT", path, body=content, headers=headers
            )
            response = connection.getresponse()
            if response.status in (401, 403):
                raise EvidenceStorageError("evidence.storage.authentication-refused")
            if content is not None:
                if response.status not in (201, 204, 412):
                    raise EvidenceStorageError("evidence.storage.publication-refused")
                return b""
            if response.status == 404:
                raise EvidenceNotFoundError()
            if response.status != 200:
                raise EvidenceStorageError("evidence.storage.response-refused")
            if response.headers.get_all("Content-Length") != [str(reference.size)]:
                raise EvidenceStorageError("evidence.storage.size-mismatch")
            if response.headers.get_all("Content-Type") != [reference.media_type]:
                raise EvidenceStorageError("evidence.storage.media-mismatch")
            if response.headers.get_all(
                "Transfer-Encoding"
            ) or response.headers.get_all("Content-Encoding"):
                raise EvidenceStorageError("evidence.storage.encoding-refused")
            # No content decoding or unbounded read. A lying/truncated server still
            # has to match the independently required exact size and digest.
            chunks = []
            remaining = reference.size
            while remaining:
                chunk = response.read1(min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return verify_evidence_bytes(reference, b"".join(chunks))
        except EvidenceStorageError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise EvidenceStorageError("evidence.storage.transport-failed") from None
        finally:
            if response is not None:
                response.close()
            connection.close()

    def get_bytes(self, reference: BlobRef) -> bytes:
        return self._request(reference)

    def put_bytes(self, content: bytes, *, media_type: str) -> BlobRef:
        if not self._writable:
            raise EvidenceStorageError("evidence.storage.read-only")
        reference = _reference(content, media_type, self._limits)
        self._request(reference, content)
        verify_evidence_bytes(reference, self.get_bytes(reference))
        return reference

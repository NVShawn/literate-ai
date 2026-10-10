"""Verify acquired Hex package bytes against both native lock checksums."""

from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass

from literate_ai.contracts import canonical_identity

from .mix_lock import MixLockedPackage
from .types import DependencyObservationError

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_LIMITS = {
    "VERSION": 32,
    "CHECKSUM": 128,
    "metadata.config": 1024 * 1024,
    "contents.tar.gz": _MAX_ARCHIVE_BYTES,
}


def _fail(message: str) -> None:
    raise DependencyObservationError("dependencies.hex-archive-invalid", message)


@dataclass(frozen=True)
class HexArchiveEvidence:
    """Verified acquired bytes; excludes compilation and runtime custody claims."""

    package: str
    version: str
    outer_sha256: str
    inner_sha256: str
    metadata_sha256: str
    contents_sha256: str

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": "literate-ai/hex-archive-evidence@1",
            "package": self.package,
            "version": self.version,
            "outer_sha256": self.outer_sha256,
            "inner_sha256": self.inner_sha256,
            "metadata_sha256": self.metadata_sha256,
            "contents_sha256": self.contents_sha256,
        }


def verify_hex_archive(
    content: bytes, *, package: MixLockedPackage
) -> HexArchiveEvidence:
    """Inspect only a bounded outer archive; never extract or evaluate metadata."""

    if not isinstance(package, MixLockedPackage):
        raise TypeError("Hex acquisition requires a checked native lock package")
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > _MAX_ARCHIVE_BYTES
        or hashlib.sha256(content).hexdigest() != package.outer_sha256
    ):
        _fail("Acquired Hex archive differs from the native lock outer checksum")
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                if (
                    member.name not in _LIMITS
                    or member.name in files
                    or not member.isfile()
                    or not 0 <= member.size <= _LIMITS[member.name]
                ):
                    _fail(
                        "Hex outer archive has invalid, duplicate or escaping entries"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    _fail("Hex archive member is unavailable")
                data = stream.read(_LIMITS[member.name] + 1)
                if len(data) != member.size:
                    _fail("Hex archive member size differs from its bounded header")
                files[member.name] = data
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise DependencyObservationError(
            "dependencies.hex-archive-invalid", "Hex package is not a valid outer tar"
        ) from exc
    if set(files) != set(_LIMITS) or files["VERSION"] != b"3":
        _fail("Hex package requires the complete version 3 archive envelope")
    inner = hashlib.sha256(
        files["VERSION"] + files["metadata.config"] + files["contents.tar.gz"]
    ).hexdigest()
    if inner != package.inner_sha256 or files["CHECKSUM"].lower() != inner.encode(
        "ascii"
    ):
        _fail("Hex package contents differ from the native lock inner checksum")
    return HexArchiveEvidence(
        package.name,
        package.version,
        package.outer_sha256,
        inner,
        hashlib.sha256(files["metadata.config"]).hexdigest(),
        hashlib.sha256(files["contents.tar.gz"]).hexdigest(),
    )

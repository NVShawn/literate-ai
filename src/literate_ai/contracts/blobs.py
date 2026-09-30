"""Portable immutable byte references shared by contracts and storage adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ._validation import fields, int_value, string_value

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BlobRef:
    """Portable identity, size, and media type for immutable bytes."""

    digest: str
    size: int
    algorithm: str = "sha256"
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if self.algorithm != "sha256":
            raise ValueError("only sha256 blob identities are currently supported")
        if not isinstance(self.digest, str) or not _SHA256.fullmatch(self.digest):
            raise ValueError("blob digest must be 64 lowercase hexadecimal characters")
        if (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
        ):
            raise ValueError("blob size must not be negative")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("blob media type must not be empty")

    @property
    def identity(self) -> str:
        return f"{self.algorithm}:{self.digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "digest": self.digest,
            "size": self.size,
            "media_type": self.media_type,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "BlobRef") -> BlobRef:
        data = fields(
            value,
            path=path,
            required=frozenset({"algorithm", "digest", "size", "media_type"}),
        )
        return cls(
            algorithm=string_value(data["algorithm"], f"{path}.algorithm"),
            digest=string_value(data["digest"], f"{path}.digest"),
            size=int_value(data["size"], f"{path}.size"),
            media_type=string_value(data["media_type"], f"{path}.media_type"),
        )


__all__ = ["BlobRef"]

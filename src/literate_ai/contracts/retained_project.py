"""Bounded binary-capable input custody for retained-project execution.

These contracts identify inputs, never acceptance or conversion authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import canonical_relative_posix_paths


@dataclass(frozen=True, slots=True)
class RetainedProjectLimits:
    max_entries: int
    max_file_bytes: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        for value in (self.max_entries, self.max_file_bytes, self.max_total_bytes):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError("Retained input bounds must be positive integers")

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, value: Any) -> RetainedProjectLimits:
        _fields(value, set(cls.__slots__))
        return cls(**value)


def _fields(value: Any, fields: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Unexpected retained input contract fields")


@dataclass(frozen=True, slots=True)
class RetainedProjectMember:
    path: str
    size: int
    mode: int
    sha256: str
    role: str

    def __post_init__(self) -> None:
        canonical_relative_posix_paths((self.path,), label="retained member")
        if type(self.size) is not int or not 0 <= self.size < 2**63:
            raise ValueError("Invalid retained input size")
        if type(self.mode) is not int or self.mode not in (0o644, 0o755):
            raise ValueError("Retained mode must be normalized 0644 or 0755")
        ContentIdentity.parse_uri("sha256:" + self.sha256)
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("Retained input requires an ownership role")

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, value: Any) -> RetainedProjectMember:
        _fields(value, set(cls.__slots__))
        return cls(**value)


@dataclass(frozen=True, slots=True)
class RetainedProjectManifest:
    roots: tuple[str, ...]
    members: tuple[RetainedProjectMember, ...]
    limits: RetainedProjectLimits
    directories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.limits, RetainedProjectLimits):
            raise TypeError("Typed retained input limits required")
        if type(self.roots) is not tuple or not self.roots:
            raise ValueError("Explicit retained input roots required")
        canonical_relative_posix_paths(self.roots, label="retained roots")
        if tuple(sorted(self.roots)) != self.roots:
            raise ValueError("Retained roots must be ordered")
        if type(self.members) is not tuple or not self.members:
            raise ValueError("Retained input manifest must not be empty")
        if not all(
            isinstance(member, RetainedProjectMember) for member in self.members
        ):
            raise TypeError("Typed retained members required")
        if type(self.directories) is not tuple or self.directories != tuple(
            sorted(self.directories)
        ):
            raise ValueError("Retained directories must be ordered")
        # Directories may be ancestors; validate aliases separately from files.
        aliases: set[str] = set()
        for directory in self.directories:
            canonical_relative_posix_paths((directory,), label="retained directory")
            if directory.casefold() in aliases:
                raise ValueError("Colliding retained directories")
            aliases.add(directory.casefold())
            if not any(
                directory == root or directory.startswith(root + "/")
                for root in self.roots
            ):
                raise ValueError("Retained directory outside declared roots")
        names = tuple(member.path for member in self.members)
        canonical_relative_posix_paths(names, label="retained members")
        if names != tuple(sorted(names)):
            raise ValueError("Retained members must be ordered")
        nodes = set(names) | set(self.directories)
        if any(root not in nodes for root in self.roots):
            raise ValueError("Retained root absent from manifest")
        for name in nodes:
            root = next(
                (
                    root
                    for root in self.roots
                    if name == root or name.startswith(root + "/")
                ),
                None,
            )
            if root is None:
                raise ValueError("Retained node outside declared roots")
            while name != root:
                name = name.rsplit("/", 1)[0]
                if name not in self.directories:
                    raise ValueError("Retained directory inventory is incomplete")
        for member in self.members:
            if member.path.casefold() in aliases or any(
                d.casefold().startswith(member.path.casefold() + "/")
                for d in self.directories
            ):
                raise ValueError("Retained file/directory collision")
            if not any(
                member.path == root or member.path.startswith(root + "/")
                for root in self.roots
            ):
                raise ValueError("Retained member outside declared roots")
            if member.size > self.limits.max_file_bytes:
                raise ValueError("Retained input exceeds per-file bound")
        if (
            len(self.members) + len(self.directories) > self.limits.max_entries
            or sum(m.size for m in self.members) > self.limits.max_total_bytes
        ):
            raise ValueError("Retained input exceeds aggregate bound")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/retained-project-manifest@1",
            "roots": list(self.roots),
            "directories": list(self.directories),
            "members": [m.to_dict() for m in self.members],
            "limits": self.limits.to_dict(),
        }

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> RetainedProjectManifest:
        _fields(value, {"schema", "roots", "members", "limits", "directories"})
        if (
            value["schema"] != "literate-ai/retained-project-manifest@1"
            or not isinstance(value["roots"], list)
            or not isinstance(value["members"], list)
            or not isinstance(value["directories"], list)
        ):
            raise ValueError("Invalid retained input manifest")
        return cls(
            tuple(value["roots"]),
            tuple(RetainedProjectMember.from_dict(m) for m in value["members"]),
            RetainedProjectLimits.from_dict(value["limits"]),
            tuple(value["directories"]),
        )

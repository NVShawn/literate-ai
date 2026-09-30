"""Persistent root-owned Gitlink authority, separate from local observations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from ._validation import fields, list_value
from .identity import canonical_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths

_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            f"{label} requires bounded nonempty text without control characters"
        )
    return value


def gitlink_url(value: str) -> str:
    """Preserve configured relative/scp/URL spelling without embedded credentials."""
    _text(value, "Gitlink URL", 4096)
    if value.startswith("-") or "?" in value or "#" in value:
        raise ValueError("Gitlink URL is ambiguous or credential-bearing")
    if "://" in value:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"file", "git", "http", "https", "ssh"}
            or parsed.password is not None
            or (parsed.scheme in {"http", "https"} and parsed.username is not None)
            or (parsed.scheme != "file" and not parsed.hostname)
        ):
            raise ValueError("Gitlink URL scheme or credentials are unsupported")
    elif "::" in value or ("@" in value and ":" in value.split("@", 1)[0]):
        raise ValueError("Gitlink URL transport or credentials are unsupported")
    return value


@dataclass(frozen=True, slots=True)
class RepositoryPin:
    name: str
    path: str
    url: str
    commit: str
    branch: str | None = None

    def __post_init__(self) -> None:
        _text(self.name, "Gitlink name", 256)
        path = canonical_relative_posix_path(self.path, label="Gitlink path")
        if path.parts[0].casefold() in {
            ".git",
            ".literate",
            ".gitmodules",
            "literate.project.json",
            "skill.md",
        }:
            raise ValueError("Gitlink path overlaps root project authority")
        gitlink_url(self.url)
        if (
            not isinstance(self.commit, str)
            or _OID.fullmatch(self.commit) is None
            or not self.commit.strip("0")
        ):
            raise ValueError("Gitlink commit requires an exact nonzero Git object ID")
        if self.branch is not None and self.branch != ".":
            branch = _text(self.branch, "Gitlink branch", 256)
            if (
                branch.startswith("-")
                or branch.endswith(("/", "."))
                or ".." in branch
                or "@{" in branch
                or any(char in " ~^:?*[\\" for char in branch)
                or any(
                    not part or part.startswith(".") or part.endswith(".lock")
                    for part in branch.split("/")
                )
            ):
                raise ValueError("Gitlink branch requires a valid branch name")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryPin:
        return cls(
            **fields(
                value,
                path="RepositoryPin",
                required=frozenset({"name", "path", "url", "commit", "branch"}),
            )
        )


@dataclass(frozen=True, slots=True, order=True)
class RepositoryRelationship:
    consumer: str
    provider: str

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.consumer, label="relationship consumer")
        canonical_relative_posix_path(self.provider, label="relationship provider")
        if self.consumer == self.provider:
            raise ValueError("repository relationship cannot refer to itself")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryRelationship:
        return cls(
            **fields(
                value,
                path="RepositoryRelationship",
                required=frozenset({"consumer", "provider"}),
            )
        )


@dataclass(frozen=True, slots=True)
class RepositoryOrchestration:
    gitmodules_identity: str
    repositories: tuple[RepositoryPin, ...]
    relationships: tuple[RepositoryRelationship, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.gitmodules_identity, str)
            or _DIGEST.fullmatch(self.gitmodules_identity) is None
        ):
            raise ValueError(
                "orchestration requires an exact .gitmodules SHA-256 identity"
            )
        if (
            not isinstance(self.repositories, tuple)
            or not 1 <= len(self.repositories) <= 128
            or any(not isinstance(pin, RepositoryPin) for pin in self.repositories)
        ):
            raise ValueError(
                "orchestration requires an immutable tuple of 1..128 repository pins"
            )
        canonical_relative_posix_paths(
            (pin.path for pin in self.repositories), label="Gitlink paths"
        )
        if len({pin.name for pin in self.repositories}) != len(self.repositories):
            raise ValueError("orchestration requires unique configured Gitlink names")
        if (
            not isinstance(self.relationships, tuple)
            or len(self.relationships) > 1024
            or any(
                not isinstance(edge, RepositoryRelationship)
                for edge in self.relationships
            )
        ):
            raise ValueError(
                "orchestration relationships require an immutable typed tuple"
            )
        paths = {pin.path for pin in self.repositories}
        if len(set(self.relationships)) != len(self.relationships) or any(
            edge.consumer not in paths or edge.provider not in paths
            for edge in self.relationships
        ):
            raise ValueError(
                "orchestration relationships must be unique "
                "and reference known repositories"
            )
        object.__setattr__(
            self,
            "repositories",
            tuple(sorted(self.repositories, key=lambda pin: pin.path)),
        )
        object.__setattr__(self, "relationships", tuple(sorted(self.relationships)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "gitmodules_identity": self.gitmodules_identity,
            "repositories": [pin.to_dict() for pin in self.repositories],
            "relationships": [edge.to_dict() for edge in self.relationships],
        }

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryOrchestration:
        data = fields(
            value,
            path="RepositoryOrchestration",
            required=frozenset(
                {"gitmodules_identity", "repositories", "relationships"}
            ),
        )
        return cls(
            data["gitmodules_identity"],
            tuple(
                RepositoryPin.from_dict(pin)
                for pin in list_value(data["repositories"], "repositories")
            ),
            tuple(
                RepositoryRelationship.from_dict(edge)
                for edge in list_value(data["relationships"], "relationships")
            ),
        )

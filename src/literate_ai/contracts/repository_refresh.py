"""Pure refresh intent and commit-only authority deltas; no publication or apply."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from ._validation import fields, list_value
from .identity import canonical_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths
from .repository_orchestration import RepositoryOrchestration

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
REQUEST_SCHEMA = "literate-ai/orchestration-refresh-request@1"


@dataclass(frozen=True, slots=True)
class RepositoryRefreshTarget:
    path: str
    commit: str

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.path, label="refresh target path")
        if len(self.path) > 4096:
            raise ValueError("refresh target path exceeds its text bound")
        if (
            not isinstance(self.commit, str)
            or _COMMIT.fullmatch(self.commit) is None
            or not self.commit.strip("0")
        ):
            raise ValueError("refresh target requires an exact nonzero Git commit ID")

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "commit": self.commit}

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryRefreshTarget:
        return cls(
            **fields(
                value,
                path="RepositoryRefreshTarget",
                required=frozenset({"path", "commit"}),
            )
        )


@dataclass(frozen=True, slots=True)
class RepositoryRefreshRequest:
    targets: tuple[RepositoryRefreshTarget, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.targets, tuple)
            or not 1 <= len(self.targets) <= 128
            or any(
                not isinstance(item, RepositoryRefreshTarget) for item in self.targets
            )
        ):
            raise ValueError("refresh requires an immutable tuple of 1..128 targets")
        canonical_relative_posix_paths(
            (item.path for item in self.targets), label="refresh target paths"
        )
        object.__setattr__(
            self, "targets", tuple(sorted(self.targets, key=lambda item: item.path))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_SCHEMA,
            "targets": [item.to_dict() for item in self.targets],
        }

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryRefreshRequest:
        data = fields(
            value,
            path="RepositoryRefreshRequest",
            required=frozenset({"schema", "targets"}),
        )
        if data["schema"] != REQUEST_SCHEMA:
            raise ValueError("refresh request requires its exact schema")
        targets = list_value(data["targets"], "refresh targets")
        if not 1 <= len(targets) <= 128:
            raise ValueError("refresh request requires 1..128 targets")
        return cls(tuple(RepositoryRefreshTarget.from_dict(item) for item in targets))


@dataclass(frozen=True, slots=True)
class RepositoryRefreshAuthority:
    """Derive prospective pins, never accept caller-supplied replacement authority.

    This is only the pure input to later custody/publication/transaction planning.
    It cannot establish a clean checkout, commit reachability, or permission to write.
    """

    previous: RepositoryOrchestration
    request: RepositoryRefreshRequest
    prospective: RepositoryOrchestration = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.previous, RepositoryOrchestration) or not isinstance(
            self.request, RepositoryRefreshRequest
        ):
            raise TypeError("refresh preparation requires typed authority and intent")
        targets = {item.path: item.commit for item in self.request.targets}
        paths = {pin.path for pin in self.previous.repositories}
        if targets.keys() - paths:
            raise ValueError("refresh targets must name exact declared Gitlink paths")
        prospective = replace(
            self.previous,
            repositories=tuple(
                replace(pin, commit=targets[pin.path]) if pin.path in targets else pin
                for pin in self.previous.repositories
            ),
        )
        object.__setattr__(self, "prospective", prospective)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "literate-ai/orchestration-refresh-authority@1",
            "previous_authority": self.previous.to_dict(),
            "previous_authority_identity": self.previous.identity,
            "request": self.request.to_dict(),
            "request_identity": self.request.identity,
            "prospective_authority": self.prospective.to_dict(),
            "prospective_authority_identity": self.prospective.identity,
            "changes": [
                {
                    "path": old.path,
                    "previous_commit": old.commit,
                    "prospective_commit": new.commit,
                }
                for old, new in zip(
                    self.previous.repositories,
                    self.prospective.repositories,
                    strict=True,
                )
                if old.commit != new.commit
            ],
            "publication": "not-checked",
            "apply_supported": False,
            "writes": False,
            "execution": False,
        }

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

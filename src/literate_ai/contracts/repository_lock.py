"""Portable repository pins; neither a Component lock nor an execution plan."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, string_value
from .identity import canonical_identity
from .repository_orchestration import RepositoryOrchestration

_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RepositoryLock:
    """Bind reviewed root authority and graph to independent child declarations.

    Local checkout paths, child HEAD and initialization state are intentionally not
    fields. Reading this contract does not establish that any input is current.
    """

    SCHEMA: ClassVar[str] = "literate-ai/repository-lock@1"

    project_id: str
    project_identity: str
    authority_identity: str
    authority_graph_identity: str
    repository_orchestration: RepositoryOrchestration

    def __post_init__(self) -> None:
        string_value(self.project_id, "repository lock project ID", max_length=256)
        for name in (
            "project_identity",
            "authority_identity",
            "authority_graph_identity",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
                raise ValueError(f"repository lock {name} requires an exact SHA-256 ID")
        if not isinstance(self.repository_orchestration, RepositoryOrchestration):
            raise TypeError("repository lock requires typed orchestration authority")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "project_id": self.project_id,
            "project_identity": self.project_identity,
            "authority_identity": self.authority_identity,
            "authority_graph_identity": self.authority_graph_identity,
            "repository_orchestration": self.repository_orchestration.to_dict(),
        }

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    @classmethod
    def from_dict(cls, value: Any) -> RepositoryLock:
        data = contract_fields(
            value,
            path="RepositoryLock",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "project_id",
                    "project_identity",
                    "authority_identity",
                    "authority_graph_identity",
                    "repository_orchestration",
                }
            ),
        )
        return cls(
            data["project_id"],
            data["project_identity"],
            data["authority_identity"],
            data["authority_graph_identity"],
            RepositoryOrchestration.from_dict(data["repository_orchestration"]),
        )

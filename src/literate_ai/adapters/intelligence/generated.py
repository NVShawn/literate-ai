"""Optional source-intelligence selection; the product has no indexer provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from literate_ai.contracts import (
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifact,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    SourceIntelligenceStageStatus,
    canonical_relative_posix_paths,
)
from literate_ai.contracts import (
    generated_source_tree_identity as _contract_source_tree_identity,
)


class SourceIntelligenceError(RuntimeError):
    """A generated tree could not be bound to requested intelligence evidence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SourceIntelligenceProviderSelection:
    """One stage's explicit provider decision at the adapter boundary."""

    stage: SourceIntelligenceStage
    mode: SourceIntelligenceMode
    provider_id: str
    provider: SourceIntelligenceProvider | None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, SourceIntelligenceStage):
            raise TypeError("source-intelligence selection stage must be typed")
        if not isinstance(self.mode, SourceIntelligenceMode):
            raise TypeError("source-intelligence selection mode must be typed")
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("source-intelligence provider ID must not be empty")
        if self.mode is SourceIntelligenceMode.OFF:
            if self.provider is not None or self.unavailable_reason is not None:
                raise ValueError("off source intelligence cannot select a provider")
        elif self.provider is None:
            if self.mode is SourceIntelligenceMode.REQUIRED:
                raise ValueError("required source intelligence needs a provider")
            if not self.unavailable_reason:
                raise ValueError(
                    "preferred unavailable intelligence requires a reason code"
                )
        elif self.unavailable_reason is not None:
            raise ValueError("an available provider cannot have an unavailable reason")

    def status(self, *, current: bool) -> dict[str, object]:
        if current != (self.provider is not None):
            raise ValueError(
                "source-intelligence status contradicts provider selection"
            )
        state = (
            "current"
            if current
            else ("off" if self.mode is SourceIntelligenceMode.OFF else "unavailable")
        )
        return SourceIntelligenceStageStatus(
            stage=self.stage,
            mode=self.mode,
            state=state,
            provider_id=self.provider_id,
            reason_code=self.unavailable_reason,
        ).to_dict()


def select_source_intelligence_provider(
    policy: ProjectSourceIntelligencePolicy,
    stage: SourceIntelligenceStage,
) -> SourceIntelligenceProviderSelection:
    """Resolve source intelligence; the product has no indexer implementation."""

    mode = policy.mode_for(stage)
    if mode is SourceIntelligenceMode.OFF or policy.provider_id == "none":
        return SourceIntelligenceProviderSelection(
            stage, SourceIntelligenceMode.OFF, policy.provider_id, None
        )
    reason = "source-intelligence.provider-unsupported"
    if mode is SourceIntelligenceMode.REQUIRED:
        raise SourceIntelligenceError(
            reason, "required source-intelligence provider is unsupported"
        )
    return SourceIntelligenceProviderSelection(
        stage, mode, policy.provider_id, None, reason
    )


@runtime_checkable
class SourceIntelligenceProvider(Protocol):
    """Derive an observation beside source without adding it to source authority."""

    def preflight(self) -> str: ...

    def index(
        self,
        source_root: Path,
        files: Mapping[str, bytes],
        *,
        source_tree_identity: str | None = None,
    ) -> SourceIntelligenceArtifact: ...

    def verify(
        self,
        source_root: Path,
        files: Mapping[str, bytes],
        evidence: SourceIntelligenceArtifact | Mapping[str, object],
    ) -> SourceIntelligenceArtifact: ...


def generated_source_tree_identity(files: Mapping[str, bytes]) -> str:
    """Return the source-only tree digest shared by workspaces and builders."""

    try:
        return _contract_source_tree_identity(_normalized_files(files))
    except (TypeError, ValueError) as exc:
        raise SourceIntelligenceError(
            "source-index.tree-invalid",
            "source-index inputs do not form a canonical source tree",
        ) from exc


def _normalized_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    paths = canonical_relative_posix_paths(
        files.keys(), label="generated source index path"
    )
    result: dict[str, bytes] = {}
    for path, content in zip(paths, files.values(), strict=True):
        if path.parts[0] == "_build":
            raise SourceIntelligenceError(
                "source-index.disposable-input",
                "_build is disposable state and cannot be a source-index input",
            )
        if not isinstance(content, bytes):
            raise SourceIntelligenceError(
                "source-index.content-invalid",
                "source-index inputs must contain byte content",
            )
        result[path.as_posix()] = content
    return result


__all__ = [
    "SourceIntelligenceArtifact",
    "SourceIntelligenceError",
    "SourceIntelligenceProvider",
    "SourceIntelligenceProviderSelection",
    "generated_source_tree_identity",
    "select_source_intelligence_provider",
]

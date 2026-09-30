"""Application-specific structural ports; implementations live outside this package."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from literate_ai.contracts.identity import ContentIdentity
from literate_ai.ports import BuildAuthorizer

from .locked_generation_authority import LockedGenerationAuthority
from .models import ComponentSourceEvidenceReadiness


@runtime_checkable
class ReadinessProvider(Protocol):
    def inspect(
        self,
        authority: LockedGenerationAuthority,
        component_revision: ContentIdentity,
    ) -> ComponentSourceEvidenceReadiness: ...


__all__ = ["BuildAuthorizer", "ReadinessProvider"]

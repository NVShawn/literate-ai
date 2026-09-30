"""Typed ports for inverse model collection and language translation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .behavioral_surfaces import BehavioralSurfaceInventoryItem
from .contracts import SpecAuthoringSkill
from .inventory import SourceInventory

if TYPE_CHECKING:
    from .model_workflow import ModelTranslation, SourceIntelligence


class SourceIntelligenceCollector(Protocol):
    def collect(
        self,
        *,
        source: Path,
        inventory: SourceInventory,
        languages: tuple[str, ...],
    ) -> SourceIntelligence: ...


class InverseLanguageTranslator(Protocol):
    def translate(
        self,
        *,
        language: str,
        intelligence: SourceIntelligence,
        skills: tuple[SpecAuthoringSkill, ...],
        egress_policy_id: str,
        root_coordinate: str,
        evidence_ids: tuple[str, ...],
        behavioral_surfaces: tuple[BehavioralSurfaceInventoryItem, ...],
        partition_ordinal: int,
        partition_count: int,
    ) -> ModelTranslation: ...

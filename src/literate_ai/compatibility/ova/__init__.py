"""Retained read-only compatibility with legacy OVA lifecycle artifacts."""

from .models import (
    CompatibilityDiagnostic,
    DiagnosticSeverity,
    FrozenValue,
    LifecycleState,
    OvaCompatibilityResult,
    freeze,
    thaw,
)
from .reader import OvaCompatibilityReader
from .shadow import (
    NeutralProjection,
    OvaShadowComparator,
    OvaShadowReport,
    ProjectionKind,
    ShadowComparison,
    ShadowDiagnostic,
    ShadowInventory,
    ShadowStatus,
)
from .yaml_subset import YamlSubsetError, load_yaml_subset

__all__ = [
    "CompatibilityDiagnostic",
    "DiagnosticSeverity",
    "FrozenValue",
    "LifecycleState",
    "NeutralProjection",
    "OvaCompatibilityReader",
    "OvaCompatibilityResult",
    "OvaShadowComparator",
    "OvaShadowReport",
    "ProjectionKind",
    "ShadowComparison",
    "ShadowDiagnostic",
    "ShadowInventory",
    "ShadowStatus",
    "YamlSubsetError",
    "freeze",
    "load_yaml_subset",
    "thaw",
]

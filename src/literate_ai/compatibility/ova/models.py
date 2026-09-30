"""Neutral immutable results emitted by OVA compatibility readers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LifecycleState(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    INTERRUPTED = "interrupted"
    DRIFTED = "drifted"


FrozenValue = (
    str
    | int
    | float
    | bool
    | None
    | Mapping[str, "FrozenValue"]
    | tuple["FrozenValue", ...]
)


def freeze(value: Any) -> FrozenValue:
    """Recursively freeze JSON/YAML-compatible data without dropping fields."""

    if isinstance(value, Mapping):
        frozen: dict[str, FrozenValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("compatibility mapping keys must be strings")
            frozen[key] = freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported compatibility value: {type(value).__name__}")


def thaw(value: FrozenValue) -> Any:
    """Return the exact mutable semantic value represented by frozen data."""

    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CompatibilityDiagnostic:
    code: str
    message: str
    source_path: str
    value_path: str = "$"
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True, slots=True)
class OvaCompatibilityResult:
    """One source artifact represented as neutral immutable records."""

    kind: str
    source_path: str
    source_digest: str | None
    root_shape: str
    records: tuple[Mapping[str, FrozenValue], ...]
    diagnostics: tuple[CompatibilityDiagnostic, ...]
    state: LifecycleState

    def __post_init__(self) -> None:
        if self.root_shape not in {"mapping", "sequence", "unreadable"}:
            raise ValueError("unsupported OVA compatibility root shape")
        if self.root_shape == "mapping" and len(self.records) > 1:
            raise ValueError("mapping roots can emit at most one record")

    @property
    def data(self) -> Mapping[str, FrozenValue]:
        if self.root_shape != "mapping" or len(self.records) != 1:
            raise ValueError("artifact does not contain one mapping root")
        return self.records[0]

    @property
    def structurally_valid(self) -> bool:
        return self.state is not LifecycleState.INVALID

    @property
    def ready(self) -> bool:
        return self.state is LifecycleState.VALID

    def mutable_value(self) -> Any:
        if self.root_shape == "mapping":
            return thaw(self.records[0]) if self.records else None
        if self.root_shape == "sequence":
            return [thaw(item) for item in self.records]
        return None


__all__ = [
    "CompatibilityDiagnostic",
    "DiagnosticSeverity",
    "FrozenValue",
    "LifecycleState",
    "OvaCompatibilityResult",
    "freeze",
    "thaw",
]

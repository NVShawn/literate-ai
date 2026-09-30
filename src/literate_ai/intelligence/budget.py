"""Deterministic selected-evidence budgeting with required evidence protected."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import EvidenceItem


class EvidenceBudgetError(RuntimeError):
    """Required evidence cannot fit without being truncated or dropped."""


@dataclass(frozen=True, slots=True)
class EvidenceBudget:
    included: tuple[EvidenceItem, ...]
    dropped_ids: tuple[str, ...]
    used_bytes: int
    limit_bytes: int


class EvidenceBudgeter:
    """Protect all required evidence, then favor selected optional evidence."""

    def __init__(self, limit_bytes: int) -> None:
        if limit_bytes < 0:
            raise ValueError("evidence budget must not be negative")
        self.limit_bytes = limit_bytes

    def apply(self, evidence: Iterable[EvidenceItem]) -> EvidenceBudget:
        items = tuple(evidence)
        if len({item.evidence_id for item in items}) != len(items):
            raise ValueError("evidence IDs must be unique")
        required = tuple(item for item in items if item.required)
        required_size = sum(item.encoded_size for item in required)
        if required_size > self.limit_bytes:
            raise EvidenceBudgetError(
                f"required evidence needs {required_size} bytes; "
                f"budget is {self.limit_bytes}"
            )

        included_ids = {item.evidence_id for item in required}
        used = required_size
        optional = (
            tuple(item for item in items if not item.required and item.selected),
            tuple(item for item in items if not item.required and not item.selected),
        )
        for group in optional:
            for item in group:
                if used + item.encoded_size <= self.limit_bytes:
                    included_ids.add(item.evidence_id)
                    used += item.encoded_size
        return EvidenceBudget(
            included=tuple(item for item in items if item.evidence_id in included_ids),
            dropped_ids=tuple(
                item.evidence_id
                for item in items
                if item.evidence_id not in included_ids
            ),
            used_bytes=used,
            limit_bytes=self.limit_bytes,
        )


__all__ = ["EvidenceBudget", "EvidenceBudgetError", "EvidenceBudgeter"]

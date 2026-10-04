"""Shared test fixtures extracted from test_live_bidirectional_roundtrip."""

from __future__ import annotations

from literate_ai.source_to_specification import (
    DraftScenario,
    DraftStatement,
)


def draft_statements(
    bundle: dict[str, object], *, language: str
) -> tuple[DraftStatement, ...]:
    base = bundle["result"]["draft"]["statements"]
    selected = tuple(
        item
        for item in bundle["flavor_drafts"]
        if item.get("flavor_id") == language
        and item.get("axis") == "implementation.language-ecosystem"
    )
    if len(selected) != 1:
        raise AssertionError(
            f"inverse draft must contain one selected {language} language Flavor"
        )
    raw = [*base, *selected[0]["statements"]]
    return tuple(
        DraftStatement(
            item["statement_id"],
            item["capability"],
            item["requirement"],
            tuple(DraftScenario(**scenario) for scenario in item["scenarios"]),
            tuple(item["observation_ids"]),
        )
        for item in raw
    )

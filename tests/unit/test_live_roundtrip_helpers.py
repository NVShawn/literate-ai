"""Tests for composed forward/inverse round-trip comparison helpers."""

from __future__ import annotations

import unittest
from unittest import mock

from tests.conformance.test_live_bidirectional_roundtrip import (
    draft_statements,
    selected_languages,
)


def _statement(identifier: str, requirement: str) -> dict[str, object]:
    return {
        "statement_id": identifier,
        "capability": identifier,
        "requirement": requirement,
        "scenarios": [
            {"name": identifier, "when": "input arrives", "then": "behavior occurs"}
        ],
        "observation_ids": [f"observation:{identifier}"],
    }


class LiveRoundTripHelperTests(unittest.TestCase):
    def test_elixir_is_an_explicit_roundtrip_selection(self) -> None:
        with mock.patch.dict(
            "os.environ", {"LITERATE_AI_LIVE_ROUNDTRIP_LANGUAGES": "elixir"}
        ):
            self.assertEqual(selected_languages(), ("elixir",))
        with mock.patch.dict(
            "os.environ", {"LITERATE_AI_LIVE_ROUNDTRIP_LANGUAGES": "elixir,elixir"}
        ):
            with self.assertRaises(ValueError):
                selected_languages()

    def test_semantic_comparison_uses_base_plus_selected_language_flavor(self) -> None:
        bundle = {
            "result": {"draft": {"statements": [_statement("base", "base rule")]}},
            "flavor_drafts": [
                {
                    "axis": "implementation.language-ecosystem",
                    "flavor_id": "python",
                    "statements": [_statement("python", "Python rule")],
                },
                {
                    "axis": "implementation.language-ecosystem",
                    "flavor_id": "rust",
                    "statements": [_statement("rust", "Rust rule")],
                },
            ],
        }

        statements = draft_statements(bundle, language="python")

        self.assertEqual(
            tuple(item.requirement for item in statements),
            ("base rule", "Python rule"),
        )

    def test_semantic_comparison_rejects_missing_selected_language_flavor(self) -> None:
        bundle = {
            "result": {"draft": {"statements": [_statement("base", "base rule")]}},
            "flavor_drafts": [],
        }

        with self.assertRaisesRegex(AssertionError, "one selected python"):
            draft_statements(bundle, language="python")


if __name__ == "__main__":
    unittest.main()

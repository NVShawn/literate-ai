from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_generated_tests``."""


from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_SCHEMA,
    MAJOR_REBUILD_GENERATION_MODE,
)

RECIPE_IDENTITY = "sha256:" + "1" * 64


def valid_suite() -> dict[str, object]:
    return {
        "schema": GENERATED_TEST_SUITE_SCHEMA,
        "recipe_identity": RECIPE_IDENTITY,
        "generation_mode": MAJOR_REBUILD_GENERATION_MODE,
        "cases": [
            {
                "case_id": "ordinary-example",
                "category": "example",
                "specification_refs": ["openspec/spec.md"],
                "arguments": [{"value": 7}],
                "expected_result": {"value": 14},
            },
            {
                "case_id": "zero-boundary",
                "category": "boundary",
                "specification_refs": ["openspec/spec.md", "cpp/openspec/spec.md"],
                "arguments": [{"value": 0}],
                "expected_result": {"value": 0},
            },
            {
                "case_id": "scaling-invariant",
                "category": "invariant",
                "specification_refs": ["openspec/spec.md"],
                "arguments": [{"value": 19}],
                "expected_result": {"value": 38},
            },
        ],
    }

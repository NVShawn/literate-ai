"""Shared test fixtures extracted from test_project_update_adapter."""

from __future__ import annotations

from literate_ai.contracts import (
    ProjectInitializationOrigin,
)


def _origin(revision: str, version: str) -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        "ssh://git.example.test/operator/literate-ai.git",
        revision * 40,
        "literate-ai",
        version,
    )

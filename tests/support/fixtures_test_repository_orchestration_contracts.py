"""Shared test fixtures extracted from test_repository_orchestration_contracts."""

from __future__ import annotations

from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryPin,
    RepositoryRelationship,
)


def authority():
    return RepositoryOrchestration(
        "sha256:" + "c" * 64,
        (
            RepositoryPin("app", "services/app", "../app.git", "a" * 40, "."),
            RepositoryPin(
                "lib", "libraries/core", "git@example.test:group/lib.git", "b" * 40
            ),
        ),
        (RepositoryRelationship("services/app", "libraries/core"),),
    )

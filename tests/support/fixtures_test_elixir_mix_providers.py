"""Shared Elixir/Mix test fixtures from test_elixir_mix_providers (PR #45)."""

from __future__ import annotations

from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.library_imports import AuthoredLibraryImport


def _surface(symbols=("add",)):
    return project_library_import_surface(
        "provider-api",
        "elixir",
        (("math.add", canonical_identity("fixture-interface")),),
        declarations=(
            AuthoredLibraryImport(
                "elixir", "provider_api", "math.add", "ProviderApi.Math", symbols
            ),
        ),
    )

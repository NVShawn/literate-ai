"""Exact endpoint dispatch for a first-class multi-model portfolio."""

from __future__ import annotations

import unittest

from literate_ai.adapters.models import ModelProviderPortfolio, PortfolioProviderError


class Provider:
    provider_id = "fixture"

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def complete_structured(self, request):
        self.calls += 1
        return {"provider": self.name, "stage": request["stage_id"]}


class ModelProviderPortfolioTests(unittest.TestCase):
    def test_each_stage_uses_the_exact_routed_endpoint(self) -> None:
        local = Provider("local")
        remote = Provider("remote")
        portfolio = ModelProviderPortfolio({"local": local, "remote": remote})

        first = portfolio.complete_structured(
            {
                "stage_id": "plan",
                "route_decision": {"selected_endpoint_id": "local"},
            }
        )
        second = portfolio.complete_structured(
            {
                "stage_id": "generate",
                "route_decision": {"selected_endpoint_id": "remote"},
            }
        )

        self.assertEqual(first["provider"], "local")
        self.assertEqual(second["provider"], "remote")
        self.assertEqual((local.calls, remote.calls), (1, 1))

    def test_missing_adapter_fails_without_implicit_fallback(self) -> None:
        portfolio = ModelProviderPortfolio({"local": Provider("local")})
        with self.assertRaisesRegex(
            PortfolioProviderError, "no provider adapter is registered"
        ):
            portfolio.complete_structured(
                {
                    "stage_id": "generate",
                    "route_decision": {"selected_endpoint_id": "remote"},
                }
            )


if __name__ == "__main__":
    unittest.main()

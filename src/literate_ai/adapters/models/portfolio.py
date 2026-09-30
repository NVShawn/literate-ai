"""Run-scoped dispatch from a model route decision to concrete provider adapters."""

from __future__ import annotations

from collections.abc import Mapping

from literate_ai.ports import ModelProvider


class PortfolioProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ModelProviderPortfolio:
    """Dispatch each stage to its exact selected endpoint without hidden fallback."""

    provider_id = "model-provider-portfolio@1"

    def __init__(self, providers: Mapping[str, ModelProvider]) -> None:
        if not providers or any(not key.strip() for key in providers):
            raise ValueError("model provider portfolio requires endpoint identities")
        self._providers = dict(providers)

    def complete_structured(
        self, request: Mapping[str, object]
    ) -> Mapping[str, object]:
        decision = request.get("route_decision")
        if not isinstance(decision, Mapping):
            raise PortfolioProviderError(
                "models.route_decision_missing",
                "model request has no immutable route decision",
            )
        endpoint_id = decision.get("selected_endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise PortfolioProviderError(
                "models.route_endpoint_missing",
                "model route decision has no selected endpoint",
            )
        provider = self._providers.get(endpoint_id)
        if provider is None:
            raise PortfolioProviderError(
                "models.route_provider_unavailable",
                f"no provider adapter is registered for endpoint {endpoint_id!r}",
            )
        response = provider.complete_structured(request)
        if not isinstance(response, Mapping):
            raise PortfolioProviderError(
                "models.provider_response_invalid",
                f"provider for endpoint {endpoint_id!r} returned a non-object response",
            )
        return response


__all__ = ["ModelProviderPortfolio", "PortfolioProviderError"]

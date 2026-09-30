"""Tests for the dependency-free Responses API adapter."""

from __future__ import annotations

import json
import unittest

from literate_ai.adapters.models import (
    HTTPResponse,
    ModelAdapterError,
    OpenAICompatibleResponsesProvider,
)
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    StageModelPolicy,
)


class FakeTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HTTPResponse:
        self.calls.append((url, dict(headers), json.loads(body)))
        return self.responses.pop(0)


def _endpoint(*, locality: Locality = Locality.REMOTE) -> ModelEndpoint:
    return ModelEndpoint(
        endpoint_id="selected",
        provider="openai-compatible",
        model="configured-model",
        base_url=(
            "https://models.example.test/v1"
            if locality is Locality.REMOTE
            else "http://127.0.0.1:11434/v1"
        ),
        locality=locality,
        capabilities=("structured",),
        context_tokens=100_000,
        credential_ref="env:MODEL_TOKEN" if locality is Locality.REMOTE else None,
        model_revision="revision-7",
    )


def _decision(endpoint: ModelEndpoint, egress: DataEgress):
    router = ModelRouter(
        endpoints=(endpoint,),
        groups=(ModelGroup("generation", "1.0.0", (endpoint.endpoint_id,)),),
    )
    return router.select(
        StageModelPolicy(
            "generate",
            "generation",
            "generation",
            data_egress=egress,
        )
    )


def _response(status: int = 200) -> HTTPResponse:
    value = {
        "id": "resp_exact",
        "status": "completed",
        "model": "configured-model-2026-08-01",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"files":["main.py"]}'}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }
    return HTTPResponse(status, {}, json.dumps(value).encode())


class OpenAICompatibleAdapterTests(unittest.TestCase):
    def test_structured_call_records_exact_provenance_and_hides_secret(self) -> None:
        endpoint = _endpoint()
        transport = FakeTransport([_response()])
        provider = OpenAICompatibleResponsesProvider(
            endpoint=endpoint,
            route_decision=_decision(endpoint, DataEgress.SOURCE_ALLOWED),
            credential_resolver=lambda reference: "top-secret",
            transport=transport,
        )
        result = provider.complete_structured(
            {
                "instructions": "Generate against supplied source evidence.",
                "input": [{"role": "user", "content": "request"}],
                "response_schema_name": "generated_files",
                "response_schema": {
                    "type": "object",
                    "properties": {
                        "files": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["files"],
                    "additionalProperties": False,
                },
                "content_kind": "source",
                "stage_id": "generate",
                "route_decision": {"policy_id": "generate"},
            }
        )
        self.assertEqual(result["output"], {"files": ["main.py"]})
        self.assertEqual(result["model_revision"], "revision-7")
        self.assertEqual(result["attempts"], 1)
        url, headers, payload = transport.calls[0]
        self.assertEqual(url, "https://models.example.test/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer top-secret")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertNotIn("top-secret", json.dumps(result))

    def test_source_egress_is_rejected_before_transport(self) -> None:
        endpoint = _endpoint()
        transport = FakeTransport([_response()])
        provider = OpenAICompatibleResponsesProvider(
            endpoint=endpoint,
            route_decision=_decision(endpoint, DataEgress.METADATA_ONLY),
            credential_resolver=lambda reference: "secret",
            transport=transport,
        )
        with self.assertRaisesRegex(ModelAdapterError, "cannot cross"):
            provider.complete_structured(
                {
                    "input": "source",
                    "response_schema_name": "answer",
                    "response_schema": {"type": "object"},
                    "content_kind": "source",
                }
            )
        self.assertEqual(transport.calls, [])

    def test_retryable_status_is_recorded(self) -> None:
        endpoint = _endpoint(locality=Locality.LOCAL)
        transport = FakeTransport([HTTPResponse(429, {}, b"{}"), _response()])
        provider = OpenAICompatibleResponsesProvider(
            endpoint=endpoint,
            route_decision=_decision(endpoint, DataEgress.NONE),
            transport=transport,
        )
        result = provider.complete_structured(
            {
                "input": "metadata",
                "response_schema_name": "answer",
                "response_schema": {"type": "object"},
                "content_kind": "metadata",
            }
        )
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["usage"]["prior_failures"], ["http:429"])

    def test_endpoint_drift_is_rejected(self) -> None:
        endpoint = _endpoint()
        decision = _decision(endpoint, DataEgress.SOURCE_ALLOWED)
        changed = ModelEndpoint(
            endpoint_id=endpoint.endpoint_id,
            provider=endpoint.provider,
            model="different-model",
            base_url=endpoint.base_url,
            locality=endpoint.locality,
            capabilities=endpoint.capabilities,
            context_tokens=endpoint.context_tokens,
        )
        with self.assertRaisesRegex(ModelAdapterError, "changed after routing"):
            OpenAICompatibleResponsesProvider(endpoint=changed, route_decision=decision)


if __name__ == "__main__":
    unittest.main()

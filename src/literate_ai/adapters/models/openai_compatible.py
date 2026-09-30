"""Dependency-free adapter for OpenAI-compatible Responses API endpoints.

The adapter owns HTTP wire details only. Model selection remains in the domain
router and credentials remain opaque references until the request boundary.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Protocol
from urllib import error, request
from urllib.parse import urlparse

from literate_ai.models import DataEgress, Locality, ModelEndpoint, ModelRouteDecision


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


class ModelAdapterError(RuntimeError):
    """A model invocation failed before yielding a valid structured result."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class JSONTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HTTPResponse: ...


class UrllibJSONTransport:
    """Small standard-library HTTP transport with bounded response reads."""

    def __init__(self, *, maximum_response_bytes: int = 16 * 1024 * 1024) -> None:
        if maximum_response_bytes < 1:
            raise ValueError("maximum_response_bytes must be positive")
        self.maximum_response_bytes = maximum_response_bytes

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HTTPResponse:
        outbound = request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with request.urlopen(outbound, timeout=timeout_seconds) as response:
                content = response.read(self.maximum_response_bytes + 1)
                if len(content) > self.maximum_response_bytes:
                    raise ModelAdapterError(
                        "models.response_too_large",
                        "model response exceeds configured byte limit",
                    )
                return HTTPResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=content,
                )
        except error.HTTPError as exc:
            content = exc.read(self.maximum_response_bytes + 1)
            return HTTPResponse(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=content[: self.maximum_response_bytes],
            )
        except (error.URLError, TimeoutError) as exc:
            raise ModelAdapterError(
                "models.transport_failed",
                "model endpoint transport failed",
                retryable=True,
            ) from exc


class EnvironmentCredentialResolver:
    """Resolve only explicit ``env:NAME`` references without logging values."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def __call__(self, reference: str) -> str | None:
        prefix = "env:"
        if not reference.startswith(prefix):
            raise ModelAdapterError(
                "models.credential_provider_unsupported",
                "credential reference is not an environment reference",
            )
        name = reference[len(prefix) :]
        if not name or not name.replace("_", "A").isalnum():
            raise ModelAdapterError(
                "models.credential_reference_invalid",
                "credential environment name is invalid",
            )
        return self._environment.get(name)


@dataclass(frozen=True, slots=True)
class StructuredModelResult:
    provider_id: str
    endpoint_id: str
    endpoint_digest: str
    model: str
    model_revision: str | None
    route_decision_digest: str
    request_digest: str
    response_digest: str
    response_id: str
    output: Mapping[str, object]
    usage: Mapping[str, object]
    attempts: int
    status: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


CredentialResolver = Callable[[str], str | None]


class OpenAICompatibleResponsesProvider:
    """Execute schema-constrained calls against one already-selected endpoint."""

    provider_id = "openai-compatible-responses-v1"

    def __init__(
        self,
        *,
        endpoint: ModelEndpoint,
        route_decision: ModelRouteDecision,
        credential_resolver: CredentialResolver | None = None,
        transport: JSONTransport | None = None,
        timeout_seconds: float = 120.0,
        maximum_attempts: int = 2,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= maximum_attempts <= 5:
            raise ValueError("maximum_attempts must be between one and five")
        if route_decision.selected_endpoint_id != endpoint.endpoint_id:
            raise ModelAdapterError(
                "models.route_endpoint_mismatch",
                "route decision does not select this endpoint",
            )
        if route_decision.selected_endpoint_digest != endpoint.digest:
            raise ModelAdapterError(
                "models.route_endpoint_drift",
                "selected endpoint changed after routing",
            )
        self.endpoint = endpoint
        self.route_decision = route_decision
        self.credential_resolver = credential_resolver
        self.transport = transport or UrllibJSONTransport()
        self.timeout_seconds = timeout_seconds
        self.maximum_attempts = maximum_attempts

    def complete_structured(
        self, invocation: Mapping[str, object]
    ) -> dict[str, object]:
        payload = self._request_payload(invocation)
        body = _canonical_bytes(payload)
        headers = {"Content-Type": "application/json"}
        if self.endpoint.credential_ref is not None:
            if self.credential_resolver is None:
                raise ModelAdapterError(
                    "models.credential_resolver_missing",
                    "endpoint requires a credential resolver",
                )
            credential = self.credential_resolver(self.endpoint.credential_ref)
            if not credential:
                raise ModelAdapterError(
                    "models.credential_unavailable",
                    "model endpoint credential is unavailable",
                )
            headers["Authorization"] = f"Bearer {credential}"

        failures: list[str] = []
        for attempt in range(1, self.maximum_attempts + 1):
            try:
                response = self.transport.post(
                    _responses_url(self.endpoint.base_url),
                    headers=headers,
                    body=body,
                    timeout_seconds=self.timeout_seconds,
                )
            except ModelAdapterError as exc:
                failures.append(exc.code)
                if not exc.retryable or attempt == self.maximum_attempts:
                    raise ModelAdapterError(
                        exc.code,
                        str(exc),
                        retryable=exc.retryable,
                        attempts=attempt,
                    ) from exc
                continue
            if response.status < 200 or response.status >= 300:
                retryable = response.status in {408, 409, 429} or response.status >= 500
                failures.append(f"http:{response.status}")
                if retryable and attempt < self.maximum_attempts:
                    continue
                raise ModelAdapterError(
                    "models.http_error",
                    f"model endpoint returned HTTP {response.status}",
                    retryable=retryable,
                    attempts=attempt,
                )
            return self._parse_response(
                response.body,
                request_digest=_digest(payload),
                attempts=attempt,
                prior_failures=tuple(failures),
            ).to_dict()
        raise AssertionError("model attempt loop ended without a result")

    def _request_payload(self, invocation: Mapping[str, object]) -> dict[str, object]:
        allowed = {
            "input",
            "instructions",
            "response_schema",
            "response_schema_name",
            "max_output_tokens",
            "metadata",
            "content_kind",
            # Immutable orchestration provenance may accompany the provider wire
            # request. It is already included in ``input`` and is not forwarded
            # as an additional API field.
            "stage_id",
            "input_identity",
            "run_input_identity",
            "component_revision_ids",
            "component_edges",
            "effective_revision",
            "source_snapshot_ids",
            "evidence_ids",
            "route_decision",
            "prior_stage_outputs",
        }
        unknown = set(invocation) - allowed
        if unknown:
            raise ModelAdapterError(
                "models.request_unknown_fields",
                f"unknown model request fields: {', '.join(sorted(unknown))}",
            )
        required = {"input", "response_schema", "response_schema_name", "content_kind"}
        missing = required - set(invocation)
        if missing:
            raise ModelAdapterError(
                "models.request_missing_fields",
                f"missing model request fields: {', '.join(sorted(missing))}",
            )
        self._enforce_egress(str(invocation["content_kind"]))
        schema = invocation["response_schema"]
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise ModelAdapterError(
                "models.response_schema_invalid",
                "structured response schema root must be an object",
            )
        name = invocation["response_schema_name"]
        if not isinstance(name, str) or not name or len(name) > 64:
            raise ModelAdapterError(
                "models.response_schema_name_invalid",
                "response schema name must contain 1 to 64 characters",
            )
        payload: dict[str, object] = {
            "model": self.endpoint.model,
            "input": invocation["input"],
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": name,
                    "schema": dict(schema),
                    "strict": True,
                }
            },
        }
        for field in ("instructions", "max_output_tokens", "metadata"):
            if field in invocation:
                payload[field] = invocation[field]
        try:
            _canonical_bytes(payload)
        except (TypeError, ValueError) as exc:
            raise ModelAdapterError(
                "models.request_not_json",
                "model request is not canonically JSON serializable",
            ) from exc
        return payload

    def _enforce_egress(self, content_kind: str) -> None:
        if content_kind not in {"metadata", "source"}:
            raise ModelAdapterError(
                "models.content_kind_invalid",
                "content_kind must be metadata or source",
            )
        if self.endpoint.locality is Locality.LOCAL:
            _require_local_url(self.endpoint.base_url)
            return
        if self.route_decision.data_egress is DataEgress.NONE:
            raise ModelAdapterError(
                "models.egress_denied", "remote model egress is not authorized"
            )
        if (
            content_kind == "source"
            and self.route_decision.data_egress is not DataEgress.SOURCE_ALLOWED
        ):
            raise ModelAdapterError(
                "models.source_egress_denied",
                "source content cannot cross this model boundary",
            )

    def _parse_response(
        self,
        body: bytes,
        *,
        request_digest: str,
        attempts: int,
        prior_failures: tuple[str, ...],
    ) -> StructuredModelResult:
        try:
            response = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelAdapterError(
                "models.response_not_json",
                "model endpoint returned invalid JSON",
                attempts=attempts,
            ) from exc
        if not isinstance(response, Mapping):
            raise ModelAdapterError(
                "models.response_invalid", "model response must be an object"
            )
        status = str(response.get("status", ""))
        if status != "completed":
            raise ModelAdapterError(
                "models.response_incomplete",
                f"model response did not complete: {status or 'unknown'}",
                retryable=False,
                attempts=attempts,
            )
        output_text = _extract_output_text(response)
        try:
            structured = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise ModelAdapterError(
                "models.structured_output_invalid",
                "model structured output is not valid JSON",
                attempts=attempts,
            ) from exc
        if not isinstance(structured, Mapping):
            raise ModelAdapterError(
                "models.structured_output_not_object",
                "model structured output must be an object",
                attempts=attempts,
            )
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ModelAdapterError(
                "models.response_id_missing", "model response has no identity"
            )
        response_model = response.get("model")
        if not isinstance(response_model, str) or not response_model:
            response_model = self.endpoint.model
        usage = response.get("usage", {})
        if not isinstance(usage, Mapping):
            usage = {}
        usage_with_failures = dict(usage)
        if prior_failures:
            usage_with_failures["prior_failures"] = list(prior_failures)
        return StructuredModelResult(
            provider_id=self.provider_id,
            endpoint_id=self.endpoint.endpoint_id,
            endpoint_digest=self.endpoint.digest,
            model=response_model,
            model_revision=self.endpoint.model_revision,
            route_decision_digest=self.route_decision.digest,
            request_digest=request_digest,
            response_digest=_digest(response),
            response_id=response_id,
            output=dict(structured),
            usage=usage_with_failures,
            attempts=attempts,
            status=status,
        )


def _responses_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    return (
        normalized if normalized.endswith("/responses") else f"{normalized}/responses"
    )


def _require_local_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "unix":
        return
    if parsed.hostname in {"localhost", "localhost.localdomain"}:
        return
    try:
        if parsed.hostname and ipaddress.ip_address(parsed.hostname).is_loopback:
            return
    except ValueError:
        pass
    raise ModelAdapterError(
        "models.locality_drift", "local endpoint transport is no longer loopback"
    )


def _extract_output_text(response: Mapping[str, object]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    output = response.get("output")
    if not isinstance(output, list):
        raise ModelAdapterError(
            "models.output_missing", "model response contains no output items"
        )
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "refusal":
                raise ModelAdapterError(
                    "models.refused", "model refused the structured request"
                )
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text_parts.append(str(part["text"]))
    if not text_parts:
        raise ModelAdapterError(
            "models.output_text_missing", "model response contains no output text"
        )
    return "".join(text_parts)

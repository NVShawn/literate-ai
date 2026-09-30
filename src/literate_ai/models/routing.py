"""Deterministic, request-scoped model routing without provider SDK dependencies."""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar, Final
from urllib.parse import urlparse

from literate_ai.contracts import (
    ContentIdentity,
    VersionedContentRef,
    canonical_identity,
    semantic_version,
)


def _digest(value: object) -> str:
    return canonical_identity(value).uri


_CANONICAL_INTEGER_MAX: Final = 2**63 - 1
_COST_IDENTITY_ENCODING: Final = "decimal-v1"

MODEL_ENDPOINT_SCHEMA: Final = "urn:literate-ai:schema:v2:model-endpoint"
MODEL_GROUP_SCHEMA: Final = "urn:literate-ai:schema:v2:model-group"
STAGE_MODEL_POLICY_SCHEMA: Final = "urn:literate-ai:schema:v2:stage-model-policy"
MODEL_ROUTE_DECISION_SCHEMA: Final = "urn:literate-ai:schema:v2:model-route-decision"

_MODEL_ROUTING_FIELDS: Final[dict[str, frozenset[str]]] = {
    MODEL_ENDPOINT_SCHEMA: frozenset(
        {
            "endpoint_id",
            "provider",
            "model",
            "base_url",
            "locality",
            "capabilities",
            "context_tokens",
            "input_cost_per_million",
            "output_cost_per_million",
            "available",
            "credential_ref",
            "model_revision",
            "version",
        }
    ),
    MODEL_GROUP_SCHEMA: frozenset(
        {"group_id", "version", "endpoint_ids", "endpoint_refs"}
    ),
    STAGE_MODEL_POLICY_SCHEMA: frozenset(
        {
            "policy_id",
            "stage_type",
            "group_id",
            "required_capabilities",
            "required_locality",
            "minimum_context_tokens",
            "maximum_input_cost_per_million",
            "allowed_providers",
            "fallback_allowed",
            "data_egress",
            "version",
            "group_ref",
        }
    ),
    MODEL_ROUTE_DECISION_SCHEMA: frozenset(
        {
            "policy_id",
            "group_digest",
            "selected_endpoint_id",
            "selected_endpoint_digest",
            "considered_endpoint_ids",
            "rejection_reasons",
            "fallback_used",
            "data_egress",
            "group_ref",
            "selected_endpoint_ref",
        }
    ),
}


def adapt_unreleased_post_v011_model_routing_document(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Add a v2 envelope to one exact historical routing field set.

    This helper identifies an envelope only.  Use the matching typed ``from_dict``
    reader to validate values and obtain a canonical current document.
    """

    if "schema" in value:
        raise ValueError("model routing legacy adapter does not accept versioned input")
    matches = [
        schema
        for schema, fields in _MODEL_ROUTING_FIELDS.items()
        if set(value) == fields
    ]
    if len(matches) != 1:
        raise ValueError("model routing fields do not match a supported legacy shape")
    return {"schema": matches[0], **dict(value)}


def normalize_model_routing_document(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Normalize only a routing envelope; typed readers validate its values."""

    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("model routing document must be an object")
    if "schema" not in value:
        return adapt_unreleased_post_v011_model_routing_document(value)
    schema = value["schema"]
    if not isinstance(schema, str) or schema not in _MODEL_ROUTING_FIELDS:
        raise ValueError(f"model routing schema is unsupported: {schema!r}")
    if set(value) != _MODEL_ROUTING_FIELDS[schema] | {"schema"}:
        raise ValueError("model routing fields do not match the current schema")
    return dict(value)


def _model_document(
    value: Mapping[str, object], expected_schema: str
) -> dict[str, object]:
    document = normalize_model_routing_document(value)
    if document["schema"] != expected_schema:
        raise ValueError(f"expected {expected_schema}, got {document['schema']!r}")
    return document


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} must be non-empty text")
    return value


def _array(value: object, path: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{path} must be an array")
    return tuple(value)


def _canonical_cost(value: object, path: str) -> str:
    """Return a portable decimal spelling for a routing cost.

    Costs remain JSON numbers in public documents. Identity material uses this
    tagged decimal form because canonical JSON v1 intentionally rejects binary
    floating-point values.
    """

    if isinstance(value, bool):
        raise ValueError(f"{path} cannot be a boolean")
    if not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be an integer or float")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be finite")
    if value < 0:
        raise ValueError(f"{path} cannot be negative")

    decimal = Decimal(str(value))
    if decimal.is_zero():
        return "0"
    _, raw_digits, exponent = decimal.as_tuple()
    digits = list(raw_digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = "".join(str(digit) for digit in digits)
    decimal_point = len(text) + exponent
    if decimal_point <= 0:
        return f"0.{('0' * -decimal_point)}{text}"
    if decimal_point >= len(text):
        return f"{text}{('0' * (decimal_point - len(text)))}"
    return f"{text[:decimal_point]}.{text[decimal_point:]}"


def _cost_identity(value: object, path: str) -> dict[str, str]:
    return {
        "encoding": _COST_IDENTITY_ENCODING,
        "value": _canonical_cost(value, path),
    }


def _cost_identity_material(
    value: dict[str, object], *fields: str
) -> dict[str, object]:
    material = dict(value)
    for field in fields:
        cost = material[field]
        if cost is not None:
            material[field] = _cost_identity(cost, field)
    return material


def _positive_canonical_integer(value: int, path: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _CANONICAL_INTEGER_MAX
    ):
        raise ValueError(f"{path} must be a positive signed 64-bit integer")


class Locality(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class DataEgress(StrEnum):
    NONE = "none"
    METADATA_ONLY = "metadata-only"
    SOURCE_ALLOWED = "source-allowed"


class RoutingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModelEndpoint:
    SCHEMA: ClassVar[str] = MODEL_ENDPOINT_SCHEMA

    endpoint_id: str
    provider: str
    model: str
    base_url: str
    locality: Locality
    capabilities: tuple[str, ...]
    context_tokens: int
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    available: bool = True
    credential_ref: str | None = None
    model_revision: str | None = None
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not self.endpoint_id or not self.provider or not self.model:
            raise ValueError("model endpoint identity cannot be empty")
        semantic_version(self.version, "ModelEndpoint.version")
        _positive_canonical_integer(self.context_tokens, "context_tokens")
        _canonical_cost(self.input_cost_per_million, "input_cost_per_million")
        _canonical_cost(self.output_cost_per_million, "output_cost_per_million")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https", "unix", "cli"}:
            raise ValueError(
                "model endpoint transport must be http, https, unix, or cli"
            )
        if self.locality is Locality.LOCAL and not _is_local_transport(parsed):
            raise ValueError(
                "local model endpoint must use a loopback or unix transport"
            )
        if parsed.scheme == "cli" and self.locality is not Locality.UNKNOWN:
            raise ValueError("coding CLI model locality must remain unknown")

    @property
    def digest(self) -> str:
        return _digest(
            _cost_identity_material(
                self.to_dict(),
                "input_cost_per_million",
                "output_cost_per_million",
            )
        )

    @property
    def ref(self) -> VersionedContentRef:
        return VersionedContentRef(
            "model-endpoint",
            self.endpoint_id,
            self.version,
            ContentIdentity.parse_uri(self.digest),
        )

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["schema"] = self.SCHEMA
        value["capabilities"] = list(self.capabilities)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ModelEndpoint:
        data = _model_document(value, cls.SCHEMA)
        capabilities = tuple(
            _text(item, "ModelEndpoint.capabilities")
            for item in _array(data["capabilities"], "ModelEndpoint.capabilities")
        )
        locality = Locality(_text(data["locality"], "ModelEndpoint.locality"))
        context_tokens = data["context_tokens"]
        available = data["available"]
        if isinstance(context_tokens, bool) or not isinstance(context_tokens, int):
            raise ValueError("ModelEndpoint.context_tokens must be an integer")
        if not isinstance(available, bool):
            raise ValueError("ModelEndpoint.available must be boolean")
        for field in ("input_cost_per_million", "output_cost_per_million"):
            if isinstance(data[field], bool) or not isinstance(
                data[field], (int, float)
            ):
                raise ValueError(f"ModelEndpoint.{field} must be numeric")
        for field in ("credential_ref", "model_revision"):
            if data[field] is not None and not isinstance(data[field], str):
                raise ValueError(f"ModelEndpoint.{field} must be text or null")
        return cls(
            endpoint_id=_text(data["endpoint_id"], "ModelEndpoint.endpoint_id"),
            provider=_text(data["provider"], "ModelEndpoint.provider"),
            model=_text(data["model"], "ModelEndpoint.model"),
            base_url=_text(data["base_url"], "ModelEndpoint.base_url"),
            locality=locality,
            capabilities=capabilities,
            context_tokens=context_tokens,
            input_cost_per_million=data["input_cost_per_million"],
            output_cost_per_million=data["output_cost_per_million"],
            available=available,
            credential_ref=data["credential_ref"],
            model_revision=data["model_revision"],
            version=_text(data["version"], "ModelEndpoint.version"),
        )


def _is_local_transport(parsed: object) -> bool:
    if getattr(parsed, "scheme", None) == "unix":
        return True
    hostname = getattr(parsed, "hostname", None)
    if hostname in {"localhost", "localhost.localdomain"}:
        return True
    if not hostname:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class ModelGroup:
    SCHEMA: ClassVar[str] = MODEL_GROUP_SCHEMA

    group_id: str
    version: str
    endpoint_ids: tuple[str, ...]
    endpoint_refs: tuple[VersionedContentRef, ...] = ()

    def __post_init__(self) -> None:
        if not self.group_id or not self.endpoint_ids:
            raise ValueError("model group requires identity, version, and endpoints")
        semantic_version(self.version, "ModelGroup.version")
        if len(set(self.endpoint_ids)) != len(self.endpoint_ids):
            raise ValueError("model group endpoint order cannot contain duplicates")
        if self.endpoint_refs:
            referenced_ids = tuple(item.identifier for item in self.endpoint_refs)
            if referenced_ids != self.endpoint_ids:
                raise ValueError(
                    "model group exact endpoint refs must match endpoint order"
                )
            if any(item.kind != "model-endpoint" for item in self.endpoint_refs):
                raise ValueError("model group refs must identify model endpoints")
            referenced_uris = {item.uri for item in self.endpoint_refs}
            if len(referenced_uris) != len(self.endpoint_refs):
                raise ValueError("model group exact endpoint refs must be unique")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    @property
    def ref(self) -> VersionedContentRef:
        return VersionedContentRef(
            "model-group",
            self.group_id,
            self.version,
            ContentIdentity.parse_uri(self.digest),
        )

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["schema"] = self.SCHEMA
        value["endpoint_ids"] = list(self.endpoint_ids)
        value["endpoint_refs"] = [item.to_dict() for item in self.endpoint_refs]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ModelGroup:
        data = _model_document(value, cls.SCHEMA)
        endpoint_refs = _array(data["endpoint_refs"], "ModelGroup.endpoint_refs")
        if any(not isinstance(item, Mapping) for item in endpoint_refs):
            raise ValueError("ModelGroup.endpoint_refs must contain objects")
        return cls(
            group_id=_text(data["group_id"], "ModelGroup.group_id"),
            version=_text(data["version"], "ModelGroup.version"),
            endpoint_ids=tuple(
                _text(item, "ModelGroup.endpoint_ids")
                for item in _array(data["endpoint_ids"], "ModelGroup.endpoint_ids")
            ),
            endpoint_refs=tuple(
                VersionedContentRef.from_dict(item) for item in endpoint_refs
            ),
        )


@dataclass(frozen=True, slots=True)
class StageModelPolicy:
    SCHEMA: ClassVar[str] = STAGE_MODEL_POLICY_SCHEMA

    policy_id: str
    stage_type: str
    group_id: str
    required_capabilities: tuple[str, ...] = ()
    required_locality: Locality | None = None
    minimum_context_tokens: int = 1
    maximum_input_cost_per_million: float | None = None
    allowed_providers: tuple[str, ...] = ()
    fallback_allowed: bool = True
    data_egress: DataEgress = DataEgress.NONE
    version: str = "1.0.0"
    group_ref: VersionedContentRef | None = None

    def __post_init__(self) -> None:
        if not self.policy_id or not self.stage_type or not self.group_id:
            raise ValueError("stage model policy identity cannot be empty")
        semantic_version(self.version, "StageModelPolicy.version")
        if self.group_ref is not None and (
            self.group_ref.kind != "model-group"
            or self.group_ref.identifier != self.group_id
        ):
            raise ValueError("stage model policy group ref does not match group ID")
        _positive_canonical_integer(
            self.minimum_context_tokens, "minimum_context_tokens"
        )
        if self.maximum_input_cost_per_million is not None:
            _canonical_cost(
                self.maximum_input_cost_per_million,
                "maximum_input_cost_per_million",
            )

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["schema"] = self.SCHEMA
        value["required_capabilities"] = list(self.required_capabilities)
        value["allowed_providers"] = list(self.allowed_providers)
        value["group_ref"] = self.group_ref.to_dict() if self.group_ref else None
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> StageModelPolicy:
        data = _model_document(value, cls.SCHEMA)
        required_locality = data["required_locality"]
        maximum_cost = data["maximum_input_cost_per_million"]
        minimum_context = data["minimum_context_tokens"]
        fallback_allowed = data["fallback_allowed"]
        if required_locality is not None and not isinstance(required_locality, str):
            raise ValueError("StageModelPolicy.required_locality must be text or null")
        if maximum_cost is not None and (
            isinstance(maximum_cost, bool) or not isinstance(maximum_cost, (int, float))
        ):
            raise ValueError(
                "StageModelPolicy.maximum_input_cost_per_million is invalid"
            )
        if isinstance(minimum_context, bool) or not isinstance(minimum_context, int):
            raise ValueError(
                "StageModelPolicy.minimum_context_tokens must be an integer"
            )
        if not isinstance(fallback_allowed, bool):
            raise ValueError("StageModelPolicy.fallback_allowed must be boolean")
        group_ref = data["group_ref"]
        if group_ref is not None and not isinstance(group_ref, Mapping):
            raise ValueError("StageModelPolicy.group_ref must be an object or null")
        return cls(
            policy_id=_text(data["policy_id"], "StageModelPolicy.policy_id"),
            stage_type=_text(data["stage_type"], "StageModelPolicy.stage_type"),
            group_id=_text(data["group_id"], "StageModelPolicy.group_id"),
            required_capabilities=tuple(
                _text(item, "StageModelPolicy.required_capabilities")
                for item in _array(
                    data["required_capabilities"],
                    "StageModelPolicy.required_capabilities",
                )
            ),
            required_locality=(
                Locality(required_locality) if required_locality is not None else None
            ),
            minimum_context_tokens=minimum_context,
            maximum_input_cost_per_million=maximum_cost,
            allowed_providers=tuple(
                _text(item, "StageModelPolicy.allowed_providers")
                for item in _array(
                    data["allowed_providers"], "StageModelPolicy.allowed_providers"
                )
            ),
            fallback_allowed=fallback_allowed,
            data_egress=DataEgress(_text(data["data_egress"], "data_egress")),
            version=_text(data["version"], "StageModelPolicy.version"),
            group_ref=(
                VersionedContentRef.from_dict(group_ref)
                if isinstance(group_ref, Mapping)
                else None
            ),
        )

    @property
    def digest(self) -> str:
        return _digest(
            _cost_identity_material(self.to_dict(), "maximum_input_cost_per_million")
        )

    @property
    def ref(self) -> VersionedContentRef:
        return VersionedContentRef(
            "model-policy",
            self.policy_id,
            self.version,
            ContentIdentity.parse_uri(self.digest),
        )


@dataclass(frozen=True, slots=True)
class ModelRouteDecision:
    SCHEMA: ClassVar[str] = MODEL_ROUTE_DECISION_SCHEMA

    policy_id: str
    group_digest: str
    selected_endpoint_id: str
    selected_endpoint_digest: str
    considered_endpoint_ids: tuple[str, ...]
    rejection_reasons: tuple[tuple[str, tuple[str, ...]], ...]
    fallback_used: bool
    data_egress: DataEgress
    group_ref: VersionedContentRef | None = None
    selected_endpoint_ref: VersionedContentRef | None = None

    def __post_init__(self) -> None:
        if not self.policy_id or not self.selected_endpoint_id:
            raise ValueError("model route decision identity cannot be empty")
        ContentIdentity.parse_uri(self.group_digest)
        ContentIdentity.parse_uri(self.selected_endpoint_digest)
        if (
            not self.considered_endpoint_ids
            or len(set(self.considered_endpoint_ids))
            != len(self.considered_endpoint_ids)
            or self.selected_endpoint_id not in self.considered_endpoint_ids
        ):
            raise ValueError(
                "model route must consider unique endpoints including its selection"
            )
        rejected_ids = tuple(item[0] for item in self.rejection_reasons)
        if len(set(rejected_ids)) != len(rejected_ids) or not set(
            rejected_ids
        ).issubset(self.considered_endpoint_ids):
            raise ValueError("model route rejection records must target considered IDs")
        if self.group_ref is None or self.group_ref.kind != "model-group":
            raise ValueError("model route requires an exact model-group reference")
        if (
            self.group_digest != self.group_ref.content_identity.uri
            or self.selected_endpoint_ref is None
            or self.selected_endpoint_ref.kind != "model-endpoint"
            or self.selected_endpoint_ref.identifier != self.selected_endpoint_id
            or self.selected_endpoint_digest
            != self.selected_endpoint_ref.content_identity.uri
        ):
            raise ValueError("model route exact references do not match its selection")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["schema"] = self.SCHEMA
        value["considered_endpoint_ids"] = list(self.considered_endpoint_ids)
        value["rejection_reasons"] = [
            [endpoint_id, list(reasons)]
            for endpoint_id, reasons in self.rejection_reasons
        ]
        value["group_ref"] = self.group_ref.to_dict() if self.group_ref else None
        value["selected_endpoint_ref"] = (
            self.selected_endpoint_ref.to_dict() if self.selected_endpoint_ref else None
        )
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ModelRouteDecision:
        data = _model_document(value, cls.SCHEMA)
        group_ref = data["group_ref"]
        endpoint_ref = data["selected_endpoint_ref"]
        if not isinstance(group_ref, Mapping) or not isinstance(endpoint_ref, Mapping):
            raise ValueError("ModelRouteDecision exact references must be objects")
        raw_rejections = _array(
            data["rejection_reasons"], "ModelRouteDecision.rejection_reasons"
        )
        rejections: list[tuple[str, tuple[str, ...]]] = []
        for item in raw_rejections:
            pair = _array(item, "ModelRouteDecision.rejection_reasons item")
            if len(pair) != 2:
                raise ValueError("ModelRouteDecision rejection must be a pair")
            rejections.append(
                (
                    _text(pair[0], "ModelRouteDecision rejection endpoint"),
                    tuple(
                        _text(reason, "ModelRouteDecision rejection reason")
                        for reason in _array(
                            pair[1], "ModelRouteDecision rejection reasons"
                        )
                    ),
                )
            )
        fallback_used = data["fallback_used"]
        if not isinstance(fallback_used, bool):
            raise ValueError("ModelRouteDecision.fallback_used must be boolean")
        return cls(
            policy_id=_text(data["policy_id"], "ModelRouteDecision.policy_id"),
            group_digest=_text(data["group_digest"], "ModelRouteDecision.group_digest"),
            selected_endpoint_id=_text(
                data["selected_endpoint_id"], "ModelRouteDecision.selected_endpoint_id"
            ),
            selected_endpoint_digest=_text(
                data["selected_endpoint_digest"],
                "ModelRouteDecision.selected_endpoint_digest",
            ),
            considered_endpoint_ids=tuple(
                _text(item, "ModelRouteDecision.considered_endpoint_ids")
                for item in _array(
                    data["considered_endpoint_ids"],
                    "ModelRouteDecision.considered_endpoint_ids",
                )
            ),
            rejection_reasons=tuple(rejections),
            fallback_used=fallback_used,
            data_egress=DataEgress(_text(data["data_egress"], "data_egress")),
            group_ref=VersionedContentRef.from_dict(group_ref),
            selected_endpoint_ref=VersionedContentRef.from_dict(endpoint_ref),
        )


class ModelRouter:
    """Pure router; every call returns isolated immutable provenance."""

    def __init__(
        self,
        *,
        endpoints: tuple[ModelEndpoint, ...],
        groups: tuple[ModelGroup, ...],
    ) -> None:
        self._endpoints = {item.ref.uri: item for item in endpoints}
        self._groups = {item.ref.uri: item for item in groups}
        if len(self._endpoints) != len(endpoints) or len(self._groups) != len(groups):
            raise ValueError("model endpoint and group revisions must be unique")
        endpoints_by_id: dict[str, list[ModelEndpoint]] = {}
        groups_by_id: dict[str, list[ModelGroup]] = {}
        for endpoint in endpoints:
            endpoints_by_id.setdefault(endpoint.endpoint_id, []).append(endpoint)
        for group in groups:
            groups_by_id.setdefault(group.group_id, []).append(group)
        self._endpoints_by_id = {
            key: tuple(value) for key, value in endpoints_by_id.items()
        }
        self._groups_by_id = {key: tuple(value) for key, value in groups_by_id.items()}
        self._group_endpoints: dict[str, tuple[ModelEndpoint, ...]] = {}
        for group in groups:
            resolved: list[ModelEndpoint] = []
            if group.endpoint_refs:
                for reference in group.endpoint_refs:
                    endpoint = self._endpoints.get(reference.uri)
                    if endpoint is None or endpoint.ref != reference:
                        raise ValueError(
                            f"model group has unknown exact endpoint: {reference.uri}"
                        )
                    resolved.append(endpoint)
            else:
                for endpoint_id in group.endpoint_ids:
                    candidates = self._endpoints_by_id.get(endpoint_id, ())
                    if not candidates:
                        raise ValueError(
                            f"model group has unknown endpoint: {endpoint_id}"
                        )
                    if len(candidates) != 1:
                        raise ValueError(
                            "legacy model group endpoint is ambiguous; "
                            "exact endpoint_refs are required"
                        )
                    resolved.append(candidates[0])
            self._group_endpoints[group.ref.uri] = tuple(resolved)

    def select(self, policy: StageModelPolicy) -> ModelRouteDecision:
        if policy.group_ref is not None:
            group = self._groups.get(policy.group_ref.uri)
            if group is None or group.ref != policy.group_ref:
                raise RoutingError("models.group_unknown", policy.group_ref.uri)
        else:
            candidates = self._groups_by_id.get(policy.group_id, ())
            if not candidates:
                raise RoutingError("models.group_unknown", policy.group_id)
            if len(candidates) != 1:
                raise RoutingError(
                    "models.legacy_group_ambiguous",
                    f"group {policy.group_id!r} requires an exact group_ref",
                )
            group = candidates[0]
        considered: list[str] = []
        rejected: list[tuple[str, tuple[str, ...]]] = []
        selected: ModelEndpoint | None = None
        selected_index = -1
        for index, endpoint in enumerate(self._group_endpoints[group.ref.uri]):
            endpoint_id = endpoint.endpoint_id
            considered.append(endpoint_id)
            reasons = self._rejections(endpoint, policy)
            if reasons:
                rejected.append((endpoint_id, tuple(reasons)))
                continue
            selected = endpoint
            selected_index = index
            break
        if selected is None:
            raise RoutingError(
                "models.no_compatible_endpoint",
                f"No endpoint in {group.group_id!r} satisfies {policy.policy_id!r}",
            )
        if selected_index > 0 and not policy.fallback_allowed:
            raise RoutingError(
                "models.fallback_disallowed",
                f"Preferred endpoint for {policy.policy_id!r} is unavailable",
            )
        return ModelRouteDecision(
            policy_id=policy.policy_id,
            group_digest=group.digest,
            selected_endpoint_id=selected.endpoint_id,
            selected_endpoint_digest=selected.digest,
            considered_endpoint_ids=tuple(considered),
            rejection_reasons=tuple(rejected),
            fallback_used=selected_index > 0,
            data_egress=policy.data_egress,
            group_ref=group.ref,
            selected_endpoint_ref=selected.ref,
        )

    @staticmethod
    def _rejections(endpoint: ModelEndpoint, policy: StageModelPolicy) -> list[str]:
        reasons: list[str] = []
        if not endpoint.available:
            reasons.append("unavailable")
        if not set(policy.required_capabilities).issubset(endpoint.capabilities):
            reasons.append("capabilities")
        if (
            policy.required_locality
            and endpoint.locality is not policy.required_locality
        ):
            reasons.append("locality")
        if endpoint.context_tokens < policy.minimum_context_tokens:
            reasons.append("context")
        if (
            policy.maximum_input_cost_per_million is not None
            and endpoint.input_cost_per_million > policy.maximum_input_cost_per_million
        ):
            reasons.append("cost")
        if (
            policy.allowed_providers
            and endpoint.provider not in policy.allowed_providers
        ):
            reasons.append("provider")
        if (
            endpoint.locality is not Locality.LOCAL
            and policy.data_egress is DataEgress.NONE
        ):
            reasons.append("egress")
        return reasons

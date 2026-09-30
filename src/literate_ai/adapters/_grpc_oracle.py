"""Native gRPC declarations for the opt-in IPC @2 oracle; no network execution."""

import re
from dataclasses import dataclass

from literate_ai.adapters._grpc_call_cases import (
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    GrpcCallCase,
    GrpcCardinality,
    GrpcErrorDetail,
    GrpcStatus,
)
from literate_ai.adapters._grpc_descriptors import (
    MAX_DESCRIPTOR_BYTES,
    GrpcDescriptorClosure,
)
from literate_ai.contracts import ContentIdentity


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("gRPC declaration has missing or unknown fields")
    return value


def _hex(value, maximum):
    if (
        not isinstance(value, str)
        or len(value) > 2 * maximum
        or re.fullmatch(r"(?:[0-9a-f]{2})*", value) is None
    ):
        raise ValueError("gRPC payload must be bounded lowercase hexadecimal")
    return bytes.fromhex(value)


def _messages(value):
    if not isinstance(value, list) or len(value) > MAX_MESSAGES:
        raise ValueError("gRPC message sequence is not bounded")
    return tuple(_hex(item, MAX_MESSAGE_BYTES) for item in value)


@dataclass(frozen=True, slots=True)
class GrpcReflectionProbe:
    descriptor_set: bytes
    timeout_milliseconds: int

    @property
    def timeout_seconds(self):
        return self.timeout_milliseconds / 1000

    def to_document(self):
        return {
            "descriptor_set_hex": self.descriptor_set.hex(),
            "timeout_milliseconds": self.timeout_milliseconds,
        }


@dataclass(frozen=True, slots=True)
class GrpcBoundCallCase:
    call: GrpcCallCase
    input_type: str
    output_type: str

    def to_document(self):
        c = self.call
        return {
            "method": c.method,
            "cardinality": c.cardinality.value,
            "input_type": self.input_type,
            "output_type": self.output_type,
            "requests": [p.hex() for p in c.requests],
            "responses": [p.hex() for p in c.responses],
            "status": c.status.value,
            "status_message": c.status_message,
            "timeout_milliseconds": c.timeout_milliseconds,
            "error_details": [
                {"type_name": d.type_name, "message": d.message.hex()}
                for d in c.error_details
            ],
        }


def load_grpc_declarations(description, cases, declared_identity):
    description = _object(description, ("descriptor_set_hex", "timeout_milliseconds"))
    timeout = description["timeout_milliseconds"]
    if type(timeout) is not int or not 1 <= timeout <= 120_000:
        raise ValueError(
            "gRPC reflection timeout must be 1..120000 integer milliseconds"
        )
    payload = _hex(description["descriptor_set_hex"], MAX_DESCRIPTOR_BYTES)
    closure = GrpcDescriptorClosure(
        payload, ContentIdentity.parse_uri(declared_identity)
    )
    if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
        raise ValueError("gRPC oracle requires 1..64 cases")
    bound = []
    for value in cases:
        value = _object(
            value,
            (
                "method",
                "cardinality",
                "input_type",
                "output_type",
                "requests",
                "responses",
                "status",
                "status_message",
                "timeout_milliseconds",
                "error_details",
            ),
        )
        details = value["error_details"]
        if not isinstance(details, list) or len(details) > 32:
            raise ValueError("gRPC error details must be a bounded array")
        parsed_details = []
        for detail in details:
            detail = _object(detail, ("type_name", "message"))
            parsed_details.append(
                GrpcErrorDetail(
                    detail["type_name"], _hex(detail["message"], MAX_MESSAGE_BYTES)
                )
            )
        call = GrpcCallCase(
            value["method"],
            GrpcCardinality(value["cardinality"]),
            _messages(value["requests"]),
            _messages(value["responses"]),
            GrpcStatus(value["status"]),
            value["timeout_milliseconds"],
            value["status_message"],
            tuple(parsed_details),
        )
        binding = closure.validate_case(call)
        if (
            value["input_type"] != binding.input_type
            or value["output_type"] != binding.output_type
        ):
            raise ValueError("gRPC declared message types disagree with the descriptor")
        bound.append(GrpcBoundCallCase(call, binding.input_type, binding.output_type))
    return GrpcReflectionProbe(payload, timeout), tuple(bound)

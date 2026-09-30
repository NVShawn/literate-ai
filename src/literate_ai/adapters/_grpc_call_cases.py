"""Internal bounded gRPC cases; descriptor validation and transport are separate.

These values describe verifier expectations, not permission to execute a call.
Message bytes must still be decoded against the pinned protobuf descriptor before
transport or acceptance. They are not a public oracle document schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_NAME = r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*"
_METHOD = re.compile(rf"/{_NAME}/[A-Za-z_][A-Za-z_0-9]*")
_TYPE = re.compile(_NAME)
MAX_MESSAGES = 1024
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_CASE_BYTES = 8 * 1024 * 1024


class GrpcCardinality(StrEnum):
    UNARY_UNARY = "unary-unary"
    UNARY_STREAM = "unary-stream"
    STREAM_UNARY = "stream-unary"
    STREAM_STREAM = "stream-stream"


class GrpcStatus(StrEnum):
    OK = "OK"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    FAILED_PRECONDITION = "FAILED_PRECONDITION"
    ABORTED = "ABORTED"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    UNIMPLEMENTED = "UNIMPLEMENTED"
    INTERNAL = "INTERNAL"
    UNAVAILABLE = "UNAVAILABLE"
    DATA_LOSS = "DATA_LOSS"
    UNAUTHENTICATED = "UNAUTHENTICATED"


@dataclass(frozen=True, slots=True)
class GrpcErrorDetail:
    type_name: str
    message: bytes

    def __post_init__(self) -> None:
        if (
            not isinstance(self.type_name, str)
            or len(self.type_name) > 512
            or _TYPE.fullmatch(self.type_name) is None
        ):
            raise ValueError("gRPC detail needs a protobuf full type name")
        if type(self.message) is not bytes or len(self.message) > MAX_MESSAGE_BYTES:
            raise ValueError("gRPC detail must contain bounded immutable bytes")


def _messages(messages: tuple[bytes, ...]) -> int:
    if type(messages) is not tuple or len(messages) > MAX_MESSAGES:
        raise ValueError("gRPC messages must be a bounded immutable sequence")
    total = 0
    for message in messages:
        if type(message) is not bytes or len(message) > MAX_MESSAGE_BYTES:
            raise ValueError("gRPC messages must contain bounded immutable bytes")
        total += len(message)
    return total


@dataclass(frozen=True, slots=True)
class GrpcCallCase:
    method: str
    cardinality: GrpcCardinality
    requests: tuple[bytes, ...]
    responses: tuple[bytes, ...]
    status: GrpcStatus
    timeout_milliseconds: int
    status_message: str = ""
    error_details: tuple[GrpcErrorDetail, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.method, str)
            or len(self.method) > 1024
            or _METHOD.fullmatch(self.method) is None
        ):
            raise ValueError("gRPC method must be an exact /service/method path")
        if not isinstance(self.cardinality, GrpcCardinality):
            raise ValueError("gRPC cardinality must be typed")
        if not isinstance(self.status, GrpcStatus):
            raise ValueError("gRPC status must be canonical and typed")
        if (
            type(self.timeout_milliseconds) is not int
            or not 1 <= self.timeout_milliseconds <= 120_000
        ):
            raise ValueError("gRPC timeout must be 1..120000 integer milliseconds")
        if (
            not isinstance(self.status_message, str)
            or len(self.status_message) > 4096
            or "\x00" in self.status_message
        ):
            raise ValueError("gRPC status message must be bounded text")
        total = _messages(self.requests) + _messages(self.responses)
        if (
            type(self.error_details) is not tuple
            or len(self.error_details) > 32
            or any(type(detail) is not GrpcErrorDetail for detail in self.error_details)
        ):
            raise ValueError("gRPC error details must be a bounded typed tuple")
        total += sum(len(detail.message) for detail in self.error_details)
        if total > MAX_CASE_BYTES:
            raise ValueError("gRPC case exceeds its aggregate payload budget")
        if (
            self.cardinality
            in (GrpcCardinality.UNARY_UNARY, GrpcCardinality.UNARY_STREAM)
            and len(self.requests) != 1
        ):
            raise ValueError("unary gRPC input requires exactly one request")
        if self.cardinality in (
            GrpcCardinality.UNARY_UNARY,
            GrpcCardinality.STREAM_UNARY,
        ) and len(self.responses) != (1 if self.status is GrpcStatus.OK else 0):
            raise ValueError("unary gRPC output cardinality disagrees with status")
        if self.status is GrpcStatus.OK and self.error_details:
            raise ValueError("successful gRPC cases cannot declare error details")

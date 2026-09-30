"""Fail-closed execution boundary for reversible downstream seam migrations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from literate_ai.contracts import (
    FrameworkReleaseIdentity,
    LifecycleRequest,
    LifecycleResult,
    LifecycleSeam,
    canonical_identity,
    wire_digest,
)


class LifecycleBridgeError(RuntimeError):
    """Stable failure before a mismatched or unsafe framework seam can run."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LifecyclePortResult:
    """Native port result; the bridge derives and verifies the public contract."""

    output_bytes: bytes
    output_payload: Mapping[str, object]
    wrote: bool
    diagnostic_codes: tuple[str, ...] = ()


class LifecyclePort(Protocol):
    """One typed implementation of one general lifecycle seam."""

    seam: LifecycleSeam

    def invoke(
        self,
        request: LifecycleRequest,
        input_bytes: bytes,
        input_payload: Mapping[str, object],
    ) -> LifecyclePortResult: ...


@dataclass(frozen=True, slots=True)
class LifecycleSeamExecution:
    request: LifecycleRequest
    result: LifecycleResult
    output_bytes: bytes
    output_payload: Mapping[str, object]


class LifecycleBridge:
    """Delegate a bound request only to the exact installed framework release."""

    def __init__(
        self,
        release: FrameworkReleaseIdentity,
        ports: Mapping[LifecycleSeam, LifecyclePort],
    ) -> None:
        self.release = release
        self._ports = dict(ports)
        if any(port.seam is not seam for seam, port in self._ports.items()):
            raise ValueError("lifecycle port keys must match their declared seams")

    def execute(
        self,
        request: LifecycleRequest,
        *,
        input_bytes: bytes,
        input_payload: Mapping[str, object],
        expected_release_identity: str,
    ) -> LifecycleSeamExecution:
        actual_release = self.release.identity
        if expected_release_identity != actual_release.uri:
            raise LifecycleBridgeError(
                "migration.release-identity-mismatch",
                "the configured framework release identity does not match",
            )
        if request.framework_release_identity != actual_release:
            raise LifecycleBridgeError(
                "migration.request-release-mismatch",
                "the request targets a different framework release",
            )
        if request.input_wire_digest != wire_digest(input_bytes):
            raise LifecycleBridgeError(
                "migration.input-wire-mismatch",
                "the exact input bytes changed before lifecycle execution",
            )
        if request.input_payload_identity != canonical_identity(input_payload):
            raise LifecycleBridgeError(
                "migration.input-payload-mismatch",
                "the semantic input changed before lifecycle execution",
            )
        port = self._ports.get(request.seam)
        if port is None:
            raise LifecycleBridgeError(
                "migration.seam-unavailable",
                f"no framework lifecycle port is registered for {request.seam.value}",
            )
        native = port.invoke(request, input_bytes, input_payload)
        if request.read_only and native.wrote:
            raise LifecycleBridgeError(
                "migration.shadow-write-attempt",
                "a read-only shadow invocation reported a write",
            )
        diagnostics = tuple(sorted(set(native.diagnostic_codes)))
        result = LifecycleResult(
            request_id=request.request_id,
            seam=request.seam,
            operation=request.operation,
            output_wire_digest=wire_digest(native.output_bytes),
            output_payload_identity=canonical_identity(native.output_payload),
            framework_release_identity=actual_release,
            wrote=native.wrote,
            diagnostic_codes=diagnostics,
        )
        return LifecycleSeamExecution(
            request=request,
            result=result,
            output_bytes=native.output_bytes,
            output_payload=native.output_payload,
        )


__all__ = [
    "LifecycleBridge",
    "LifecycleBridgeError",
    "LifecycleSeamExecution",
    "LifecyclePort",
    "LifecyclePortResult",
]

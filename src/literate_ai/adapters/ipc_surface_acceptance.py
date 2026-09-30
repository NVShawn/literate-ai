"""A protocol-neutral IPC-surface conformance port and its fail-closed decision.

ADR 0029 decides a single first-class notion -- an *IPC surface*: a Component
boundary that exposes named operations to out-of-process callers under four
properties (a declared schema, a served self-description, a conformance acceptance
oracle, and a version + compatibility promise). MCP, REST, and gRPC are *adapters*
of that one notion, not three drifting skills. This module is the framework
machinery those adapters plug into; it is deliberately not shaped to any one
protocol.

Like :mod:`literate_ai.adapters.browser_acceptance` (ADR-0028), the two halves are
separable on purpose so the decision is unit-testable without a live server:

* :class:`IpcSurfaceProbe` is the port -- a small infrastructure boundary
  (ADR-0004) an adapter implements. It launches nothing and decides nothing; given
  a ``base_url`` and the loaded contract it *fetches* the served self-description
  and *drives* the declared request cases, returning a plain
  :class:`IpcSurfaceObservation` of what it saw. How the served description is
  interpreted (OpenAPI/JSON-Schema at ``/openapi.json`` for REST, server reflection
  + the ``.proto`` descriptor for gRPC, the tool/resource listing for MCP) and how
  a response is validated against the declared schema are the adapter's job, keyed
  off the contract's open ``protocol`` tag. The port reports the *result* of that
  validation as facts; it never raises the accept/reject decision.

* :func:`decide_ipc_surface_conformance` is the pure decision. Given the declared
  contract and an :class:`IpcSurfaceObservation` it either returns an evidence
  document or raises :class:`IpcSurfaceAcceptanceError`. It imports no protocol
  toolchain, so the fail-closed logic is fully testable with a hand-built
  observation and no live server: it fails closed when the served description's
  identity does not match the declared schema identity, when any request case's
  response did not validate against the declared schema, or when the served
  description was absent.

The default :class:`HttpIpcSurfaceProbe` fetches the served description and drives
request cases over plain HTTP by issuing each declared
:class:`~literate_ai.adapters.component_acceptance.ServiceHttpProbe` through an
injected ``fetch`` callable ``(base_url, probe) -> bytes | None``, so a REST surface
needs no bespoke transport and the probe stays importable and unit-testable without
a live server. A gRPC or MCP adapter that cannot be expressed as an HTTP GET
supplies its own :class:`IpcSurfaceProbe`.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from literate_ai.contracts import ContentIdentity, canonical_identity

if TYPE_CHECKING:
    from literate_ai.adapters.component_acceptance import (
        IpcSurfaceConformanceAcceptance,
    )


IPC_SURFACE_ACCEPTANCE_EVIDENCE_SCHEMA = (
    "literate-ai/ipc-surface-conformance-observation@1"
)


class IpcSurfaceAcceptanceError(RuntimeError):
    """A served IPC surface that fails the verifier-owned conformance contract, or
    an unavailable probe/toolchain. Either way acceptance fails closed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IpcRequestCaseObservation:
    """What a probe observed for one declared request case.

    ``validated`` is the adapter's verdict on whether the response validated against
    the *declared schema* (JSON-Schema for REST, the protobuf message type for gRPC,
    the tool output schema for MCP). The pure decision fails closed on any case
    whose response did not validate; the adapter, which owns the protocol toolchain,
    performs the actual validation and reports the boolean here.
    """

    case_index: int
    validated: bool
    response_identity: str
    detail: str = ""

    def to_document(self) -> dict[str, Any]:
        return {
            "case_index": self.case_index,
            "validated": self.validated,
            "response_identity": self.response_identity,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class IpcSurfaceObservation:
    """What a probe observed after fetching the description and driving the cases.

    This is the neutral hand-off between any adapter and the pure decision. A probe
    never decides pass/fail; it reports facts. ``description_present`` is ``False``
    when the surface served no self-description at all (fail closed).
    ``served_description_identity`` is the identity the adapter derived from the
    served description bytes -- for a schema-first surface this is the declared
    schema identity the served bytes advertise; the decision requires it to equal
    the contract's ``declared_schema_identity``.
    """

    description_present: bool
    served_description_identity: str
    request_cases: tuple[IpcRequestCaseObservation, ...] = ()
    served_description_bytes: bytes = b""
    protocol_detail: str = ""


class IpcSurfaceProbe(ABC):
    """The protocol-neutral port an adapter implements (ADR-0004 boundary).

    A probe fetches the surface's served self-description at ``base_url`` and drives
    the contract's declared request cases, returning an
    :class:`IpcSurfaceObservation`. It raises :class:`IpcSurfaceAcceptanceError`
    only for an unavailable probe/toolchain or an undrivable case -- the pass/fail
    decision against the contract is decided by
    :func:`decide_ipc_surface_conformance`.
    """

    @property
    @abstractmethod
    def tool_identity(self) -> ContentIdentity:
        """Identity of the driving probe/toolchain, bound into the evidence."""

    def is_ready(
        self,
        base_url: str,
        contract: IpcSurfaceConformanceAcceptance,
        *,
        timeout_seconds: float,
    ) -> bool:
        """Observe readiness without executing acceptance cases.

        Protocol adapters override this operation and honor its remaining time
        budget. False means retry; faults use IpcSurfaceAcceptanceError. Legacy
        REST adapters may omit it and retain the lifecycle's HTTP readiness.
        Other protocols require an override before the lifecycle launches them.
        """
        raise NotImplementedError("this probe supplies no protocol readiness")

    def observe_with_timeout(
        self,
        base_url: str,
        contract: IpcSurfaceConformanceAcceptance,
        *,
        timeout_seconds: float,
    ) -> IpcSurfaceObservation:
        """Observe within the lifecycle's remaining process budget.

        Native transports override this method and bound every operation by both
        its declared deadline and the remaining total budget. The default keeps
        existing observe-only implementations compatible; it cannot preempt them.
        The lifecycle checks process health and elapsed time again after return.
        """
        return self.observe(base_url, contract)

    @abstractmethod
    def observe(
        self,
        base_url: str,
        contract: IpcSurfaceConformanceAcceptance,
    ) -> IpcSurfaceObservation:
        """Fetch the served description, drive the cases, report what was seen."""


def _bytes_identity(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def decide_ipc_surface_conformance(
    contract: IpcSurfaceConformanceAcceptance,
    observation: IpcSurfaceObservation,
) -> dict[str, Any]:
    """Fail closed on a non-conforming surface; otherwise return evidence.

    This is the entire conformance decision, protocol-toolchain-free. It fails
    closed, in order, on: a surface that served no self-description at all; a served
    description whose identity does not match the contract's declared schema
    identity; and any request case whose response did not validate against the
    declared schema. A pass is evidence for exactly this surface + this declared
    schema + this probe, nothing more. It binds the protocol tag, the declared and
    served description identities, the surface version, and the per-case outcomes so
    the receipt is auditable.
    """

    if not observation.description_present:
        raise IpcSurfaceAcceptanceError(
            "ipc_surface_acceptance.description_missing",
            f"the {contract.protocol!r} surface served no self-description; a "
            "conforming surface must serve its own machine-readable description "
            "(ADR-0029 property 2)",
        )
    if observation.served_description_identity != contract.declared_schema_identity:
        raise IpcSurfaceAcceptanceError(
            "ipc_surface_acceptance.description_mismatch",
            "served description identity does not match the declared schema "
            f"identity; expected={contract.declared_schema_identity!r}; "
            f"served={observation.served_description_identity!r}",
        )
    cases = observation.request_cases
    if len(cases) != len(contract.request_cases) or any(
        type(case.case_index) is not int or case.case_index != index
        for index, case in enumerate(cases)
    ):
        raise IpcSurfaceAcceptanceError(
            "ipc_surface_acceptance.case_coverage",
            "request observations must cover every declared case exactly once "
            "in declared order",
        )
    failures = [case for case in cases if case.validated is not True]
    if failures:
        joined = "; ".join(
            f"case {item.case_index}: {item.detail or 'did not validate'}"
            for item in failures[:8]
        )
        raise IpcSurfaceAcceptanceError(
            "ipc_surface_acceptance.response_invalid",
            "one or more responses did not validate against the declared schema: "
            + joined,
        )
    return {
        "schema": IPC_SURFACE_ACCEPTANCE_EVIDENCE_SCHEMA,
        "protocol": contract.protocol,
        "declared_schema_identity": contract.declared_schema_identity,
        "served_description_identity": observation.served_description_identity,
        "surface_version": contract.surface_version,
        "compatibility": contract.compatibility.to_dict(),
        "request_cases": [item.to_document() for item in observation.request_cases],
        "outcome": "accepted",
    }


@dataclass(frozen=True, slots=True)
class HttpIpcSurfaceProbe(IpcSurfaceProbe):
    """Default HTTP probe: fetch the served description and drive cases over HTTP.

    This realizes the neutral port for any surface whose self-description is fetched
    with a plain HTTP GET and whose responses are body bytes -- the dominant
    REST/HTTP+JSON case. All transport is behind the injected ``fetch`` callable
    ``(base_url, probe) -> bytes | None`` (``None`` = nothing served), so the probe
    is importable and unit-testable without a live server: the lifecycle injects a
    real HTTP GET, a test injects a canned mapping.

    ``describe_identity`` maps the fetched description bytes to the identity the
    decision compares against the declared schema; by default the served
    description's own byte identity is used, which fits a surface that serves its
    exact declared-schema document. ``validate_response(contract, index, body)``
    returns whether a case's response validated against the declared schema; a REST
    adapter passes a JSON-Schema check, a gRPC adapter a protobuf decode. When it is
    ``None`` a nonempty response counts as validated (the verifier-declared
    expectations having already been asserted by the transport).
    """

    fetch: Callable[[str, Any], bytes | None]
    describe_identity: Callable[[bytes], str] | None = None
    validate_response: (
        Callable[[IpcSurfaceConformanceAcceptance, int, bytes], bool] | None
    ) = None

    @property
    def tool_identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/ipc-surface-probe@1",
                "transport": "http",
            }
        )

    def observe(
        self,
        base_url: str,
        contract: IpcSurfaceConformanceAcceptance,
    ) -> IpcSurfaceObservation:
        description = self._fetch(base_url, contract.description_probe)
        if description is None:
            return IpcSurfaceObservation(
                description_present=False,
                served_description_identity="",
            )
        served_identity = (
            self.describe_identity(description)
            if self.describe_identity is not None
            else _bytes_identity(description)
        )
        cases: list[IpcRequestCaseObservation] = []
        for index, probe in enumerate(contract.request_cases):
            body = self._fetch(base_url, probe)
            validated = body is not None and (
                self.validate_response is None
                or bool(self.validate_response(contract, index, body))
            )
            cases.append(
                IpcRequestCaseObservation(
                    case_index=index,
                    validated=validated,
                    response_identity=_bytes_identity(body or b""),
                    detail="" if validated else "no validating response observed",
                )
            )
        return IpcSurfaceObservation(
            description_present=True,
            served_description_identity=served_identity,
            request_cases=tuple(cases),
            served_description_bytes=description,
        )

    def _fetch(self, base_url: str, probe: Any) -> bytes | None:
        try:
            return self.fetch(base_url, probe)
        except IpcSurfaceAcceptanceError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on any probe fault
            raise IpcSurfaceAcceptanceError(
                "ipc_surface_acceptance.probe_failed",
                f"IPC-surface probe failed against {base_url}: {exc}",
            ) from exc


__all__ = [
    "IPC_SURFACE_ACCEPTANCE_EVIDENCE_SCHEMA",
    "HttpIpcSurfaceProbe",
    "IpcRequestCaseObservation",
    "IpcSurfaceAcceptanceError",
    "IpcSurfaceObservation",
    "IpcSurfaceProbe",
    "decide_ipc_surface_conformance",
]

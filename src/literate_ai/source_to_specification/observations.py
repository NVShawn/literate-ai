"""Separately authorized dynamic observation for source-to-specification runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from literate_ai.security import (
    FailClosedObservationExecutionAuthorizationVerifier,
    ObservationExecutionAuthorization,
    ObservationExecutionAuthorizationVerifier,
    ObservationRequest,
)

from .contracts import canonical_digest
from .errors import SourceToSpecificationError
from .workflow import SourceTreeFingerprint


class ObservationSandbox(Protocol):
    runner_id: str

    def run(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class DynamicObservationResult:
    request_digest: str
    authorization_id: str
    runner_id: str
    output_digest: str
    output: Mapping[str, object]

    @property
    def identity(self) -> str:
        return canonical_digest(self)


class AuthorizedDynamicObserver:
    """Execute source only through an exact, unexpired observation grant."""

    def __init__(
        self,
        runner: ObservationSandbox,
        authorization_verifier: ObservationExecutionAuthorizationVerifier | None = None,
    ) -> None:
        self.runner = runner
        self.authorization_verifier = (
            authorization_verifier
            or FailClosedObservationExecutionAuthorizationVerifier()
        )

    def observe(
        self,
        request: ObservationRequest,
        authorization: ObservationExecutionAuthorization,
        *,
        source_root: str | Path,
        now: datetime,
    ) -> DynamicObservationResult:
        if request.runner_id != self.runner.runner_id:
            raise SourceToSpecificationError(
                "observation.runner_mismatch",
                "observation authorization targets a different sandbox runner",
            )
        guard = SourceTreeFingerprint(source_root)
        try:
            if guard.digest not in request.source_digests:
                raise SourceToSpecificationError(
                    "observation.source_mismatch",
                    "observation request does not bind the actual source tree",
                )
            request_wire = asdict(request)
            authorization_wire = {
                "authorization_id": authorization.authorization_id,
                "classification_digest": authorization.classification_digest,
                "request_digest": authorization.request_digest,
                "privileges": list(authorization.privileges),
                "issued_at": authorization.issued_at.isoformat(),
                "expires_at": authorization.expires_at.isoformat(),
            }
            self.authorization_verifier.require_observation_valid(
                authorization,
                request,
                now=now,
            )
            raw = self.runner.run(request_wire, authorization_wire)
            try:
                output = json.loads(
                    json.dumps(
                        dict(raw),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise SourceToSpecificationError(
                    "observation.output_invalid",
                    "sandbox observation output must be canonical JSON",
                ) from exc
            return DynamicObservationResult(
                request_digest=authorization.request_digest,
                authorization_id=authorization.authorization_id,
                runner_id=self.runner.runner_id,
                output_digest=canonical_digest(output),
                output=output,
            )
        finally:
            guard.require_unchanged()


__all__ = [
    "AuthorizedDynamicObserver",
    "DynamicObservationResult",
    "ObservationSandbox",
]

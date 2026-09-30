"""Single-use admission for trusted launchers; never a containment observation.

Composition supplies an issuer-authenticating, live-revocation verifier and a store
outside worker authority. Successful admission permits only the already bound
request. It does not launch work, provision a runner, or qualify enforcement.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .isolation.build_binding import ProductionBuildBinding
from .policy import BuildAuthorization, BuildAuthorizationVerifier, BuildRequest


class BuildGrantConsumptionStore(Protocol):
    def consume(self, grant: BuildAuthorization, request: BuildRequest) -> None:
        """Durably spend this authorization ID or raise before launch."""
        ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SingleUseBuildAdmission:
    verifier: BuildAuthorizationVerifier
    store: BuildGrantConsumptionStore
    clock: Callable[[], datetime] = _now

    def admit_contained_build(
        self,
        grant: BuildAuthorization,
        request: BuildRequest,
        binding: ProductionBuildBinding,
    ) -> None:
        """Require the launcher's independently reviewed binding before spending."""
        binding.require_request(request)
        self.admit(grant, request)

    def admit(self, grant: BuildAuthorization, request: BuildRequest) -> None:
        """Recheck after durable consumption; never refund a spent grant."""
        now = self.clock()
        grant.require_valid(request, now=now)
        self.verifier.require_build_valid(grant, request, now=now)
        self.store.consume(grant, request)
        now = self.clock()
        grant.require_valid(request, now=now)
        self.verifier.require_build_valid(grant, request, now=now)

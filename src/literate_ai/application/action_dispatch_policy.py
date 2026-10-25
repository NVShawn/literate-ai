"""Identity-bound reservations for one action-DAG coordinator.

Capacities are admitted available budgets, not total host capacity. Callers own
fresh health admission. Exclusive claims protect only this coordinator's run;
external writers still require their owning database/signer lock.
"""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.contracts.identity import ContentIdentity, canonical_identity


@dataclass(frozen=True, slots=True)
class ActionResources:
    cpu_millis: int = 0
    memory_mib: int = 0
    disk_mib: int = 0
    network_mbps: int = 0

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value <= 2**63 - 1
            for value in self.values
        ):
            raise ValueError("action resources must be bounded nonnegative integers")

    @property
    def values(self) -> tuple[int, ...]:
        return self.cpu_millis, self.memory_mib, self.disk_mib, self.network_mbps

    def to_dict(self) -> dict[str, int]:
        return dict(
            zip(
                ("cpu_millis", "memory_mib", "disk_mib", "network_mbps"),
                self.values,
                strict=True,
            )
        )


@dataclass(frozen=True, slots=True)
class ActionDispatchPolicy:
    resources: ActionResources = ActionResources()
    exclusive_claims: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.resources, ActionResources):
            raise ValueError("action resources must be typed")
        claims = self.exclusive_claims
        if (
            not isinstance(claims, tuple)
            or any(not isinstance(x, str) or not x or len(x) > 512 for x in claims)
            or claims != tuple(sorted(set(claims)))
        ):
            raise ValueError("exclusive claims must be canonical unique names")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/action-dispatch-policy@1",
                "resources": self.resources.to_dict(),
                "exclusive_claims": list(self.exclusive_claims),
            }
        )

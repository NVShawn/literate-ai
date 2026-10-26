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
    retry_safe: bool = False
    max_attempts: int = 1
    retryable_failure_codes: tuple[str, ...] = ()
    loss_fencing_policy_identity: ContentIdentity | None = None

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

        if (
            type(self.retry_safe) is not bool
            or type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 16
        ):
            raise ValueError("retry selection and attempt bound must be typed")
        codes = self.retryable_failure_codes
        if (
            not isinstance(codes, tuple)
            or any(
                not isinstance(code, str) or not code or len(code) > 127
                for code in codes
            )
            or codes != tuple(sorted(set(codes)))
        ):
            raise ValueError("retry failure codes must be canonical and unique")
        if self.loss_fencing_policy_identity is not None and not isinstance(
            self.loss_fencing_policy_identity, ContentIdentity
        ):
            raise ValueError("worker-loss fencing policy must be a typed identity")
        if self.max_attempts > 1 and (not self.retry_safe or not codes or claims):
            raise ValueError(
                "retries require declared safe work and explicit failures "
                "without mutable side-effect claims"
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/action-dispatch-policy@2"
                if self.retry_safe
                or self.max_attempts != 1
                or self.retryable_failure_codes
                or self.loss_fencing_policy_identity is not None
                else "literate-ai/action-dispatch-policy@1",
                **(
                    {
                        "retry_safe": self.retry_safe,
                        "max_attempts": self.max_attempts,
                        "retryable_failure_codes": list(self.retryable_failure_codes),
                        "loss_fencing_policy_identity": None
                        if self.loss_fencing_policy_identity is None
                        else self.loss_fencing_policy_identity.uri,
                    }
                    if self.retry_safe
                    or self.max_attempts != 1
                    or self.retryable_failure_codes
                    or self.loss_fencing_policy_identity is not None
                    else {}
                ),
                "resources": self.resources.to_dict(),
                "exclusive_claims": list(self.exclusive_claims),
            }
        )

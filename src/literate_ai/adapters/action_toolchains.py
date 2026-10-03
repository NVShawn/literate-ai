"""Worker-owned toolchain bindings selected by exact portable identities."""

from __future__ import annotations

from types import MappingProxyType

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

MAX_WORKER_TOOLCHAINS = 256


class WorkerToolchainRegistry:
    """Private startup bindings; inbound requests may select identities only."""

    def __init__(self, bindings: tuple[LocalComponentToolBinding, ...]):
        if not isinstance(bindings, tuple) or len(bindings) > MAX_WORKER_TOOLCHAINS:
            raise ActionWireError(
                "action_tools.invalid", "invalid worker toolchain inventory"
            )
        observed = {}
        for binding in bindings:
            if not isinstance(binding, LocalComponentToolBinding):
                raise ActionWireError(
                    "action_tools.invalid", "worker tools must be locally bound"
                )
            if (
                binding.authority_identity is not None
                and binding._authority_guard is None
            ):
                raise ActionWireError(
                    "action_tools.unobserved",
                    "toolchain authority requires a live observation guard",
                )
            identity = self._identity(binding)
            if identity in observed:
                raise ActionWireError(
                    "action_tools.duplicate", "duplicate worker toolchain identity"
                )
            observed[identity] = binding
        self._bindings = MappingProxyType(observed)
        self._identities = tuple(sorted(observed, key=lambda item: item.uri))

    @staticmethod
    def _identity(binding):
        try:
            return binding.toolchain_identity
        except (OSError, ValueError, RuntimeError) as exc:
            raise ActionWireError(
                "action_tools.changed",
                "worker toolchain observation is no longer current",
            ) from exc

    @property
    def identities(self) -> tuple[ContentIdentity, ...]:
        self.select(self._identities)
        return self._identities

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/worker-toolchain-inventory@1",
                "toolchain_identities": [item.uri for item in self.identities],
            }
        )

    def select(
        self, identities: tuple[ContentIdentity, ...]
    ) -> tuple[LocalComponentToolBinding, ...]:
        if (
            not isinstance(identities, tuple)
            or len(identities) > MAX_WORKER_TOOLCHAINS
            or any(not isinstance(item, ContentIdentity) for item in identities)
        ):
            raise ActionWireError(
                "action_tools.invalid",
                "toolchain selection requires bounded identities",
            )
        if identities != tuple(sorted(set(identities), key=lambda item: item.uri)):
            raise ActionWireError(
                "action_tools.invalid",
                "toolchain selection must be unique and canonical",
            )
        if any(identity not in self._bindings for identity in identities):
            raise ActionWireError(
                "action_tools.unavailable", "worker lacks an exact requested toolchain"
            )
        selected = tuple(self._bindings[identity] for identity in identities)
        for identity, binding in zip(identities, selected, strict=True):
            if self._identity(binding) != identity:
                raise ActionWireError(
                    "action_tools.changed", "worker toolchain identity changed"
                )
        return selected

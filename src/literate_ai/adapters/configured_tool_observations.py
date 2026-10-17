"""Startup-owned Standard observations bound to the actual BUILD tool registry."""

import sys
from types import MappingProxyType

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.standard_toolchain_observations import (
    capture_standard_tool_observations,
)
from literate_ai.adapters.worker_tool_selectors import verify_worker_tool_selector


class ConfiguredToolObservations:
    def __init__(self, registry, tools):
        if not isinstance(registry, WorkerToolchainRegistry):
            raise TypeError("Standard observations require a worker tool registry")
        self.registry = registry
        self.tools = MappingProxyType(dict(tools))
        self.platform = {"darwin": "macos", "win32": "windows", "linux": "linux"}.get(
            sys.platform
        )
        self._initial = self._capture(lambda: None)

    def _capture(self, require_current):
        def guard():
            require_current()
            _ = self.registry.identities

        value = capture_standard_tool_observations(
            self.platform, self.tools, require_current=guard
        )
        identities = tuple(
            sorted(
                {item.toolchain_identity for item in value.tools},
                key=lambda item: item.uri,
            )
        )
        if identities != self.registry.identities:
            raise ActionWireError(
                "action_tools.observation_mismatch",
                "observations differ from BUILD tool inventory",
            )
        bindings = {
            item.toolchain_identity: item for item in self.registry.select(identities)
        }
        for tool in value.tools:
            binding = bindings[tool.toolchain_identity]
            if tool.command != binding.command or tool.environment != tuple(
                sorted(binding.environment)
            ):
                raise ActionWireError(
                    "action_tools.observation_mismatch",
                    "observed tool differs from registered command",
                )
        guard()
        return value

    @property
    def initial(self):
        """The observation measured at startup and compared by every capture."""
        return self._initial

    def capture(self, *, require_current):
        value = self._capture(require_current)
        if value != self._initial:
            raise ActionWireError(
                "action_tools.observation_changed",
                "Standard tool observations changed after startup",
            )
        return value

    def verify_selector(self, role, selector, *, environment, require_current):
        observed = self.capture(require_current=require_current)
        if not isinstance(role, str) or role not in self.tools:
            raise ActionWireError(
                "action_tools.selector_mismatch",
                "selected worker tool role is unavailable",
            )
        verify_worker_tool_selector(
            self.tools[role],
            selector,
            environment=environment,
            require_current=require_current,
        )
        if self.capture(require_current=require_current) != observed:
            raise ActionWireError(
                "action_tools.observation_changed",
                "tool inventory changed during selector verification",
            )
        return next(tool for tool in observed.tools if tool.role == role)

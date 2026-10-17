"""Startup-owned tool profiles for configured lifecycle receivers."""

from types import MappingProxyType

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.configured_tool_observations import ConfiguredToolObservations
from literate_ai.adapters.worker_tool_dependencies import (
    capture_worker_tool_dependencies,
)
from literate_ai.contracts import ContentIdentity, canonical_identity


class ConfiguredToolWorker:
    """Startup-only observed commands; requests cannot configure paths or imports."""

    def __init__(
        self, launcher, tool_bindings, *, phase, environment, standard_tools=None
    ):
        if phase not in {"GENERATE", "BUILD", "TEST", "EXECUTE", "ACCEPT"}:
            raise ValueError("unsupported configured worker phase")
        self._phase = phase
        self.launcher = launcher
        self.launchers = WorkerToolchainRegistry((launcher,))
        self.tools = WorkerToolchainRegistry(tool_bindings)
        private_environment = dict(environment)
        if (phase not in {"GENERATE", "ACCEPT"} and not self.tools.identities) or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "\0" in key
            or "\0" in value
            for key, value in private_environment.items()
        ):
            raise ValueError(
                f"{phase} startup requires observed tools and a private environment"
            )
        # Observation modes and phase execution use different wire protocols.
        # Their receiver-owned marker must not alter the private phase runtime
        # or leak into its child. Compare case-insensitively for Windows parity.
        self.environment = MappingProxyType(
            {
                key: value
                for key, value in private_environment.items()
                if key.casefold() != "litai_dispatch_protocol"
            }
        )
        self._standard_observations = (
            None
            if standard_tools is None
            else ConfiguredToolObservations(self.tools, standard_tools)
        )

    def observe_tools(self, *, require_current):
        if self._standard_observations is None:
            raise ActionWireError(
                "action_tools.not_configured",
                "Standard tool observations are not configured",
            )
        return self._standard_observations.capture(require_current=require_current)

    def verify_tool_selector(self, role, selector, *, require_current):
        if self._standard_observations is None:
            raise ActionWireError(
                "action_tools.not_configured",
                "Standard tool observations are not configured",
            )
        profile = self.identity

        def guard():
            require_current()
            if self.identity != profile:
                raise ActionWireError(
                    "action_tools.observation_changed",
                    "worker profile changed during selector verification",
                )

        return self._standard_observations.verify_selector(
            role, selector, environment=self.environment, require_current=guard
        )

    def observe_tool_dependencies(
        self, identities, *, root_ref, require_current, expected_identity=None
    ):
        if expected_identity is not None and not isinstance(
            expected_identity, ContentIdentity
        ):
            raise TypeError("expected dependency identity must be typed")
        profile = self.identity

        def guard():
            require_current()
            if self.identity != profile:
                raise ActionWireError(
                    "action_tools.observation_changed",
                    "worker profile changed during dependency observation",
                )

        result = capture_worker_tool_dependencies(
            self.tools,
            identities,
            environment=self.environment,
            root_ref=root_ref,
            require_current=guard,
        )
        if expected_identity is not None and result.identity != expected_identity:
            raise ActionWireError(
                "action_tools.dependencies_changed",
                "worker native dependencies changed since observation",
            )
        return result

    @property
    def standard_tools_identity(self):
        return (
            None
            if self._standard_observations is None
            else self.observe_tools(require_current=lambda: None).identity
        )

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": f"literate-ai/configured-{self._phase.lower()}-worker@1",
                "launcher": self.launchers.identity.uri,
                "tools": self.tools.identity.uri,
                "environment": dict(self.environment),
                # The startup observation, not a re-capture: this profile is read
                # by per-read guards. Tool bytes stay live-checked through
                # self.tools above, and observe_tools() re-captures at boundaries.
                **(
                    {
                        "standard_tools": (
                            self._standard_observations.initial.identity.uri
                        )
                    }
                    if self._standard_observations is not None
                    else {}
                ),
            }
        )

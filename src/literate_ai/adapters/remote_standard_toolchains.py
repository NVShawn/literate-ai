"""Data-only Standard discovery from an admitted worker's guarded observations."""

from dataclasses import dataclass
from types import MappingProxyType

from literate_ai.adapters.action_admission import CommandActionWorkerPool
from literate_ai.adapters.action_capabilities import probe_command_action_capabilities
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_tool_dependencies import (
    probe_command_tool_dependencies,
)
from literate_ai.adapters.action_tool_observation import (
    WorkerToolObservation,
    probe_command_tool_observations,
)
from literate_ai.adapters.action_tool_selectors import probe_command_tool_selectors
from literate_ai.adapters.builders.zig import DEFAULT_ZIG_MINIMUM_VERSION
from literate_ai.contracts import (
    ContentIdentity,
    ToolchainConstraint,
    canonical_identity,
)

_DEFAULT_MINIMUMS = {
    "python": (3, 11),
    "node": (20,),
    "make": (3, 81),
    "cargo": (1, 70),
    "cmake": (3, 20),
    "zig": DEFAULT_ZIG_MINIMUM_VERSION,
    "zig-cc": DEFAULT_ZIG_MINIMUM_VERSION,
}


def _refuse(message):
    raise ActionWireError("action_tools.constraint_mismatch", message)


@dataclass(frozen=True)
class _ObservedTool:
    owner: object
    observation: object

    @property
    def command(self):
        return self.observation.command

    @property
    def identity(self):
        return self.observation.toolchain_identity.uri

    @property
    def environment(self):
        return self.observation.environment

    @property
    def version(self):
        return self.observation.version

    @property
    def version_info(self):
        return self.observation.version_info

    @property
    def node(self):
        return self.owner._tools["node"] if self.observation.role == "npm" else None

    def require_unchanged(self):
        self.owner.require_unchanged()


class RemoteStandardToolchains:
    def __init__(self, observation, *, require_current, verify_selectors=None):
        if not isinstance(observation, WorkerToolObservation) or not callable(
            require_current
        ):
            raise TypeError(
                "remote discovery requires admitted observations and a live guard"
            )
        if (
            observation.tools.identity != observation.capability.build_standard_tools
            or tuple(
                sorted(
                    {tool.toolchain_identity for tool in observation.tools.tools},
                    key=lambda item: item.uri,
                )
            )
            != observation.capability.build_toolchains
        ):
            raise ValueError("remote tool inventory differs from admitted capability")
        self._observation = observation
        self._guard = require_current
        if verify_selectors is not None and not callable(verify_selectors):
            raise TypeError("selector verifier must be callable")
        self._verify_selectors = verify_selectors
        self._selectors = ()
        self._selector_identity = None
        self._tools = MappingProxyType(
            {item.role: _ObservedTool(self, item) for item in observation.tools.tools}
        )
        self.require_unchanged()

    @classmethod
    def from_admission(cls, admission, worker):
        if not isinstance(admission, CommandActionWorkerPool):
            raise TypeError("remote discovery requires a live command worker pool")
        admission.revalidate(worker)
        selected = admission.catalog.worker(worker.worker_id)
        capability = next(
            item
            for item in admission.capabilities
            if item.worker_identity == selected.identity
        )
        observed = probe_command_tool_observations(
            selected,
            capability,
            admission.deadline,
            cwd=admission.cwd,
            environment=admission.environment,
        )

        def verify(selectors):
            admission.revalidate(worker)
            current = probe_command_action_capabilities(
                selected,
                admission.deadline,
                cwd=admission.cwd,
                environment=admission.environment,
            )
            if current.capability_identity != observed.capability.capability_identity:
                _refuse("selector worker differs from admitted tool observation")
            result = probe_command_tool_selectors(
                selected,
                WorkerToolObservation(current, observed.tools),
                admission.deadline,
                selectors,
                cwd=admission.cwd,
                environment=admission.environment,
            )
            admission.revalidate(worker)
            return result

        return cls(
            observed,
            require_current=lambda: admission.revalidate(worker),
            verify_selectors=verify,
        )

    @property
    def observation(self):
        return self._observation

    @property
    def observer_identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/remote-standard-tool-observer@1",
                "capability": self.observation.capability.capability_identity.uri,
                "inventory": self.observation.tools.identity.uri,
            }
        )

    @property
    def platform(self):
        return self.observation.tools.platform

    def require_unchanged(self):
        self._guard()
        if self._selectors:
            if self._verify_selectors(dict(self._selectors)) != self._selector_identity:
                _refuse("worker selector verification changed")
            self._guard()

    def discover(self, role, constraint, _environment):
        self.require_unchanged()
        if not isinstance(role, str) or role not in self._tools:
            _refuse("admitted worker lacks the selected Standard tool role")
        if constraint is not None and (
            not isinstance(constraint, ToolchainConstraint)
            or constraint.toolchain != role
        ):
            _refuse("tool constraint names a different role")
        tool = self._tools[role]
        if (
            constraint is not None
            and constraint.command is not None
            and constraint.command != tool.command
        ):
            if self._verify_selectors is None:
                _refuse("authored command requires worker-side resolution")
            selectors = dict(self._selectors)
            if role in selectors and selectors[role] != constraint.command:
                _refuse("a retained role selector cannot be replaced")
            selectors[role] = constraint.command
            verified = self._verify_selectors(selectors)
            if not isinstance(verified, ContentIdentity):
                _refuse("worker selector verifier returned no authority")
            self._selectors = tuple(sorted(selectors.items()))
            self._selector_identity = verified
        if role == "npm":
            if constraint is None:
                _refuse("npm requires its locked version constraint")
            try:
                constraint.version_range()
            except ValueError as exc:
                raise ActionWireError(
                    "action_tools.constraint_mismatch",
                    "npm requires a bounded version range",
                ) from exc
        minimum = None if constraint is None else constraint.minimum_version
        minimum = minimum or _DEFAULT_MINIMUMS.get(role)
        required = None if constraint is None else constraint.required_version
        maximum = None if constraint is None else constraint.maximum_exclusive_version
        if any(bound is not None for bound in (minimum, required, maximum)):
            version = tool.version_info
            if version is None:
                _refuse(
                    "worker has no structured version observation for the constraint"
                )
            if (
                (
                    minimum is not None
                    and version < (*minimum, *(0,) * (3 - len(minimum)))
                )
                or (required is not None and version[: len(required)] != required)
                or (
                    maximum is not None
                    and version >= (*maximum, *(0,) * (3 - len(maximum)))
                )
            ):
                _refuse(
                    "observed worker tool version does not satisfy "
                    "the locked constraint"
                )
        self.require_unchanged()
        return tool


def project_remote_standard_toolchain_closure(
    snapshot, execution_plan, admission, worker, *, native_sdk_inputs=None
):
    """Project one locked remote closure with current dependency custody."""
    from literate_ai.adapters.standard_project import (
        project_locked_standard_toolchain_closure,
    )

    consumer = RemoteStandardToolchains.from_admission(admission, worker)
    selected = {}
    retained = []
    root_ref = f"urn:literate-ai:component:{execution_plan.root_revision.digest}"

    def discover(role, constraint, environment):
        tool = consumer.discover(role, constraint, environment)
        selected[role] = tool
        return tool

    def capture(expected_identity=None):
        consumer.require_unchanged()
        target = admission.catalog.worker(worker.worker_id)
        capability = probe_command_action_capabilities(
            target,
            admission.deadline,
            cwd=admission.cwd,
            environment=admission.environment,
        )
        if (
            capability.capability_identity
            != consumer.observation.capability.capability_identity
        ):
            raise ActionWireError(
                "action_tools.observation_changed",
                "dependency worker no longer matches tool observation",
            )
        identities = tuple(
            sorted(
                {
                    ContentIdentity.parse_uri(tool.identity)
                    for tool in selected.values()
                },
                key=lambda item: item.uri,
            )
        )
        value = probe_command_tool_dependencies(
            target,
            capability,
            admission.deadline,
            identities,
            root_ref=root_ref,
            cwd=admission.cwd,
            environment=admission.environment,
            expected_identity=expected_identity,
        )
        consumer.require_unchanged()
        return value.dependencies

    def observe(commands):
        if commands != tuple(tool.command for tool in selected.values()) or retained:
            raise ValueError(
                "dependency observation differs from locked tool selection"
            )
        value = capture()
        retained.append(value)
        return value.observation

    def guard():
        if len(retained) != 1:
            raise ValueError("remote dependency observation is absent")
        if capture(retained[0].identity) != retained[0]:
            raise ValueError("remote dependency graph changed")

    return project_locked_standard_toolchain_closure(
        snapshot,
        execution_plan,
        environment={},
        host_platform=consumer.platform,
        toolchain_discoverer=discover,
        dependency_observer=observe,
        observer_identity=consumer.observer_identity,
        dependency_guard=guard,
        command_phases=(),
        native_sdk_inputs=native_sdk_inputs,
    )

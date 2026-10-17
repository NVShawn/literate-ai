"""Portable Standard ports for reference-receiver phase children.

Contracts arrive in the controller's admitted request. Before any command runs,
the `portable-starter@1` policy requires each contract to be exactly what the
packaged Make and Python profiles produce for this worker's own private tools:
the request may not name a tool path, driver, layout or command the worker did
not choose. Provider edges, native SDKs and multi-Component plans are refused.
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.builders.make import discover_make_toolchain
from literate_ai.adapters.builders.python import discover_python_toolchain
from literate_ai.adapters.dependencies import PortableHostDependencyObserver
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_PYTHON_RUNTIME_DRIVER,
)
from literate_ai.adapters.standard_project import (
    _STANDARD_MAKE_BUILD_DRIVER,
    LocalObservedToolchainAuthority,
    assemble_standard_lifecycle_ports,
    project_standard_toolchain_closure,
)
from literate_ai.contracts import (
    LITAI_SMOKE_MODE_FLAG,
    LITAI_TEST_MODE_FLAG,
    ComponentCommandPhase,
    canonical_identity,
)

_SOURCE_ENTRYPOINT = "source/main.py"


def _file_metadata(path, *, follow):
    status = os.stat(path, follow_symlinks=follow)
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


class ReceiverTools:
    """The worker's private Python and Make, fully measured at each boundary.

    Discovery at process startup is a full measurement, and `measure()` repeats
    it before a receiver returns any result. Reads in between (binding and
    authority guards) compare executable metadata only; any difference forces
    a full re-measurement, which refuses a tool whose observation changed.
    """

    def __init__(self, config, *, launcher=False):
        environment = dict(config.child_environment)
        self.python = discover_python_toolchain(
            environment, pinned_command=config.tools["python"]
        )
        self.make = discover_make_toolchain(
            environment, pinned_command=config.tools["make"]
        )
        # Phase children run on this receiver's own interpreter.
        self.launcher = (
            discover_python_toolchain(pinned_command=(sys.executable,))
            if launcher
            else None
        )
        self._toolchains = tuple(
            tool for tool in (self.python, self.make, self.launcher) if tool
        )
        self._metadata = self._current_metadata()
        self.python_binding = LocalComponentToolBinding.from_observed_toolchain(
            self.python, guard=self.check
        )
        self.make_binding = LocalComponentToolBinding.from_observed_toolchain(
            self.make, guard=self.check
        )

    @property
    def bindings(self):
        return (self.python_binding, self.make_binding)

    def authorities(self):
        return tuple(
            LocalObservedToolchainAuthority.from_observed_toolchain(
                tool, guard=self.check
            )
            for tool in (self.python, self.make)
        )

    def _current_metadata(self):
        paths = []
        for tool in self._toolchains:
            paths += [tool.command[0], tool.launcher_executable]
            paths.append(getattr(tool, "runtime_executable", None))
        # The resolved path covers every link in the chain; lstat covers the
        # named link itself and stat the executable bytes it selects.
        return tuple(
            (
                path,
                os.path.realpath(path, strict=True),
                _file_metadata(path, follow=False),
                _file_metadata(path, follow=True),
            )
            for path in paths
            if path is not None
        )

    def check(self):
        """Cheap per-read guard between full boundary measurements."""
        try:
            current = self._current_metadata()
        except OSError:
            current = None
        if current != self._metadata:
            self.measure()

    def measure(self):
        """Full boundary measurement: launcher bytes and live tool probes."""
        before = self._current_metadata()
        for tool in self._toolchains:
            tool.require_unchanged()
        if self._current_metadata() != before:
            raise ActionWireError(
                "standard_receiver.tools_changed",
                "receiver tools changed during measurement",
            )
        self._metadata = before

    def require_unchanged(self):
        self.measure()


def _refuse(reason):
    raise ActionWireError("standard_receiver.contract_refused", reason)


def _expected_commands(tools):
    python = tools.python
    if len(python.command) != 1:
        _refuse("the Make lifecycle requires one exact Python executable")
    build = (
        "{tool}",
        "-c",
        _STANDARD_MAKE_BUILD_DRIVER,
        json.dumps(list(tools.make.command), separators=(",", ":")),
        python.command[0],
        json.dumps(list(getattr(python, "environment", ())), separators=(",", ":")),
        "{source_root}",
        "{object_root}",
        "{artifact_root}",
        "{export_path}",
        "source/Makefile",
        "all",
    )

    def runtime(mode):
        return {
            (
                "{tool}",
                "-c",
                STANDARD_PYTHON_RUNTIME_DRIVER,
                "{artifact_root}",
                "{export_path}",
                layout,
                _SOURCE_ENTRYPOINT,
                mode,
            )
            for layout in ("file", "tree")
        }

    return {
        ComponentCommandPhase.BUILD: {build},
        ComponentCommandPhase.TEST: runtime(LITAI_TEST_MODE_FLAG),
        ComponentCommandPhase.EXECUTE: runtime(LITAI_SMOKE_MODE_FLAG),
    }


def require_portable_starter_build(build, tools):
    """Admit only a single-Component portable Make/Python contract for our tools."""
    contract = build.inputs.contract
    execution = build.execution_plan
    root = (execution.root_revision,)
    if (
        len(execution.generation_plans) != 1
        or any(
            plan.component_revisions != root or plan.dependency_edges
            for plan in execution.action_plans
        )
        or build.accepted_providers
        or build.provider_builds
    ):
        _refuse("only single-Component plans without providers are supported")
    if (
        contract.entrypoint_contracts is not None
        or contract.library_import_surface is not None
        or contract.native_layout is not None
    ):
        _refuse("only single-entrypoint portable applications are supported")
    python = tools.python_binding.toolchain_identity
    make = tools.make_binding.toolchain_identity
    if (
        contract.build_system_toolchain_identity != make
        or contract.language_compiler_identity != python
        or contract.language_runtime_identity != python
        or any(item.toolchain_identity != python for item in contract.tool_bindings)
    ):
        _refuse("contract toolchains differ from this worker's private tools")
    expected = _expected_commands(tools)
    commands = {command.phase: command.argv for command in contract.commands}
    if len(commands) != len(contract.commands) or set(commands) != set(expected):
        _refuse("contract phases differ from the portable starter profile")
    if any(argv not in expected[phase] for phase, argv in commands.items()):
        _refuse("contract commands differ from the packaged Make/Python profiles")
    return contract


def _observation(build, tools):
    with tempfile.TemporaryDirectory(prefix="litai-receiver-observation-") as root:
        return PortableHostDependencyObserver(
            toolchain_commands=tuple(
                tool.command for tool in (tools.python, tools.make)
            ),
            lifecycle_commands=(),
        ).observe(
            {"artifact_path": root},
            root_ref=(
                f"urn:literate-ai:component:{build.execution_plan.root_revision.digest}"
            ),
        )


def portable_runtime_factory(tools, phase):
    """Return the child's runtime factory for BUILD, TEST or EXECUTE."""
    if phase not in (
        ComponentCommandPhase.BUILD,
        ComponentCommandPhase.TEST,
        ComponentCommandPhase.EXECUTE,
    ):
        raise ValueError("portable command runtime supports BUILD, TEST and EXECUTE")

    @contextmanager
    def runtime(build, registry, recorder):
        contract = require_portable_starter_build(build, tools)
        tools.require_unchanged()
        closure = project_standard_toolchain_closure(
            build.execution_plan,
            contracts=(contract,),
            tool_bindings=(tools.python_binding,),
            dependency_observation=_observation(build, tools),
            observer_identity=canonical_identity(
                {
                    "schema": "literate-ai/standard-toolchain-observer@1",
                    "adapter": "standard-receiver@1",
                }
            ),
            toolchain_authorities=tools.authorities(),
            command_phases=(phase,),
        )
        composition = assemble_standard_lifecycle_ports(
            source_trees=registry,
            object_root=Path("objects").resolve(),
            toolchain_closure=closure,
            command_phases=(phase,),
        )
        composition.ports.retain_evidence_with(recorder)
        yield composition.ports

    return runtime


def accept_runtime_factory(tools):
    """ACCEPT reopens evidence only and runs no application tool."""

    @contextmanager
    def runtime(build, registry, recorder):
        contract = require_portable_starter_build(build, tools)
        ports = LocalStandardLifecyclePorts(
            source_trees=registry,
            object_root=Path("objects").resolve(),
            contracts=(contract,),
            tool_bindings=(),
            command_phases=(),
        )
        ports.retain_evidence_with(recorder)
        yield ports

    return runtime

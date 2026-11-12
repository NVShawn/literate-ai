"""Supported v2 consumer execution with live source/package/input custody."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.cargo_workspace_patches_v2 import (
    verify_cargo_workspace_patches_v2,
)
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_cargo_current import _unique_object
from literate_ai.adapters.retained_cargo_execution import (
    RetainedCargoCommandObservation,
    RetainedCargoExecutionError,
)
from literate_ai.adapters.retained_cargo_execution_inputs_v2 import (
    RetainedCargoExecutionInputsV2,
)
from literate_ai.adapters.retained_cargo_materialization_v2 import (
    RetainedCargoMaterializationV2,
)
from literate_ai.adapters.retained_cargo_test_execution_v2 import (
    observe_retained_cargo_tests_v2,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.retained_cargo_test_protocol import (
    RetainedCargoCustomProtocol,
    RetainedCargoExcludedProtocol,
)


@dataclass(frozen=True, slots=True)
class RetainedCargoExecutionV2:
    observations: tuple[RetainedCargoCommandObservation, ...]
    target_outcomes: tuple
    consumer_inputs_identity: object


def _require_distinct_extra_guard(tool, extra_guard):
    """Share an identical current tool callback, never a cached guard result."""
    if not (
        type(tool) is LocalComponentToolBinding and extra_guard is tool._authority_guard
    ):
        extra_guard()


def execute_retained_cargo_consumer_v2(
    materialized: RetainedCargoMaterializationV2,
    *,
    cargo: LocalComponentToolBinding,
    rustc: LocalComponentToolBinding,
    gate_tools: Mapping[str, LocalComponentToolBinding],
    environment: Mapping[str, str],
    consumer_inputs: RetainedCargoExecutionInputsV2,
    allow_host_execution: bool,
    read_exclusion_permission=None,
    registry_sources: Mapping[str, str] | None = None,
    offline: bool = True,
    timeout_seconds: float = 900,
    max_output_bytes: int = 4 * 1024 * 1024,
    max_total_output_bytes: int = 64 * 1024 * 1024,
) -> RetainedCargoExecutionV2:
    """Bracket complete reviewed gates and typed target coverage with real metadata.

    No retirement or source mutation occurs here. Authorization is explicit, all
    commands share one finite deadline/output budget, and a successful process is
    not a durable admission. The caller owns independently measured tools and
    current registry-source/permission authority; candidate documents cannot
    supply those permissions. This is same-user execution, not OS containment.
    """
    if allow_host_execution is not True:
        raise ValueError("retained.execution.v2-authorization-required")
    if (
        type(materialized) is not RetainedCargoMaterializationV2
        or type(consumer_inputs) is not RetainedCargoExecutionInputsV2
        or consumer_inputs.materialized is not materialized
    ):
        raise TypeError("retained.execution.v2-live-inputs-required")
    if (
        type(offline) is not bool
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or any(
            type(x) is not int or x < 2
            for x in (max_output_bytes, max_total_output_bytes)
        )
    ):
        raise ValueError("retained.execution.v2-bounds-invalid")
    materialized.require_unchanged()
    reviewed = materialized.files.reviewed
    plan, policy = reviewed.plan, reviewed.policy
    plan.require_gate_policy(policy)
    root, workspace = reviewed.project.root, materialized.scratch.context.workspace
    output = workspace / plan.graph.common.output_directory
    if not any(
        x.kind == "cargo-output" and workspace / x.path == output
        for x in plan.scratch_roots
    ):
        raise ValueError("retained.execution.v2-owned-output-required")
    if (
        consumer_inputs.scope.policy != policy
        or consumer_inputs.scope._scratch is not materialized.scratch
    ):
        raise ValueError("retained.execution.v2-input-policy-mismatch")
    base_environment = dict(environment)
    baseline = consumer_inputs.current_identity(environment=base_environment)
    registry = {} if registry_sources is None else dict(registry_sources)
    registry_identity = canonical_identity(registry)

    def measured(tool):
        if (
            not isinstance(tool, LocalComponentToolBinding)
            or tool.authority_identity is None
            or not callable(tool._authority_guard)
        ):
            raise ValueError("retained.execution.v2-measured-tool-required")
        tool.require_unchanged()

    measured(cargo)
    measured(rustc)
    if (
        cargo.toolchain_identity != plan.common.cargo_identity
        or rustc.toolchain_identity != plan.common.rustc_identity
        or rustc.arguments
    ):
        raise ValueError("retained.execution.v2-native-tool-mismatch")
    tools = dict(gate_tools)
    needed = {command.argv[0] for command in plan.common.gates}
    needed.update(
        target.protocol.command.argv[0]
        for target in reviewed.inventory.targets
        if isinstance(target.protocol, RetainedCargoCustomProtocol)
        and target.protocol.command.argv[0] != "{target_executable}"
    )
    if set(tools) != needed:
        raise ValueError("retained.execution.v2-gate-tools-incomplete")
    for tool in tools.values():
        measured(tool)
        if tool.toolchain_identity not in policy.toolchains:
            raise ValueError("retained.execution.v2-gate-tool-unreviewed")
    if offline and any(command.network for command in plan.common.gates):
        raise ValueError("retained.execution.v2-network-gate-refused")
    roots = dict(consumer_inputs.scope.roots)

    def reserved(values, cwd):
        if values.get("CARGO_HOME") != str(roots["cargo-home"]):
            raise ValueError("retained.execution.v2-cargo-home-override")
        for name in policy.external_input_variables:
            if values.get(name) != str(roots["external:" + name]):
                raise ValueError("retained.execution.v2-external-input-override")
        selected = values.get("CARGO_TARGET_DIR", str(output))
        path = Path(selected)
        if (path if path.is_absolute() else cwd / path) != output:
            raise ValueError("retained.execution.v2-output-override")
        if values.get("RUSTC", rustc.executable) != rustc.executable:
            raise ValueError("retained.execution.v2-compiler-override")

    reserved(base_environment, workspace)
    deadline = time.monotonic() + timeout_seconds
    observations, remaining = [], max_total_output_bytes
    exclusion_permissions = tuple(
        target.protocol.permission_identity
        for target in reviewed.inventory.targets
        if isinstance(target.protocol, RetainedCargoExcludedProtocol)
    )

    def permissions():
        if exclusion_permissions and not callable(read_exclusion_permission):
            raise ValueError("retained.execution.v2-exclusion-authority-required")
        for identity in exclusion_permissions:
            if read_exclusion_permission(identity) != identity:
                raise ValueError("retained.execution.v2-exclusion-authority-changed")

    def guard():
        for tool in (cargo, rustc, *tools.values()):
            tool.require_unchanged()
        # This complete input check already brackets its capture with live
        # materialized source/package/scratch checks. Do not repeat that scan
        # before the independently measured tool observations above.
        if consumer_inputs.current_identity(environment=base_environment) != baseline:
            raise ValueError("retained.execution.v2-consumer-inputs-changed")
        if (
            registry_sources is not None
            and canonical_identity(dict(registry_sources)) != registry_identity
        ):
            raise ValueError("retained.execution.v2-registry-selection-changed")
        permissions()
        if time.monotonic() >= deadline:
            raise ValueError("retained.execution.v2-deadline")

    def run(
        command,
        tool,
        *,
        extra_guard=lambda: None,
        timeout_seconds=None,
        max_output_bytes=None,
    ):
        nonlocal remaining
        guard()
        _require_distinct_extra_guard(tool, extra_guard)
        # A derived native tool's own guard is the exact binary/runtime/source
        # guard. Run it once at this boundary; distinct extra guards still run.
        measured(tool)
        cwd = root / command.working_directory
        require_safe_directory(cwd)
        values = {
            **base_environment,
            **dict(tool.environment),
            **{entry.name: entry.value for entry in command.environment},
        }
        reserved(values, cwd)
        require_safe_directory(output)
        values.update(CARGO_TARGET_DIR=str(output), RUSTC=rustc.executable)
        cap = min(output_limit, remaining // 2)
        if max_output_bytes is not None:
            cap = min(cap, max_output_bytes)
        duration = deadline - time.monotonic()
        if timeout_seconds is not None:
            duration = min(duration, timeout_seconds)
        if cap < 1 or duration <= 0:
            raise ValueError("retained.execution.v2-budget-exhausted")
        argv = (*tool.command, *command.argv[1:])
        try:
            result = run_bounded_process(
                argv,
                cwd=cwd,
                environment=values,
                timeout_seconds=duration,
                stdout_limit_bytes=cap,
                stderr_limit_bytes=cap,
                error_prefix="retained.execution.v2",
            )
            observations.append(
                RetainedCargoCommandObservation(
                    command,
                    argv,
                    canonical_identity(values),
                    result,
                    tool.authority_identity,
                )
            )
            remaining -= len(result.stdout) + len(result.stderr)
            if (
                max_output_bytes is not None
                and len(result.stdout) + len(result.stderr) > max_output_bytes
            ):
                raise ValueError("retained.execution.v2-custom-output-exhausted")
        finally:
            guard()
            _require_distinct_extra_guard(tool, extra_guard)
            tool.require_unchanged()
            require_safe_directory(output)
        if result.returncode != 0:
            raise ValueError("retained.execution.v2-command-failed")
        return result

    output_limit = max_output_bytes

    def metadata():
        result = run(plan.metadata_command(offline=offline), cargo)
        document = json.loads(result.stdout, object_pairs_hook=_unique_object)
        verify_cargo_workspace_patches_v2(
            document,
            workspace_root=workspace,
            plan=plan,
            workspace_manifest=(workspace / "Cargo.toml").read_bytes(),
            lockfile=(workspace / "Cargo.lock").read_bytes(),
            registry_sources=registry,
        )
        consumer_inputs.require_metadata_paths(document, environment=base_environment)
        guard()
        return document

    try:
        guard()
        before = metadata()
        for command in plan.common.gates:
            run(command, tools[command.argv[0]])
        outcomes = observe_retained_cargo_tests_v2(
            materialized,
            before,
            cargo,
            rustc=rustc,
            environment=base_environment,
            offline=offline,
            run=run,
            observation_identity=lambda: observations[-1].identity,
            source_guard=guard,
            gate_tools=tools,
            read_exclusion_permission=read_exclusion_permission,
            registry_sources=registry,
        )
        metadata()
        guard()
    except Exception as error:
        raise RetainedCargoExecutionError(
            "retained.execution.v2-failed", observations
        ) from error
    return RetainedCargoExecutionV2(tuple(observations), outcomes, baseline)

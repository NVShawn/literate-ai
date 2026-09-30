"""Bounded native execution under retained package and consumer-input custody."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import ensure_safe_directory, require_safe_directory
from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.cargo_workspace_graph import verify_cargo_workspace_graph
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_cargo_current import (
    RetainedCargoImporterAuthority,
    _unique_object,
)
from literate_ai.adapters.retained_cargo_execution_inputs import (
    RetainedCargoExecutionInputs,
)
from literate_ai.adapters.retained_cargo_materialization import (
    RetainedCargoMaterialization,
)
from literate_ai.adapters.retained_cargo_test_authority import (
    RetainedCargoTestAuthority,
)
from literate_ai.adapters.retained_cargo_test_execution import (
    observe_retained_cargo_tests,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.repositories import RepositoryBuildCommand


@dataclass(frozen=True, slots=True)
class RetainedCargoCommandObservation:
    command: RepositoryBuildCommand
    argv: tuple[str, ...]
    environment_identity: ContentIdentity
    result: BoundedProcessResult
    executable_authority: ContentIdentity | None = None

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "command": self.command.to_dict(),
                "argv": list(self.argv),
                "environment_identity": self.environment_identity.uri,
                "exit_code": self.result.returncode,
                "stdout": hashlib.sha256(self.result.stdout).hexdigest(),
                "stderr": hashlib.sha256(self.result.stderr).hexdigest(),
                "executable_authority": (
                    self.executable_authority.uri if self.executable_authority else None
                ),
            }
        )


class RetainedCargoExecutionError(ValueError):
    def __init__(self, code, observations):
        super().__init__(code)
        self.observations = tuple(observations)


def execute_retained_cargo_consumer(
    materialized: RetainedCargoMaterialization,
    importer: RetainedCargoImporterAuthority,
    *,
    cargo: LocalComponentToolBinding,
    rustc: LocalComponentToolBinding,
    gate_tools: Mapping[str, LocalComponentToolBinding],
    environment: Mapping[str, str],
    consumer_inputs: RetainedCargoExecutionInputs,
    allow_host_execution: bool,
    test_authority: RetainedCargoTestAuthority | None = None,
    offline: bool = True,
    timeout_seconds: float = 900,
    max_output_bytes: int = 4 * 1024 * 1024,
    max_total_output_bytes: int = 64 * 1024 * 1024,
) -> tuple[RetainedCargoCommandObservation, ...]:
    """Run all reviewed gates; successful processes are not positive test receipts.

    Captured inputs recheck local source, Cargo configuration and provisioned
    registry/Git files. The caller still owns complete gate-tool discovery and
    any additional external inputs used by configuration/scripts. Environment is
    explicit; this same-user runner does not provide OS network containment.
    Offline mode adds Cargo's offline flag and refuses network-enabled reviewed gates.
    """
    if allow_host_execution is not True:
        raise ValueError("retained.execution.authorization-required")
    if not isinstance(materialized, RetainedCargoMaterialization) or not isinstance(
        importer, RetainedCargoImporterAuthority
    ):
        raise TypeError("retained.execution.current-authority-required")
    if type(offline) is not bool or not isinstance(
        consumer_inputs, RetainedCargoExecutionInputs
    ):
        raise ValueError("retained.execution.configuration-invalid")
    if consumer_inputs.materialized is not materialized:
        raise ValueError("retained.execution.input-materialization-mismatch")
    if (
        type(timeout_seconds) not in (float, int)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("retained.execution.timeout-invalid")
    if any(
        type(n) is not int or n < 2 for n in (max_output_bytes, max_total_output_bytes)
    ):
        raise ValueError("retained.execution.output-limit-invalid")
    plan = materialized.plan
    if test_authority is not None:
        if (
            not isinstance(test_authority, RetainedCargoTestAuthority)
            or test_authority.materialized is not materialized
            or test_authority.importer is not importer
        ):
            raise ValueError("retained.execution.test-authority-mismatch")
        test_authority.require_unchanged()
    root = importer.project.root
    if (
        root != materialized._files.project.root
        or importer.reviewed_binding_identity != materialized._files.binding.identity
        or importer.gates.commands != plan.gates
    ):
        raise ValueError("retained.execution.importer-mismatch")
    if (
        (
            importer.gates.external_input_variables
            or consumer_inputs.gate_policy_identity is not None
        )
        and consumer_inputs.gate_policy_identity != importer.gates.identity
    ) or tuple(
        name for name, _ in consumer_inputs.external_roots
    ) != importer.gates.external_input_variables:
        raise ValueError("retained.execution.external-policy-mismatch")
    if not isinstance(cargo, LocalComponentToolBinding) or not isinstance(
        rustc, LocalComponentToolBinding
    ):
        raise TypeError("retained.execution.measured-tools-required")

    def require_measured_tool(tool):
        if tool.authority_identity is None or not callable(tool._authority_guard):
            raise ValueError("retained.execution.measured-tool-custody-required")
        tool.require_unchanged()

    require_measured_tool(cargo)
    require_measured_tool(rustc)
    if (
        cargo.toolchain_identity != plan.cargo_identity
        or rustc.toolchain_identity != plan.rustc_identity
        or rustc.arguments
    ):
        raise ValueError("retained.execution.native-tool-mismatch")
    tools = dict(gate_tools)
    if set(tools) != {gate.argv[0] for gate in plan.gates}:
        raise ValueError("retained.execution.gate-tools-incomplete")
    for tool in tools.values():
        if not isinstance(tool, LocalComponentToolBinding):
            raise ValueError("retained.execution.gate-tool-unreviewed")
        require_measured_tool(tool)
        if tool.toolchain_identity not in importer.gates.toolchains:
            raise ValueError("retained.execution.gate-tool-unreviewed")
    if offline and any(gate.network for gate in plan.gates):
        raise ValueError("retained.execution.network-gate-refused")
    base_environment = dict(environment)
    if any(
        not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v
        for k, v in base_environment.items()
    ):
        raise ValueError("retained.execution.environment-invalid")
    if canonical_identity(base_environment) != consumer_inputs.environment_identity:
        raise ValueError("retained.execution.input-environment-mismatch")
    output = root / plan.workspace_root / plan.graph.output_directory

    def check_reserved_environment(values, cwd):
        if any(
            values.get(name) != str(path)
            for name, path in consumer_inputs.external_roots
        ):
            raise ValueError("retained.execution.external-input-override-refused")
        if values.get("CARGO_HOME") != str(consumer_inputs.cargo_home):
            raise ValueError("retained.execution.cargo-home-override-refused")
        configured_output = values.get("CARGO_TARGET_DIR")
        if configured_output is not None:
            selected_output = Path(configured_output)
            if not selected_output.is_absolute():
                selected_output = cwd / selected_output
            if selected_output != output:
                raise ValueError("retained.execution.output-override-refused")
        if values.get("RUSTC", rustc.executable) != rustc.executable:
            raise ValueError("retained.execution.compiler-override-refused")

    check_reserved_environment(base_environment, root / plan.workspace_root)
    baseline = consumer_inputs.current_identity()
    observations = []
    remaining = max_total_output_bytes

    def guard():
        if test_authority is not None:
            test_authority.require_unchanged()
        materialized.require_unchanged()
        importer.require_unchanged()
        cargo.require_unchanged()
        rustc.require_unchanged()
        for tool in tools.values():
            tool.require_unchanged()
        if consumer_inputs.current_identity() != baseline:
            raise ValueError("retained.execution.consumer-inputs-changed")
        importer.require_unchanged()

    def run(command, tool, *, extra_guard=lambda: None):
        nonlocal remaining
        guard()
        extra_guard()
        tool.require_unchanged()
        cwd = root / command.working_directory
        require_safe_directory(cwd)
        process_environment = {
            **base_environment,
            **dict(tool.environment),
            **{entry.name: entry.value for entry in command.environment},
        }
        check_reserved_environment(process_environment, cwd)
        ensure_safe_directory(output)
        process_environment.update(CARGO_TARGET_DIR=str(output), RUSTC=rustc.executable)
        limit = min(max_output_bytes, remaining // 2)
        if limit < 1:
            raise ValueError("retained.execution.total-output-exhausted")
        argv = (*tool.command, *command.argv[1:])
        try:
            result = run_bounded_process(
                argv,
                cwd=cwd,
                environment=process_environment,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=limit,
                stderr_limit_bytes=limit,
                error_prefix="retained.execution",
            )
            observations.append(
                RetainedCargoCommandObservation(
                    command,
                    argv,
                    canonical_identity(process_environment),
                    result,
                    tool.authority_identity,
                )
            )
            remaining -= len(result.stdout) + len(result.stderr)
        finally:
            guard()
            extra_guard()
            tool.require_unchanged()
            require_safe_directory(output)
        if result.returncode != 0:
            raise ValueError("retained.execution.command-failed")
        return result

    def metadata():
        content = run(plan.metadata_command(offline=offline), cargo)
        data = json.loads(content.stdout, object_pairs_hook=_unique_object)
        verify_cargo_workspace_graph(
            data, workspace_root=root / plan.workspace_root, expected=plan.graph
        )
        consumer_inputs.require_metadata_paths(data)
        return data

    try:
        with materialized.custody():
            guard()
            current_metadata = metadata()
            for command in plan.gates:
                run(command, tools[command.argv[0]])
            if test_authority is not None:
                observe_retained_cargo_tests(
                    test_authority,
                    current_metadata,
                    cargo,
                    rustc=rustc,
                    environment=base_environment,
                    offline=offline,
                    run=run,
                )
            metadata()
            guard()
    except Exception as exc:
        raise RetainedCargoExecutionError(
            "retained.execution.failed", observations
        ) from exc
    return tuple(observations)

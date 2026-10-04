"""Shared test fixtures extracted from test_standard_project_factory."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
)
from literate_ai.adapters.models import CodingCliSelection
from literate_ai.adapters.standard_project import (
    project_standard_toolchain_closure,
)
from literate_ai.contracts import (
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentLifecycleCommand,
    DependencyInputKind,
    canonical_identity,
)
from tests.support.fixtures_test_component_node_generation_preparation import (
    _fixture,  # noqa: F401
)


def _selection() -> CodingCliSelection:
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return CodingCliSelection("codex", str(executable), f"sha256:{digest}")


def _command_contracts(execution):
    binding = LocalComponentToolBinding(sys.executable)
    commands = (
        ComponentLifecycleCommand(
            ComponentCommandPhase.BUILD,
            (
                "{tool}",
                "-c",
                "pass",
                "{source_root}",
                "{object_root}",
                "{export_path}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            ("{tool}", "-c", "pass", "{artifact_root}"),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.EXECUTE,
            ("{tool}", "-c", "pass", "{artifact_root}"),
        ),
    )
    contracts = tuple(
        ComponentCommandContract(
            component_revision=plan.component_revision,
            locked_build_authority_identity=canonical_identity(
                {"build-authority": plan.component_revision.uri}
            ),
            build_system_resolver_identity=canonical_identity({"resolver": "test"}),
            build_system_toolchain_identity=binding.toolchain_identity,
            language_compiler_identity=binding.toolchain_identity,
            language_runtime_identity=binding.toolchain_identity,
            commands=commands,
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, binding.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
            artifact_export=ComponentArtifactExportShape(
                f"artifact-{plan.component_revision.digest[:12]}",
                "executable",
                canonical_identity({"abi": "test"}),
                canonical_identity({"target": "test"}),
                "application/vnd.literate-ai.executable",
                canonical_identity({"producer": "test"}),
            ),
        )
        for plan in execution.generation_plans
    )
    return contracts, (binding,)


def _toolchain_closure(execution, contracts, tool_bindings):
    by_revision = {item.component_revision.uri: item for item in contracts}
    provider_exports = sorted(
        {
            by_revision[edge.provider_revision.uri].artifact_export.export_id
            for action_plan in execution.action_plans
            for edge in action_plan.dependency_edges
            if edge.semantics.consumed_input
            in {DependencyInputKind.ARTIFACT_EXPORT, DependencyInputKind.TOOLCHAIN}
        }
    )
    provider_environment = {
        export_id: (f"LITAI_PROVIDER_{index}", "artifact-manifest.json")
        for index, export_id in enumerate(provider_exports)
    }
    observation = HostDependencyObservation(
        (
            {
                "type": "application",
                "bom-ref": "toolchain:test-python",
                "name": "test-python",
            },
        ),
        (("component:test-root", "toolchain:test-python"),),
    )
    return project_standard_toolchain_closure(
        execution,
        contracts=contracts,
        tool_bindings=tool_bindings,
        provider_environment=provider_environment,
        dependency_observation=observation,
        observer_identity=canonical_identity({"observer": "test-host-closure@1"}),
    )

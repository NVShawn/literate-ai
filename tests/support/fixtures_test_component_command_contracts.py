from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_component_command_contracts``."""



from literate_ai.contracts import (
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentEntrypointCommandContract,
    ComponentLifecycleCommand,
    canonical_identity,
)


def identity(label: str):
    return canonical_identity({"component-command-test": label})

def command(phase: ComponentCommandPhase) -> ComponentLifecycleCommand:
    if phase is ComponentCommandPhase.BUILD:
        argv = (
            "{tool}",
            "build",
            "{source_root}",
            "--object-root",
            "{object_root}",
            "--output",
            "{export_path}",
            "{provider_artifacts}",
        )
    else:
        argv = ("{tool}", phase.value, "{artifact_root}")
    return ComponentLifecycleCommand(phase, argv)

def contract() -> ComponentCommandContract:
    revision = identity("revision")
    compiler = identity("compiler")
    export = ComponentArtifactExportShape(
        "sample-app",
        "executable",
        identity("abi"),
        identity("target"),
        "application/vnd.literate-ai.executable",
        identity("producer"),
    )
    return ComponentCommandContract(
        component_revision=revision,
        locked_build_authority_identity=identity("locked-flavor-build-authority"),
        build_system_resolver_identity=identity("build-system-resolver"),
        build_system_toolchain_identity=identity("bazel-toolchain"),
        language_compiler_identity=compiler,
        language_runtime_identity=identity("runtime"),
        commands=tuple(command(phase) for phase in ComponentCommandPhase),
        tool_bindings=(
            ComponentCommandToolBinding(
                ComponentCommandPhase.BUILD, identity("bazel-toolchain")
            ),
            ComponentCommandToolBinding(
                ComponentCommandPhase.TEST, identity("bazel-toolchain")
            ),
            ComponentCommandToolBinding(
                ComponentCommandPhase.EXECUTE, identity("runtime")
            ),
        ),
        artifact_export=export,
    )

def entrypoint_command(phase: ComponentCommandPhase) -> ComponentLifecycleCommand:
    return ComponentLifecycleCommand(phase, ("{tool}", phase.value, "{artifact_root}"))

def entrypoint_contract(label: str) -> ComponentEntrypointCommandContract:
    export = ComponentArtifactExportShape(
        f"surface-{label}",
        "portable-application",
        identity(f"abi-{label}"),
        identity("target"),
        "application/vnd.literate-ai.executable",
        identity("producer"),
    )
    return ComponentEntrypointCommandContract(
        entrypoint_identity=export.identity,
        deployment_unit=label,
        commands=(
            entrypoint_command(ComponentCommandPhase.TEST),
            entrypoint_command(ComponentCommandPhase.EXECUTE),
        ),
        tool_bindings=(
            ComponentCommandToolBinding(
                ComponentCommandPhase.TEST, identity("runtime")
            ),
            ComponentCommandToolBinding(
                ComponentCommandPhase.EXECUTE, identity("runtime")
            ),
        ),
        artifact_export=export,
    )


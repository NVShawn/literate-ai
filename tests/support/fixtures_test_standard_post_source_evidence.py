from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_standard_post_source_evidence``."""




from literate_ai.contracts.blobs import BlobRef

from literate_ai.contracts.executable_components import ArtifactExport

from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from literate_ai.contracts.sbom import CycloneDxBomBinding, CycloneDxLifecycle

from literate_ai.contracts.standard_post_source_evidence import (
    StandardBuildEvidence,
    StandardComponentAcceptanceEvidence,
    StandardEntrypointExecutionEvidence,
    StandardEntrypointGeneratedTestEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestCaseEvidence,
    StandardGeneratedTestExecutionEvidence,
)


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})

def _source_bom() -> CycloneDxBomBinding:
    return CycloneDxBomBinding(
        CycloneDxLifecycle.SOURCE,
        _identity("source-bom"),
        _identity("source-dependency-graph"),
        _identity("managed-graph"),
        _identity("resolved-component-graph"),
        None,
        "urn:fixture:root",
        2,
        1,
    )

def _resolved_bom(source: CycloneDxBomBinding) -> CycloneDxBomBinding:
    return CycloneDxBomBinding(
        CycloneDxLifecycle.RESOLVED,
        _identity("resolved-bom"),
        _identity("resolved-dependency-graph"),
        source.managed_graph_identity,
        source.resolved_graph_identity,
        source.bom_identity,
        source.root_ref,
        3,
        2,
    )

def _export(
    component: ContentIdentity,
    source_tree: ContentIdentity,
    export_id: str = "fixture-app",
) -> ArtifactExport:
    media_type = "application/vnd.literate-ai.fixture"
    return ArtifactExport(
        export_id,
        component,
        "executable",
        _identity("abi"),
        _identity("target"),
        media_type,
        _identity("producer"),
        source_tree,
        _identity("toolchain"),
        _identity("authorization"),
        (),
        BlobRef("ab" * 32, 17, media_type=media_type),
    )

def _evidence() -> StandardComponentAcceptanceEvidence:
    component = _identity("component")
    source_tree = _identity("source-tree")
    source_bom = _source_bom()
    export = _export(component, source_tree)
    build = StandardBuildEvidence(
        component_revision=component,
        build_plan_identity=_identity("build-plan"),
        source_tree_identity=source_tree,
        source_custody_identity=_identity("source-custody"),
        source_sbom=source_bom,
        resolved_sbom=_resolved_bom(source_bom),
        exports=(export,),
        resolved_sbom_export_identities=(export.identity,),
        build_observation_identity=_identity("build-observation"),
        artifact_custody_identity=_identity("artifact-custody"),
    )
    case = StandardGeneratedTestCaseEvidence(
        "generated-contract-case",
        _identity("generated-case"),
        _identity("test-case-observation"),
    )
    generated_tests = StandardGeneratedTestExecutionEvidence(
        component_revision=component,
        generated_test_suite_identity=_identity("generated-suite"),
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
        runner_identity=_identity("test-runner"),
        test_custody_identity=_identity("test-custody"),
        selected_case_identities=(case.case_identity,),
        cases=(case,),
        selected_count=1,
        executed_count=1,
        passed_count=1,
    )
    execution = StandardExecutionEvidence(
        component_revision=component,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
        root_export_identity=export.identity,
        execution_contract_identity=_identity("execution-contract"),
        runtime_identity=_identity("runtime"),
        artifact_custody_identity=build.artifact_custody_identity,
        observation_identity=_identity("execution-observation"),
        stdout_identity=_identity("stdout"),
        stderr_identity=_identity("stderr"),
        exit_code=0,
    )
    return StandardComponentAcceptanceEvidence(
        component_revision=component,
        source_generation_identity=_identity("source-generation"),
        generated_test_suite_identity=generated_tests.generated_test_suite_identity,
        build=build,
        generated_tests=generated_tests,
        execution=execution,
        acceptance_policy_identity=_identity("acceptance-policy"),
    )

def _multi_evidence() -> StandardComponentAcceptanceEvidence:
    component = _identity("multi-component")
    source_tree = _identity("multi-source-tree")
    source_bom = _source_bom()
    exports = (
        _export(component, source_tree, "app"),
        _export(component, source_tree, "worker"),
    )
    build = StandardBuildEvidence(
        component_revision=component,
        build_plan_identity=_identity("multi-build-plan"),
        source_tree_identity=source_tree,
        source_custody_identity=_identity("multi-source-custody"),
        source_sbom=source_bom,
        resolved_sbom=_resolved_bom(source_bom),
        exports=exports,
        resolved_sbom_export_identities=tuple(item.identity for item in exports),
        build_observation_identity=_identity("multi-build-observation"),
        artifact_custody_identity=_identity("multi-artifact-custody"),
    )
    suite = _identity("multi-suite")
    entrypoint_tests = []
    aggregate_cases = []
    entrypoint_executions = []
    for index, (unit, export) in enumerate(
        zip(("primary", "worker"), exports, strict=True)
    ):
        case = StandardGeneratedTestCaseEvidence(
            f"unit-{unit}",
            _identity(f"case-{unit}"),
            _identity(f"case-observation-{unit}"),
        )
        aggregate_cases.append(case)
        entrypoint = _identity(f"entrypoint-{unit}")
        entrypoint_tests.append(
            StandardEntrypointGeneratedTestEvidence(
                entrypoint_identity=entrypoint,
                deployment_unit=unit,
                export_identity=export.identity,
                command_identity=_identity(f"test-command-{unit}"),
                runner_identity=_identity(f"test-runner-{unit}"),
                process_observation_identity=_identity(f"test-process-{unit}"),
                test_custody_identity=_identity(f"test-custody-{unit}"),
                selected_case_identities=(case.case_identity,),
                cases=(case,),
                selected_count=1,
                executed_count=1,
                passed_count=1,
            )
        )
        entrypoint_executions.append(
            StandardEntrypointExecutionEvidence(
                entrypoint_identity=entrypoint,
                deployment_unit=unit,
                export_identity=export.identity,
                execution_contract_identity=_identity(f"execute-command-{unit}"),
                runtime_identity=_identity(f"runtime-{index}"),
                observation_identity=_identity(f"execution-observation-{unit}"),
                stdout_identity=_identity(f"stdout-{unit}"),
                stderr_identity=_identity(f"stderr-{unit}"),
                exit_code=0,
            )
        )
    tests = StandardGeneratedTestExecutionEvidence(
        component_revision=component,
        generated_test_suite_identity=suite,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
        runner_identity=_identity("multi-test-runners"),
        test_custody_identity=_identity("multi-test-custody"),
        selected_case_identities=tuple(item.case_identity for item in aggregate_cases),
        cases=tuple(aggregate_cases),
        selected_count=2,
        executed_count=2,
        passed_count=2,
        entrypoint_evidence=tuple(entrypoint_tests),
    )
    execution = StandardExecutionEvidence(
        component_revision=component,
        build_evidence_identity=build.identity,
        export_identities=build.export_identities,
        root_export_identity=exports[0].identity,
        execution_contract_identity=_identity("multi-execution-contract"),
        runtime_identity=_identity("multi-runtime"),
        artifact_custody_identity=build.artifact_custody_identity,
        observation_identity=_identity("multi-execution-observation"),
        stdout_identity=_identity("multi-stdout"),
        stderr_identity=_identity("multi-stderr"),
        exit_code=0,
        entrypoint_evidence=tuple(entrypoint_executions),
    )
    return StandardComponentAcceptanceEvidence(
        component_revision=component,
        source_generation_identity=_identity("multi-source-generation"),
        generated_test_suite_identity=suite,
        build=build,
        generated_tests=tests,
        execution=execution,
        acceptance_policy_identity=_identity("multi-acceptance-policy"),
    )


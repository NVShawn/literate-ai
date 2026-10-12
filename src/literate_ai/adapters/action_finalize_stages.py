"""Run existing root integration stages only inside exact private runtime custody."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)


def execute_finalize_stages(
    prepared,
    *,
    ports,
    deadline,
    admission_guard,
    require_execution_authority,
):
    """Private composition owns runtime/grants; each existing stage owns its oracle."""
    if (
        not isinstance(prepared, FinalizeRuntimeInputs)
        or not callable(admission_guard)
        or not callable(require_execution_authority)
    ):
        raise TypeError(
            "FINALIZE requires verified inputs and explicit execution authority"
        )
    value, package = prepared.intent, prepared.package
    plan = value.package_input.plan
    root = prepared.root_build
    arguments = (
        value.component_lock,
        value.package_input.execution_plan,
        value.project_plan,
        plan,
        package,
    )

    def current():
        deadline.remaining()
        admission_guard()
        prepared.package_tree.require_unchanged()
        source = prepared.source_trees.evidence(root.candidate.tree_identity)
        custody = ports.project_package_custody(plan, package)
        if (
            ports.source_trees is not prepared.source_trees
            or source.identity != root.source_custody_identity
            or custody.root != prepared.package_tree.root
            or custody.root_plan != root.plan
            or custody.generated_test_suite != source.generated_test_suite
            or any(
                ports.contracts.get(build.plan.component_revision.uri)
                != build.inputs.contract
                for build in prepared.component_builds
            )
        ):
            raise ActionWireError(
                "action_finalize.runtime_mismatch",
                "FINALIZE private runtime custody differs",
            )
        deadline.remaining()

    def stage(name, operation, *extra):
        current()
        require_execution_authority(name, prepared)
        current()
        identity = operation(*arguments, *extra)
        current()
        require_execution_authority(name, prepared)
        current()
        if not isinstance(identity, ContentIdentity):
            raise ActionWireError(
                "action_finalize.stage_invalid",
                "FINALIZE stage returned no exact identity",
            )
        return identity

    tested = stage("root-integration-test", ports.test_root_integration)
    executed = stage("packaged-execution", ports.execute_packaged_project)
    accepted = stage(
        "independent-project-acceptance",
        ports.accept_project_independently,
        tested,
        executed,
    )
    graph = value.package_input.artifact_graph
    link = next(
        item for item in graph.link_plans if item.identity == plan.link_plan_identity
    )
    evidence = StandardRootIntegrationEvidence(
        value.component_lock.identity,
        value.package_input.execution_plan.identity,
        value.project_plan.identity,
        graph,
        link,
        plan,
        package,
        tested,
        executed,
        accepted,
    )
    current()
    return evidence

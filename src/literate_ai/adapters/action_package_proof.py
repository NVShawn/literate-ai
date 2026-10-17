"""PACKAGE authority is reconstructed from independently reopened LINK proof."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_link_dependencies import reopen_link_result_closure
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.artifact_graph import create_artifact_build_graph
from literate_ai.application.release_artifacts import (
    plan_accepted_assembly_dependencies,
)


def reopen_package_input(
    *,
    input_record,
    input_identity,
    records,
    cas,
    deadline,
    admission_guard,
    blob_source=None,
):
    if not callable(admission_guard):
        raise TypeError("PACKAGE proof requires live admission")
    value = PackageWorkerInput.admit(input_record, input_identity, deadline)
    closure = reopen_link_result_closure(
        value.link_results,
        execution_plan_identity=value.execution_plan.identity,
        records=records,
        cas=cas,
        deadline=deadline,
        admission_guard=admission_guard,
        blob_source=blob_source,
    )
    manifests = tuple(
        sorted(
            (result.manifest for _, result in closure),
            key=lambda item: item.component_revision.uri,
        )
    )
    receipts = tuple(item.acceptance_result.evidence for item, _ in closure)
    graph = value.artifact_graph
    if manifests != graph.manifests:
        raise ActionWireError(
            "action_package.graph_mismatch",
            "PACKAGE manifests differ from verified LINK",
        )
    bindings = plan_accepted_assembly_dependencies(value.execution_plan, receipts)
    root_exports = next(
        (
            manifest.exports
            for manifest in manifests
            if manifest.component_revision == value.execution_plan.root_revision
        ),
        None,
    )
    if root_exports is None:
        raise ActionWireError(
            "action_package.graph_mismatch", "PACKAGE has no root manifest"
        )
    root = value.plan.root_artifact_identity
    if root not in {item.identity for item in root_exports}:
        raise ActionWireError(
            "action_package.graph_mismatch",
            "PACKAGE root is not a verified root export",
        )
    roots = (root,) if len(root_exports) == 1 else ()
    groups = (
        ()
        if len(root_exports) == 1
        else (
            (root, *(item.identity for item in root_exports if item.identity != root)),
        )
    )
    expected = create_artifact_build_graph(
        build_system_driver_identity=graph.build_system_driver_identity,
        manifests=manifests,
        assembly_dependencies=bindings,
        link_roots=tuple(roots),
        link_root_groups=tuple(groups),
    )
    if expected != graph:
        raise ActionWireError(
            "action_package.graph_mismatch",
            "PACKAGE graph differs from accepted dependencies",
        )
    admission_guard()
    deadline.remaining()
    return value

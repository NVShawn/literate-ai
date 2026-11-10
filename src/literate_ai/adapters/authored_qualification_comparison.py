"""Reopen every native observation against the original current private oracles."""

from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    _verify_library_acceptance_observation,
)
from literate_ai.contracts.authored_qualification_acceptance import (
    AuthoredGraphIndependentAcceptance,
)
from literate_ai.contracts.authored_qualification_comparisons import (
    AuthoredQualificationComparison,
)
from literate_ai.contracts.executable_components import LibraryImportSurface
from literate_ai.contracts.executable_components.packages import PackageFileKind
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)


def compare_authored_qualification(
    current,
    *,
    run_identity,
    root,
    reader,
) -> AuthoredQualificationComparison:
    """Compare actual records, without granting qualification or importer trust.

    The executing runner must separately supply successful fresh all-node Standard
    execution and receipts; receiving must reopen its complete trusted closure.
    This comparison preserves the real root/package context for every provider.
    """

    if (
        not isinstance(current, CurrentAuthoredQualificationPlan)
        or not isinstance(run_identity, ContentIdentity)
        or not isinstance(root, StandardRootIntegrationEvidence)
        or not isinstance(reader, QualificationEvidenceReader)
    ):
        raise TypeError("current authored owner and typed native evidence required")
    current.require_unchanged()
    with current.oracles.capture_current():
        lock = current.oracles.authority.locked.authority.lock
        graph = AuthoredGraphIndependentAcceptance.from_dict(
            reader.read_json(root.independent_acceptance_identity)
        )
        expected_revisions = {n.revision.identity for n in lock.nodes}
        bindings = {b.component_revision_identity: b for b in current.oracles.bindings}
        if (
            graph.identity != root.independent_acceptance_identity
            or graph.origin != current.plan.origin
            or graph.oracle_set_identity != current.oracles.identity
            or root.component_lock_identity != lock.identity
            or root.package_plan.root_component_revision != lock.root_revision
            or root.execution_plan_identity != current.plan.execution_plan.identity
            or graph.execution_plan_identity != root.execution_plan_identity
            or graph.package_plan_identity != root.package_plan.identity
            or graph.package_result_identity != root.package_result.identity
            or graph.root_integration_test_identity
            != root.root_generated_integration_test_identity
            or graph.packaged_execution_identity != root.packaged_execution_identity
            or {n.component_revision_identity for n in graph.nodes}
            != expected_revisions
            or set(bindings) != expected_revisions
            or len(bindings) != len(current.oracles.bindings)
            or {m.component_revision for m in root.artifact_graph.manifests}
            != expected_revisions
        ):
            raise QualificationCaptureError(
                "qualification.authored.comparison_membership_mismatch"
            )
        exports = {
            e.identity: e for m in root.artifact_graph.manifests for e in m.exports
        }
        for node in graph.nodes:
            binding = bindings[node.component_revision_identity]
            if (
                node.oracle_identity != binding.oracle_identity
                or node.case_map_identity != binding.case_map.identity
                or node.case_count != len(binding.case_map.cases)
            ):
                raise QualificationCaptureError(
                    "qualification.authored.comparison_case_map_mismatch"
                )
            actual = reader.read_json(node.observation_identity)
            try:
                export = exports[ContentIdentity.parse_uri(actual["artifact_identity"])]
            except (KeyError, TypeError, ValueError) as exc:
                raise QualificationCaptureError(
                    "qualification.authored.comparison_export_missing"
                ) from exc
            if (
                export.component_revision != node.component_revision_identity
                or export.role != "library"
                or export.target_identity != root.package_plan.target_identity
                or not any(
                    item.kind is PackageFileKind.ARTIFACT
                    and item.source_identity == export.identity
                    and item.target_identity == export.target_identity
                    and item.blob == export.blob
                    for item in root.package_plan.inputs
                )
            ):
                raise QualificationCaptureError(
                    "qualification.authored.comparison_export_mismatch"
                )
            surface = LibraryImportSurface.from_dict(
                reader.read_json(binding.oracle.import_surface_identity)
            )
            if surface.identity != binding.oracle.import_surface_identity:
                raise QualificationCaptureError(
                    "qualification.authored.comparison_surface_mismatch"
                )
            _verify_library_acceptance_observation(
                reader,
                root=root,
                export=export,
                surface=surface,
                oracle=binding.oracle,
                observation_identity=node.observation_identity,
            )
        result = AuthoredQualificationComparison(
            current.plan.origin,
            current.plan.identity,
            run_identity,
            current.oracles.identity,
            root.independent_acceptance_identity,
            graph.nodes,
        )
    current.require_unchanged()
    return result

"""Reopen authored runs against current authority and shared Standard checks.

Retrieval supplies immutable records. The caller must independently project its
current recipes and command contracts under the original current plan's custody;
archive documents cannot select those expectations. This returns observations,
never publication, native execution or importer permission.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from literate_ai.adapters.authored_qualification_capture import (
    ReopenedAuthoredProductMembership,
    _acceptances,
    _reopen_authored_product_records,
)
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.authored_qualification_runs import (
    observe_authored_qualification_run,
)
from literate_ai.adapters.models.coding_cli import GenerationRecipe
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    capture_qualification_product_blobs,
    reopen_qualification_lifecycle,
    reopen_standard_qualification_root,
    verify_qualification_boms,
    verify_qualification_command_authority,
    verify_qualification_generated_suite,
    verify_qualification_generation_records,
    verify_qualification_packaged_library_processes,
    verify_qualification_source_records,
    verify_qualification_standard_execution,
)
from literate_ai.contracts.executable_components import ComponentCommandContract
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleExecution,
)


@dataclass(frozen=True, slots=True)
class ReopenedAuthoredQualification:
    membership: ReopenedAuthoredProductMembership
    executions: tuple[tuple[ContentIdentity, QualificationLifecycleExecution], ...]


def reopen_authored_qualification(
    current,
    reader,
    products_identity,
    *,
    current_recipes,
    current_commands,
    max_package_bytes,
):
    """Verify all runs before reading products; execute no retained content.

    Independently projected current recipe/command maps are required, as on the
    source-derived route. A separate receiving owner must bind them to importer
    review and native tools before any materialization or consumer admission.
    """

    if (
        not isinstance(current, CurrentAuthoredQualificationPlan)
        or not isinstance(reader, QualificationEvidenceReader)
        or not isinstance(products_identity, ContentIdentity)
    ):
        raise TypeError("original current authored plan and bounded reader required")
    if type(max_package_bytes) is not int or max_package_bytes <= 0:
        raise QualificationCaptureError("qualification.capture.limit-invalid")
    current.require_unchanged()
    with current.oracles.capture_current():
        lock = current.oracles.authority.locked.authority.lock
        revisions = {n.revision.identity for n in lock.nodes}
        if (
            not isinstance(current_recipes, Mapping)
            or not isinstance(current_commands, Mapping)
            or set(current_recipes) != revisions
            or set(current_commands) != revisions
        ):
            raise QualificationCaptureError(
                "qualification.authored.reopen_authority_incomplete"
            )
        recipes, commands = dict(current_recipes), dict(current_commands)
        if any(
            not isinstance(recipes[r], GenerationRecipe)
            or recipes[r].component_lock_identity != lock.identity
            or not isinstance(commands[r], ComponentCommandContract)
            or commands[r].component_revision != r
            for r in revisions
        ):
            raise QualificationCaptureError(
                "qualification.authored.reopen_authority_invalid"
            )
        reopened = _reopen_authored_product_records(reader, products_identity)
        if reopened.result.plan != current.plan:
            raise QualificationCaptureError(
                "qualification.authored.reopen_plan_mismatch"
            )
        bindings = {b.component_revision_identity: b for b in current.oracles.bindings}
        if set(bindings) != revisions or len(bindings) != len(current.oracles.bindings):
            raise QualificationCaptureError(
                "qualification.authored.reopen_authority_incomplete"
            )
        exports_by_run = dict(reopened.exports)
        references = {}
        for _run, exports in reopened.exports:
            for manifest in exports.graph.manifests:
                for export in manifest.exports:
                    reference = export.blob
                    prior = references.get(reference.identity)
                    if prior is not None and prior.size != reference.size:
                        raise QualificationCaptureError(
                            "qualification.capture.blob-mismatch"
                        )
                    references[reference.identity] = reference
        if sum(r.size for r in references.values()) > max_package_bytes:
            raise QualificationCaptureError("qualification.capture.byte-limit")
        executions = []
        for run in reopened.result.runs:
            current.require_unchanged()
            lifecycle = reopen_qualification_lifecycle(
                reader, run.lifecycle_result_identity
            )
            execution = QualificationLifecycleExecution(
                lifecycle,
                run.receipt,
                run.lifecycle_request_identity,
                run.lifecycle_invocation_identity,
                run.driver_identity,
                run.lifecycle_policy_identity,
                run.framework_distribution_identity,
            )
            derived = verify_qualification_standard_execution(
                reader,
                execution=execution,
                component_lock_identity=lock.identity,
                current_commands=commands,
            )
            root = reopen_standard_qualification_root(
                reader,
                lifecycle_result_identity=run.lifecycle_result_identity,
                project_receipt_identity=run.receipt.identity,
                component_lock_identity=lock.identity,
                target_profile_identity=lock.target_profile_identity,
                lifecycle_policy_identity=run.lifecycle_policy_identity,
                generated_test_total=run.generated_test_total,
                lifecycle_invocation_identity=run.lifecycle_invocation_identity,
                lifecycle_request_identity=run.lifecycle_request_identity,
                evidence_sets={
                    "acceptance-result": derived[7],
                    "build-result": derived[2],
                    "source-intelligence": derived[1],
                    "resolved-sbom": derived[3],
                    "test-report": derived[5],
                },
            )
            if root != run.root or root != lifecycle.root_integration:
                raise QualificationCaptureError(
                    "qualification.authored.reopen_run_mismatch"
                )
            plans = {p.identity: p for p in lifecycle.project_build_plan.components}
            for node in lifecycle.node_results:
                recipe = recipes[node.component_revision]
                verify_qualification_command_authority(
                    reader,
                    contract=commands[node.component_revision],
                    authorization_identity=node.authorization_identity,
                    plan=plans[node.build_plan_identity],
                )
                verify_qualification_source_records(
                    reader, candidate=node.source_output.candidate
                )
                verify_qualification_generation_records(
                    reader, source_output=node.source_output
                )
                verify_qualification_boms(
                    reader, component_lock=lock, build=node.build_evidence
                )
                verify_qualification_generated_suite(
                    reader,
                    recipe=recipe,
                    component_lock_identity=lock.identity,
                    tests=node.generated_test_evidence,
                )
                if (
                    node.source_output.candidate.recipe_identity.uri != recipe.identity
                    or node.source_output.provenance.recipe_identity.uri
                    != recipe.identity
                ):
                    raise QualificationCaptureError(
                        "qualification.capture.suite-source-mismatch"
                    )
            root_node = next(
                n
                for n in lifecycle.node_results
                if n.component_revision == lock.root_revision
            )
            verify_qualification_packaged_library_processes(
                reader, root=root, tests=root_node.generated_test_evidence
            )
            # This reuses complete current-plan/node membership and recomputes
            # every native independent comparison under the original oracles.
            observed = observe_authored_qualification_run(
                current, request=run.request, execution=execution, reader=reader
            )
            if observed != run:
                raise QualificationCaptureError(
                    "qualification.authored.reopen_run_mismatch"
                )
            exports = exports_by_run[run.request.run_identity]
            for product in exports.libraries:
                revision = product.artifact_export.component_revision
                contract = commands[revision]
                if (
                    product.import_surface.identity
                    != bindings[revision].oracle.import_surface_identity
                    or product.import_surface != contract.library_import_surface
                    or product.native_layout != getattr(contract, "native_layout", None)
                ):
                    raise QualificationCaptureError(
                        "qualification.authored.reopen_surface_mismatch"
                    )
            executions.append((run.request.run_identity, execution))
        current.require_unchanged()
        # Refuse stage/authority failures in either run before any product read.
        for run in reopened.result.runs:
            exports = exports_by_run[run.request.run_identity]
            capture_qualification_product_blobs(
                exports,
                _acceptances(run, exports, lock.target_profile_identity),
                target_profile_identity=lock.target_profile_identity,
                read_blob=lambda ref: reader.read_bytes(
                    ContentIdentity.parse_uri(ref.identity)
                ),
                max_bytes=max_package_bytes,
            )
            current.require_unchanged()
    current.require_unchanged()
    return ReopenedAuthoredQualification(reopened, tuple(executions))

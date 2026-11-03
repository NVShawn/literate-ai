"""Reopen original assertions and compare them with independently admitted output.

Neither the historical context nor an equivalence digest grants execution. Only
an existing live retained owner and LINK handoff can authorize native verification.
"""

from dataclasses import dataclass

from literate_ai.adapters.retained_project_unit_packages import (
    plan_retained_unit_packages,
)
from literate_ai.adapters.retained_unit_inspection import (
    read_retained_unit_planning_inputs,
)
from literate_ai.adapters.retained_unit_link import retained_unit_link_document
from literate_ai.adapters.retained_unit_run_inspection import reopen_retained_unit_run
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.storage.cas import FileSystemCAS

_REOPENED_CONTEXT = object()


@dataclass(frozen=True, slots=True)
class OriginalUnitContext:
    evidence: object
    inputs: object
    run: object
    projection: object
    _seal: object = None
    transfer_custody: object = None
    workspace_roots: tuple = ()

    def __post_init__(self):
        if self._seal is not _REOPENED_CONTEXT:
            raise TypeError("retained.unit.reopened-original-context-required")


def read_original_unit_context(
    evidence,
    *,
    source,
    manifest,
    planning,
    component_locks,
    store,
    policy,
    pkg_tool,
    tar_tool,
    test_receipt_policy,
    admission_guard,
    expected_request,
    expected_worker_identity,
    sdk_snapshots=(),
    expected_sdk_platform=None,
    expected_sdk_architecture=None,
    specification_context=None,
    transfer_custody=None,
):
    """Bounded historical joins only, with current source, policies and SDK pins."""
    evidence.require_budget(policy)
    if (
        evidence.retained_plan_identity != planning.plan.identity
        or evidence.worker_identity != expected_worker_identity
    ):
        raise ValueError("retained.unit.original-current-context-differs")
    admission_guard()
    request_raw = store.get_bytes(evidence.execution_request)
    if request_raw != canonical_json_bytes(expected_request):
        raise ValueError("retained.unit.original-execution-intent-differs")
    inputs = read_retained_unit_planning_inputs(
        source,
        manifest,
        planning.plan,
        component_locks,
        evidence.retained_receipt,
        store,
        planning_evidence_reference=evidence.planning_evidence,
        maximum_metadata_bytes=policy.max_provider_metadata_bytes,
        maximum_metadata_memory_bytes=policy.max_provider_memory_bytes,
    )
    workspace_roots = []
    run = reopen_retained_unit_run(
        inputs,
        planning,
        evidence.accepted_run_evidence,
        policy=policy,
        test_receipt_policy=test_receipt_policy,
        admission_guard=admission_guard,
        expected_sdk_platform=expected_sdk_platform,
        expected_sdk_architecture=expected_sdk_architecture,
        current_sdk_snapshots=sdk_snapshots,
        workspace_root_observer=workspace_roots.extend,
    )
    projection = plan_retained_unit_packages(
        inputs,
        pkg_tool=pkg_tool,
        tar_tool=tar_tool,
        specification_context=specification_context,
        **policy.owner_arguments(),
    )
    if evidence.link_proof.size > policy.max_index_bytes:
        raise ValueError("retained.unit.original-link-budget-exceeded")
    raw = store.get_bytes(evidence.link_proof)
    if raw != canonical_json_bytes(
        retained_unit_link_document(
            run, inputs.receipt, projection, inputs.payload_manifest
        )
    ):
        raise ValueError("retained.unit.original-link-context-differs")
    from literate_ai.adapters.retained_unit_results import read_retained_unit_results

    read_retained_unit_results(
        projection,
        evidence.result_index,
        store,
        max_descriptor_bytes=policy.max_descriptor_bytes,
        max_descriptor_total_bytes=policy.max_descriptor_total_bytes,
        max_index_bytes=policy.max_index_bytes,
        max_metadata_memory_bytes=policy.max_metadata_memory_bytes,
    )
    admission_guard()
    return OriginalUnitContext(
        evidence,
        inputs,
        run,
        projection,
        _REOPENED_CONTEXT,
        transfer_custody,
        tuple(workspace_roots),
    )


def require_equivalent_unit_projection(
    original, fresh, fresh_run, fresh_inputs, fresh_workspace_roots
):
    """Compare every stable field; explicitly bind epoch-specific evidence separately.

    Each projection must first have been reconstructed from its complete receipt,
    run, and LINK. Only receipt and resolved-BOM provenance may differ; all source,
    provider policy, declarations, topology, target, recipe and payload stay exact.
    """
    old_run = original.run
    if (
        old_run.source_bom.content != fresh_run.source_bom.content
        or old_run.output_manifest != fresh_run.output_manifest
        or old_run.resolved_bom.output_manifest
        != fresh_run.resolved_bom.output_manifest
        or old_run.test_policy != fresh_run.test_policy
        or old_run.configured_test_policy != fresh_run.configured_test_policy
        or old_run.acceptance_policy != fresh_run.acceptance_policy
    ):
        raise ValueError("retained.unit.original-source-output-or-acceptance-differs")

    from literate_ai.adapters.retained_unit_sdk_equivalence import (
        retained_unit_sdk_context,
    )

    policy = original.inputs.store.policy
    remaining_memory = policy.max_provider_memory_bytes - (
        32 * original.inputs.store.account["total"]
        + 1024 * len(original.inputs.store.references)
    )
    old_sdk = retained_unit_sdk_context(
        old_run,
        original.inputs,
        original.workspace_roots,
        original.inputs.store.references,
        maximum_memory_bytes=remaining_memory // 3,
        maximum_steps=policy.max_closure_steps,
    )
    fresh_sdk = retained_unit_sdk_context(
        fresh_run,
        fresh_inputs,
        fresh_workspace_roots,
        original.inputs.store.references,
        maximum_memory_bytes=remaining_memory // 3,
        maximum_steps=policy.max_closure_steps,
    )
    if old_sdk != fresh_sdk:
        raise ValueError("retained.unit.original-sdk-dependency-closure-differs")

    from literate_ai.adapters.retained_unit_runtime_equivalence import (
        retained_unit_runtime_context,
    )

    policy = original.inputs.store.policy
    remaining_memory = policy.max_provider_memory_bytes - (
        32 * original.inputs.store.account["total"]
        + 1024 * len(original.inputs.store.references)
    )
    old_runtime = retained_unit_runtime_context(
        old_run,
        original.inputs,
        original.workspace_roots,
        policy,
        working_memory_bytes=remaining_memory // 3,
    )
    fresh_runtime = retained_unit_runtime_context(
        fresh_run,
        fresh_inputs,
        fresh_workspace_roots,
        policy,
        working_memory_bytes=remaining_memory // 3,
    )
    if old_runtime != fresh_runtime:
        raise ValueError("retained.unit.original-runtime-dependency-closure-differs")

    def command_results(run):
        return tuple(
            (
                canonical_identity(item.action.to_dict()),
                item.tool_identity,
                item.result.returncode,
                item.result.stdout,
                item.result.stderr,
            )
            for item in run.observations
        )

    def acceptance_results(run):
        return tuple(
            (
                item.case_identity,
                item.result.returncode,
                item.result.stdout,
                item.result.stderr,
            )
            for item in run.acceptance_observations
        )

    if command_results(old_run) != command_results(fresh_run) or acceptance_results(
        old_run
    ) != acceptance_results(fresh_run):
        raise ValueError("retained.unit.original-command-or-acceptance-output-differs")
    old, new = original.projection, fresh
    old_plan, new_plan = old.set_plan.to_dict(), new.set_plan.to_dict()
    for key in ("receipt_identity", "graph_index"):
        old_plan.pop(key)
        new_plan.pop(key)
    if old_plan != new_plan or old.graph.tree.identity != new.graph.tree.identity:
        raise ValueError("retained.unit.original-payload-or-policy-differs")

    def normalized(projection):
        definitions = projection.graph.partition.definitions
        names = {item.identity: item.unit_name for item in definitions}
        result = []
        for definition, parameters in zip(
            definitions, projection.parameters, strict=True
        ):
            document = definition.to_dict()
            for key in (
                "receipt_identity",
                "resolved_bom_identity",
                "provider_parameters",
            ):
                document.pop(key)
            params = parameters.to_dict()
            params["evidence"] = [
                item
                for item in params["evidence"]
                if item["role"] not in {"retained-receipt", "cyclonedx-resolved-sbom"}
            ]
            result.append((definition.unit_name, document, params))
        ownership = [
            {**value.to_dict(), "unit_identity": names[value.unit_identity]}
            for value in projection.graph.partition.ownership
        ]
        dependencies = [
            {
                **value.to_dict(),
                "source_unit_identity": names[value.source_unit_identity],
                "target_unit_identity": names[value.target_unit_identity],
            }
            for value in projection.graph.partition.dependencies
        ]
        return {
            "units": sorted(result, key=lambda item: item[0]),
            "ownership": sorted(ownership, key=canonical_json_bytes),
            "dependencies": sorted(dependencies, key=canonical_json_bytes),
        }

    old_context, fresh_context = normalized(old), normalized(new)
    if old_context != fresh_context:
        raise ValueError(
            "retained.unit.original-ownership-dependencies-or-provenance-differ"
        )
    return canonical_identity(
        {
            "packages": old_context,
            "runtime": old_runtime,
            "sdk_dependencies": old_sdk,
        }
    )


class OriginalUnitMetadataBudget(FileSystemCAS):
    """One aggregate unique metadata allowance for both historical epochs."""

    def __init__(self, store, policy, guard, *, shared_budget=None):
        super().__init__(store.root, create=False)
        self.store, self.policy, self.guard = store, policy, guard
        if shared_budget is not None and not isinstance(
            shared_budget, OriginalUnitMetadataBudget
        ):
            raise TypeError("retained.unit.shared-metadata-budget-required")
        self.references = {} if shared_budget is None else shared_budget.references
        self.observed = {}
        self.account = {"total": 0} if shared_budget is None else shared_budget.account

    def _charge(self, reference):
        self.guard()
        previous = self.references.get(reference.identity)
        self.observed[reference.identity] = reference
        if previous is not None and previous.size != reference.size:
            raise ValueError("retained.unit.original-metadata-reference-differs")
        if previous is None:
            total = self.account["total"] + reference.size
            if (
                reference.size
                > min(64 * 1024**2, self.policy.max_provider_metadata_bytes)
                or total > self.policy.max_provider_metadata_bytes
                or 32 * total + 1024 * (len(self.references) + 1)
                > self.policy.max_provider_memory_bytes
            ):
                raise ValueError(
                    "retained.unit.original-aggregate-metadata-budget-exceeded"
                )
            self.references[reference.identity] = reference
            self.account["total"] = total

    def get_bytes(self, reference):
        self._charge(reference)
        raw = self.store.get_bytes(reference)
        self.guard()
        return raw

    def verify(self, reference):
        self._charge(reference)
        self.store.verify(reference)
        self.guard()


def verify_original_unit_packages(
    context, owner, handoff, *, object_root, fresh_workspace_roots
):
    """Native original-byte verification inside the separate genuine fresh scope."""
    import platform

    from literate_ai.adapters.freebsd_composed_tree_packaging import (
        verify_freebsd_composed_package_set,
    )
    from literate_ai.adapters.retained_project_unit_packages import (
        RetainedProjectUnitPackages,
    )
    from literate_ai.adapters.retained_unit_link import RetainedUnitLinkHandoff
    from literate_ai.adapters.retained_unit_results import read_retained_unit_results

    if (
        type(owner) is not RetainedProjectUnitPackages
        or type(handoff) is not RetainedUnitLinkHandoff
        or not isinstance(context, OriginalUnitContext)
    ):
        raise TypeError("retained.unit.live-original-verification-scope-required")
    if platform.system().casefold() != "freebsd":
        raise ValueError("retained.unit.original-native-verification-requires-freebsd")
    handoff.require_current(owner)
    owner.admission.require_native_package_scope()
    equivalence = require_equivalent_unit_projection(
        context,
        owner.projection,
        owner.admission.run,
        owner.inputs,
        fresh_workspace_roots,
    )
    index, results = read_retained_unit_results(
        context.projection,
        context.evidence.result_index,
        owner.admission.store,
        **owner.tree_policy,
    )
    verify_freebsd_composed_package_set(
        context.projection,
        results,
        store=owner.admission.store,
        object_root=object_root,
    )
    handoff.require_current(owner)
    owner.admission.require_native_package_scope()
    owner.pkg_tool.require_unchanged()
    owner.tar_tool.require_current()
    if (
        require_equivalent_unit_projection(
            context,
            owner.projection,
            owner.admission.run,
            owner.inputs,
            fresh_workspace_roots,
        )
        != equivalence
    ):
        raise ValueError("retained.unit.original-equivalence-changed")
    return index, results, equivalence


def original_unit_verification_proof(
    context,
    fresh_projection,
    *,
    fresh_completed_run,
    fresh_inputs,
    fresh_workspace_roots,
    fresh_receipt,
    fresh_planning,
    fresh_run,
    fresh_link,
    result_index,
    results,
):
    """A closed assertion binding both epochs; decoding grants no admission."""
    equivalence = require_equivalent_unit_projection(
        context,
        fresh_projection,
        fresh_completed_run,
        fresh_inputs,
        fresh_workspace_roots,
    )
    if result_index != context.evidence.result_index:
        raise ValueError("retained.unit.original-result-selection-differs")
    return {
        "schema": "literate-ai/retained-unit-original-verification-proof@1",
        "original_evidence_identity": context.evidence.identity.uri,
        "original_set_plan_identity": context.projection.set_plan.identity.uri,
        "fresh_set_plan_identity": fresh_projection.set_plan.identity.uri,
        "original_source_bom_identity": context.run.source_bom.binding.bom_identity.uri,
        "original_resolved_bom_identity": (
            context.run.resolved_bom.binding.bom_identity.uri
        ),
        "fresh_source_bom_identity": (
            fresh_completed_run.source_bom.binding.bom_identity.uri
        ),
        "fresh_resolved_bom_identity": (
            fresh_completed_run.resolved_bom.binding.bom_identity.uri
        ),
        "fresh_retained_receipt": fresh_receipt.to_dict(),
        "fresh_planning_evidence": fresh_planning.to_dict(),
        "fresh_accepted_run_evidence": fresh_run.to_dict(),
        "fresh_link_proof": fresh_link.to_dict(),
        "equivalence_identity": equivalence.uri,
        "verified_result_index": result_index.to_dict(),
        "archives": [item.archive.to_dict() for item in results],
        "admitted": False,
        "release_authorized": False,
    }


def reopen_fresh_unit_workspace_roots(
    context, owner, planning, policy, run_reference, *, sdk_snapshots, guard
):
    """Observe validated launch anchors, bracketed by the actual fresh scope."""
    from dataclasses import replace

    owner.require_current()
    owner.admission.require_native_package_scope()
    metadata = OriginalUnitMetadataBudget(
        owner.admission.store,
        policy,
        guard,
        shared_budget=context.inputs.store,
    )
    inputs = replace(owner.inputs, store=metadata)
    roots = []
    reopened = reopen_retained_unit_run(
        inputs,
        planning,
        run_reference,
        policy=policy,
        test_receipt_policy=owner.admission.run.configured_test_policy,
        admission_guard=guard,
        current_sdk_snapshots=sdk_snapshots,
        workspace_root_observer=roots.extend,
    )
    if reopened.to_dict() != owner.admission.run.to_dict():
        raise ValueError("retained.unit.fresh-run-context-differs")
    owner.require_current()
    owner.admission.require_native_package_scope()
    return tuple(roots)

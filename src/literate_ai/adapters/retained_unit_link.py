"""Retained-origin LINK joins real acceptance, payload and selected unit closure.

Serialized proofs are assertions. Only this canonical owner can issue its local
handoff, after reopening the accepted artifact and dependency closure under the
original live admission. The frozen generated-Component LINK path is unchanged.
"""

from dataclasses import replace
from itertools import chain

from literate_ai.adapters.native_tree_shards import (
    _document,
    read_native_tree,
    retain_native_tree,
)
from literate_ai.adapters.retained_unit_graphs import (
    read_retained_unit_graph,
    retain_retained_unit_graph,
)
from literate_ai.adapters.retained_unit_inspection import (
    retained_unit_metadata_reference,
)
from literate_ai.contracts.identity import canonical_json_bytes

LINK_PROOF_SCHEMA = "literate-ai/retained-unit-link-proof@1"
_LIVE_LINK = object()


class RetainedUnitLinkHandoff:
    """An operation-local completed LINK; there is deliberately no wire decoder."""

    def __init__(self, owner, reference, *, _seal=None):
        if _seal is not _LIVE_LINK:
            raise TypeError("retained.unit.live-link-handoff-required")
        self._owner, self._reference = owner, reference

    @property
    def reference(self):
        return self._reference

    def require_current(self, owner):
        if owner is not self._owner:
            raise ValueError("retained.unit.link-handoff-selection-differs")
        expected = _verify_link_inputs(owner)
        if (
            _document(
                owner.admission.store,
                self.reference,
                owner.tree_policy["max_index_bytes"],
            )
            != expected
        ):
            raise ValueError("retained.unit.link-proof-context-differs")


def _verify_link_inputs(owner):
    owner.require_current()
    admission, projection = owner.admission, owner.projection
    admission.require_native_package_scope()
    # Re-run the canonical completed-run join. In particular, successful exits
    # cannot replace policy-complete tests and independent acceptance observations
    # bound to the exact exports and their dependency/environment identities.
    run = replace(admission.run)
    receipt, store = admission.receipt, admission.store
    payload = admission.payload_manifest
    expected_tree = owner._tree(payload)
    if expected_tree.identity != projection.graph.tree.identity:
        raise ValueError("retained.unit.link-payload-selection-differs")
    receipt_ref = retained_unit_metadata_reference(
        store,
        receipt.identity,
        maximum_bytes=min(
            64 * 1024**2, owner.provider_policy["max_provider_metadata_bytes"]
        ),
    )
    metadata_bytes = receipt_ref.size + sum(ref.size for _, ref in receipt.roots)
    memory = 16 * metadata_bytes + 256 * len(receipt.records)
    if (
        metadata_bytes > owner.provider_policy["max_provider_metadata_bytes"]
        or memory > owner.provider_policy["max_provider_memory_bytes"]
    ):
        raise ValueError("retained.unit.link-metadata-policy-exceeded")
    records = {ref.identity: ref for ref in receipt.records}
    seen = {ref.identity for _, ref in receipt.roots} | {receipt_ref.identity}

    def require_document(reference, value):
        if reference.identity != value.identity.uri:
            raise ValueError("retained.unit.link-accepted-record-differs")
        store.verify(reference)

    for name, value in (
        ("plan", run.source_bom.plan),
        ("completed-run", run),
        ("input-manifest", run.source_bom.manifest),
        ("output-manifest", run.output_manifest),
        ("payload-manifest", payload),
    ):
        require_document(receipt.root(name), value)
    require_document(receipt_ref, receipt)
    if (
        receipt.root("source-bom").identity != run.source_bom.binding.bom_identity.uri
        or receipt.root("resolved-bom").identity
        != run.resolved_bom.binding.bom_identity.uri
        or run.source_bom.plan.profile.acceptance_policy_identity is None
        or receipt.root("acceptance-policy-bytes").identity
        != run.source_bom.plan.profile.acceptance_policy_identity.uri
    ):
        raise ValueError("retained.unit.link-bom-or-verifier-differs")
    for name in ("source-bom", "resolved-bom", "acceptance-policy-bytes"):
        store.verify(receipt.root(name))
    for value in chain(
        (run.test_policy, run.configured_test_policy, run.acceptance_policy),
        run.observations,
        run.test_reports,
        run.acceptance_observations,
    ):
        reference = records.get(value.identity.uri)
        if reference is None or reference.size > 64 * 1024**2:
            raise ValueError("retained.unit.link-acceptance-record-missing")
        if reference.identity not in seen:
            metadata_bytes += reference.size
            if (
                metadata_bytes > owner.provider_policy["max_provider_metadata_bytes"]
                or 16 * metadata_bytes + 256 * len(receipt.records)
                > owner.provider_policy["max_provider_memory_bytes"]
            ):
                raise ValueError("retained.unit.link-metadata-policy-exceeded")
            seen.add(reference.identity)
        require_document(reference, value)
    policy = owner.tree_policy
    tree = read_native_tree(
        projection.set_plan.tree_index,
        store,
        expected_identity=expected_tree.identity,
        limits=owner.limits,
        maximum_descriptor_bytes=policy["max_descriptor_bytes"],
        maximum_descriptor_total_bytes=policy["max_descriptor_total_bytes"],
        maximum_index_bytes=policy["max_index_bytes"],
        maximum_metadata_memory_bytes=policy["max_metadata_memory_bytes"],
    )
    graph = read_retained_unit_graph(
        projection.set_plan.graph_index,
        store,
        expected_identity=projection.graph.identity,
        tree=tree,
        per_unit_limits=owner.per_unit_limits,
        max_units=owner.max_units,
        max_dependencies=owner.max_dependencies,
        max_metadata_bytes=owner.max_metadata_bytes,
        **policy,
    )
    if graph.partition != projection.graph.partition:
        raise ValueError("retained.unit.link-selected-graph-differs")
    # Reopened graph validation owns exact coverage, typed DAG and alias closure.
    # Compare the provider's complete projection too, including alias-only units'
    # license/specification/BOM evidence and structural directory geometry.
    reopened = replace(projection, graph=graph)
    if reopened.identity != projection.identity:
        raise ValueError("retained.unit.link-provider-projection-differs")
    for params in reopened.parameters:
        for evidence in params.evidence:
            store.verify(evidence.blob)
    for file in tree.files:
        store.verify(file.blob)
    admission.require_native_package_scope()
    return retained_unit_link_document(run, receipt, reopened, payload)


def retained_unit_link_document(run, receipt, projection, payload):
    """Canonical facts shared by live issuance and historical inspection.

    Formatting these assertions never creates a live LINK handoff or authority.
    """
    graph, tree = projection.graph, projection.graph.tree
    return {
        "schema": LINK_PROOF_SCHEMA,
        "authority_mode": "retained-project",
        "retained_plan_identity": run.source_bom.plan.identity.uri,
        "retained_receipt_identity": receipt.identity.uri,
        "completed_run_identity": run.identity.uri,
        "output_manifest_identity": run.output_manifest.identity.uri,
        "payload_manifest_identity": payload.identity.uri,
        "graph_identity": graph.identity.uri,
        "tree_identity": tree.identity.uri,
        "set_plan_identity": projection.set_plan.identity.uri,
        "provider_policy_identity": projection.set_plan.provider_policy_identity.uri,
        "packager_identity": projection.set_plan.packager_identity.uri,
        "tool_identity": projection.set_plan.tool_identity.uri,
        "target_identity": projection.set_plan.target_identity.uri,
        "unit_count": graph.index.unit_count,
        "ownership_count": graph.index.ownership_count,
        "dependency_count": graph.index.dependency_count,
        "sdk_final_check_identity": (
            run.native_sdk_final_check_identity.uri
            if run.native_sdk_final_check_identity is not None
            else None
        ),
        "link_complete": True,
        "release_authorized": False,
    }


def link_retained_unit_packages(owner):
    """Complete real retained LINK before minting an in-process PACKAGE handoff."""
    from literate_ai.adapters.retained_project_unit_packages import (
        RetainedProjectUnitPackages,
    )

    if not isinstance(owner, RetainedProjectUnitPackages):
        raise TypeError("retained.unit.live-admission-required")
    owner.require_current()
    owner.admission.require_native_package_scope()
    projection, store = owner.projection, owner.admission.store
    for reference, content in owner._metadata.items():
        if store.put_bytes(content, media_type=reference.media_type) != reference:
            raise ValueError("retained.unit.link-provider-metadata-differs")
    if (
        retain_native_tree(projection.graph.tree, store)
        != projection.set_plan.tree_index
        or retain_retained_unit_graph(projection.graph, store)
        != projection.set_plan.graph_index
    ):
        raise ValueError("retained.unit.link-graph-retention-differs")
    document = _verify_link_inputs(owner)
    content = canonical_json_bytes(document)
    if len(content) > owner.tree_policy["max_index_bytes"]:
        raise ValueError("retained.unit.link-proof-index-budget-exceeded")
    reference = store.put_bytes(content, media_type="application/json")
    owner.admission.require_native_package_scope()
    return RetainedUnitLinkHandoff(owner, reference, _seal=_LIVE_LINK)

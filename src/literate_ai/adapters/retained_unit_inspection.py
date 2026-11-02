"""Read historical package-planning assertions without minting a live owner."""

from __future__ import annotations

import stat
from pathlib import Path

from literate_ai.adapters.native_tree_shards import _document
from literate_ai.adapters.retained_project_completion import _stage_manifest
from literate_ai.adapters.retained_project_inputs import verify_retained_project_inputs
from literate_ai.adapters.retained_project_unit_packages import (
    RetainedUnitPackagePlanningInputs,
)
from literate_ai.adapters.retained_unit_planning_evidence import (
    PLANNING_EVIDENCE_SCHEMA,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.native_tree_shards import _integer
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan
from literate_ai.contracts.retained_project_receipt import RetainedProjectReceipt
from literate_ai.storage.cas import FileSystemCAS


def retained_unit_metadata_reference(store, identity, *, maximum_bytes):
    """Locate a bounded assertion without decoding it before the caller's gate."""
    if not isinstance(store, FileSystemCAS) or not isinstance(
        identity, ContentIdentity
    ):
        raise TypeError("retained.unit.typed-metadata-locator-required")
    _integer(maximum_bytes, 1, 64 * 1024**2, "unit-metadata-document")
    path = store.path_for(BlobRef(identity.digest, 0))
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum_bytes:
        raise ValueError("retained.unit.metadata-size-or-type-differs")
    return BlobRef(identity.digest, metadata.st_size, media_type="application/json")


def read_retained_unit_planning_inputs(
    source,
    source_manifest,
    plan,
    component_locks,
    receipt_reference,
    store,
    *,
    planning_evidence_reference,
    maximum_document_bytes=64 * 1024**2,
    maximum_metadata_bytes=512 * 1024**2,
    maximum_metadata_memory_bytes=1024**3,
):
    """Join current authority to bounded historical facts, with no admission.

    The current retained planner owns `plan` and its selected lock tuple. Receipt
    observations describe historical outputs; this reader never authenticates
    execution or restores SDK/runtime/LINK owners from their serialized records.
    """
    if (
        not isinstance(source, Path)
        or not isinstance(source_manifest, RetainedProjectManifest)
        or not isinstance(plan, RetainedProjectPlan)
        or not isinstance(store, FileSystemCAS)
        or not isinstance(receipt_reference, BlobRef)
        or not isinstance(planning_evidence_reference, BlobRef)
    ):
        raise TypeError("retained.unit.typed-inspection-inputs-required")
    for name, value, maximum in (
        ("document", maximum_document_bytes, 64 * 1024**2),
        ("metadata", maximum_metadata_bytes, 512 * 1024**2),
        ("memory", maximum_metadata_memory_bytes, 16 * 1024**3),
    ):
        _integer(value, 1, maximum, "unit-inspection-" + name)
    if (
        receipt_reference.size + planning_evidence_reference.size
        > maximum_metadata_bytes
        or 16 * (receipt_reference.size + planning_evidence_reference.size)
        > maximum_metadata_memory_bytes
    ):
        raise ValueError("retained.unit.inspection-metadata-policy-exceeded")
    receipt = RetainedProjectReceipt.from_dict(
        _document(store, receipt_reference, maximum_document_bytes)
    )
    # Root references are finite even when the physical record inventory is
    # large. Charge all roots before decoding any additional document.
    total = (
        receipt_reference.size
        + planning_evidence_reference.size
        + sum(ref.size for _, ref in receipt.roots)
    )
    if (
        any(ref.size > maximum_document_bytes for _, ref in receipt.roots)
        or total > maximum_metadata_bytes
        or 16 * total > maximum_metadata_memory_bytes
    ):
        raise ValueError("retained.unit.inspection-metadata-policy-exceeded")
    if (
        receipt.root("plan").identity != plan.identity.uri
        or receipt.root("input-manifest").identity != source_manifest.identity.uri
        or source_manifest.identity != plan.manifest_identity
    ):
        raise ValueError("retained.unit.inspection-current-authority-differs")
    for _, reference in receipt.roots:
        store.verify(reference)
    completed = _document(store, receipt.root("completed-run"), maximum_document_bytes)
    evidence = _document(
        store, planning_evidence_reference, min(maximum_document_bytes, 8 * 1024**2)
    )
    output = RetainedProjectManifest.from_dict(
        _document(store, receipt.root("output-manifest"), maximum_document_bytes)
    )
    payload = RetainedProjectManifest.from_dict(
        _document(store, receipt.root("payload-manifest"), maximum_document_bytes)
    )
    if (
        type(completed) is not dict
        or completed.get("schema") != "literate-ai/retained-project-completed-run@1"
        or completed.get("plan_identity") != plan.identity.uri
        or completed.get("output_manifest_identity") != output.identity.uri
        or completed.get("package_inventory") != list(plan.profile.package_inventory)
        or completed.get("sdk_admitted") is not False
        or completed.get("lifecycle_receipt") is not False
    ):
        raise ValueError("retained.unit.inspection-historical-context-differs")
    _require_observation_context(
        evidence, receipt, completed, plan, source_manifest, output
    )
    verify_retained_project_inputs(source, source_manifest)
    return RetainedUnitPackagePlanningInputs(
        source,
        store,
        receipt,
        plan,
        source_manifest,
        payload,
        output,
        component_locks,
    )


def _require_observation_context(evidence, receipt, completed, plan, source, output):
    if (
        type(evidence) is not dict
        or set(evidence)
        != {
            "schema",
            "retained_receipt_identity",
            "source_observation",
            "resolved_observation",
            "admitted",
            "release_authorized",
        }
        or evidence["schema"] != PLANNING_EVIDENCE_SCHEMA
        or evidence["retained_receipt_identity"] != receipt.identity.uri
        or evidence["admitted"] is not False
        or evidence["release_authorized"] is not False
    ):
        raise ValueError("retained.unit.planning-evidence-context-differs")
    source_observation, resolved = (
        evidence["source_observation"],
        evidence["resolved_observation"],
    )
    source_fields = {
        "schema",
        "plan_identity",
        "input_manifest_identity",
        "managed_graph_identity",
        "source_bom_identity",
        "sdk_admitted",
        "lifecycle_receipt",
    }
    resolved_fields = {
        "schema",
        "source_observation_identity",
        "output_manifest_identity",
        "resolved_bom_identity",
        "tool_observation_identity",
        "export_observation_identity",
        "sdk_admitted",
        "lifecycle_receipt",
    }
    if (
        type(source_observation) is not dict
        or type(resolved) is not dict
        or set(source_observation) != source_fields
        or set(resolved) - (resolved_fields | {"sdk_dependencies"})
        or not resolved_fields <= set(resolved)
    ):
        raise ValueError("retained.unit.closed-bom-observation-required")
    for observation in (source_observation, resolved):
        for key, value in observation.items():
            if key.endswith("_identity"):
                ContentIdentity.parse_uri(value)
        if (
            observation["sdk_admitted"] is not False
            or observation["lifecycle_receipt"] is not False
        ):
            raise ValueError("retained.unit.bom-observation-is-not-admission")
    dependencies = resolved.get("sdk_dependencies", [])
    if type(dependencies) is not list:
        raise ValueError("retained.unit.bounded-sdk-observation-required")
    for identity in dependencies:
        ContentIdentity.parse_uri(identity)
    roots, build_roots = [], ()
    for action in plan.profile.actions:
        roots.extend(action.outputs)
        if action.stage == "build":
            build_roots = tuple(roots)
    if (
        source_observation["schema"] != "literate-ai/retained-source-bom-observation@1"
        or resolved["schema"] != "literate-ai/retained-resolved-bom-observation@1"
        or source_observation["plan_identity"] != plan.identity.uri
        or source_observation["input_manifest_identity"] != source.identity.uri
        or source_observation["source_bom_identity"]
        != receipt.root("source-bom").identity
        or resolved["resolved_bom_identity"] != receipt.root("resolved-bom").identity
        or resolved["source_observation_identity"]
        != canonical_identity(source_observation).uri
        or resolved["output_manifest_identity"]
        != _stage_manifest(output, build_roots).identity.uri
        or completed.get("source_observation_identity")
        != canonical_identity(source_observation).uri
        or completed.get("resolved_observation_identity")
        != canonical_identity(resolved).uri
        or plan.profile.acceptance_policy_identity is None
        or receipt.root("acceptance-policy-bytes").identity
        != plan.profile.acceptance_policy_identity.uri
    ):
        raise ValueError("retained.unit.bom-or-verifier-observation-context-differs")

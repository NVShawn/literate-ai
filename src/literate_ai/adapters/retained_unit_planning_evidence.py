"""Opt-in historical observation joins captured only from a live retained run."""

from literate_ai.adapters.retained_project_receipt import AdmittedRetainedProject
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.native_tree_shards import _integer

PLANNING_EVIDENCE_SCHEMA = "literate-ai/retained-unit-package-planning-evidence@1"


def retain_retained_unit_planning_evidence(
    admission,
    *,
    maximum_bytes=8 * 1024**2,
    maximum_memory_bytes=256 * 1024**2,
):
    """Retain assertions without changing the existing receipt or aggregate path."""
    if not isinstance(admission, AdmittedRetainedProject):
        raise TypeError("retained.unit.live-admission-required")
    _integer(maximum_bytes, 1, 8 * 1024**2, "unit-planning-evidence-bytes")
    _integer(maximum_memory_bytes, 1, 16 * 1024**3, "unit-planning-evidence-memory")
    # Each serialized dependency identity is fixed width. Refuse before making
    # the observation list, then check the actual canonical bytes as well.
    estimate = 4096 + 80 * len(admission.run.resolved_bom.sdk_dependencies)
    if estimate > maximum_bytes or 16 * estimate > maximum_memory_bytes:
        raise ValueError("retained.unit.planning-evidence-policy-exceeded")
    admission.require_current()
    document = {
        "schema": PLANNING_EVIDENCE_SCHEMA,
        "retained_receipt_identity": admission.receipt.identity.uri,
        "source_observation": admission.run.source_bom.to_dict(),
        "resolved_observation": admission.run.resolved_bom.to_dict(),
        "admitted": False,
        "release_authorized": False,
    }
    content = canonical_json_bytes(document)
    if len(content) > maximum_bytes or 16 * len(content) > maximum_memory_bytes:
        raise ValueError("retained.unit.planning-evidence-policy-exceeded")
    reference = admission.store.put_bytes(content, media_type="application/json")
    admission.require_current()
    return reference

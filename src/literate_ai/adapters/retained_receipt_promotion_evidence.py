"""Live-captured promotion assertions; historical decoding never issues owners."""

from __future__ import annotations

import json
import os
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, replace

from literate_ai.adapters.retained_project_execution import (
    RetainedProjectExecutionWorkspace,
)
from literate_ai.adapters.retained_project_receipt import AdmittedRetainedProject
from literate_ai.adapters.retained_unit_inspection import (
    read_retained_unit_planning_inputs,
    retained_unit_metadata_reference,
)
from literate_ai.adapters.retained_unit_planning_evidence import (
    retain_retained_unit_planning_evidence,
)
from literate_ai.adapters.retained_unit_run_evidence import (
    retain_retained_unit_run_evidence,
)
from literate_ai.adapters.retained_unit_run_inspection import reopen_retained_unit_run
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes

PROMOTION_EVIDENCE_SCHEMA = "literate-ai/retained-project-promotion-evidence@1"
ADMISSION_EVIDENCE_SCHEMA = "literate-ai/retained-project-admission-assertion@1"
_CONTROL_BYTES = 65536
_ISSUER = object()
_ACTIVE = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class RetainedPromotionReadPolicy:
    maximum_metadata_bytes: int = 512 * 1024**2
    maximum_memory_bytes: int = 4 * 1024**3
    maximum_document_bytes: int = 64 * 1024**2
    max_closure_steps: int = 1_000_000
    maximum_payload_bytes: int = 64 * 1024**3
    timeout_seconds: int = 1800

    def __post_init__(self):
        for name, maximum in (
            ("maximum_metadata_bytes", 512 * 1024**2),
            ("maximum_memory_bytes", 16 * 1024**3),
            ("maximum_document_bytes", 64 * 1024**2),
            ("max_closure_steps", 1_000_000),
            ("maximum_payload_bytes", 1024**4),
            ("timeout_seconds", 10800),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("retained.promotion.finite-metadata-policy-required")

    @property
    def max_provider_metadata_bytes(self):
        return self.maximum_metadata_bytes

    @property
    def max_provider_memory_bytes(self):
        return self.maximum_memory_bytes


class _LiveCapture:
    __slots__ = ("__weakref__",)

    def __init__(self, seal):
        if seal is not _ISSUER:
            raise TypeError("retained.promotion.original-live-capture-required")

    def __copy__(self):
        raise TypeError("retained.promotion.capture-cannot-be-copied")

    def __deepcopy__(self, _memo):
        raise TypeError("retained.promotion.capture-cannot-be-copied")

    def __reduce_ex__(self, _protocol):
        raise TypeError("retained.promotion.capture-cannot-be-serialized")


def _bindings(admission, workspace):
    return (
        admission.receipt,
        admission.run,
        admission.store,
        admission.source,
        admission.current_plan,
        workspace.plan,
        workspace.source,
        workspace.current_plan,
    )


@contextmanager
def _issued_capture(admission, workspace):
    if type(admission) is not AdmittedRetainedProject or type(workspace) is not (
        RetainedProjectExecutionWorkspace
    ):
        raise TypeError("retained.promotion.original-live-admission-required")
    if (
        workspace.plan is not admission.run.source_bom.plan
        or workspace.source != admission.source
        or workspace.current_plan is not admission.current_plan
    ):
        raise ValueError("retained.promotion.live-workspace-context-differs")
    scope = _LiveCapture(_ISSUER)
    _ACTIVE[scope] = (
        os.getpid(),
        admission,
        workspace,
        _bindings(admission, workspace),
    )
    try:
        yield scope
    finally:
        _ACTIVE.pop(scope, None)


def _original(scope):
    value = _ACTIVE.get(scope) if type(scope) is _LiveCapture else None
    if value is None or value[0] != os.getpid():
        raise ValueError("retained.promotion.original-live-capture-required")
    _, admission, workspace, bound = value
    if any(
        a is not b for a, b in zip(_bindings(admission, workspace), bound, strict=True)
    ):
        raise ValueError("retained.promotion.live-capture-retargeted")
    return admission, workspace


def _current(scope):
    admission, workspace = _original(scope)
    workspace._require_current_plan()
    admission.require_native_package_scope()
    _original(scope)


def _assertion(receipt, run, payload, planning_reference, run_reference):
    return {
        "schema": ADMISSION_EVIDENCE_SCHEMA,
        "capture_origin": "original-live-retained-runtime",
        "retained_receipt_identity": receipt.identity.uri,
        "plan_identity": run.source_bom.plan.identity.uri,
        "completed_run_identity": run.identity.uri,
        "input_manifest_identity": run.source_bom.manifest.identity.uri,
        "output_manifest_identity": run.output_manifest.identity.uri,
        "payload_manifest_identity": payload.identity.uri,
        "source_observation_identity": run.source_bom.identity.uri,
        "resolved_observation_identity": run.resolved_bom.identity.uri,
        "test_policy_identity": run.test_policy.identity.uri,
        "project_test_policy_identity": run.configured_test_policy.identity.uri,
        "acceptance_policy_identity": run.acceptance_policy.identity.uri,
        "command_count": len(run.observations),
        "test_report_count": len(run.test_reports),
        "acceptance_case_count": len(run.acceptance_observations),
        "native_sdk_scope_checked": True,
        "planning_evidence": planning_reference.to_dict(),
        "accepted_run_evidence": run_reference.to_dict(),
        "admitted": False,
        "release_authorized": False,
    }


def _retain(scope, policy):
    admission, _workspace = _original(scope)
    receipt = admission.receipt
    # These original graphs/receipt records remain live while the writers copy
    # additional data. Reserve their roots and finite row storage before guards
    # or serialization; each subordinate writer receives only residual headroom.
    original_bytes = sum(reference.size for _, reference in receipt.roots)
    original_memory = 16 * original_bytes + 4096 * (
        len(receipt.records) + len(receipt.roots)
    )
    payload_bytes = sum(
        member.size for member in admission.run.source_bom.manifest.members
    )
    payload_bytes += 2 * sum(
        member.size for member in admission.run.output_manifest.members
    )
    if (
        original_bytes + 2 * _CONTROL_BYTES >= policy.maximum_metadata_bytes
        or original_memory + 32 * _CONTROL_BYTES >= policy.maximum_memory_bytes
        or payload_bytes > policy.maximum_payload_bytes
    ):
        raise ValueError("retained.promotion.capture-metadata-policy-exceeded")
    expires = time.monotonic() + policy.timeout_seconds

    def poll():
        _original(scope)
        if time.monotonic() >= expires:
            raise ValueError("retained.promotion.capture-deadline")

    def current():
        poll()
        _current(scope)
        poll()

    current()
    planning = retain_retained_unit_planning_evidence(
        admission,
        maximum_bytes=min(
            8 * 1024**2,
            policy.maximum_metadata_bytes - original_bytes - 2 * _CONTROL_BYTES,
        ),
        maximum_memory_bytes=policy.maximum_memory_bytes - original_memory,
    )
    run = retain_retained_unit_run_evidence(
        admission,
        maximum_bytes=policy.maximum_metadata_bytes
        - original_bytes
        - planning.size
        - 2 * _CONTROL_BYTES,
        maximum_memory_bytes=policy.maximum_memory_bytes
        - original_memory
        - 16 * planning.size
        - 32 * _CONTROL_BYTES,
        admission_guard=current,
        metadata_poll_guard=poll,
    )
    current()
    content = canonical_json_bytes(
        _assertion(receipt, admission.run, admission.payload_manifest, planning, run)
    )
    if len(content) > _CONTROL_BYTES:
        raise ValueError("retained.promotion.admission-descriptor-policy-exceeded")
    assertion = admission.store.put_bytes(content, media_type="application/json")
    document = {
        "schema": PROMOTION_EVIDENCE_SCHEMA,
        "retained_receipt_identity": receipt.identity.uri,
        "planning_evidence": planning.to_dict(),
        "accepted_run_evidence": run.to_dict(),
        "workspace_admission": assertion.to_dict(),
        "admitted": False,
        "release_authorized": False,
    }
    content = canonical_json_bytes(document)
    if len(content) > _CONTROL_BYTES:
        raise ValueError("retained.promotion.root-descriptor-policy-exceeded")
    current()
    reference = admission.store.put_bytes(content, media_type="application/json")
    current()
    return reference


def capture_retained_receipt_promotion_evidence(scope, *, policy=None):
    """Consume the original runtime-issued scope; records never restore one."""
    policy = RetainedPromotionReadPolicy() if policy is None else policy
    if type(policy) is not RetainedPromotionReadPolicy:
        raise TypeError("retained.promotion.typed-read-policy-required")
    _original(scope)
    return _retain(scope, policy)


def read_retained_receipt_promotion_evidence(
    store,
    receipt_identity,
    evidence_identity,
    *,
    source,
    source_manifest,
    planning,
    component_locks,
    test_receipt_policy,
    require_current,
    policy=None,
    current_sdk_snapshots=(),
):
    """Reopen complete historical assertions without issuing admission or SDK grants."""
    policy = RetainedPromotionReadPolicy() if policy is None else policy
    if type(policy) is not RetainedPromotionReadPolicy or not callable(require_current):
        raise TypeError("retained.promotion.typed-policy-and-current-guard-required")
    if not isinstance(receipt_identity, ContentIdentity) or not isinstance(
        evidence_identity, ContentIdentity
    ):
        raise TypeError("retained.promotion.exact-evidence-identities-required")
    controls, total = {}, 0
    expires = time.monotonic() + policy.timeout_seconds

    def poll():
        if time.monotonic() >= expires:
            raise ValueError("retained.promotion.inspection-deadline")

    def document(reference):
        nonlocal total
        poll()
        if (
            reference.media_type != "application/json"
            or not 0 < reference.size <= _CONTROL_BYTES
        ):
            raise ValueError("retained.promotion.closed-control-document-required")
        known = controls.get(reference.identity)
        if known is not None:
            if known[0] != reference:
                raise ValueError("retained.promotion.control-reference-differs")
            store.verify(reference)
            return known[1]
        if (
            total + reference.size > policy.maximum_metadata_bytes
            or 16 * (total + reference.size) > policy.maximum_memory_bytes
        ):
            raise ValueError("retained.promotion.control-memory-policy-exceeded")
        total += reference.size
        raw = store.get_bytes(reference)
        value = json.loads(raw)
        if canonical_json_bytes(value) != raw:
            raise ValueError("retained.promotion.canonical-control-required")
        controls[reference.identity] = (reference, value)
        return value

    require_current()
    reference = retained_unit_metadata_reference(
        store, evidence_identity, maximum_bytes=_CONTROL_BYTES
    )
    root = document(reference)
    if (
        type(root) is not dict
        or set(root)
        != {
            "schema",
            "retained_receipt_identity",
            "planning_evidence",
            "accepted_run_evidence",
            "workspace_admission",
            "admitted",
            "release_authorized",
        }
        or root["schema"] != PROMOTION_EVIDENCE_SCHEMA
        or root["retained_receipt_identity"] != receipt_identity.uri
        or root["admitted"] is not False
        or root["release_authorized"] is not False
    ):
        raise ValueError("retained.promotion.exact-root-context-required")
    planning_ref = BlobRef.from_dict(root["planning_evidence"])
    run_ref = BlobRef.from_dict(root["accepted_run_evidence"])
    admission_ref = BlobRef.from_dict(root["workspace_admission"])
    if (
        planning_ref.media_type != "application/json"
        or run_ref.media_type != "application/json"
        or not 0 < planning_ref.size <= min(policy.maximum_document_bytes, 8 * 1024**2)
        or not 0 < run_ref.size <= policy.maximum_document_bytes
    ):
        raise ValueError("retained.promotion.exact-supplement-references-required")
    assertion = document(admission_ref)
    # The current managed graph, source manifest and selected lock tuples remain
    # in the caller while historical copies are reopened. Account their encoded
    # authority before constructing another current/historical graph copy.
    graph_rows = (
        len(planning.managed_sbom_graph.components)
        + len(planning.managed_sbom_graph.edges)
        + sum(len(lock.nodes) for lock in component_locks)
    )
    caller_reserve = 4096 * (graph_rows + len(source_manifest.members))
    remaining_memory = policy.maximum_memory_bytes - 16 * total - caller_reserve
    remaining_metadata = policy.maximum_metadata_bytes - total
    if remaining_memory <= 0 or remaining_metadata <= 0:
        raise ValueError("retained.promotion.current-graph-memory-policy-exceeded")
    receipt_ref = retained_unit_metadata_reference(
        store, receipt_identity, maximum_bytes=policy.maximum_document_bytes
    )
    inputs = read_retained_unit_planning_inputs(
        source,
        source_manifest,
        planning.plan,
        component_locks,
        receipt_ref,
        store,
        planning_evidence_reference=planning_ref,
        maximum_document_bytes=policy.maximum_document_bytes,
        maximum_metadata_bytes=remaining_metadata,
        maximum_metadata_memory_bytes=remaining_memory,
    )
    run_policy = replace(
        policy,
        maximum_metadata_bytes=remaining_metadata,
        maximum_memory_bytes=remaining_memory,
    )
    # This canonical reader checks current policies, exact acceptance streams,
    # filesystem oracles, every stage manifest, resolved SBOM and SDK assertions.
    # It returns a completed-run value, never AdmittedRetainedProject.
    run = reopen_retained_unit_run(
        inputs,
        planning,
        run_ref,
        policy=run_policy,
        test_receipt_policy=test_receipt_policy,
        admission_guard=poll,
        current_sdk_snapshots=current_sdk_snapshots,
    )
    # The completed-run constructor joins topology and dependencies. Independently
    # stream every selected original/output/payload blob through the SAME canonical
    # CAS descriptor, with no writes or full-payload allocation. A manifest digest
    # alone must never satisfy workspace-admission.
    count = 0

    class Discard:
        def write(self, block):
            nonlocal count
            poll()
            count += len(block)
            if count > policy.maximum_payload_bytes:
                raise ValueError("retained.promotion.payload-byte-policy-exceeded")
            return len(block)

    for manifest in (
        inputs.source_manifest,
        inputs.output_manifest,
        inputs.payload_manifest,
    ):
        for member in manifest.members:
            poll()
            if count + member.size > policy.maximum_payload_bytes:
                raise ValueError("retained.promotion.payload-byte-policy-exceeded")
            store.stream_to(BlobRef(member.sha256, member.size), Discard())
    expected = _assertion(
        inputs.receipt, run, inputs.payload_manifest, planning_ref, run_ref
    )
    if canonical_json_bytes(assertion) != canonical_json_bytes(expected):
        raise ValueError("retained.promotion.original-admission-assertion-differs")
    for snapshot in current_sdk_snapshots:
        poll()
        snapshot.require_unchanged()
    require_current()
    poll()
    return inputs, run, admission_ref.identity

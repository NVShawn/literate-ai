"""Prepare and retain exact signed receipt custody before current-map admission."""

import hashlib
import json
from dataclasses import dataclass, field, replace

from literate_ai.contracts import (
    ProjectTestReceiptFinalizedCandidate,
    canonical_json_bytes,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.security.evidence.current import PLAN_MEDIA_TYPE, CurrentEvidenceMap
from literate_ai.security.evidence.dsse import MAX_ENVELOPE_BYTES, DsseEnvelope
from literate_ai.security.evidence.graph import EvidenceGraphLimits
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from literate_ai.security.evidence.records import EvidenceLocator, EvidenceMatrix
from literate_ai.security.evidence.retention import (
    CheckedRetainedEvidenceGraph,
    SignedEvidenceRetention,
)
from literate_ai.security.evidence.statements import EvidenceStatement
from literate_ai.security.evidence.storage import (
    EvidenceReadLimits,
    EvidenceStorageError,
    verify_evidence_bytes,
)
from literate_ai.test_receipts import (
    MAXIMUM_PROJECT_TEST_RECEIPT_BYTES,
    validate_project_test_receipt_finalized_value,
)

from .evidence_verification import _plan, verify_retained_evidence_graph

_DEFAULT_GRAPH_LIMITS = EvidenceGraphLimits()
_DEFAULT_READ_LIMITS = EvidenceReadLimits()


@dataclass(frozen=True, slots=True)
class PreparedCurrentEvidence:
    """Verified bytes ready for immutable storage, not permission to replace current."""

    current: CurrentEvidenceMap
    objects: tuple[tuple[BlobRef, bytes], ...] = field(repr=False)
    verification: CheckedRetainedEvidenceGraph = field(repr=False)


def prepare_current_evidence(
    project,
    *,
    plan: EvidenceVerificationPlan,
    policy,
    resolver,
    retention,
    current_state,
    project_authority,
    limits: EvidenceGraphLimits = _DEFAULT_GRAPH_LIMITS,
) -> PreparedCurrentEvidence:
    """Validate the receipt matrix and preserve all verification roots and bytes.

    Authority providers, policy and plan are trusted independent caller inputs.
    No file or receipt is written. Later admission must refresh authority again.
    """

    if (
        not isinstance(plan, EvidenceVerificationPlan)
        or project_authority() != plan.project_authority
    ):
        raise ValueError("evidence.current.project-mismatch")
    root_requirement = next(r for r in plan.requirements if r.envelope == plan.root)
    if root_requirement.expectation.predicate_type != EvidenceMatrix.SCHEMA:
        raise ValueError("evidence.current.matrix-required")
    receipt_ref = root_requirement.expectation.subject
    if (
        receipt_ref.media_type != "application/json"
        or receipt_ref.size > MAXIMUM_PROJECT_TEST_RECEIPT_BYTES
    ):
        raise ValueError("evidence.current.receipt-invalid")
    plan_bytes = canonical_json_bytes(plan.to_dict()) + b"\n"
    if len(plan_bytes) > 4 * 1024 * 1024 or limits.reads.maximum_objects < 2:
        raise ValueError("evidence.current.plan-limit")
    remaining = limits.reads.maximum_total_bytes - len(plan_bytes)
    if remaining < 1:
        raise ValueError("evidence.current.plan-limit")
    graph_limits = replace(
        limits,
        reads=replace(
            limits.reads,
            maximum_objects=limits.reads.maximum_objects - 1,
            maximum_total_bytes=remaining,
            maximum_blob_bytes=min(limits.reads.maximum_blob_bytes, remaining),
        ),
    )
    checked = verify_retained_evidence_graph(
        plan.root,
        requirements=plan.requirements,
        policy=policy,
        resolver=resolver,
        retention=retention,
        current_state=current_state,
        limits=graph_limits,
    )
    objects = {
        item.reference.identity: (item.reference, item.content)
        for item in checked.graph.objects
    }
    receipt_bytes = objects[receipt_ref.identity][1]
    finalized = ProjectTestReceiptFinalizedCandidate.from_dict(
        json.loads(receipt_bytes)
    )
    # Exact canonical bytes prohibit duplicate keys, whitespace variants and
    # alternate serialization from silently becoming another receipt identity.
    if receipt_bytes != canonical_json_bytes(finalized.to_dict()) + b"\n":
        raise ValueError("evidence.current.receipt-not-canonical")
    validate_project_test_receipt_finalized_value(
        project, finalized, project_revision_identity=plan.project_authority
    )
    plan_ref = BlobRef(
        hashlib.sha256(plan_bytes).hexdigest(),
        len(plan_bytes),
        media_type=PLAN_MEDIA_TYPE,
    )
    if plan_ref.identity in objects and objects[plan_ref.identity] != (
        plan_ref,
        plan_bytes,
    ):
        raise ValueError("evidence.current.reference-conflict")
    objects[plan_ref.identity] = (plan_ref, plan_bytes)
    roots = []
    for item in checked.retention:
        ref = item.claim.reference
        if ref.identity in objects and objects[ref.identity] != (
            ref,
            item.claim.envelope,
        ):
            raise ValueError("evidence.current.reference-conflict")
        objects[ref.identity] = (ref, item.claim.envelope)
        roots.append(ref)
    if project_authority() != plan.project_authority:
        raise ValueError("evidence.current.authority-changed")
    current = CurrentEvidenceMap(
        plan.project_authority,
        policy.identity,
        receipt_ref,
        plan_ref,
        plan.root,
        tuple(sorted(roots, key=lambda r: r.identity)),
    )
    return PreparedCurrentEvidence(
        current, tuple(objects[key] for key in sorted(objects)), checked
    )


def retain_prepared_current_evidence(
    prepared: PreparedCurrentEvidence,
    store,
    *,
    limits: EvidenceReadLimits = _DEFAULT_READ_LIMITS,
) -> CurrentEvidenceMap:
    """Persist immutable bytes with readback; never write a mutable current pointer.

    Partial failure can leave unreferenced immutable objects, but no returned map or
    current receipt. The returned index still requires fresh verification on use.
    """

    if not isinstance(prepared, PreparedCurrentEvidence):
        raise ValueError("evidence.current.prepared-invalid")
    if (
        not isinstance(prepared.current, CurrentEvidenceMap)
        or not isinstance(prepared.objects, tuple)
        or not 1 <= len(prepared.objects) <= limits.maximum_objects
    ):
        raise ValueError("evidence.current.bundle-limit")
    if sum(ref.size for ref, _ in prepared.objects) > limits.maximum_total_bytes:
        raise ValueError("evidence.current.bundle-limit")
    refs = {}
    for reference, content in prepared.objects:
        limits.require_reference(reference)
        verify_evidence_bytes(reference, content)
        if reference.identity in refs:
            raise ValueError("evidence.current.reference-conflict")
        refs[reference.identity] = reference
    required = (
        prepared.current.receipt,
        prepared.current.plan,
        prepared.current.matrix,
        *prepared.current.retention_roots,
    )
    if any(refs.get(ref.identity) != ref for ref in required):
        raise ValueError("evidence.current.bundle-incomplete")
    if prepared.current.plan.size > 4 * 1024 * 1024:
        raise ValueError("evidence.current.plan-limit")
    contents = {ref.identity: content for ref, content in prepared.objects}
    plan_bytes = contents[prepared.current.plan.identity]
    plan = EvidenceVerificationPlan.from_dict(json.loads(plan_bytes))
    if (
        plan_bytes != canonical_json_bytes(plan.to_dict()) + b"\n"
        or plan.project_authority != prepared.current.project_authority
        or plan.root != prepared.current.matrix
    ):
        raise ValueError("evidence.current.plan-mismatch")
    root = next(r for r in plan.requirements if r.envelope == plan.root)
    if (
        root.expectation.predicate_type != EvidenceMatrix.SCHEMA
        or root.expectation.subject != prepared.current.receipt
    ):
        raise ValueError("evidence.current.receipt-mismatch")
    all_required = list(required)
    for requirement in plan.requirements:
        all_required.extend(
            (
                requirement.envelope,
                *(c.blob for c in requirement.children),
                *(r for _, r in requirement.artifacts),
            )
        )
    if set(refs) != {ref.identity for ref in all_required} or any(
        refs.get(ref.identity) != ref for ref in all_required
    ):
        raise ValueError("evidence.current.bundle-incomplete")
    for reference, content in prepared.objects:
        observed = store.put_bytes(content, media_type=reference.media_type)
        if observed != reference:
            raise EvidenceStorageError("evidence.current.publication-mismatch")
        verify_evidence_bytes(reference, store.get_bytes(reference))
    return prepared.current


def verify_current_evidence(
    project,
    current: CurrentEvidenceMap,
    *,
    plan: EvidenceVerificationPlan,
    policy,
    resolver,
    bundle_store,
    current_state,
    project_authority,
    limits: EvidenceGraphLimits = _DEFAULT_GRAPH_LIMITS,
) -> PreparedCurrentEvidence:
    """Refresh signed receipt custody; a stored map never supplies its own authority.

    The bundle store supplies detached roots, not a substitute for the signed
    graph's explicitly configured stores. No stale authentication flag is consumed.
    """

    if not isinstance(current, CurrentEvidenceMap) or not isinstance(
        plan, EvidenceVerificationPlan
    ):
        raise ValueError("evidence.current.invalid")
    if (
        current.project_authority != plan.project_authority
        or current.trust_policy != policy.identity
        or current.matrix != plan.root
    ):
        raise ValueError("evidence.current.authority-mismatch")
    if project_authority() != plan.project_authority:
        raise ValueError("evidence.current.project-mismatch")
    plan_bytes = canonical_json_bytes(plan.to_dict()) + b"\n"
    if len(plan_bytes) > 4 * 1024 * 1024:
        raise ValueError("evidence.current.plan-limit")
    plan_ref = BlobRef(
        hashlib.sha256(plan_bytes).hexdigest(),
        len(plan_bytes),
        media_type=PLAN_MEDIA_TYPE,
    )
    if current.plan != plan_ref:
        raise ValueError("evidence.current.plan-mismatch")
    nodes, references = _plan(plan.root, plan.requirements, limits)
    root = nodes[plan.root.identity]
    if (
        root.expectation.predicate_type != EvidenceMatrix.SCHEMA
        or root.expectation.subject != current.receipt
    ):
        raise ValueError("evidence.current.receipt-mismatch")
    # Preflight the combined graph, plan and detached roots before any store read.
    for reference in (plan_ref, *current.retention_roots):
        limits.reads.require_reference(reference)
        if reference != plan_ref and reference.size > MAX_ENVELOPE_BYTES:
            raise ValueError("evidence.current.retention-limit")
        if references.setdefault(reference.identity, reference) != reference:
            raise ValueError("evidence.current.reference-conflict")
    if (
        len(references) > limits.reads.maximum_objects
        or sum(r.size for r in references.values()) > limits.reads.maximum_total_bytes
    ):
        raise ValueError("evidence.current.bundle-limit")
    observed_plan = verify_evidence_bytes(plan_ref, bundle_store.get_bytes(plan_ref))
    if observed_plan != plan_bytes:
        raise ValueError("evidence.current.plan-mismatch")
    retention = []
    for reference in current.retention_roots:
        content = verify_evidence_bytes(reference, bundle_store.get_bytes(reference))
        statement = EvidenceStatement.from_bytes(
            DsseEnvelope.from_bytes(content).payload
        )
        if not isinstance(statement.predicate, EvidenceLocator):
            raise ValueError("evidence.current.retention-invalid")
        retention.append(SignedEvidenceRetention(statement.predicate, content))
    prepared = prepare_current_evidence(
        project,
        plan=plan,
        policy=policy,
        resolver=resolver,
        retention=tuple(retention),
        current_state=current_state,
        project_authority=project_authority,
        limits=limits,
    )
    if prepared.current != current:
        raise ValueError("evidence.current.map-mismatch")
    return prepared

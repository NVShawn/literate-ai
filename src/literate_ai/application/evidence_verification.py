"""Authenticate a complete planned run graph, without promoting a receipt."""

from __future__ import annotations

from collections.abc import Callable

from literate_ai.application.evidence_resolution import EvidenceResolutionSession
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.evidence.checks import (
    EvidenceTrustError,
    check_retention_evidence,
    check_run_evidence,
)
from literate_ai.security.evidence.dsse import DsseEnvelope
from literate_ai.security.evidence.graph import (
    CheckedRunEvidenceGraph,
    EvidenceGraphLimits,
    EvidenceRunRequirement,
    EvidenceVerificationState,
)
from literate_ai.security.evidence.records import (
    DerivationRun,
    EvidenceLocator,
    EvidenceMatrix,
    PlatformRun,
)
from literate_ai.security.evidence.retention import (
    CheckedEvidenceRetention,
    CheckedRetainedEvidenceGraph,
    SignedEvidenceRetention,
)
from literate_ai.security.evidence.storage import EvidenceResolver, EvidenceStorageError
from literate_ai.security.evidence.trust import EvidenceTrustPolicy

_DEFAULT_GRAPH_LIMITS = EvidenceGraphLimits()


def _plan(root, requirements, limits):
    if (
        not isinstance(limits, EvidenceGraphLimits)
        or not isinstance(requirements, tuple)
        or not 1 <= len(requirements) <= min(1024, limits.reads.maximum_objects)
        or any(not isinstance(item, EvidenceRunRequirement) for item in requirements)
    ):
        raise EvidenceTrustError("evidence.graph.plan-invalid")
    limits.reads.require_reference(root)
    nodes = {item.envelope.identity: item for item in requirements}
    if len(nodes) != len(requirements) or root.identity not in nodes:
        raise EvidenceTrustError("evidence.graph.plan-invalid")
    if nodes[root.identity].envelope != root:
        raise EvidenceTrustError("evidence.graph.reference-conflict")
    references = {}
    for item in requirements:
        for reference in (
            item.envelope,
            *(child.blob for child in item.children),
            *(blob for _, blob in item.artifacts),
        ):
            limits.reads.require_reference(reference)
            if references.setdefault(reference.identity, reference) != reference:
                raise EvidenceTrustError("evidence.graph.reference-conflict")
        kind = item.expectation.predicate_type
        child_kind = (
            PlatformRun.SCHEMA
            if kind == EvidenceMatrix.SCHEMA
            else DerivationRun.SCHEMA
        )
        for child in item.children:
            required = nodes.get(child.blob.identity)
            if required is None or required.envelope != child.blob:
                raise EvidenceTrustError("evidence.graph.child-missing")
            if required.expectation.predicate_type != child_kind:
                raise EvidenceTrustError("evidence.graph.child-role-mismatch")
    seen = set()
    pending = [root.identity]
    while pending:
        identity = pending.pop()
        if identity not in seen:
            seen.add(identity)
            pending.extend(child.blob.identity for child in nodes[identity].children)
    if seen != set(nodes):
        raise EvidenceTrustError("evidence.graph.unreachable-run")
    if len(references) > limits.reads.maximum_objects:
        raise EvidenceStorageError("evidence.storage.object-limit")
    if sum(ref.size for ref in references.values()) > limits.reads.maximum_total_bytes:
        raise EvidenceStorageError("evidence.storage.total-limit")
    return nodes, references


def _assert_edges(predicate, requirement):
    artifacts = [("subject", predicate.subject)]
    children = ()
    if isinstance(predicate, EvidenceMatrix):
        children = predicate.cells
    elif isinstance(predicate, PlatformRun):
        children = (("derivation", predicate.derivation),)
        artifacts.append(("environment", predicate.environment))
        artifacts.extend(("check/" + item.name, item.blob) for item in predicate.checks)
    else:
        artifacts.append(("journal", predicate.journal))
        artifacts.extend(("input/" + item.name, item.blob) for item in predicate.inputs)
    if isinstance(predicate, EvidenceMatrix):
        children = tuple((item.name, item.blob) for item in children)
    if children != tuple((item.name, item.blob) for item in requirement.children):
        raise EvidenceTrustError("evidence.graph.child-mismatch")
    if tuple(sorted(artifacts)) != requirement.artifacts:
        raise EvidenceTrustError("evidence.graph.artifact-mismatch")


def _verify_graph(
    root: BlobRef,
    *,
    requirements: tuple[EvidenceRunRequirement, ...],
    resolver: EvidenceResolver,
    locators: tuple[EvidenceLocator, ...],
    policy: EvidenceTrustPolicy,
    current_state: Callable[[], EvidenceVerificationState],
    limits: EvidenceGraphLimits,
    retention: tuple[SignedEvidenceRetention, ...] | None,
) -> tuple[CheckedRunEvidenceGraph, tuple[CheckedEvidenceRetention, ...]]:
    """Share graph, optional retention and refreshed-state checks under one budget."""
    if not isinstance(policy, EvidenceTrustPolicy) or not callable(current_state):
        raise EvidenceTrustError("evidence.graph.configuration-invalid")
    nodes, references = _plan(root, requirements, limits)
    if (
        not isinstance(locators, tuple)
        or not 1 <= len(locators) <= limits.reads.maximum_objects
    ):
        raise EvidenceStorageError("evidence.storage.object-limit")
    locations = {}
    for locator in locators:
        if not isinstance(locator, EvidenceLocator):
            raise EvidenceStorageError("evidence.storage.locator-invalid")
        if references.get(locator.subject.identity) != locator.subject:
            raise EvidenceStorageError("evidence.storage.locator-mismatch")
        key = (locator.subject.identity, locator.store_id)
        if locations.setdefault(key, locator) != locator:
            raise EvidenceStorageError("evidence.storage.locator-conflict")
    if {identity for identity, _ in locations} != set(references):
        raise EvidenceStorageError("evidence.storage.not-found")

    claims = ()
    if retention is not None:
        claims = tuple(
            sorted(
                retention,
                key=lambda item: (item.locator.subject.identity, item.locator.store_id),
            )
        )
        keys = tuple(
            (item.locator.subject.identity, item.locator.store_id) for item in claims
        )
        if len(set(keys)) != len(keys):
            raise EvidenceTrustError("evidence.retention.claim-duplicate")
        bounded = dict(references)
        total_bytes = sum(ref.size for ref in bounded.values())
        for claim in claims:
            reference = claim.reference
            limits.reads.require_reference(reference)
            previous = bounded.get(reference.identity)
            if previous is not None and previous != reference:
                raise EvidenceTrustError("evidence.graph.reference-conflict")
            if previous is None:
                if len(bounded) >= limits.reads.maximum_objects:
                    raise EvidenceStorageError("evidence.storage.object-limit")
                total_bytes += reference.size
                if total_bytes > limits.reads.maximum_total_bytes:
                    raise EvidenceStorageError("evidence.storage.total-limit")
                bounded[reference.identity] = reference
    owners = {identity: set() for identity in references}
    for identity, requirement in nodes.items():
        for reference in (
            requirement.envelope,
            *(child.blob for child in requirement.children),
            *(ref for _, ref in requirement.artifacts),
        ):
            owners[reference.identity].add(identity)

    def state():
        try:
            result = current_state()
        except Exception:
            raise EvidenceTrustError("evidence.graph.state-unavailable") from None
        if not isinstance(result, EvidenceVerificationState):
            raise EvidenceTrustError("evidence.graph.state-invalid")
        return result

    routing = {identity: [] for identity in references}
    for (identity, _), locator in sorted(locations.items()):
        routing[identity].append(locator)
    routing = {identity: tuple(items) for identity, items in routing.items()}
    initial = state()
    session = EvidenceResolutionSession(resolver, limits=limits.reads)
    objects = {}
    envelopes = {}
    signature_checks = 0
    claim_envelopes = tuple(DsseEnvelope.from_bytes(claim.envelope) for claim in claims)

    def read(reference):
        item = session.resolve_many((reference,), locators=routing[reference.identity])[
            0
        ]
        objects[reference.identity] = item
        return item.content

    def charge(envelope):
        nonlocal signature_checks
        # One conservative budget for every configured key/signature pair in both
        # passes, covering graph assertions and detached retention roots together.
        signature_checks += len(envelope.signatures) * len(policy.signers)
        if signature_checks > limits.maximum_signature_checks:
            raise EvidenceTrustError("evidence.graph.signature-limit")

    def check(identity, snapshot):
        envelope = envelopes[identity]
        charge(envelope)
        return check_run_evidence(
            envelope,
            expectation=nodes[identity].expectation,
            policy=policy,
            revocations=snapshot.revocations,
            now=snapshot.now,
        )

    def check_claims(snapshot):
        checked_claims = []
        for claim, envelope in zip(claims, claim_envelopes, strict=True):
            checked_runs = []
            for identity in sorted(owners[claim.locator.subject.identity]):
                charge(envelope)
                checked = check_retention_evidence(
                    envelope,
                    subject=claim.locator.subject,
                    store_id=claim.locator.store_id,
                    expectation=nodes[identity].expectation,
                    policy=policy,
                    revocations=snapshot.revocations,
                    now=snapshot.now,
                )
                if checked.authenticated.statement.predicate != claim.locator:
                    raise EvidenceTrustError("evidence.retention.locator-mismatch")
                checked_runs.append((nodes[identity].envelope, checked))
            checked_claims.append(CheckedEvidenceRetention(claim, tuple(checked_runs)))
        return tuple(checked_claims)

    check_claims(initial)
    pending = [root.identity]
    while pending:
        identity = pending.pop()
        if identity in envelopes:
            continue
        requirement = nodes[identity]
        envelope = DsseEnvelope.from_bytes(read(requirement.envelope))
        envelopes[identity] = envelope
        checked = check(identity, initial)
        _assert_edges(checked.authenticated.statement.predicate, requirement)
        for _, reference in requirement.artifacts:
            read(reference)
        pending.extend(child.blob.identity for child in reversed(requirement.children))
    if set(envelopes) != set(nodes) or set(objects) != set(references):
        raise EvidenceTrustError("evidence.graph.incomplete")
    final = state()
    if (
        final.now < initial.now
        or final.revocations.issued_at < initial.revocations.issued_at
    ):
        raise EvidenceTrustError("evidence.graph.state-rollback")
    runs = tuple(
        (nodes[identity].envelope, check(identity, final)) for identity in sorted(nodes)
    )
    checked_retention = check_claims(final)
    graph = CheckedRunEvidenceGraph(
        root,
        canonical_identity([nodes[key].to_dict() for key in sorted(nodes)]),
        policy.identity,
        initial,
        final,
        runs,
        tuple(objects[key] for key in sorted(objects)),
    )
    return graph, checked_retention


def verify_run_evidence_graph(
    root: BlobRef,
    *,
    requirements: tuple[EvidenceRunRequirement, ...],
    resolver: EvidenceResolver,
    locators: tuple[EvidenceLocator, ...],
    policy: EvidenceTrustPolicy,
    current_state: Callable[[], EvidenceVerificationState],
    limits: EvidenceGraphLimits = _DEFAULT_GRAPH_LIMITS,
) -> CheckedRunEvidenceGraph:
    """Authenticate all planned run/artifact bytes; locator claims remain unchecked.

    Current trusted state is requested before I/O and after all reads. Use the
    retained variant when admission also requires authorized store/retention claims.
    """
    graph, _ = _verify_graph(
        root,
        requirements=requirements,
        resolver=resolver,
        locators=locators,
        policy=policy,
        current_state=current_state,
        limits=limits,
        retention=None,
    )
    return graph


def verify_retained_evidence_graph(
    root: BlobRef,
    *,
    requirements: tuple[EvidenceRunRequirement, ...],
    resolver: EvidenceResolver,
    retention: tuple[SignedEvidenceRetention, ...],
    policy: EvidenceTrustPolicy,
    current_state: Callable[[], EvidenceVerificationState],
    limits: EvidenceGraphLimits = _DEFAULT_GRAPH_LIMITS,
) -> CheckedRetainedEvidenceGraph:
    """Authenticate complete retention coverage and retrieve the planned graph.

    Detached roots count toward the same unique object/byte budget as fetched graph
    data. Claims are authenticated for every referencing run before I/O and again
    against fresh trusted time/revocations after retrieval. Their exact signed
    metadata supplies all routing; no unsigned mirror can satisfy this verification.
    Availability is observed during this call, not guaranteed after it returns.
    """
    if (
        not isinstance(limits, EvidenceGraphLimits)
        or not isinstance(retention, tuple)
        or not 1 <= len(retention) <= limits.reads.maximum_objects
        or any(not isinstance(item, SignedEvidenceRetention) for item in retention)
    ):
        raise EvidenceTrustError("evidence.retention.claims-invalid")
    graph, checked = _verify_graph(
        root,
        requirements=requirements,
        resolver=resolver,
        locators=tuple(item.locator for item in retention),
        policy=policy,
        current_state=current_state,
        limits=limits,
        retention=retention,
    )
    return CheckedRetainedEvidenceGraph(graph, checked)

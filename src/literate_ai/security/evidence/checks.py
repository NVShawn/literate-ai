"""Check signed assertions against independent policy; never promote receipts."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.contracts._validation import int_value
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .dsse import DsseEnvelope
from .records import EvidenceLocator, EvidenceMatrix
from .statements import AuthenticatedEvidenceStatement, verify_evidence_statement
from .trust import (
    EvidenceRevocations,
    EvidenceSignerRule,
    EvidenceTrustPolicy,
    RunEvidenceExpectation,
)


class EvidenceTrustError(ValueError):
    """Stable refusal without echoing payloads or authority identifiers."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CheckedEvidenceAssertion:
    """An in-memory assertion check, not proof of complete or retained execution."""

    authenticated: AuthenticatedEvidenceStatement
    policy_identity: ContentIdentity
    revocations_identity: ContentIdentity
    expectation_identity: ContentIdentity
    checked_at: int
    qualified_signer_key_identities: tuple[str, ...]


def _eligible_signers(
    expectation: RunEvidenceExpectation,
    policy: EvidenceTrustPolicy,
    revocations: EvidenceRevocations,
    now: int,
    predicate_type: str,
    store_id: str | None = None,
) -> tuple[EvidenceSignerRule, ...]:
    if (
        not isinstance(expectation, RunEvidenceExpectation)
        or not isinstance(policy, EvidenceTrustPolicy)
        or not isinstance(revocations, EvidenceRevocations)
    ):
        raise EvidenceTrustError("evidence.trust.configuration-invalid")
    int_value(now, "evidence.trust.now")
    if (
        now < revocations.issued_at
        or now >= revocations.expires_at
        or now - revocations.issued_at > policy.maximum_revocation_age_seconds
    ):
        raise EvidenceTrustError("evidence.trust.revocations-stale")
    if expectation.invocation_id in revocations.invocation_ids:
        raise EvidenceTrustError("evidence.trust.invocation-revoked")
    eligible = tuple(
        rule
        for rule in policy.signers
        if rule.valid_from <= now < rule.valid_until
        and rule.key_identity not in revocations.key_identities
        and rule.issuer not in revocations.issuers
        and expectation.repository in rule.repositories
        and expectation.workflow in rule.workflows
        and expectation.target in rule.targets
        and predicate_type in rule.predicate_types
        and (store_id is None or store_id in rule.store_ids)
    )
    if len(eligible) < policy.minimum_signatures:
        raise EvidenceTrustError("evidence.trust.signer-threshold-unmet")
    return eligible


def check_run_assertion(
    authenticated: AuthenticatedEvidenceStatement,
    *,
    expectation: RunEvidenceExpectation,
    now: int,
    maximum_clock_skew_seconds: int,
    maximum_run_age_seconds: int,
    maximum_run_duration_seconds: int,
) -> None:
    """Check run semantics after authentication; no signer grant or admission.

    The caller separately establishes signature/key authority. Keeping semantics
    shared lets pinned-key and live CI identity profiles require the same exact run.
    """

    if not isinstance(authenticated, AuthenticatedEvidenceStatement) or not isinstance(
        expectation, RunEvidenceExpectation
    ):
        raise EvidenceTrustError("evidence.trust.configuration-invalid")
    for name, value in (
        ("now", now),
        ("clock_skew", maximum_clock_skew_seconds),
        ("run_age", maximum_run_age_seconds),
        ("run_duration", maximum_run_duration_seconds),
    ):
        int_value(value, "evidence.trust." + name)
    predicate = authenticated.statement.predicate
    if predicate.SCHEMA != expectation.predicate_type:
        raise EvidenceTrustError("evidence.trust.predicate-mismatch")
    if predicate.subject != expectation.subject:
        raise EvidenceTrustError("evidence.trust.subject-mismatch")
    context = predicate.context
    for field in ("invocation_id", "repository", "revision", "workflow", "target"):
        if getattr(context, field) != getattr(expectation, field):
            raise EvidenceTrustError(f"evidence.trust.{field}-mismatch")
    if (
        context.started_at < expectation.earliest_start
        or context.finished_at > expectation.latest_finish
    ):
        raise EvidenceTrustError("evidence.trust.run-window-mismatch")
    if context.finished_at - now > maximum_clock_skew_seconds:
        raise EvidenceTrustError("evidence.trust.run-in-future")
    if now - context.finished_at > maximum_run_age_seconds:
        raise EvidenceTrustError("evidence.trust.run-stale")
    if context.finished_at - context.started_at > maximum_run_duration_seconds:
        raise EvidenceTrustError("evidence.trust.run-duration-exceeded")
    if isinstance(predicate, EvidenceMatrix):
        if predicate.required_cells != expectation.required_cells:
            raise EvidenceTrustError("evidence.trust.matrix-coverage-mismatch")
    elif predicate.status != "passed":
        raise EvidenceTrustError("evidence.trust.run-not-passed")


def check_run_evidence(
    envelope: DsseEnvelope,
    *,
    expectation: RunEvidenceExpectation,
    policy: EvidenceTrustPolicy,
    revocations: EvidenceRevocations,
    now: int,
) -> CheckedEvidenceAssertion:
    """Require an eligible signature threshold and exact independently required run.

    Revocations and expectations are trusted verifier inputs, not values inferred
    from the assertion. Repeated read-only checks are idempotent; a prior invocation
    cannot satisfy a new invocation's independently supplied expectation.
    """

    if not isinstance(expectation, RunEvidenceExpectation):
        raise EvidenceTrustError("evidence.trust.configuration-invalid")
    eligible = _eligible_signers(
        expectation, policy, revocations, now, expectation.predicate_type
    )
    authenticated = verify_evidence_statement(
        envelope,
        trusted_public_keys=tuple(rule.public_key for rule in eligible),
        minimum_signatures=policy.minimum_signatures,
    )
    check_run_assertion(
        authenticated,
        expectation=expectation,
        now=now,
        maximum_clock_skew_seconds=policy.maximum_clock_skew_seconds,
        maximum_run_age_seconds=policy.maximum_run_age_seconds,
        maximum_run_duration_seconds=policy.maximum_run_duration_seconds,
    )
    context = authenticated.statement.predicate.context
    valid_keys = {
        rule.key_identity.uri
        for rule in eligible
        if rule.valid_from <= context.started_at
        and context.finished_at < rule.valid_until
    }
    qualified = tuple(
        sorted(valid_keys.intersection(authenticated.signer_key_identities))
    )
    if len(qualified) < policy.minimum_signatures:
        raise EvidenceTrustError("evidence.trust.signer-run-window-mismatch")
    return CheckedEvidenceAssertion(
        authenticated,
        policy.identity,
        revocations.identity,
        canonical_identity(expectation.to_dict()),
        now,
        qualified,
    )


def check_retention_evidence(
    envelope: DsseEnvelope,
    *,
    subject: BlobRef,
    store_id: str,
    expectation: RunEvidenceExpectation,
    policy: EvidenceTrustPolicy,
    revocations: EvidenceRevocations,
    now: int,
) -> CheckedEvidenceAssertion:
    """Authenticate an authorized storage claim; retrieving the bytes is separate."""

    # Validate caller-owned object/store selectors without trusting a claimed expiry.
    EvidenceLocator(store_id, subject, 0)
    eligible = _eligible_signers(
        expectation, policy, revocations, now, EvidenceLocator.SCHEMA, store_id
    )
    authenticated = verify_evidence_statement(
        envelope,
        trusted_public_keys=tuple(rule.public_key for rule in eligible),
        minimum_signatures=policy.minimum_signatures,
    )
    locator = authenticated.statement.predicate
    if not isinstance(locator, EvidenceLocator):
        raise EvidenceTrustError("evidence.trust.predicate-mismatch")
    if locator.subject != subject or locator.store_id != store_id:
        raise EvidenceTrustError("evidence.trust.retention-subject-mismatch")
    if locator.retained_until - now < policy.minimum_retention_seconds:
        raise EvidenceTrustError("evidence.trust.retention-too-short")
    expected = {
        "run": expectation.to_dict(),
        "subject": subject.to_dict(),
        "store_id": store_id,
    }
    return CheckedEvidenceAssertion(
        authenticated,
        policy.identity,
        revocations.identity,
        canonical_identity(expected),
        now,
        authenticated.signer_key_identities,
    )

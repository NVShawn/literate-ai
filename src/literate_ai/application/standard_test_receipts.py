"""Project test-receipt projection for an accepted Standard lifecycle."""

from __future__ import annotations

from collections.abc import Iterable

from literate_ai.contracts.identity import (
    ContentIdentity,
    VersionedContentRef,
    canonical_identity,
)
from literate_ai.contracts.rebuild_cache import rebuild_project_authority_identity
from literate_ai.contracts.standard_lifecycle_policy import (
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
    StandardLifecyclePolicy,
)
from literate_ai.contracts.testing import (
    PROJECT_TEST_OUTCOME_PASSED,
    PROJECT_TEST_SUITE_KIND,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
)

from .standard_project_lifecycle import (
    StandardNodeLifecycleResult,
    StandardProjectLifecycleResult,
)


class StandardTestReceiptProjectionError(ValueError):
    """A Standard result cannot honestly satisfy the project receipt boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _error(code: str, message: str) -> None:
    raise StandardTestReceiptProjectionError(code, message)


def _aggregate(kind: str, identities: Iterable[ContentIdentity]) -> ContentIdentity:
    ordered = tuple(sorted(identities, key=lambda item: item.uri))
    if not ordered:
        _error(
            "standard_receipt.evidence_missing",
            f"Standard lifecycle omitted {kind} evidence",
        )
    return canonical_identity(
        {
            "schema": "literate-ai/standard-project-evidence-set@1",
            "kind": kind,
            "members": [item.uri for item in ordered],
        }
    )


def _strict_nodes(
    lifecycle: StandardProjectLifecycleResult,
) -> tuple[StandardNodeLifecycleResult, ...]:
    if not isinstance(lifecycle, StandardProjectLifecycleResult):
        raise TypeError("lifecycle must be a StandardProjectLifecycleResult")
    if not lifecycle.successful or lifecycle.aggregate_receipt is None:
        _error(
            "standard_receipt.lifecycle_not_accepted",
            "only a fully accepted Standard lifecycle can produce a project receipt",
        )
    nodes = lifecycle.node_results
    if any(
        node.source_output is None
        or node.build_evidence is None
        or node.generated_test_evidence is None
        or node.execution_evidence is None
        or node.acceptance_evidence is None
        for node in nodes
    ):
        _error(
            "standard_receipt.strict_evidence_missing",
            "project receipt projection requires the complete typed evidence chain",
        )
    return nodes


def _evidence(
    lifecycle: StandardProjectLifecycleResult,
    *,
    lifecycle_request_identity: ContentIdentity,
    lifecycle_invocation_identity: ContentIdentity,
    runner_identity: ContentIdentity,
) -> dict[str, ContentIdentity]:
    nodes = _strict_nodes(lifecycle)
    assert lifecycle.aggregate_receipt is not None
    assert lifecycle.admission_identity is not None
    return {
        "acceptance-result": _aggregate(
            "acceptance-result",
            (node.acceptance_evidence.identity for node in nodes),  # type: ignore[union-attr]
        ),
        "build-result": _aggregate(
            "build-result",
            (node.build_evidence.identity for node in nodes),  # type: ignore[union-attr]
        ),
        "source-intelligence": _aggregate(
            "source-intelligence",
            (node.index_identity for node in nodes if node.index_identity is not None),
        ),
        "generation-provenance": _aggregate(
            "generation-provenance",
            (node.source_output.provenance_identity for node in nodes),  # type: ignore[union-attr]
        ),
        # The historical evidence-kind name is retained on the wire.  Its value is an
        # exact in-process Standard invocation, never a fabricated external argv.
        "lifecycle-command": lifecycle_invocation_identity,
        "lifecycle-plan": lifecycle.execution_plan_identity,
        "lifecycle-request": lifecycle_request_identity,
        "observation-result": _aggregate(
            "observation-result",
            (node.execution_evidence.identity for node in nodes),  # type: ignore[union-attr]
        ),
        "resolved-sbom": _aggregate(
            "resolved-sbom",
            (node.build_evidence.resolved_sbom.bom_identity for node in nodes),  # type: ignore[union-attr]
        ),
        "security-scan-report": _aggregate(
            "security-scan-report",
            (
                node.authorization_identity
                for node in nodes
                if node.authorization_identity is not None
            ),
        ),
        "source-cache-decision": canonical_identity(
            {
                "schema": "literate-ai/standard-source-cache-decisions@1",
                "lifecycle_membership_identity": (
                    lifecycle.lifecycle_membership.identity.uri
                ),
                "decisions": [
                    item.identity.uri
                    for item in lifecycle.lifecycle_membership.cache_decisions
                ],
            }
        ),
        "source-cache-lifecycle": lifecycle.lifecycle_membership.identity,
        "source-sbom": _aggregate(
            "source-sbom",
            (node.source_output.candidate.source_bom_identity for node in nodes),  # type: ignore[union-attr]
        ),
        "test-report": _aggregate(
            "test-report",
            (node.generated_test_evidence.identity for node in nodes),  # type: ignore[union-attr]
        ),
        "test-runner": runner_identity,
        "workspace-admission": lifecycle.admission_identity,
    }


def project_standard_project_test_receipt(
    lifecycle: StandardProjectLifecycleResult,
    *,
    project_id: str,
    project_revision_identity: ContentIdentity,
    lifecycle_policy: StandardLifecyclePolicy,
    receipt_policy: ProjectTestReceiptPolicy,
    lifecycle_request_identity: ContentIdentity,
    lifecycle_invocation_identity: ContentIdentity,
    runner_identity: ContentIdentity,
) -> ProjectTestReceipt:
    """Project one successful Standard result through exact receipt policy."""

    if not isinstance(lifecycle_policy, StandardLifecyclePolicy):
        raise TypeError("lifecycle_policy must be a StandardLifecyclePolicy")
    if not isinstance(receipt_policy, ProjectTestReceiptPolicy):
        raise TypeError("receipt_policy must be a ProjectTestReceiptPolicy")
    if receipt_policy.runner_identity != runner_identity:
        _error(
            "standard_receipt.runner_mismatch",
            "project receipt policy does not authorize this Standard runner",
        )
    if (
        receipt_policy.suite_id != lifecycle_policy.policy_id
        or receipt_policy.suite_version != lifecycle_policy.policy_version
    ):
        _error(
            "standard_receipt.policy_mismatch",
            "project receipt suite does not identify the selected Standard policy",
        )
    required = frozenset(lifecycle_policy.required_evidence_kinds)
    if required != frozenset(STANDARD_FULL_REBUILD_EVIDENCE_KINDS):
        _error(
            "standard_receipt.lifecycle_policy_unsupported",
            "selected Standard policy does not require the complete rebuild "
            "evidence set",
        )
    if not set(receipt_policy.required_evidence_kinds) <= required:
        _error(
            "standard_receipt.project_policy_unsupported",
            "project receipt policy asks for evidence outside the Standard policy",
        )
    evidence = _evidence(
        lifecycle,
        lifecycle_request_identity=lifecycle_request_identity,
        lifecycle_invocation_identity=lifecycle_invocation_identity,
        runner_identity=runner_identity,
    )
    missing = required - evidence.keys()
    if missing:
        _error(
            "standard_receipt.evidence_missing",
            "Standard lifecycle omitted required evidence: "
            + ", ".join(sorted(missing)),
        )
    total = sum(
        node.generated_test_evidence.executed_count  # type: ignore[union-attr]
        for node in lifecycle.node_results
    )
    minimum_tests = max(
        lifecycle_policy.minimum_test_count,
        receipt_policy.minimum_test_count,
    )
    if total < minimum_tests:
        _error(
            "standard_receipt.test_count_insufficient",
            "Standard lifecycle executed fewer tests than the selected policies "
            "require",
        )
    assert lifecycle.aggregate_receipt is not None
    return ProjectTestReceipt(
        project_id=project_id,
        project_revision_identity=project_revision_identity,
        subject_identity=lifecycle.aggregate_receipt.identity,
        suite=VersionedContentRef(
            PROJECT_TEST_SUITE_KIND,
            lifecycle_policy.policy_id,
            lifecycle_policy.policy_version,
            lifecycle_policy.identity,
        ),
        outcome=PROJECT_TEST_OUTCOME_PASSED,
        summary=ProjectTestSummary(total, total, 0, 0),
        result_identity=lifecycle.identity,
        evidence=tuple(
            ProjectTestEvidence(kind, identity)
            for kind, identity in sorted(evidence.items())
        ),
    )


def combine_standard_project_test_receipts(
    candidates: tuple[ProjectTestReceiptFinalizedCandidate, ...],
    *,
    project_id: str,
    validated_project_identity: ContentIdentity,
    component_lock_identities: tuple[ContentIdentity, ...],
    lifecycle_policy: StandardLifecyclePolicy,
    receipt_policy: ProjectTestReceiptPolicy,
) -> ProjectTestReceiptFinalizedCandidate:
    """Bind the complete accepted root set without replacing any member proof."""

    if not candidates or len(candidates) != len(component_lock_identities):
        _error("standard_receipt.root_set_mismatch", "one receipt per root is required")
    observed_locks = []
    for candidate in candidates:
        if not isinstance(candidate, ProjectTestReceiptFinalizedCandidate):
            raise TypeError("root receipts must be finalized candidates")
        if len(candidate.component_lock_identities) != 1:
            _error("standard_receipt.root_set_mismatch", "each root must bind one lock")
        observed_locks.extend(candidate.component_lock_identities)
        receipt = candidate.receipt
        evidence = {item.kind: item.identity for item in receipt.evidence}
        if (
            receipt.project_id != project_id
            or receipt.project_revision_identity
            != rebuild_project_authority_identity(
                validated_project_identity, candidate.component_lock_identities
            )
            or receipt.suite.identifier != lifecycle_policy.policy_id
            or receipt.suite.version != lifecycle_policy.policy_version
            or receipt.suite.content_identity != lifecycle_policy.identity
            or receipt_policy.suite_id != lifecycle_policy.policy_id
            or receipt_policy.suite_version != lifecycle_policy.policy_version
            or evidence.get("test-runner") != receipt_policy.runner_identity
            or set(evidence) != set(STANDARD_FULL_REBUILD_EVIDENCE_KINDS)
            or set(lifecycle_policy.required_evidence_kinds)
            != set(STANDARD_FULL_REBUILD_EVIDENCE_KINDS)
            or not set(receipt_policy.required_evidence_kinds) <= set(evidence)
            or receipt.summary.total
            < max(
                lifecycle_policy.minimum_test_count, receipt_policy.minimum_test_count
            )
        ):
            _error(
                "standard_receipt.root_context_mismatch",
                "root receipt does not bind the current project and Standard policy",
            )
    if (
        len(set(observed_locks)) != len(observed_locks)
        or tuple(sorted(observed_locks, key=lambda item: item.uri))
        != component_lock_identities
    ):
        _error(
            "standard_receipt.root_set_mismatch",
            "root receipts do not bind the complete canonical Component lock set",
        )
    if len(candidates) == 1:
        return candidates[0]
    evidence = {
        kind: _aggregate(
            kind,
            (
                next(
                    item.identity
                    for item in candidate.receipt.evidence
                    if item.kind == kind
                )
                for candidate in candidates
            ),
        )
        for kind in STANDARD_FULL_REBUILD_EVIDENCE_KINDS
    }
    # Runner authorization is common to every member, not a new aggregate runner.
    evidence["test-runner"] = receipt_policy.runner_identity
    total = sum(candidate.receipt.summary.total for candidate in candidates)
    receipt = ProjectTestReceipt(
        project_id=project_id,
        project_revision_identity=rebuild_project_authority_identity(
            validated_project_identity, component_lock_identities
        ),
        subject_identity=_aggregate(
            "accepted-roots", (item.identity for item in candidates)
        ),
        suite=candidates[0].receipt.suite,
        outcome=PROJECT_TEST_OUTCOME_PASSED,
        summary=ProjectTestSummary(total, total, 0, 0),
        result_identity=_aggregate(
            "root-results", (item.receipt.result_identity for item in candidates)
        ),
        evidence=tuple(
            ProjectTestEvidence(kind, value) for kind, value in sorted(evidence.items())
        ),
    )
    provisional = ProjectTestReceiptProvisional(
        lifecycle_request_identity=evidence["lifecycle-request"],
        lifecycle_command_identity=evidence["lifecycle-command"],
        source_cache_control_identity=_aggregate(
            "source-cache-control",
            (item.source_cache_control_identity for item in candidates),
        ),
        component_lock_identities=component_lock_identities,
        receipt_identity=receipt.identity,
        receipt=receipt,
    )
    return ProjectTestReceiptFinalizedCandidate.finalize(
        provisional,
        source_cache_decision_identity=evidence["source-cache-decision"],
        source_cache_lifecycle_identity=evidence["source-cache-lifecycle"],
    )


__all__ = [
    "StandardTestReceiptProjectionError",
    "combine_standard_project_test_receipts",
    "project_standard_project_test_receipt",
]

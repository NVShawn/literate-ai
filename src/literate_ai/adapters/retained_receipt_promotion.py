"""Promote one admitted retained-project run into a project test-receipt candidate.

A retained-project run (the original-source lifecycle behind ``--retained-project``)
leaves a content-addressed retained receipt in its custody store.  This owner
re-verifies that receipt against the *current* project authority and the project's
configured ``test_receipt_policy`` and, only if every check passes, projects it into
the finalized candidate envelope that ``litai project test-receipt update`` accepts.

Nothing here executes a command, widens a policy, or trusts a stored assertion that is
not re-derived or re-hashed: every record is re-read from custody, the retained plan,
manifest, verifier policy, source SBOM and test policy are recomputed from the current
project, and the policy's runner identity must equal the identity derived from the
run's own test actions.  The envelope is a typed TCB boundary like every other
finalized candidate, not a cryptographic attestation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts.identity import (
    ContentIdentity,
    VersionedContentRef,
    canonical_identity,
)
from literate_ai.contracts.rebuild_cache import rebuild_project_authority_identity
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan
from literate_ai.contracts.retained_project_receipt import RetainedProjectReceipt
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
from literate_ai.storage.cas import FileSystemCAS, StorageError

MAXIMUM_DOCUMENT_BYTES = 64 * 1024 * 1024
RETAINED_PROJECT_RUNNER_SCHEMA = "literate-ai/retained-project-test-runner@2"
RETAINED_PROJECT_PROMOTION_EVIDENCE_KINDS = frozenset(
    {
        "acceptance-result",
        "build-result",
        "lifecycle-command",
        "lifecycle-plan",
        "lifecycle-request",
        "observation-result",
        "resolved-sbom",
        "source-sbom",
        "test-report",
        "test-runner",
        "workspace-admission",
    }
)


class RetainedReceiptPromotionError(ValueError):
    """A retained run cannot honestly satisfy the project test-receipt boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _refuse(code: str, message: str):
    raise RetainedReceiptPromotionError("retained_promotion." + code, message)


def _evidence_set(kind: str, identities) -> ContentIdentity:
    members = sorted({item.uri for item in identities})
    if not members:
        _refuse(
            "evidence_missing",
            f"the retained run carries no {kind} evidence",
        )
    return canonical_identity(
        {
            "schema": "literate-ai/retained-project-evidence-set@1",
            "kind": kind,
            "members": members,
        }
    )


def retained_project_runner_identity(
    plan: RetainedProjectPlan,
    manifest: RetainedProjectManifest,
    test_policy,
) -> ContentIdentity:
    """Bind complete reviewed test semantics, omitting only the policy cycle.

    This projection is intent, never proof that a runner executed. The current
    complete verifier separately checks the full policy identity and its project
    policy binding before any candidate can be finalized.
    """
    from literate_ai.contracts.retained_project_tests import RetainedProjectTestPolicy

    if type(test_policy) is not RetainedProjectTestPolicy:
        raise TypeError("retained.promotion.typed-test-policy-required")
    tools = {name: identity.uri for name, _path, identity in plan.profile.tools}
    actions = [
        {"action": action.to_dict(), "tool_identity": tools.get(action.tool)}
        for action in plan.profile.actions
        if action.stage == "test"
    ]
    if not actions or any(row["tool_identity"] is None for row in actions):
        _refuse("runner_missing", "the current plan must declare exact test tools")
    semantic_policy = test_policy.to_dict()
    del semantic_policy["project_policy_identity"]
    return canonical_identity(
        {
            "schema": RETAINED_PROJECT_RUNNER_SCHEMA,
            "actions": actions,
            "definitions": [
                member.to_dict()
                for member in sorted(manifest.members, key=lambda row: row.path)
                if member.role == "test-definition"
            ],
            "test_policy": semantic_policy,
        }
    )


@dataclass(frozen=True, slots=True)
class VerifiedRetainedRun:
    """Facts re-derived from custody; a value of this type is never serialized."""

    receipt: RetainedProjectReceipt
    plan: RetainedProjectPlan
    manifest: RetainedProjectManifest
    completed_run_identity: ContentIdentity
    runner_identity: ContentIdentity
    command_identities: tuple[ContentIdentity, ...]
    build_identities: tuple[ContentIdentity, ...]
    test_identities: tuple[ContentIdentity, ...]
    acceptance_identities: tuple[ContentIdentity, ...]
    passed: int
    workspace_admission_identity: ContentIdentity


def verify_retained_run(
    store: FileSystemCAS,
    receipt_identity: ContentIdentity,
    *,
    source: Path,
    manifest: RetainedProjectManifest,
    planning,
    component_locks,
    test_receipt_policy: ProjectTestReceiptPolicy,
    promotion_evidence_identity: ContentIdentity,
    require_current,
    read_policy=None,
    current_sdk_snapshots=(),
    expected_plan_identity: ContentIdentity | None = None,
) -> VerifiedRetainedRun:
    """Reopen complete canonical run facts; serialized facts never restore owners."""
    from literate_ai.adapters.retained_receipt_promotion_evidence import (
        read_retained_receipt_promotion_evidence,
    )

    plan = planning.plan
    if expected_plan_identity is not None and expected_plan_identity != plan.identity:
        _refuse("plan_stale", "the requested retained plan is not current")
    try:
        inputs, run, admission_identity = read_retained_receipt_promotion_evidence(
            store,
            receipt_identity,
            promotion_evidence_identity,
            source=source,
            source_manifest=manifest,
            planning=planning,
            component_locks=component_locks,
            test_receipt_policy=test_receipt_policy,
            require_current=require_current,
            policy=read_policy,
            current_sdk_snapshots=current_sdk_snapshots,
        )
    except (StorageError, OSError, ValueError, TypeError) as exc:
        _refuse("run_unqualified", f"complete retained-run verification refused: {exc}")
    passed = sum(item.report.passed for item in run.test_reports)
    skipped = sum(item.report.skipped for item in run.test_reports)
    if skipped:
        _refuse("tests_skipped", "a project receipt requires passing tests only")
    require_current()
    return VerifiedRetainedRun(
        inputs.receipt,
        plan,
        manifest,
        run.identity,
        retained_project_runner_identity(plan, manifest, run.test_policy),
        tuple(item.identity for item in run.observations),
        tuple(
            item.identity for item in run.observations if item.action.stage == "build"
        ),
        tuple(item.identity for item in run.test_reports),
        tuple(item.identity for item in run.acceptance_observations),
        passed,
        ContentIdentity.parse_uri(admission_identity),
    )


def promote_retained_run(
    verified: VerifiedRetainedRun,
    *,
    project_id: str,
    project_revision_identity: ContentIdentity,
    component_lock_identities: tuple[ContentIdentity, ...],
    receipt_policy: ProjectTestReceiptPolicy,
) -> ProjectTestReceiptFinalizedCandidate:
    """Project one verified run through the project's exact receipt policy."""

    from literate_ai.adapters.retained_harness_receipts import (
        RETAINED_HARNESS_SUITE_ID,
    )

    plan, receipt = verified.plan, verified.receipt
    if project_id != plan.project_id:
        _refuse("project_mismatch", "the retained run belongs to another project")
    if receipt_policy.suite_id == RETAINED_HARNESS_SUITE_ID:
        _refuse(
            "suite_unsupported",
            "this project's policy names the retained-harness suite; use "
            "'litai project test-receipt run-retained' for it",
        )
    if tuple(component_lock_identities) != tuple(
        sorted(component_lock_identities, key=lambda item: item.uri)
    ) or set(component_lock_identities) != set(plan.component_locks):
        _refuse(
            "locks_mismatch",
            "the current Component locks are not those the retained plan binds",
        )
    unsupported = set(receipt_policy.required_evidence_kinds) - (
        RETAINED_PROJECT_PROMOTION_EVIDENCE_KINDS
    )
    if unsupported:
        _refuse(
            "evidence_missing",
            "the policy requires evidence a retained run cannot supply: "
            + ", ".join(sorted(unsupported)),
        )
    if verified.runner_identity != receipt_policy.runner_identity:
        _refuse(
            "runner_mismatch",
            "the policy's runner identity is not the identity of this run's test "
            f"runner (the run's runner is {verified.runner_identity.uri})",
        )
    if verified.passed < receipt_policy.minimum_test_count:
        _refuse(
            "test_count_insufficient",
            f"the run passed {verified.passed} tests; the policy requires "
            f"{receipt_policy.minimum_test_count}",
        )

    request = canonical_identity(
        {
            "schema": "literate-ai/retained-project-promotion-request@1",
            "project_id": project_id,
            "project_revision_identity": project_revision_identity.uri,
            "plan_identity": plan.identity.uri,
            "retained_receipt_identity": receipt.identity.uri,
            "completed_run_identity": verified.completed_run_identity.uri,
            "runner_identity": verified.runner_identity.uri,
            "receipt_policy_identity": receipt_policy.identity.uri,
        }
    )
    command = canonical_identity(
        {
            "schema": "literate-ai/retained-project-command-set@1",
            "request_identity": request.uri,
            "observations": [item.uri for item in verified.command_identities],
            "commands": [
                {
                    "id": action.action_id,
                    "stage": action.stage,
                    "tool": action.tool,
                    "argv": list(action.argv),
                }
                for action in plan.profile.actions
            ],
        }
    )
    control = canonical_identity(
        {
            "schema": "literate-ai/retained-project-cache-control@1",
            "request_identity": request.uri,
            "disposition": "not-applicable-original-source-authority",
        }
    )
    decision = canonical_identity(
        {
            "schema": "literate-ai/retained-project-cache-decision@1",
            "control_identity": control.uri,
            "decision": "not-applicable",
        }
    )
    lifecycle = canonical_identity(
        {
            "schema": "literate-ai/retained-project-cache-lifecycle@1",
            "control_identity": control.uri,
            "decision_identity": decision.uri,
            "state": "not-used",
        }
    )
    evidence = {
        "acceptance-result": _evidence_set(
            "acceptance-result", verified.acceptance_identities
        ),
        "build-result": _evidence_set("build-result", verified.build_identities),
        "lifecycle-command": command,
        "lifecycle-plan": plan.identity,
        "lifecycle-request": request,
        "observation-result": verified.completed_run_identity,
        "resolved-sbom": ContentIdentity.parse_uri(
            receipt.root("resolved-bom").identity
        ),
        "source-cache-decision": decision,
        "source-cache-lifecycle": lifecycle,
        "source-sbom": ContentIdentity.parse_uri(receipt.root("source-bom").identity),
        "test-report": _evidence_set("test-report", verified.test_identities),
        "test-runner": verified.runner_identity,
        "workspace-admission": verified.workspace_admission_identity,
    }
    suite = canonical_identity(
        {
            "schema": "literate-ai/retained-project-suite@1",
            "suite_id": receipt_policy.suite_id,
            "suite_version": receipt_policy.suite_version,
            "runner_identity": verified.runner_identity.uri,
            "plan_identity": plan.identity.uri,
        }
    )
    project_receipt = ProjectTestReceipt(
        project_id=project_id,
        project_revision_identity=project_revision_identity,
        subject_identity=receipt.identity,
        suite=VersionedContentRef(
            PROJECT_TEST_SUITE_KIND,
            receipt_policy.suite_id,
            receipt_policy.suite_version,
            suite,
        ),
        outcome=PROJECT_TEST_OUTCOME_PASSED,
        summary=ProjectTestSummary(verified.passed, verified.passed, 0, 0),
        result_identity=verified.completed_run_identity,
        evidence=tuple(
            ProjectTestEvidence(kind, identity)
            for kind, identity in sorted(evidence.items())
        ),
    )
    return ProjectTestReceiptFinalizedCandidate.finalize(
        ProjectTestReceiptProvisional(
            lifecycle_request_identity=request,
            lifecycle_command_identity=command,
            source_cache_control_identity=control,
            component_lock_identities=tuple(component_lock_identities),
            receipt_identity=project_receipt.identity,
            receipt=project_receipt,
        ),
        source_cache_decision_identity=decision,
        source_cache_lifecycle_identity=lifecycle,
    )


def promoted_project_revision(
    base_revision: ContentIdentity,
    component_lock_identities: tuple[ContentIdentity, ...],
) -> ContentIdentity:
    """Bind the receipt to the same revision ``update`` will recompute."""

    return rebuild_project_authority_identity(
        base_revision, tuple(component_lock_identities)
    )


__all__ = [
    "RETAINED_PROJECT_PROMOTION_EVIDENCE_KINDS",
    "RetainedReceiptPromotionError",
    "VerifiedRetainedRun",
    "promote_retained_run",
    "promoted_project_revision",
    "retained_project_runner_identity",
    "verify_retained_run",
]

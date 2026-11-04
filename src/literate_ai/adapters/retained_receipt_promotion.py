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

from literate_ai.adapters.native_tree_shards import _document
from literate_ai.adapters.retained_project_acceptance import (
    read_retained_acceptance_policy,
)
from literate_ai.adapters.retained_project_boms import RetainedProjectSourceBom
from literate_ai.adapters.retained_project_inputs import (
    verify_retained_project_inputs,
)
from literate_ai.adapters.retained_project_testing import (
    read_retained_project_test_policy,
)
from literate_ai.adapters.retained_unit_inspection import (
    retained_unit_metadata_reference,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.contracts.identity import (
    ContentIdentity,
    VersionedContentRef,
    canonical_identity,
)
from literate_ai.contracts.rebuild_cache import rebuild_project_authority_identity
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan
from literate_ai.contracts.retained_project_receipt import RetainedProjectReceipt
from literate_ai.contracts.retained_project_tests import RetainedProjectTestReport
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
RETAINED_PROJECT_RUNNER_SCHEMA = "literate-ai/retained-project-test-runner@1"
RETAINED_PROJECT_PROMOTION_EVIDENCE_KINDS = frozenset(
    {
        "acceptance-result",
        "build-result",
        "lifecycle-command",
        "lifecycle-plan",
        "lifecycle-request",
        "observation-result",
        "resolved-sbom",
        "source-cache-decision",
        "source-cache-lifecycle",
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
    plan: RetainedProjectPlan, manifest: RetainedProjectManifest
) -> ContentIdentity:
    """Name the test runner from the run's own locked test actions and definitions.

    The identity covers each ``test`` action's exact tool identity and argv template
    plus every reviewed ``test-definition`` input member.  It deliberately excludes
    the ``test-policy`` member, whose bytes name the project receipt policy (and so
    this runner) and would make the definition circular.
    """

    tools = {name: identity.uri for name, _path, identity in plan.profile.tools}
    actions = [
        {
            "id": action.action_id,
            "tool": action.tool,
            "tool_identity": tools.get(action.tool),
            "argv": list(action.argv),
        }
        for action in plan.profile.actions
        if action.stage == "test"
    ]
    if not actions:
        _refuse("runner_missing", "the retained plan declares no test action")
    if any(item["tool_identity"] is None for item in actions):
        _refuse("runner_missing", "a test action names a tool without an identity")
    definitions = sorted(
        (member.path, member.sha256)
        for member in manifest.members
        if member.role == "test-definition"
    )
    return canonical_identity(
        {
            "schema": RETAINED_PROJECT_RUNNER_SCHEMA,
            "actions": actions,
            "definitions": [
                {"path": path, "sha256": digest} for path, digest in definitions
            ],
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


def _read(store, reference, what):
    try:
        return _document(store, reference, MAXIMUM_DOCUMENT_BYTES)
    except (StorageError, OSError, ValueError, TypeError) as exc:
        _refuse("custody_invalid", f"{what} cannot be read from custody: {exc}")


def _identities(document, key, count=None):
    values = document.get(key)
    if type(values) is not list or (count is not None and len(values) != count):
        _refuse("run_incomplete", f"the completed run does not list every {key}")
    try:
        parsed = tuple(ContentIdentity.parse_uri(value) for value in values)
    except (ValueError, TypeError):
        _refuse("run_invalid", f"the completed run {key} are not identities")
    if len({item.uri for item in parsed}) != len(parsed):
        _refuse("run_invalid", f"the completed run repeats a {key} identity")
    return parsed


def verify_retained_run(
    store: FileSystemCAS,
    receipt_identity: ContentIdentity,
    *,
    source: Path,
    manifest: RetainedProjectManifest,
    planning,
    component_locks,
    test_receipt_policy: ProjectTestReceiptPolicy,
    expected_plan_identity: ContentIdentity | None = None,
) -> VerifiedRetainedRun:
    """Re-verify one retained receipt against the current project, or refuse."""

    if not isinstance(store, FileSystemCAS):
        raise TypeError("a FileSystemCAS custody store is required")
    plan = planning.plan
    if expected_plan_identity is not None and expected_plan_identity != plan.identity:
        _refuse(
            "plan_stale",
            "the current retained plan is "
            f"{plan.identity.uri}, not the requested {expected_plan_identity.uri}",
        )
    try:
        reference = retained_unit_metadata_reference(
            store, receipt_identity, maximum_bytes=MAXIMUM_DOCUMENT_BYTES
        )
        raw = store.get_bytes(reference)
        receipt = RetainedProjectReceipt.from_dict(_read(store, reference, "receipt"))
    except RetainedReceiptPromotionError:
        raise
    except (StorageError, OSError, ValueError, TypeError) as exc:
        _refuse("receipt_unreadable", f"the retained receipt is unavailable: {exc}")
    if (
        receipt.identity != receipt_identity
        or canonical_json_bytes(receipt.to_dict()) != raw
    ):
        _refuse(
            "receipt_tampered",
            "the retained receipt is not the canonical document it is named by",
        )
    if receipt.project_id != plan.project_id:
        _refuse("project_mismatch", "the retained receipt names another project")
    try:
        for record in receipt.records:
            store.verify(record)
        for _name, root in receipt.roots:
            store.verify(root)
    except (StorageError, OSError, ValueError) as exc:
        _refuse("custody_invalid", f"retained custody failed verification: {exc}")

    if (
        receipt.root("plan").identity != plan.identity.uri
        or receipt.root("input-manifest").identity != manifest.identity.uri
        or manifest.identity != plan.manifest_identity
    ):
        _refuse(
            "plan_stale",
            "the retained run was admitted under a different plan or input manifest "
            "than the current project authority",
        )
    if _read(store, receipt.root("plan"), "plan") != plan.to_dict():
        _refuse(
            "plan_stale", "the retained plan document differs from the current plan"
        )
    try:
        verify_retained_project_inputs(source, manifest)
    except (OSError, ValueError) as exc:
        _refuse("inputs_changed", f"the current retained inputs differ: {exc}")
    acceptance = None
    try:
        acceptance = read_retained_acceptance_policy(
            source, plan.profile, plan.component_locks, manifest
        )
        test_policy = read_retained_project_test_policy(
            source, manifest, plan.profile, test_receipt_policy
        )
    except (OSError, ValueError, TypeError) as exc:
        _refuse(
            "policy_stale",
            "the current verifier or test policy no longer matches the project "
            f"receipt policy: {exc}",
        )
    if (
        acceptance is None
        or plan.profile.acceptance_policy_identity is None
        or receipt.root("acceptance-policy-bytes").identity
        != plan.profile.acceptance_policy_identity.uri
    ):
        _refuse("policy_stale", "the retained verifier policy is not the current one")
    source_bom = RetainedProjectSourceBom(
        plan, manifest, planning.managed_sbom_graph, tuple(component_locks)
    )
    try:
        source_bom.require_content(store.get_bytes(receipt.root("source-bom")))
    except (ValueError, OSError, StorageError):
        _refuse(
            "sbom_mismatch",
            "the retained source SBOM is not the one derived from the current plan",
        )

    completed = _read(store, receipt.root("completed-run"), "completed run")
    output = receipt.root("output-manifest")
    if (
        type(completed) is not dict
        or completed.get("schema") != "literate-ai/retained-project-completed-run@1"
        or completed.get("authority_mode") != "retained-project"
        or completed.get("plan_identity") != plan.identity.uri
        or completed.get("output_manifest_identity") != output.identity
        or completed.get("test_policy_identity") != test_policy.identity.uri
        or completed.get("project_test_policy_identity")
        != test_receipt_policy.identity.uri
        or completed.get("acceptance_policy_identity") != acceptance.identity.uri
        or completed.get("source_observation_identity") != source_bom.identity.uri
        or completed.get("sdk_admitted") is not False
        or completed.get("lifecycle_receipt") is not False
        or completed.get("release_authority") != "original-source"
    ):
        _refuse(
            "run_context_mismatch",
            "the completed run does not bind the current plan, policies and SBOM",
        )
    if completed.get("package_inventory") != list(plan.profile.package_inventory):
        _refuse(
            "run_context_mismatch",
            "the completed run package inventory differs from the plan",
        )
    if completed.get("resolved_observation_identity") is None:
        _refuse("run_incomplete", "the completed run has no resolved observation")

    def record(identity: ContentIdentity):
        target = next(
            (item for item in receipt.records if item.identity == identity.uri), None
        )
        if target is None:
            _refuse("run_incomplete", f"{identity.uri} is not a retained record")
        value = _read(store, target, identity.uri)
        if type(value) is not dict:
            _refuse("run_invalid", f"{identity.uri} is not a retained observation")
        if canonical_identity(value) != identity:
            _refuse("receipt_tampered", f"{identity.uri} is not its own content")
        return value

    actions = plan.profile.actions
    commands = _identities(completed, "command_observations", len(actions))
    stage_of = {}
    for action, identity in zip(actions, commands, strict=True):
        value = record(identity)
        if (
            value.get("plan_identity") != plan.identity.uri
            or value.get("action") != action.to_dict()
            or value.get("exit_code") != 0
        ):
            _refuse(
                "run_context_mismatch",
                f"command observation for {action.action_id} is not a clean run of "
                "the current plan",
            )
        stage_of[action.action_id] = identity
    build = tuple(
        stage_of[action.action_id] for action in actions if action.stage == "build"
    )

    reports = []
    tests = _identities(completed, "test_observations", len(test_policy.reports))
    for identity in tests:
        value = record(identity)
        try:
            report = RetainedProjectTestReport.from_dict(value["report"])
        except (KeyError, ValueError, TypeError):
            _refuse("run_invalid", "a retained test report is malformed")
        if (
            value.get("plan_identity") != plan.identity.uri
            or value.get("policy_identity") != test_policy.identity.uri
            or value.get("observation_identity")
            != getattr(stage_of.get(report.action_id), "uri", None)
        ):
            _refuse(
                "run_context_mismatch",
                "a retained test report is not bound to this plan and its action",
            )
        reports.append(report)
    try:
        test_policy.require_complete(reports, test_receipt_policy)
    except ValueError as exc:
        _refuse("tests_unqualified", str(exc))
    passed = sum(report.passed for report in reports)
    skipped = sum(report.skipped for report in reports)
    if passed != receipt.passed or skipped != receipt.skipped:
        _refuse(
            "summary_mismatch",
            "the retained receipt summary differs from its own test reports",
        )
    if skipped:
        _refuse(
            "tests_skipped",
            "a project test receipt accounts for passing tests only; the retained "
            f"run skipped {skipped} authorized test(s) and cannot be promoted",
        )

    cases = tuple(
        case
        for action in actions
        if action.stage == "accept"
        for case in acceptance.cases
        if case.action_id == action.action_id
    )
    accepted = _identities(completed, "acceptance_observations", len(cases))
    for case, identity in zip(cases, accepted, strict=True):
        value = record(identity)
        if (
            value.get("plan_identity") != plan.identity.uri
            or value.get("policy_identity") != acceptance.identity.uri
            or value.get("case_identity") != case.identity.uri
            or value.get("exit_code") != case.expected_exit
        ):
            _refuse(
                "run_context_mismatch",
                f"acceptance observation for {case.case_id} does not match its case",
            )
    return VerifiedRetainedRun(
        receipt,
        plan,
        manifest,
        canonical_identity(completed),
        retained_project_runner_identity(plan, manifest),
        commands,
        build,
        tuple(tests),
        accepted,
        passed,
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
        "workspace-admission": canonical_identity(
            {
                "schema": "literate-ai/retained-project-workspace-admission@1",
                "retained_receipt_identity": receipt.identity.uri,
                "plan_identity": plan.identity.uri,
                "input_manifest_identity": verified.manifest.identity.uri,
            }
        ),
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

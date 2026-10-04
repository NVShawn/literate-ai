"""Shared test fixtures extracted from test_project_test_receipt_cli."""

from __future__ import annotations

import io
import json
from pathlib import Path

from literate_ai.cli import main
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
    rebuild_project_authority_identity,
)
from literate_ai.projects import load_project
from tests.support.fixtures_test_project_cli import (
    refresh_authority_review,
)
from tests.support.root_parent_adapter import root_parent_for_fixture_project

_FINALIZATION_EVIDENCE = {
    "lifecycle-command": canonical_identity({"fixture": "command"}),
    "lifecycle-request": canonical_identity({"fixture": "request"}),
    "source-cache-decision": canonical_identity({"fixture": "decision"}),
    "source-cache-lifecycle": canonical_identity({"fixture": "lifecycle"}),
}


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    with root_parent_for_fixture_project(arguments):
        status = main(arguments, stdout=output, stderr=errors)
    content = output.getvalue() if status == 0 else errors.getvalue()
    return status, json.loads(content)


def fixture_policy(version: str = "1.0.0") -> ProjectTestReceiptPolicy:
    return ProjectTestReceiptPolicy(
        suite_id="portable-e2e",
        suite_version=version,
        runner_identity=canonical_identity({"fixture": "trusted-test-runner"}),
        required_evidence_kinds=(
            "build-result",
            "generation-provenance",
            "observation-result",
            "security-scan-report",
            "test-report",
            "test-runner",
        ),
        minimum_test_count=4,
    )


def configure_receipt_policy(project_root: Path) -> ProjectTestReceiptPolicy:
    manifest_path = project_root / "literate.project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy = fixture_policy(str(manifest["version"]))
    manifest["test_receipt_policy"] = policy.to_dict()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    refresh_authority_review(project_root)
    return policy


def make_candidate(
    project_root: Path,
    *,
    project_id: str | None = None,
    policy: ProjectTestReceiptPolicy | None = None,
) -> dict:
    project = load_project(project_root)
    policy = policy or project.definition.test_receipt_policy
    if policy is None:
        raise AssertionError("candidate fixture requires an explicit receipt policy")
    status, envelope = invoke("project", "documentation-review", str(project_root))
    if status != 0:
        raise AssertionError(envelope)
    project_revision_identity = ContentIdentity.parse_uri(
        str(envelope["result"]["authority_identity"])
    )
    evidence_by_kind = {
        kind: (
            policy.runner_identity
            if kind == "test-runner"
            else canonical_identity({"fixture": kind})
        )
        for kind in policy.required_evidence_kinds
    }
    evidence_by_kind.update(_FINALIZATION_EVIDENCE)
    receipt = ProjectTestReceipt(
        project_id=project_id or project.definition.project_id,
        project_revision_identity=project_revision_identity,
        subject_identity=canonical_identity({"fixture": "current-authority"}),
        suite=VersionedContentRef(
            "test-suite",
            policy.suite_id,
            policy.suite_version,
            canonical_identity({"fixture": "current-suite"}),
        ),
        outcome="passed",
        summary=ProjectTestSummary(
            policy.minimum_test_count,
            policy.minimum_test_count,
            0,
            0,
        ),
        result_identity=canonical_identity({"fixture": "passing-result"}),
        evidence=tuple(
            ProjectTestEvidence(kind, identity)
            for kind, identity in sorted(evidence_by_kind.items())
        ),
    )
    return receipt.to_dict()


def finalize_candidate(receipt_value: dict) -> dict:
    component_lock = canonical_identity({"fixture": "component-lock"})
    bound_value = dict(receipt_value)
    bound_value["project_revision"] = rebuild_project_authority_identity(
        ContentIdentity.parse_uri(str(receipt_value["project_revision"])),
        (component_lock,),
    ).uri
    receipt = ProjectTestReceipt.from_dict(bound_value)
    provisional = ProjectTestReceiptProvisional(
        lifecycle_request_identity=_FINALIZATION_EVIDENCE["lifecycle-request"],
        lifecycle_command_identity=_FINALIZATION_EVIDENCE["lifecycle-command"],
        source_cache_control_identity=canonical_identity({"fixture": "control"}),
        component_lock_identities=(component_lock,),
        receipt_identity=receipt.identity,
        receipt=receipt,
    )
    return ProjectTestReceiptFinalizedCandidate.finalize(
        provisional,
        source_cache_decision_identity=_FINALIZATION_EVIDENCE["source-cache-decision"],
        source_cache_lifecycle_identity=_FINALIZATION_EVIDENCE[
            "source-cache-lifecycle"
        ],
    ).to_dict()

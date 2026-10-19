"""Reopen reviewed policies and sealed original test outcomes with bounded reads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.retained_project_metadata import (
    read_retained_project_document,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestReceiptPolicy,
    canonical_identity,
)
from literate_ai.contracts.retained_project import (
    RetainedProjectManifest,
    RetainedProjectMember,
)
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectTestPolicy,
    RetainedProjectTestReport,
)

_MAXIMUM_DOCUMENT_BYTES = 16 * 1024 * 1024


def _document(root: Path, member: RetainedProjectMember):
    return read_retained_project_document(
        root,
        member,
        maximum_bytes=_MAXIMUM_DOCUMENT_BYTES,
        error_prefix="retained.tests",
    )


def read_retained_project_test_policy(
    root: Path,
    manifest: RetainedProjectManifest,
    profile: RetainedProjectProfile,
    configured: ProjectTestReceiptPolicy,
) -> RetainedProjectTestPolicy:
    members = [member for member in manifest.members if member.role == "test-policy"]
    if (
        len(members) != 1
        or ContentIdentity.parse_uri("sha256:" + members[0].sha256)
        != profile.test_policy_identity
    ):
        raise ValueError("retained.tests.original-policy-input-required")
    policy = RetainedProjectTestPolicy.from_dict(_document(root, members[0]))
    policy.require_profile(profile, configured)
    return policy


@dataclass(frozen=True, slots=True)
class RetainedProjectVerifiedTestReport:
    """Case coverage bound to actual command/output custody; never a receipt."""

    plan_identity: ContentIdentity
    policy_identity: ContentIdentity
    observation_identity: ContentIdentity
    blob_identity: ContentIdentity
    report: RetainedProjectTestReport

    def __post_init__(self):
        if any(
            not isinstance(identity, ContentIdentity)
            for identity in (
                self.plan_identity,
                self.policy_identity,
                self.observation_identity,
                self.blob_identity,
            )
        ) or not isinstance(self.report, RetainedProjectTestReport):
            raise TypeError("retained.tests.typed-coverage-required")

    def to_dict(self):
        return dict(
            schema="literate-ai/retained-project-verified-tests@1",
            plan_identity=self.plan_identity.uri,
            policy_identity=self.policy_identity.uri,
            observation_identity=self.observation_identity.uri,
            blob_identity=self.blob_identity.uri,
            report=self.report.to_dict(),
            acceptance_receipt=False,
        )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())


def read_retained_project_test_report(
    root: Path,
    manifest: RetainedProjectManifest,
    policy: RetainedProjectTestPolicy,
    *,
    action_id: str,
    plan_identity: ContentIdentity,
    observation_identity: ContentIdentity,
) -> RetainedProjectVerifiedTestReport:
    path = dict(policy.reports).get(action_id)
    member = next((member for member in manifest.members if member.path == path), None)
    if member is None:
        raise ValueError("retained.tests.sealed-report-missing")
    report = RetainedProjectTestReport.from_dict(_document(root, member))
    if report.action_id != action_id:
        raise ValueError("retained.tests.report-action-mismatch")
    policy.require_report(report)
    return RetainedProjectVerifiedTestReport(
        plan_identity,
        policy.identity,
        observation_identity,
        ContentIdentity.parse_uri("sha256:" + member.sha256),
        report,
    )

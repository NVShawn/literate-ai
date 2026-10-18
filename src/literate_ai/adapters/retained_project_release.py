"""Bind retained planning to the existing configured release-policy owner."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.retained_project_metadata import (
    read_retained_project_document,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.project_releases import (
    RELEASE_POLICY_FILE,
    ProjectReleaseError,
    ReleasePolicy,
    load_release_policy,
)


def read_retained_project_release_policy(
    root: Path, manifest: RetainedProjectManifest, expected_identity: ContentIdentity
) -> ReleasePolicy:
    members = [member for member in manifest.members if member.role == "release-policy"]
    if (
        len(members) != 1
        or members[0].path != RELEASE_POLICY_FILE
        or ContentIdentity.parse_uri("sha256:" + members[0].sha256) != expected_identity
    ):
        raise ValueError("retained.release.configured-policy-input-required")
    member = members[0]
    reviewed = read_retained_project_document(root, member)
    try:
        policy = ReleasePolicy.from_dict(reviewed)
        actual_root, actual = load_release_policy(root)
    except ProjectReleaseError as exc:
        raise ValueError("retained.release.invalid-policy: " + str(exc)) from exc
    if (
        actual_root != root.resolve(strict=True)
        or replace(policy, default_branch=actual.default_branch) != actual
    ):
        raise ValueError("retained.release.configured-policy-mismatch")
    # The existing owner reopens the policy and derives the repository branch.
    # Recheck raw custody so a semantic alias or concurrent change cannot pass.
    if read_retained_project_document(root, member) != reviewed:
        raise ValueError("retained.release.policy-changed")
    return actual

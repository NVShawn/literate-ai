"""Read-only admission planning for original-authoritative retained projects."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from literate_ai.adapters.component_lock_application import (
    current_project_component_lock_identities,
)
from literate_ai.adapters.conversion_authority import FilesystemConversionAuthorityStore
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.adapters.retained_project_inputs import verify_retained_project_inputs
from literate_ai.adapters.standard_lifecycle_binding import (
    ResolvedStandardProjectLifecycleDriver,
)
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectPlan,
    RetainedProjectProfile,
)
from literate_ai.projects import LoadedProject


def retained_role_identity(
    manifest: RetainedProjectManifest, roles: set[str]
) -> ContentIdentity:
    """Bind the complete declared byte closure for a set of input ownership roles."""
    return canonical_identity(
        {
            "schema": "literate-ai/retained-role-closure@1",
            "roles": sorted(roles),
            "members": [
                member.to_dict() for member in manifest.members if member.role in roles
            ],
        }
    )


def plan_retained_project(
    project: LoadedProject,
    binding: ResolvedStandardProjectLifecycleDriver,
    manifest: RetainedProjectManifest,
    profile: RetainedProjectProfile,
    *,
    worker_identity: ContentIdentity,
    target: str,
    profile_path: Path,
    profile_bytes: bytes,
) -> RetainedProjectPlan:
    """Validate current authority and complete retained inputs without acquisition.

    No native SDK builder, model provider, process runner, or cache writer is called.
    The resulting identity is a review boundary only, never acceptance evidence.
    """
    if profile.target != target or profile.worker_identity != worker_identity:
        raise ValueError("Retained profile names another target or worker")
    binding.require_unchanged()
    project_identity = validated_project_authority_identity(
        project.root, synchronize_source_intelligence=False
    )
    locks = current_project_component_lock_identities(project)
    conversion = FilesystemConversionAuthorityStore(project.root).load_optional()
    if conversion is None or conversion.project_id != project.definition.project_id:
        raise ValueError(
            "Retained planning requires the project's explicit conversion authority"
        )
    if conversion.stage is ConversionAuthorityStage.QUALIFIED:
        raise ValueError(
            "Qualified conversion uses the existing regenerative lifecycle"
        )
    verify_retained_project_inputs(project.root, manifest)
    members = {member.path: member for member in manifest.members}
    relative_profile = (
        profile_path.absolute().relative_to(project.root.absolute()).as_posix()
    )
    profile_member = members.get(relative_profile)
    if profile_member is None or profile_member.role != "build-definition":
        raise ValueError("Profile must belong to the reviewed build-definition closure")
    if RetainedProjectProfile.from_dict(json.loads(profile_bytes)) != profile:
        raise ValueError("Typed profile differs from its reviewed source bytes")
    # The parser's bytes must be the same bytes admitted in the input manifest.
    if hashlib.sha256(profile_bytes).hexdigest() != profile_member.sha256:
        raise ValueError("Retained command profile changed during planning")
    for _name, path, identity in profile.tools:
        member = members.get(path)
        if (
            member is None
            or member.role not in {"toolchain", "sdk"}
            or member.mode != 0o755
        ):
            raise ValueError(
                "Every command tool requires executable SDK/toolchain input custody"
            )
        if ContentIdentity.parse_uri("sha256:" + member.sha256) != identity:
            raise ValueError("Command tool bytes differ from their declared identity")
    if profile.toolchain_identity != retained_role_identity(
        manifest, {"sdk", "toolchain"}
    ):
        raise ValueError("Retained SDK/toolchain closure is incomplete or changed")
    if profile.dependency_identity != retained_role_identity(manifest, {"dependency"}):
        raise ValueError("Retained dependency closure is incomplete or changed")
    for role, identity in (
        ("test-policy", profile.test_policy_identity),
        ("release-policy", profile.release_policy_identity),
    ):
        policy = [member for member in manifest.members if member.role == role]
        if (
            len(policy) != 1
            or ContentIdentity.parse_uri("sha256:" + policy[0].sha256) != identity
        ):
            raise ValueError(
                "Retained policy requires exactly one current original policy input"
            )
    required_roles = {
        "source",
        "build-definition",
        "test-definition",
        "docs-definition",
    }
    if not required_roles <= {member.role for member in manifest.members}:
        raise ValueError(
            "Retained manifest omits mandatory source/build/test/docs definitions"
        )
    # Re-open all authority after streaming input capture, including conversion state.
    if (
        validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        != project_identity
        or current_project_component_lock_identities(project) != locks
        or FilesystemConversionAuthorityStore(project.root).load_optional()
        != conversion
    ):
        raise ValueError("Project authority changed during retained planning")
    binding.require_unchanged()
    return RetainedProjectPlan(
        project.definition.project_id,
        project_identity,
        locks,
        conversion.identity,
        canonical_identity(binding.trust_binding.to_dict()),
        manifest.identity,
        profile,
    )

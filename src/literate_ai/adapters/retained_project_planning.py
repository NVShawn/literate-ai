"""Read-only admission planning for original-authoritative retained projects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.component_lock_application import (
    current_project_component_lock_identities,
    current_project_component_locks,
    project_component_roots,
)
from literate_ai.adapters.conversion_authority import FilesystemConversionAuthorityStore
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.adapters.retained_project_acceptance import (
    read_retained_acceptance_policy,
)
from literate_ai.adapters.retained_project_inputs import verify_retained_project_inputs
from literate_ai.adapters.retained_project_release import (
    read_retained_project_release_policy,
)
from literate_ai.adapters.retained_project_testing import (
    read_retained_project_test_policy,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    ResolvedStandardProjectLifecycleDriver,
)
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from literate_ai.contracts.retained_component_ownership import (
    require_registered_retained_owners,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectPlan,
    RetainedProjectProfile,
)
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.sbom import CycloneDxManagedGraph, ManagedComponentKind
from literate_ai.projects import LoadedProject, ProjectConfigurationStore


def retained_role_identity(
    manifest: RetainedProjectManifest, roles: set[str]
) -> ContentIdentity:
    """Bind the complete declared byte closure for a set of input ownership roles."""
    return manifest.role_identity(roles)


@dataclass(frozen=True, slots=True)
class RetainedProjectPlanningResult:
    plan: RetainedProjectPlan
    managed_sbom_graph: CycloneDxManagedGraph
    owned_components: tuple[ContentIdentity, ...] = ()
    owned_inputs: int = 0
    ownership_catalog_identity: ContentIdentity | None = None

    def __post_init__(self):
        if not isinstance(self.plan, RetainedProjectPlan) or not isinstance(
            self.managed_sbom_graph, CycloneDxManagedGraph
        ):
            raise TypeError("retained planning requires a typed plan and managed graph")
        root = next(c for c in self.managed_sbom_graph.components if c.kind.is_root)
        if (
            self.managed_sbom_graph.resolved_graph_identity != self.plan.identity
            or root.kind is not ManagedComponentKind.RETAINED_PROJECT
            or root.identity != self.plan.identity
        ):
            raise ValueError("retained.planning.managed-graph-mismatch")
        if (
            type(self.owned_components) is not tuple
            or any(
                not isinstance(item, ContentIdentity) for item in self.owned_components
            )
            or self.owned_components
            != tuple(sorted(set(self.owned_components), key=lambda i: i.uri))
        ):
            raise ValueError("retained.planning.invalid-ownership-summary")
        if type(self.owned_inputs) is not int or (
            (
                self.owned_components
                and (
                    self.owned_inputs <= 0
                    or not isinstance(self.ownership_catalog_identity, ContentIdentity)
                )
            )
            or (
                not self.owned_components
                and (
                    self.owned_inputs != 0
                    or self.ownership_catalog_identity is not None
                )
            )
        ):
            raise ValueError("retained.planning.invalid-ownership-summary")

    def to_dict(self):
        value = {
            "plan": self.plan.to_dict(),
            "managed_sbom_graph": self.managed_sbom_graph.to_dict(),
            "authorization_identity": self.plan.identity.uri,
            "admitted": False,
            "release_authority": "original-source",
        }
        if self.owned_components:
            value["component_ownership"] = {
                "state": "ownership-validated",
                "catalog_identity": self.ownership_catalog_identity.uri,
                "registered_component_authorings": [
                    identity.uri for identity in self.owned_components
                ],
                "input_nodes": self.owned_inputs,
                "native_action_admission": False,
                "acceptance": "not-qualified",
            }
        return value


def plan_retained_project_with_graph(
    project: LoadedProject,
    binding: ResolvedStandardProjectLifecycleDriver,
    manifest: RetainedProjectManifest,
    profile: RetainedProjectProfile,
    *,
    worker_identity: ContentIdentity,
    target: str,
    profile_path: Path,
    profile_bytes: bytes,
) -> RetainedProjectPlanningResult:
    """Validate current authority and complete retained inputs without acquisition.

    No native SDK builder, model provider, process runner, or cache writer is called.
    The resulting identity is a review boundary only, never acceptance evidence.
    """
    if profile.target != target or profile.worker_identity != worker_identity:
        raise ValueError("Retained profile names another target or worker")
    if ProjectConfigurationStore(project.root).read().definition != project.definition:
        raise ValueError("retained.planning.project-definition-changed")
    binding.require_unchanged()
    project_identity = validated_project_authority_identity(
        project.root, synchronize_source_intelligence=False
    )
    current_locks = current_project_component_locks(project)
    locks = tuple(lock.identity for lock in current_locks)
    ownership_catalog = None
    owned_components = ()
    if manifest.component_ownership:
        ownership_catalog = FilesystemLockedGenerationAuthorityReader().load_catalog(
            project_component_roots(project)[0]
        )
        owned_components = require_registered_retained_owners(
            manifest.component_ownership, ownership_catalog.authorings
        )
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
    read_retained_project_test_policy(
        project.root, manifest, profile, project.definition.test_receipt_policy
    )
    read_retained_acceptance_policy(project.root, profile, locks, manifest)
    read_retained_project_release_policy(
        project.root, manifest, profile.release_policy_identity
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
        ProjectConfigurationStore(project.root).read().definition != project.definition
        or validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        != project_identity
        or current_project_component_lock_identities(project) != locks
        or FilesystemConversionAuthorityStore(project.root).load_optional()
        != conversion
    ):
        raise ValueError("Project authority changed during retained planning")
    binding.require_unchanged()
    if ownership_catalog is not None:
        ownership_catalog.require_unchanged()
    plan = RetainedProjectPlan(
        project.definition.project_id,
        project_identity,
        locks,
        conversion.identity,
        canonical_identity(binding.trust_binding.to_dict()),
        manifest.identity,
        profile,
    )
    return RetainedProjectPlanningResult(
        plan,
        project_retained_project_managed_graph(
            plan, current_locks, version=project.definition.version
        ),
        owned_components,
        len(manifest.members) + len(manifest.directories) if owned_components else 0,
        ownership_catalog.catalog_audit_identity
        if ownership_catalog is not None
        else None,
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
    """Preserve the identity-only planning API with the same complete graph checks."""
    return plan_retained_project_with_graph(
        project,
        binding,
        manifest,
        profile,
        worker_identity=worker_identity,
        target=target,
        profile_path=profile_path,
        profile_bytes=profile_bytes,
    ).plan

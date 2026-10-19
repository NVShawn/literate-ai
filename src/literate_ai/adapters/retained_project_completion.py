"""Bind a whole retained run for later admission; never issue a receipt here."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.adapters.retained_project_acceptance import (
    RetainedProjectAcceptanceObservation,
)
from literate_ai.adapters.retained_project_boms import (
    RetainedProjectResolvedBom,
    RetainedProjectSourceBom,
)
from literate_ai.adapters.retained_project_execution import (
    RetainedProjectCommandObservation,
)
from literate_ai.adapters.retained_project_testing import (
    RetainedProjectVerifiedTestReport,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestReceiptPolicy,
    canonical_identity,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_tests import RetainedProjectTestPolicy


def _under(path, roots):
    while path:
        if path in roots:
            return True
        path = path.rpartition("/")[0]
    return False


def _stage_manifest(manifest, roots):
    roots = set(roots)
    return RetainedProjectManifest(
        tuple(sorted(roots)),
        tuple(member for member in manifest.members if _under(member.path, roots)),
        manifest.limits,
        tuple(path for path in manifest.directories if _under(path, roots)),
    )


@dataclass(frozen=True, slots=True)
class RetainedProjectCompletedRun:
    """Immutable joined observations, without SDK/package/release acceptance.

    Construction checks relationships, not the authenticity of transported facts.
    The live workspace must revalidate inputs, policies and exports before returning
    this object. Consumers still need their own receipt and artifact admission.
    """

    source_bom: RetainedProjectSourceBom
    resolved_bom: RetainedProjectResolvedBom
    observations: tuple[RetainedProjectCommandObservation, ...]
    test_policy: RetainedProjectTestPolicy
    configured_test_policy: ProjectTestReceiptPolicy
    test_reports: tuple[RetainedProjectVerifiedTestReport, ...]
    acceptance_policy: RetainedProjectAcceptancePolicy
    acceptance_observations: tuple[RetainedProjectAcceptanceObservation, ...]
    output_manifest: RetainedProjectManifest
    native_sdk_final_check_identity: ContentIdentity | None = None

    def __post_init__(self):
        if (
            not isinstance(self.source_bom, RetainedProjectSourceBom)
            or not isinstance(self.resolved_bom, RetainedProjectResolvedBom)
            or not isinstance(self.output_manifest, RetainedProjectManifest)
            or not isinstance(self.test_policy, RetainedProjectTestPolicy)
            or not isinstance(self.configured_test_policy, ProjectTestReceiptPolicy)
            or not isinstance(self.acceptance_policy, RetainedProjectAcceptancePolicy)
        ):
            raise TypeError("retained.completion.typed-authority-required")
        for values, kind in (
            (self.observations, RetainedProjectCommandObservation),
            (self.test_reports, RetainedProjectVerifiedTestReport),
            (self.acceptance_observations, RetainedProjectAcceptanceObservation),
        ):
            if type(values) is not tuple or any(
                not isinstance(v, kind) for v in values
            ):
                raise TypeError("retained.completion.typed-observations-required")
        plan = self.source_bom.plan
        profile = plan.profile
        if self.resolved_bom.source != self.source_bom:
            raise ValueError("retained.completion.bom-source-mismatch")
        if (
            len(self.observations) != len(profile.actions)
            or tuple(item.action for item in self.observations) != profile.actions
            or any(
                item.plan_identity != plan.identity or item.result.returncode != 0
                for item in self.observations
            )
        ):
            raise ValueError(
                "retained.completion.complete-successful-commands-required"
            )
        if (
            len({item.environment_identity for item in self.observations}) != 1
            or len({item.dependency_identity for item in self.observations}) != 1
        ):
            raise ValueError("retained.completion.command-context-mismatch")
        sdk_mode = any(m.role == "sdk-policy" for m in self.source_bom.manifest.members)
        sdk_identities = {item.native_sdk_inputs_identity for item in self.observations}
        if (
            len(sdk_identities) != 1
            or (None in sdk_identities) == sdk_mode
            or (
                sdk_mode
                and not isinstance(
                    self.native_sdk_final_check_identity, ContentIdentity
                )
            )
            or (not sdk_mode and self.native_sdk_final_check_identity is not None)
        ):
            raise ValueError("retained.completion.sdk-authority-mismatch")
        sdk_counts = {len(item.native_sdk_executions) for item in self.observations}
        if len(sdk_counts) != 1 or bool(next(iter(sdk_counts))) != bool(
            self.resolved_bom.sdk_dependencies
        ):
            raise ValueError("retained.completion.sdk-command-closure-mismatch")
        roots = []
        stage_manifests = {}
        build_manifest = None
        for item in self.observations:
            roots.extend(item.action.outputs)
            if roots:
                root_set = set(roots)
                ancestors = set()
                for root in roots:
                    parent = root.rpartition("/")[0]
                    while parent:
                        ancestors.add(parent)
                        parent = parent.rpartition("/")[0]
                expected = _stage_manifest(self.output_manifest, roots)
                actual = item.output_manifest
                if (
                    actual is None
                    or actual.members != expected.members
                    or actual.limits != expected.limits
                    or _stage_manifest(actual, roots) != expected
                    or any(
                        not _under(path, root_set) and path not in ancestors
                        for path in actual.directories
                    )
                ):
                    raise ValueError("retained.completion.command-export-mismatch")
                stage_manifests[item.action.action_id] = expected
                if item.action.stage == "build":
                    build_manifest = expected
            elif item.output_manifest is not None:
                raise ValueError("retained.completion.undeclared-command-exports")
        if (
            self.output_manifest.limits != self.source_bom.manifest.limits
            or _stage_manifest(self.output_manifest, roots) != self.output_manifest
            or build_manifest != self.resolved_bom.output_manifest
        ):
            raise ValueError("retained.completion.final-export-mismatch")
        self.test_policy.require_profile(profile, self.configured_test_policy)
        self.test_policy.require_complete(
            (item.report for item in self.test_reports), self.configured_test_policy
        )
        by_action = {item.action.action_id: item for item in self.observations}
        report_paths = dict(self.test_policy.reports)
        members = {member.path: member for member in self.output_manifest.members}
        package_nodes = set(members) | set(self.output_manifest.directories)
        for path in tuple(package_nodes):
            parent = path.rpartition("/")[0]
            while parent:
                package_nodes.add(parent)
                parent = parent.rpartition("/")[0]
        if not set(profile.package_inventory) <= package_nodes:
            raise ValueError("retained.completion.package-inventory-missing")
        for item in self.test_reports:
            action_id = item.report.action_id
            member = members.get(report_paths[action_id])
            if (
                member is None
                or item.plan_identity != plan.identity
                or item.policy_identity != self.test_policy.identity
                or item.observation_identity != by_action[action_id].identity
                or item.blob_identity.uri != "sha256:" + member.sha256
            ):
                raise ValueError("retained.completion.test-observation-mismatch")
        self.acceptance_policy.require_profile(profile, plan.component_locks)
        expected_cases = tuple(
            case
            for action in profile.actions
            if action.stage == "accept"
            for case in self.acceptance_policy.cases
            if case.action_id == action.action_id
        )
        if len(expected_cases) != len(self.acceptance_observations):
            raise ValueError("retained.completion.complete-acceptance-required")
        from literate_ai.adapters.retained_project_filesystem import (
            retained_filesystem_matches,
        )

        for case, item in zip(
            expected_cases, self.acceptance_observations, strict=True
        ):
            if len(item.native_sdk_executions) not in sdk_counts:
                raise ValueError("retained.completion.sdk-case-closure-mismatch")
            if (case.filesystem is None) != (item.filesystem_observation is None) or (
                case.filesystem is not None
                and not retained_filesystem_matches(
                    case.filesystem, item.filesystem_observation
                )
            ):
                raise ValueError("retained.completion.filesystem-observation-mismatch")
            if (
                item.plan_identity != plan.identity
                or item.policy_identity != self.acceptance_policy.identity
                or item.case_identity != case.identity
                or item.output_manifest_identity
                != stage_manifests[case.action_id].identity
                or (item.exit_code, item.result.stdout, item.result.stderr)
                != (case.expected_exit, case.expected_stdout, case.expected_stderr)
            ):
                raise ValueError("retained.completion.acceptance-observation-mismatch")

    def to_dict(self):
        plan = self.source_bom.plan
        return {
            "schema": "literate-ai/retained-project-completed-run@1",
            "authority_mode": "retained-project",
            "plan_identity": plan.identity.uri,
            "source_observation_identity": self.source_bom.identity.uri,
            "resolved_observation_identity": self.resolved_bom.identity.uri,
            "command_observations": [item.identity.uri for item in self.observations],
            "test_policy_identity": self.test_policy.identity.uri,
            "project_test_policy_identity": self.configured_test_policy.identity.uri,
            "test_observations": [item.identity.uri for item in self.test_reports],
            "acceptance_policy_identity": self.acceptance_policy.identity.uri,
            "acceptance_observations": [
                item.identity.uri for item in self.acceptance_observations
            ],
            "output_manifest_identity": self.output_manifest.identity.uri,
            "package_inventory": list(plan.profile.package_inventory),
            "sdk_admitted": False,
            "lifecycle_receipt": False,
            "release_authority": "original-source",
            **(
                {
                    "native_sdk_final_check_identity": (
                        self.native_sdk_final_check_identity.uri
                    )
                }
                if self.native_sdk_final_check_identity is not None
                else {}
            ),
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

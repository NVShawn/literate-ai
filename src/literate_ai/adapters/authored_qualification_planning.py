"""Read-only authored planning through actual current authority owners."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.authored_qualification_authority import (
    read_current_authored_qualification_authority,
)
from literate_ai.adapters.authored_qualification_oracles import (
    CurrentAuthoredQualificationOracles,
    read_current_authored_qualification_oracles,
)
from literate_ai.adapters.generation_preparation import (
    LockedComponentModelSelectionAdapter,
)
from literate_ai.adapters.models import (
    InheritedSessionProviderConfig,
    InheritedSessionSelection,
    select_coding_cli,
    selected_coding_provider,
)
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.adapters.standard_lifecycle_binding import (
    ResolvedStandardProjectLifecycleDriver,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectPlanningAdapter,
    PlannedStandardProject,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts import ContentIdentity, StandardProjectLifecycleDriver
from literate_ai.contracts.authored_qualification_plans import (
    AuthoredQualificationPlan,
    AuthoredQualificationProjectReview,
)
from literate_ai.projects import ProjectConfigurationStore, discover_project


def _coding_selection():
    if selected_coding_provider() == "inherited-session":
        return InheritedSessionSelection(
            InheritedSessionProviderConfig.from_environment().provider_identity
        )
    return select_coding_cli()


def _project_roots(oracles):
    authority = oracles.authority
    roots = {authority.project.root}
    for node in authority.locked.authority.lock.nodes:
        component = authority.locked.component_root(node.revision.authoring_identity)
        project = discover_project(component)
        if project is None:
            raise ValueError("qualification.authored.project_review_missing")
        roots.add(project.root)
    return tuple(sorted(roots))


def _reviews(oracles, reviewer):
    oracles.require_unchanged()
    rows = set()
    for root in _project_roots(oracles):
        configuration = ProjectConfigurationStore(root).read()
        review = reviewer(root, synchronize_source_intelligence=False)
        if not isinstance(review, ContentIdentity):
            raise TypeError("current project review must return a ContentIdentity")
        rows.add(
            AuthoredQualificationProjectReview(configuration.content_identity, review)
        )
    oracles.require_unchanged()
    return tuple(
        sorted(
            rows,
            key=lambda r: (
                r.project_configuration_identity.uri,
                r.reviewed_authority_identity.uri,
            ),
        )
    )


def _require_output_directory(root, relative):
    # Planning allocates nothing. Apply still needs its original output reservation
    # and recoverable writer; this observation supplies no publication permission.
    for part in Path(relative).parts:
        root = root / part
        if root.exists() or root.is_symlink():
            require_safe_directory(root)
        else:
            break


@dataclass(frozen=True, slots=True)
class CurrentAuthoredQualificationPlan:
    """Live observations accompanying a passive plan; deliberately no decoder."""

    plan: AuthoredQualificationPlan
    oracles: CurrentAuthoredQualificationOracles
    binding: ResolvedStandardProjectLifecycleDriver
    standard: PlannedStandardProject
    _reviewer: object = field(repr=False, compare=False)
    _coding_selector: object = field(repr=False, compare=False)
    _pipeline_model: str | None = field(default=None, repr=False, compare=False)

    def require_unchanged(self):
        self.oracles.require_unchanged()
        self.binding.require_unchanged()
        self.standard.coding_cli.require_unchanged()
        current_selection = self._coding_selector()
        current_selection.require_unchanged()
        current_standard = FilesystemStandardProjectPlanningAdapter(
            coding_cli_selector=lambda: current_selection,
            model_selector=LockedComponentModelSelectionAdapter(
                pipeline_model=self._pipeline_model
            ),
        ).plan(self.oracles.authority.locked)
        if (
            self.oracles.authority.origin != self.plan.origin
            or self.oracles.identity != self.plan.oracle_set_identity
            or self.oracles.profile.identity != self.plan.profile_identity
            or self.oracles.profile.minimum_clean_runs != self.plan.minimum_clean_runs
            or self.standard.execution_plan != self.plan.execution_plan
            or current_standard.execution_plan != self.plan.execution_plan
            or ContentIdentity.parse_uri(self.standard.coding_cli.tool_binding_identity)
            != self.plan.coding_provider_binding_identity
            or current_selection.name != self.standard.coding_cli.name
            or ContentIdentity.parse_uri(current_selection.tool_binding_identity)
            != self.plan.coding_provider_binding_identity
            or self.binding.driver.identity != self.plan.driver_identity
            or self.binding.policy.identity != self.plan.lifecycle_policy_identity
            or self.binding.distribution.identity
            != self.plan.framework_distribution_identity
            or _reviews(self.oracles, self._reviewer) != self.plan.project_reviews
        ):
            raise ValueError("qualification.authored.plan_changed")
        _require_output_directory(
            self.oracles.authority.project.root, self.plan.output_root
        )
        self.oracles.require_unchanged()


class FilesystemAuthoredQualificationPlanner:
    def __init__(
        self,
        *,
        reviewer=validated_project_authority_identity,
        binding_resolver=resolve_standard_project_lifecycle_driver,
        coding_selector=_coding_selection,
    ):
        self.reviewer = reviewer
        self.binding_resolver = binding_resolver
        self.coding_selector = coding_selector

    def plan(
        self,
        project_root: Path,
        selection: GenerationPreparationRequest,
        *,
        profile_path="litai/qualification.json",
        output_root="litai/qualification",
        pipeline_model=None,
    ) -> CurrentAuthoredQualificationPlan:
        authority = read_current_authored_qualification_authority(
            project_root, selection
        )
        oracles = read_current_authored_qualification_oracles(authority, profile_path)
        with oracles.capture_current():
            reviews = _reviews(oracles, self.reviewer)
            driver = authority.project.definition.lifecycle_driver
            if not isinstance(driver, StandardProjectLifecycleDriver):
                raise ValueError("qualification.authored.standard_driver_required")
            binding = self.binding_resolver(driver)
            if (
                not isinstance(binding, ResolvedStandardProjectLifecycleDriver)
                or binding.driver != driver
            ):
                raise TypeError("original resolved Standard binding required")
            standard = FilesystemStandardProjectPlanningAdapter(
                coding_cli_selector=self.coding_selector,
                model_selector=LockedComponentModelSelectionAdapter(
                    pipeline_model=pipeline_model
                ),
            ).plan(authority.locked)
            plan = AuthoredQualificationPlan(
                authority.origin,
                reviews,
                oracles.profile.identity,
                oracles.identity,
                standard.execution_plan,
                ContentIdentity.parse_uri(standard.coding_cli.tool_binding_identity),
                binding.driver.identity,
                binding.policy.identity,
                binding.distribution.identity,
                output_root,
                oracles.profile.minimum_clean_runs,
            )
            current = CurrentAuthoredQualificationPlan(
                plan,
                oracles,
                binding,
                standard,
                self.reviewer,
                self.coding_selector,
                pipeline_model,
            )
            current.require_unchanged()
        current.require_unchanged()
        return current

"""Compose current project, generation, verifier and persisted provider authority."""

from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.authority import FileAuthorityProjectionStore
from literate_ai.adapters.qualification_authority import (
    CurrentQualificationAuthority,
    read_current_qualification_authority,
)
from literate_ai.adapters.retained_provider_generation import (
    RetainedProviderGeneration,
    read_retained_provider_generation,
)
from literate_ai.adapters.retained_provider_promotion import (
    CurrentQualifiedPromotion,
    read_current_qualified_promotion,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    ResolvedStandardProjectLifecycleDriver,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.application.source_promotion import SourcePromotionService
from literate_ai.contracts import StandardProjectLifecycleDriver
from literate_ai.projects import (
    PROJECT_FILENAME,
    LoadedProject,
    PinnedInputClosure,
    ProjectConfigurationStore,
)


@dataclass(frozen=True, slots=True)
class RetainedProviderAuthority:
    project: LoadedProject
    generation: RetainedProviderGeneration
    qualification: CurrentQualificationAuthority
    promotion: CurrentQualifiedPromotion
    lifecycle: ResolvedStandardProjectLifecycleDriver
    _configuration: PinnedInputClosure = field(repr=False, compare=False)

    def require_unchanged(self) -> None:
        require_safe_directory(self.project.root)
        self._configuration.require_unchanged()
        self.generation.require_unchanged()
        self.qualification.require_unchanged()
        self.lifecycle.require_unchanged()
        self.promotion.require_unchanged()
        self.generation.require_unchanged()
        self.qualification.require_unchanged()
        self.lifecycle.require_unchanged()
        self._configuration.require_unchanged()


def read_retained_provider_authority(
    project_root: Path,
    request: GenerationPreparationRequest,
    *,
    provider_id: str,
    profile_path: str,
    pipeline_model: str | None = None,
    maximum_file_bytes: int = 16 * 1024 * 1024,
) -> RetainedProviderAuthority:
    """Resolve provider expectations from current files and installed authority.

    The importer selects the project, Component/target, logical model and profile
    independently of candidate archives. This reads the configured Standard pin;
    it never advances it or instantiates a rebuild runtime. Native tool/command and
    library oracle resolution, importer review, full archive reopening and consumer
    execution remain additional admission steps.
    """
    if type(maximum_file_bytes) is not int or maximum_file_bytes <= 0:
        raise ValueError("retained.provider.file-limit-invalid")
    root = Path(project_root).absolute()
    if ".." in root.parts:
        raise ValueError("retained.provider.project-path-unsafe")
    require_safe_directory(root)
    configuration = ProjectConfigurationStore(
        root, maximum_bytes=maximum_file_bytes
    ).read()
    inputs = PinnedInputClosure(
        maximum_files=1,
        maximum_file_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_file_bytes,
    )
    inputs.pin(
        root / PROJECT_FILENAME,
        boundary=root,
        label=PROJECT_FILENAME,
        expected_content=configuration.content,
    )
    driver = configuration.definition.lifecycle_driver
    if not isinstance(driver, StandardProjectLifecycleDriver):
        raise ValueError("retained.provider.standard-driver-required")
    lifecycle = resolve_standard_project_lifecycle_driver(driver)
    generation = read_retained_provider_generation(
        request, provider_id=provider_id, pipeline_model=pipeline_model
    )
    if not generation.prepared.component_root.is_relative_to(root):
        raise ValueError("retained.provider.component-outside-project")
    authority = generation.prepared.locked_authority_snapshot.authority
    projection = FileAuthorityProjectionStore(root).current(
        authority.root_authoring.coordinate.uri
    )
    evidence = SourcePromotionService().verify_evidence(root, projection)
    result = evidence.qualification_lifecycle_result
    if result is None:
        raise ValueError("retained.provider.qualification-required")
    qualification = read_current_qualification_authority(
        root,
        profile_path,
        source_snapshot_identity=projection.source_snapshot_identity,
        maximum_bytes=maximum_file_bytes,
    )
    closure = generation.generation_closure(
        root, evidence, qualification_identity=result.identity
    )
    promotion = read_current_qualified_promotion(
        root,
        authority,
        current_generation_closure=closure,
        current_verifier_identity=qualification.case_map.verifier_identity,
        current_policy_identity=lifecycle.policy.identity,
    )
    if promotion.evidence != evidence:
        raise ValueError("retained.provider.promotion-changed")
    if any(
        run.driver_identity != driver.identity
        or run.framework_distribution_identity != lifecycle.distribution.identity
        for run in result.runs
    ):
        raise ValueError("retained.provider.lifecycle-binding-stale")
    current = RetainedProviderAuthority(
        LoadedProject(configuration.root, configuration.definition),
        generation,
        qualification,
        promotion,
        lifecycle,
        inputs,
    )
    current.require_unchanged()
    return current

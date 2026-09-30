"""Filesystem application adapter for one complete Standard project rebuild."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from literate_ai.application import (
    StandardTestReceiptProjectionError,
    project_standard_project_test_receipt,
)
from literate_ai.application.component_generation_context import (
    GenerationComplexityBudget,
)
from literate_ai.application.component_generation_scheduling import (
    ComponentInvalidationDecision,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptProvisional,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
    StandardProjectLifecycleDriver,
    StandardSourceSelectorSet,
    canonical_identity,
    rebuild_project_authority_identity,
)
from literate_ai.projects import LoadedProject, ProjectError
from literate_ai.storage import FileSystemCAS
from literate_ai.test_receipts import update_project_test_receipt_finalized_value

from .cache import FileSystemSourceCache, SourceCacheMaterializer, SourceCacheResolver
from .generation_preparation import (
    LockedComponentNodePreparationAdapter,
    PreparedLockedGeneration,
)
from .intelligence import DisabledGenerationIndexer
from .lifecycle import (
    LocalIndependentAcceptanceOracle,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
)
from .project_validation import validated_project_authority_identity
from .retained_source import RetainedSourceInput
from .standard_lifecycle_binding import (
    ResolvedStandardProjectLifecycleDriver,
    resolve_standard_project_lifecycle_driver,
)
from .standard_project import (
    ExecutedStandardProject,
    FilesystemStandardProjectPlanningAdapter,
    FilesystemStandardProjectRuntime,
    FilesystemStandardSourceGenerationAdapter,
    StandardAcceptedSourceContinuationError,
    StandardProjectExecutionRequest,
    admit_locked_authored_assets,
    assemble_filesystem_standard_project_runtime,
    compose_filesystem_standard_lifecycle_checkpoints,
    compose_filesystem_standard_source_cache,
    project_locked_standard_toolchain_closure,
)


class FilesystemStandardRebuildError(RuntimeError):
    """Stable failure from the public Standard filesystem composition boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


ProjectAuthorityValidator = Callable[[Path], ContentIdentity]


@dataclass(frozen=True, slots=True)
class FilesystemStandardRebuildRequest:
    prepared: PreparedLockedGeneration
    source_root: Path
    invalidation: ComponentInvalidationDecision
    update_receipt: bool = False
    max_parallelism: int = 1
    budget: GenerationComplexityBudget | None = None
    accepted_source_only: bool = False
    source_selectors: StandardSourceSelectorSet | None = None
    directory_custody_identity: ContentIdentity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prepared, PreparedLockedGeneration):
            raise TypeError("prepared must be a PreparedLockedGeneration")
        if not isinstance(self.source_root, Path):
            raise TypeError("source_root must be a Path")
        if not isinstance(self.invalidation, ComponentInvalidationDecision):
            raise TypeError("invalidation must be a ComponentInvalidationDecision")
        if type(self.update_receipt) is not bool:
            raise TypeError("update_receipt must be a bool")
        if type(self.accepted_source_only) is not bool:
            raise TypeError("accepted_source_only must be a bool")
        if self.directory_custody_identity is not None and not isinstance(
            self.directory_custody_identity, ContentIdentity
        ):
            raise TypeError("directory_custody_identity must be a ContentIdentity")
        if self.accepted_source_only and not isinstance(
            self.source_selectors, StandardSourceSelectorSet
        ):
            raise TypeError("accepted_source_only requires source selectors")
        if self.accepted_source_only and self.directory_custody_identity is None:
            raise TypeError(
                "accepted_source_only requires explicit directory custody identity"
            )
        if (
            not isinstance(self.max_parallelism, int)
            or not 1 <= self.max_parallelism <= 256
        ):
            raise ValueError("max_parallelism must be between 1 and 256")


@dataclass(frozen=True, slots=True)
class FilesystemStandardRebuildResult:
    project_revision_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    lifecycle_invocation_identity: ContentIdentity
    execution: ExecutedStandardProject
    receipt: ProjectTestReceipt
    finalized_receipt: ProjectTestReceiptFinalizedCandidate
    receipt_update: dict[str, object] | None


class FilesystemStandardRebuildAdapter:
    """Own host paths, current authority, execution, and receipt finalization."""

    def __init__(
        self,
        *,
        project: LoadedProject,
        binding: ResolvedStandardProjectLifecycleDriver,
        runtime: FilesystemStandardProjectRuntime,
        source_cache_configuration: SourceCacheConfiguration,
        authority_validator: ProjectAuthorityValidator = (
            validated_project_authority_identity
        ),
        retained_source: RetainedSourceInput | None = None,
        retained_source_authorization: str | None = None,
    ) -> None:
        if not isinstance(project, LoadedProject):
            raise TypeError("project must be a LoadedProject")
        if not isinstance(binding, ResolvedStandardProjectLifecycleDriver):
            raise TypeError("binding must be a ResolvedStandardProjectLifecycleDriver")
        if not isinstance(runtime, FilesystemStandardProjectRuntime):
            raise TypeError("runtime must be a FilesystemStandardProjectRuntime")
        if not isinstance(source_cache_configuration, SourceCacheConfiguration):
            raise TypeError(
                "source_cache_configuration must be a SourceCacheConfiguration"
            )
        driver = project.definition.lifecycle_driver
        if (
            not isinstance(driver, StandardProjectLifecycleDriver)
            or driver != binding.driver
        ):
            raise FilesystemStandardRebuildError(
                "standard_rebuild.binding_mismatch",
                "resolved Standard binding differs from project authority",
            )
        self.project = project
        self.binding = binding
        self.runtime = runtime
        self.source_cache_configuration = source_cache_configuration
        self.authority_validator = authority_validator
        self.retained_source = retained_source
        if retained_source is not None:
            retained_source.require_authorization(retained_source_authorization)

    def rebuild(
        self, request: FilesystemStandardRebuildRequest
    ) -> FilesystemStandardRebuildResult:
        if not isinstance(request, FilesystemStandardRebuildRequest):
            raise TypeError("request must be a FilesystemStandardRebuildRequest")
        prepared = request.prepared
        snapshot = prepared.locked_authority_snapshot
        if prepared.boundary != self.project.root:
            raise FilesystemStandardRebuildError(
                "standard_rebuild.project_mismatch",
                "prepared Component authority does not belong to this project",
            )
        source_root = request.source_root.resolve()
        if source_root == self.project.root or source_root.is_relative_to(
            self.project.root
        ):
            raise FilesystemStandardRebuildError(
                "standard_rebuild.source_root_inside_project",
                "generated source runtime must remain outside project authority",
            )
        self.binding.require_unchanged()
        snapshot.require_unchanged()
        validated = self.authority_validator(self.project.root)
        if self.retained_source is not None:
            self.retained_source.require_unchanged()
            if (
                request.accepted_source_only
                or self.retained_source.project_authority_identity != validated
                or self.retained_source.component_lock_identity
                != snapshot.authority.lock.identity
                or self.retained_source.target != snapshot.authority.lock.target_name
            ):
                raise FilesystemStandardRebuildError(
                    "retained_source.authority_mismatch",
                    "Retained source authority changed or conflicts with "
                    "accepted-only mode",
                )
        project_revision = rebuild_project_authority_identity(
            validated,
            (snapshot.authority.lock.identity,),
        )
        planned = self.runtime.plan(snapshot)
        readiness = self.runtime.production_readiness(planned)
        if not readiness.ready:
            raise FilesystemStandardRebuildError(
                "standard_rebuild.runtime_unready",
                "Standard runtime lacks required capabilities: "
                + ", ".join(readiness.blockers),
            )
        lifecycle_request = canonical_identity(
            {
                "schema": "literate-ai/standard-filesystem-rebuild-request@1",
                "project_id": self.project.definition.project_id,
                "project_revision_identity": project_revision.uri,
                "component_root": prepared.component_root.relative_to(
                    self.project.root
                ).as_posix(),
                "component_lock_identity": snapshot.authority.lock.identity.uri,
                "execution_plan_identity": planned.execution_plan.identity.uri,
                "invalidation_identity": request.invalidation.identity.uri,
                "standard_driver_identity": self.binding.driver.identity.uri,
                "standard_policy_identity": self.binding.policy.identity.uri,
                "framework_distribution_identity": (
                    self.binding.distribution.identity.uri
                ),
                "max_parallelism": request.max_parallelism,
                "accepted_source_only": request.accepted_source_only,
                **(
                    {"retained_source_identity": self.retained_source.identity.uri}
                    if self.retained_source is not None
                    else {}
                ),
                "directory_custody_identity": (
                    None
                    if request.directory_custody_identity is None
                    else request.directory_custody_identity.uri
                ),
            }
        )
        closure = self.runtime.toolchain_closure
        assert closure is not None  # guaranteed by production_readiness
        invocation = canonical_identity(
            {
                "schema": "literate-ai/standard-in-process-invocation@1",
                "lifecycle_request_identity": lifecycle_request.uri,
                "driver_identity": self.binding.driver.identity.uri,
                "policy_identity": self.binding.policy.identity.uri,
                "framework_distribution_identity": (
                    self.binding.distribution.identity.uri
                ),
                "toolchain_closure_identity": closure.record.identity.uri,
            }
        )
        try:
            execution = self.runtime.execute(
                snapshot,
                StandardProjectExecutionRequest(
                    planned,
                    source_root,
                    request.invalidation,
                    max_parallelism=request.max_parallelism,
                    budget=request.budget,
                    accepted_source_only=request.accepted_source_only,
                    expected_framework_distribution_identity=(
                        self.binding.distribution.identity
                    ),
                    worker_source_selectors=request.source_selectors,
                    directory_custody_identity=request.directory_custody_identity,
                    fresh_source=self.retained_source is not None,
                ),
            )
        except StandardAcceptedSourceContinuationError as exc:
            raise FilesystemStandardRebuildError(exc.code, exc.message) from exc
        except LocalStandardLifecycleError as exc:
            raise FilesystemStandardRebuildError(
                getattr(exc, "code", "standard_rebuild.independent_acceptance_failed"),
                getattr(exc, "message", str(exc)),
            ) from exc
        if not execution.lifecycle.successful:
            failures = tuple(
                item.failure_evidence
                for item in execution.lifecycle.node_results
                if item.failure_evidence is not None
            )
            detail = ", ".join(
                f"{item.phase.value}:{item.code} for "
                f"{item.component_revision.uri} "
                f"(evidence {item.identity.uri})"
                + (
                    f": {diagnostic}"
                    if (diagnostic := getattr(item, "diagnostic", None))
                    else ""
                )
                for item in failures
            )
            raise FilesystemStandardRebuildError(
                "standard_rebuild.lifecycle_failed",
                "Standard lifecycle did not produce an accepted project"
                + (f": {detail}" if detail else ""),
            )
        self.binding.require_unchanged()
        snapshot.require_unchanged()
        if self.authority_validator(self.project.root) != validated:
            raise FilesystemStandardRebuildError(
                "standard_rebuild.project_changed",
                "project authority changed during the Standard rebuild",
            )
        if self.retained_source is not None:
            self.retained_source.require_unchanged()
        receipt_policy = self.project.definition.test_receipt_policy
        if receipt_policy is None:
            raise FilesystemStandardRebuildError(
                "standard_rebuild.receipt_policy_unconfigured",
                "project must configure a test receipt policy",
            )
        try:
            receipt = project_standard_project_test_receipt(
                execution.lifecycle,
                project_id=self.project.definition.project_id,
                project_revision_identity=project_revision,
                lifecycle_policy=self.binding.policy,
                receipt_policy=receipt_policy,
                lifecycle_request_identity=lifecycle_request,
                lifecycle_invocation_identity=invocation,
                runner_identity=self.binding.driver.identity,
            )
            evidence = {item.kind: item.identity for item in receipt.evidence}
            source_cache_control_identity = canonical_identity(
                {
                    "schema": "literate-ai/standard-source-cache-control@1",
                    "lifecycle_request_identity": lifecycle_request.uri,
                    "project_revision_identity": project_revision.uri,
                    "component_lock_identities": [snapshot.authority.lock.identity.uri],
                    "configuration": self.source_cache_configuration.to_dict(),
                    "invalidation_identity": request.invalidation.identity.uri,
                }
            )
            provisional = ProjectTestReceiptProvisional(
                lifecycle_request_identity=lifecycle_request,
                lifecycle_command_identity=invocation,
                source_cache_control_identity=source_cache_control_identity,
                component_lock_identities=(snapshot.authority.lock.identity,),
                receipt_identity=receipt.identity,
                receipt=receipt,
            )
            finalized_receipt = ProjectTestReceiptFinalizedCandidate.finalize(
                provisional,
                source_cache_decision_identity=evidence["source-cache-decision"],
                source_cache_lifecycle_identity=evidence["source-cache-lifecycle"],
            )
            receipt_update = (
                update_project_test_receipt_finalized_value(
                    self.project,
                    finalized_receipt,
                    project_revision_identity=validated,
                )
                if request.update_receipt
                else None
            )
        except (ProjectError, StandardTestReceiptProjectionError) as exc:
            raise FilesystemStandardRebuildError(
                getattr(exc, "code", "standard_rebuild.receipt_invalid"),
                getattr(exc, "message", str(exc)),
            ) from exc
        return FilesystemStandardRebuildResult(
            project_revision,
            lifecycle_request,
            invocation,
            execution,
            receipt,
            finalized_receipt,
            receipt_update,
        )


_RUNTIME_TARGET_ID = "standard-local"


def _resolved_source_cache(
    project: LoadedProject, runtime_root: Path
) -> tuple[SourceCacheConfiguration, dict[str, FileSystemSourceCache]]:
    """Honour the project's declared cache targets, defaulting to the runtime one.

    The contract has always described "ordered read targets and the sole explicit write
    target", with project-relative roots a repository may commit and operator-bound
    roots supplied at runtime. Nothing exercised it: this composition hardcoded a single
    operator-bound target, so a project that declared a committed cache was ignored.

    Read order is a cost decision, not a safety one. A cache hit requires an exact
    generation-key match, and that key binds the ordered specification set, so a changed
    specification misses in every target. Committed source cannot go stale here; it can
    only be found sooner or later.
    """

    declared = project.definition.source_cache
    if declared is None or declared.mode is SourceCacheMode.OFF:
        target = SourceCacheTarget(
            target_id=_RUNTIME_TARGET_ID,
            root_kind=SourceCacheRootKind.OPERATOR_BOUND,
            root_reference="standard-local-source-cache",
        )
        return (
            SourceCacheConfiguration(
                mode=SourceCacheMode.READ_WRITE,
                targets=(target,),
                write_target_id=target.target_id,
                require_unique=True,
            ),
            {
                target.target_id: FileSystemSourceCache(
                    target.target_id,
                    runtime_root,
                )
            },
        )

    caches: dict[str, FileSystemSourceCache] = {}
    for target in declared.targets:
        if target.root_kind is SourceCacheRootKind.PROJECT_RELATIVE:
            root = project.root.joinpath(*Path(target.root_reference).parts)
        elif target.target_id == _RUNTIME_TARGET_ID:
            root = runtime_root
        else:
            # An operator-bound root the caller never supplied cannot be invented.
            raise FilesystemStandardRebuildError(
                "rebuild.source_cache_root_unbound",
                f"source-cache target {target.target_id!r} is operator-bound but no "
                "root was supplied for this run",
            )
        caches[target.target_id] = FileSystemSourceCache(
            target.target_id,
            root,
            writable=(
                declared.mode.can_write and target.target_id == declared.write_target_id
            ),
        )
    return declared, caches


def _configured_python_wheelhouse(
    explicit: Path | None, environment: Mapping[str, str]
) -> Path | None:
    """Private operator input location; never source/package authority."""
    value = explicit
    if value is None:
        raw = environment.get("LITAI_PYTHON_WHEELHOUSE")
        if raw is None:
            return None
        if not raw or "\x00" in raw:
            raise FilesystemStandardRebuildError(
                "standard_rebuild.python_wheelhouse_invalid",
                "LITAI_PYTHON_WHEELHOUSE must name an absolute wheel directory",
            )
        value = Path(raw)
    if not isinstance(value, Path) or not value.is_absolute():
        raise FilesystemStandardRebuildError(
            "standard_rebuild.python_wheelhouse_invalid",
            "Python wheel provisioning requires an absolute directory path",
        )
    return value


def assemble_filesystem_standard_rebuild_adapter(
    *,
    project: LoadedProject,
    prepared: PreparedLockedGeneration,
    object_root: Path,
    generated_source_cache_root: Path,
    source_cache_root: Path,
    candidate_cas_root: Path,
    accepted_cas_root: Path,
    checkpoint_root: Path,
    python_wheelhouse: Path | None = None,
    binding: ResolvedStandardProjectLifecycleDriver | None = None,
    independent_acceptance_oracle: LocalIndependentAcceptanceOracle | None = None,
    pipeline_model: str | None = None,
    accepted_source_provider_id: str | None = None,
    accepted_source_provider_identity: ContentIdentity | None = None,
    retained_source: RetainedSourceInput | None = None,
    retained_source_authorization: str | None = None,
    authority_validator: ProjectAuthorityValidator = (
        validated_project_authority_identity
    ),
) -> FilesystemStandardRebuildAdapter:
    """Compose every production Standard host capability behind one adapter."""

    if not isinstance(project, LoadedProject):
        raise TypeError("project must be a LoadedProject")
    if not isinstance(prepared, PreparedLockedGeneration):
        raise TypeError("prepared must be a PreparedLockedGeneration")
    driver = project.definition.lifecycle_driver
    if not isinstance(driver, StandardProjectLifecycleDriver):
        raise FilesystemStandardRebuildError(
            "standard_rebuild.driver_not_standard",
            "project does not select the Standard lifecycle driver",
        )
    selected_binding = (
        resolve_standard_project_lifecycle_driver(driver)
        if binding is None
        else binding
    )
    if selected_binding.driver != driver:
        raise FilesystemStandardRebuildError(
            "standard_rebuild.binding_mismatch",
            "supplied Standard binding differs from project authority",
        )
    selected_binding.require_unchanged()
    project_authority_identity = authority_validator(project.root)
    if retained_source is not None:
        retained_source.require_authorization(retained_source_authorization)
        if (
            retained_source.project_authority_identity != project_authority_identity
            or retained_source.component_lock_identity
            != prepared.locked_authority_snapshot.authority.lock.identity
            or retained_source.target
            != prepared.locked_authority_snapshot.authority.lock.target_name
            or len(prepared.locked_authority_snapshot.authority.lock.nodes) != 1
        ):
            raise FilesystemStandardRebuildError(
                "retained_source.authority_mismatch",
                "Retained source currently requires one exact locked Component",
            )
    source_generation = FilesystemStandardSourceGenerationAdapter.from_environment(
        project_root=project.root,
        cache_root=generated_source_cache_root,
        cas_root=candidate_cas_root,
        pipeline_model=pipeline_model,
        accepted_source_provider_id=accepted_source_provider_id,
        accepted_source_provider_identity=accepted_source_provider_identity,
        project_authority_identity=project_authority_identity,
        native_sdk_inputs=prepared.native_sdk_inputs,
    )
    planning = FilesystemStandardProjectPlanningAdapter(
        coding_cli_selector=lambda: source_generation.generator.selection,
        model_selector=source_generation.model_selector,
        native_sdk_inputs=prepared.native_sdk_inputs,
    )
    snapshot = prepared.locked_authority_snapshot
    candidate_cas = FileSystemCAS(candidate_cas_root)
    assets = admit_locked_authored_assets(snapshot, cas=candidate_cas)
    planned = planning.plan(snapshot, assets=assets)
    closure = project_locked_standard_toolchain_closure(
        snapshot,
        planned.execution_plan,
        native_sdk_inputs=prepared.native_sdk_inputs,
    )
    generator, _local_cache = source_generation.runner(
        snapshot,
        planned.execution_plan,
        assets=assets,
        cas=candidate_cas,
    )
    generator.retained_source = retained_source
    source_trees = LocalSourceTreeRegistry()
    indexer = DisabledGenerationIndexer(source_trees)
    runtime = assemble_filesystem_standard_project_runtime(
        generator=generator,
        object_root=object_root,
        toolchain_closure=closure,
        python_wheelhouse=(
            _configured_python_wheelhouse(python_wheelhouse, os.environ)
            if closure.python_targets
            else None
        ),
        planning=planning,
        node_preparation=LockedComponentNodePreparationAdapter(
            model_selector=source_generation.model_selector,
            coding_cli=source_generation.generator.selection.name,
            native_sdk_inputs=prepared.native_sdk_inputs,
        ),
        source_trees=source_trees,
        indexer=indexer,
        independent_acceptance_oracle=independent_acceptance_oracle,
        native_sdk_inputs=prepared.native_sdk_inputs,
    )
    configuration, caches = _resolved_source_cache(project, source_cache_root)
    resolver = SourceCacheResolver(configuration, caches)
    runtime = compose_filesystem_standard_source_cache(
        runtime,
        generator=generator,
        resolver=resolver,
        caller_cas=FileSystemCAS(accepted_cas_root),
        indexer=indexer,
        materializer=SourceCacheMaterializer(),
    )
    runtime = compose_filesystem_standard_lifecycle_checkpoints(
        runtime,
        checkpoint_root=checkpoint_root,
    )
    return FilesystemStandardRebuildAdapter(
        project=project,
        binding=selected_binding,
        runtime=runtime,
        source_cache_configuration=configuration,
        authority_validator=authority_validator,
        retained_source=retained_source,
        retained_source_authorization=retained_source_authorization,
    )


__all__ = [
    "FilesystemStandardRebuildAdapter",
    "FilesystemStandardRebuildError",
    "FilesystemStandardRebuildRequest",
    "FilesystemStandardRebuildResult",
    "assemble_filesystem_standard_rebuild_adapter",
]

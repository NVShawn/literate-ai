"""Hold original authored owners through complete clean-run observation.

This is execution composition, not publication or retained admission. The public
apply transaction must still capture and reopen the closed products and use the
guarded recoverable writer before it can publish a positive authored reference.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
    _project_roots,
)
from literate_ai.adapters.authored_qualification_runs import (
    observe_authored_qualification_run,
)
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
    LockedComponentModelSelectionAdapter,
    PreparedLockedGeneration,
)
from literate_ai.adapters.qualification import (
    FilesystemQualificationError,
    FilesystemQualificationWorkspaceAllocator,
    FilesystemStandardQualificationLifecyclePort,
)
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectPlanningAdapter,
)
from literate_ai.application.component_generation_scheduling import (
    ComponentInvalidationDecision,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts import ComponentChangeSurface
from literate_ai.contracts.authored_qualification_requests import (
    AuthoredQualificationLifecycleRequest,
)
from literate_ai.contracts.authored_qualification_runs import (
    AuthoredQualificationResult,
)
from literate_ai.contracts.identity import ContentIdentity


@dataclass(frozen=True, slots=True)
class CurrentAuthoredQualificationExecution:
    """Original execution/capture scope; no decoder and no publication grant."""

    result: AuthoredQualificationResult
    _lifecycle: FilesystemStandardQualificationLifecyclePort = field(repr=False)
    _recorder: QualificationEvidenceRecorder = field(repr=False)
    _check: Callable[[], None] = field(repr=False)

    def require_unchanged(self):
        self._check()

    @property
    def evidence_records(self):
        self.require_unchanged()
        return self._recorder.entries


@contextmanager
def open_authored_qualification_execution(
    current: CurrentAuthoredQualificationPlan,
    selection: GenerationPreparationRequest,
    *,
    acknowledged_plan: ContentIdentity,
    allow_host_execution: bool,
    scratch_root: Path,
    max_parallelism: int = 1,
    maximum_capture_bytes: int = 256 * 1024 * 1024,
) -> Iterator[CurrentAuthoredQualificationExecution]:
    """Execute every clean run under the original plan and still-held owners.

    Allocations are fresh and single-use. The caller must finish immutable product
    capture inside this context; escaped result records cannot restore the held
    namespace or verifier owners. Failure preserves scratch diagnostics, closes
    every owner and returns no successful execution scope.
    """

    if not isinstance(current, CurrentAuthoredQualificationPlan):
        raise TypeError("original current authored plan owner required")
    if (
        not isinstance(acknowledged_plan, ContentIdentity)
        or acknowledged_plan != current.plan.identity
    ):
        raise FilesystemQualificationError(
            "qualification.authored.acknowledgement_required",
            "execution requires acknowledgement of the exact current authored plan",
        )
    if allow_host_execution is not True:
        raise FilesystemQualificationError(
            "qualification.authored.host_execution_required",
            "authored execution requires explicit permission to run "
            "generated host code",
        )
    if not isinstance(selection, GenerationPreparationRequest):
        raise TypeError("original generation selection required")
    if (
        type(max_parallelism) is not int
        or not 1 <= max_parallelism <= 256
        or type(maximum_capture_bytes) is not int
        or maximum_capture_bytes <= 0
    ):
        raise ValueError("authored execution requires bounded parallelism and capture")
    if not isinstance(scratch_root, Path):
        raise TypeError("scratch_root must be a Path")
    current.require_unchanged()
    require_safe_directory(scratch_root)
    scratch_root = scratch_root.resolve(strict=True)
    if any(
        scratch_root.is_relative_to(root) for root in _project_roots(current.oracles)
    ):
        raise FilesystemQualificationError(
            "qualification.authored.scratch_in_authority",
            "generated run workspaces must remain outside every selected project",
        )

    with current.oracles.capture_current():
        preparation = FilesystemLockedGenerationApplicationAdapter()
        prepared = preparation.prepare(selection)
        authority = current.oracles.authority
        lock = authority.locked.authority.lock
        root_revision = next(
            n.revision for n in lock.nodes if n.revision.identity == lock.root_revision
        )
        if (
            not isinstance(prepared, PreparedLockedGeneration)
            or prepared.boundary != authority.project.root
            or prepared.component_root
            != authority.locked.component_root(root_revision.authoring_identity)
            or prepared.component_revision != root_revision
            or prepared.locked_authority_snapshot.authority.identity
            != authority.locked.authority.identity
        ):
            raise FilesystemQualificationError(
                "qualification.authored.prepared_plan_mismatch",
                "prepared generation must select the original authored root and graph",
            )
        planned = FilesystemStandardProjectPlanningAdapter(
            coding_cli_selector=lambda: current.standard.coding_cli,
            model_selector=LockedComponentModelSelectionAdapter(
                pipeline_model=current._pipeline_model
            ),
            native_sdk_inputs=prepared.native_sdk_inputs,
        ).plan(prepared.locked_authority_snapshot)
        if planned.execution_plan != current.plan.execution_plan:
            raise FilesystemQualificationError(
                "qualification.authored.prepared_plan_mismatch",
                "prepared model plan differs from the acknowledged complete plan",
            )
        preparation.require_unchanged(prepared)
        current.require_unchanged()
        recorder = QualificationEvidenceRecorder(
            max_bytes=maximum_capture_bytes, max_records=100_000
        )
        recorder.remember_json(current.plan.to_dict())
        recorder.remember_json(current.oracles.profile.to_dict())
        revisions = tuple(node.revision.identity for node in lock.nodes)
        invalidation = ComponentInvalidationDecision(
            "litai-authored-qualification",
            lock.root_revision,
            ComponentChangeSurface.LOCAL_AUTHORITY,
            revisions,
            revisions,
            revisions,
        )
        workspaces = FilesystemQualificationWorkspaceAllocator(scratch_root)
        lifecycle = None
        active = True
        requests = []

        def check():
            if not active:
                raise ValueError("qualification.authored.execution_owner_expired")
            current.require_unchanged()
            preparation.require_unchanged(prepared)
            lifecycle.require_current_authority()
            for request in requests:
                workspaces.resolve(request.workspace_allocation_identity)
            for execution, ports in lifecycle.product_sources.values():
                integration = execution.lifecycle.root_integration
                ports.project_package_custody(
                    integration.package_plan, integration.package_result
                )

        try:
            lifecycle = FilesystemStandardQualificationLifecyclePort(
                project=authority.project,
                prepared=prepared,
                binding=current.binding,
                invalidation=invalidation,
                workspaces=workspaces,
                authored_plan=current,
                max_parallelism=max_parallelism,
                retain_library_products=True,
                pipeline_model=current._pipeline_model,
            )
            lifecycle.evidence_recorder = recorder
            runs = []
            for ordinal in range(current.plan.minimum_clean_runs):
                check()
                run_identity = recorder.remember_json(
                    {
                        "schema": "literate-ai/authored-qualification-run-selection@1",
                        "plan_identity": current.plan.identity.uri,
                        "session_identity": workspaces.session_identity.uri,
                        "ordinal": ordinal,
                    }
                )
                workspace = workspaces.allocate_authored(run_identity)
                request = AuthoredQualificationLifecycleRequest(
                    current.plan.origin,
                    current.plan.identity,
                    current.oracles.identity,
                    run_identity,
                    workspace.allocation_identity,
                )
                requests.append(request)
                recorder.remember_json(request.to_dict())
                execution = lifecycle.execute(request)
                check()
                run = observe_authored_qualification_run(
                    current,
                    request=request,
                    execution=execution,
                    reader=QualificationEvidenceReader(
                        recorder.entries,
                        max_bytes=maximum_capture_bytes,
                        max_records=100_000,
                    ),
                )
                recorder.remember_json(run.to_dict())
                runs.append(run)
            result = AuthoredQualificationResult(current.plan, tuple(runs))
            recorder.remember_json(result.to_dict())
            owner = CurrentAuthoredQualificationExecution(
                result, lifecycle, recorder, check
            )
            owner.require_unchanged()
            yield owner
            owner.require_unchanged()
        finally:
            active = False
            if lifecycle is not None:
                lifecycle.product_sources.clear()
                lifecycle.evidence_recorder = None
            workspaces.close()

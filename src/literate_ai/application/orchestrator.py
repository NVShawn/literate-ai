"""Provider-neutral, resumable generation and acceptance orchestration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import partial

from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    CycloneDxManagedGraph,
    canonical_identity,
    canonical_json_bytes,
    canonical_relative_posix_paths,
    project_component_lock_managed_graph,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GeneratedTestSuiteError,
    ValidatedGeneratedTestSuite,
    validate_generated_test_suite,
)
from literate_ai.ports import (
    AcceptanceRunner,
    Builder,
    Classifier,
    DependencyResolver,
    EventStore,
    GeneratedTestRunner,
    ModelProvider,
    PortContractError,
    Validator,
    WorkspaceCommitter,
    require_acceptance_result,
    require_build_authorization_result,
    require_build_request_document,
    require_build_result,
    require_classification_result,
    require_committed_tree_result,
    require_dependency_resolution_result,
    require_dependency_source_validation_result,
    require_generated_test_result,
    require_prepared_tree_result,
    require_validation_result,
)
from literate_ai.security import BuildRequest, BuildRequestDeclaration

from .models import (
    ComponentSourceEvidenceReadiness,
    GenerationEvent,
    GenerationProvenance,
    GenerationRequest,
    GenerationRun,
    GenerationStatus,
    LifecycleExecution,
    StageExecution,
    route_decision_dict,
)
from .ports import BuildAuthorizer, ReadinessProvider


class GenerationFailure(RuntimeError):
    def __init__(
        self, code: str, message: str, run: GenerationRun | None = None
    ) -> None:
        self.code = code
        self.run = run
        super().__init__(message)


_STABLE_FAILURE_TYPES: tuple[tuple[type[Exception], str], ...] = (
    (PermissionError, "PermissionError"),
    (TimeoutError, "TimeoutError"),
    (OSError, "OSError"),
    (TypeError, "TypeError"),
    (ValueError, "ValueError"),
    (RuntimeError, "RuntimeError"),
)
_STABLE_FAILURE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def _stable_failure_type(error: Exception) -> str:
    """Classify failures without persisting provider-controlled class names or text."""

    return next(
        (
            category
            for exception_type, category in _STABLE_FAILURE_TYPES
            if isinstance(error, exception_type)
        ),
        "Exception",
    )


def _stable_failure_code(error: Exception) -> str | None:
    """Retain a bounded machine code without persisting provider-controlled prose."""

    code = getattr(error, "code", None)
    return (
        code if isinstance(code, str) and _STABLE_FAILURE_CODE.fullmatch(code) else None
    )


class GenerationOrchestrator:
    """Coordinates pure decisions and provider ports without importing any adapter."""

    def __init__(
        self,
        *,
        readiness_provider: ReadinessProvider,
        model_provider: ModelProvider,
        validator: Validator,
        classifier: Classifier,
        build_authorizer: BuildAuthorizer,
        builder: Builder,
        dependency_resolver: DependencyResolver,
        generated_test_runner: GeneratedTestRunner,
        acceptance_runner: AcceptanceRunner,
        workspace: WorkspaceCommitter,
        event_store: EventStore | None = None,
    ) -> None:
        self.readiness_provider = readiness_provider
        self.model_provider = model_provider
        self.validator = validator
        self.classifier = classifier
        self.build_authorizer = build_authorizer
        self.builder = builder
        self.dependency_resolver = dependency_resolver
        self.generated_test_runner = generated_test_runner
        self.acceptance_runner = acceptance_runner
        self.workspace = workspace
        self.event_store = event_store

    def execute(
        self,
        request: GenerationRequest,
        *,
        resume: GenerationRun | None = None,
        stop_after: str | None = None,
    ) -> GenerationRun:
        authority = request.locked_authority
        lock = authority.lock
        component_revision = next(
            item.revision
            for item in lock.nodes
            if item.revision.identity == request.component_revision
        )
        try:
            _require_managed_graph_matches_lock(
                request.managed_sbom_graph, lock, request.component_revision
            )
        except (TypeError, ValueError) as error:
            raise GenerationFailure(
                "generation.managed-sbom-graph-mismatch",
                "generation request managed SBOM authority does not match the "
                "already-resolved Component lock",
            ) from error
        readiness = self.readiness_provider.inspect(
            authority, request.component_revision
        )
        plan = request.execution_plan
        valid_stop_points = ("generate", *plan.lifecycle_steps)
        if stop_after is not None and stop_after not in valid_stop_points:
            raise GenerationFailure(
                "generation.stop-point-invalid",
                f"generation stop point {stop_after!r} is not in the exact plan",
            )
        routes = plan.route_decisions
        acceptance_policy = plan.independent_acceptance_policy
        try:
            acceptance_policy.require_runner(
                runner_id=self.acceptance_runner.runner_id,
                suite_identity=self.acceptance_runner.acceptance_suite_identity,
            )
        except (TypeError, ValueError) as error:
            raise GenerationFailure(
                "generation.acceptance-policy-mismatch",
                "independent acceptance runner does not match the exact plan policy",
            ) from error
        input_identity = canonical_identity(
            {
                "component_lock": lock.to_dict(),
                "locked_generation_authority": authority.to_dict(),
                "managed_sbom_graph": request.managed_sbom_graph.to_dict(),
                "component_revision": component_revision.to_dict(),
                "generation_context": request.generation_context.to_dict(),
                "readiness": readiness.identity.to_dict(),
                "execution_plan_identity": plan.identity.to_dict(),
                "workflow_reference": plan.workflow_reference.to_dict(),
                "routing_reference": plan.routing_reference.to_dict(),
                "model_stages": [
                    {
                        "stage_id": stage.stage_id,
                        "dependencies": list(stage.dependencies),
                        "produces_tree": stage.produces_tree,
                        "instructions_digest": canonical_identity(
                            stage.instructions
                        ).uri,
                        "response_schema_name": stage.response_schema_name,
                        "response_schema": dict(stage.response_schema),
                        "content_kind": stage.content_kind,
                        "max_output_tokens": stage.max_output_tokens,
                        "route_decision": route_decision_dict(route),
                        "route_decision_digest": route.digest,
                    }
                    for stage, route in zip(request.model_stages, routes, strict=True)
                ],
                "lifecycle_steps": list(plan.lifecycle_steps),
                "generated_test_suite_policy": (
                    request.generated_test_suite_policy.to_dict()
                ),
                "independent_acceptance_policy": acceptance_policy.to_dict(),
                "build_request_declaration": (
                    request.build_request_declaration.to_dict()
                ),
                "workspace_reference": request.workspace_reference,
                "lifecycle_runners": {
                    "dependency_resolver_id": self.dependency_resolver.resolver_id,
                    "generated_test_runner_id": self.generated_test_runner.runner_id,
                    "acceptance_runner_id": self.acceptance_runner.runner_id,
                    "acceptance_suite_identity": (
                        self.acceptance_runner.acceptance_suite_identity
                    ),
                },
            }
        )
        complete_checkpoint: GenerationRun | None = None
        if resume is None:
            run = GenerationRun(
                run_id=f"generation:{input_identity.digest[:24]}",
                input_identity=input_identity,
                status=GenerationStatus.RUNNING,
                locked_authority=authority,
                readiness=readiness,
                route_decisions=routes,
            )
            run = self._event(
                run, "run-started", None, input_identity=input_identity.uri
            )
        else:
            if resume.input_identity != input_identity:
                raise GenerationFailure(
                    "generation.resume-identity-mismatch",
                    "cannot resume with different immutable generation inputs",
                    resume,
                )
            self._require_resume_checkpoint(
                resume,
                input_identity=input_identity,
                locked_authority=authority,
                readiness=readiness,
                route_decisions=routes,
                build_request_declaration=request.build_request_declaration,
                model_stage_ids=tuple(item.stage_id for item in request.model_stages),
                lifecycle_step_ids=plan.lifecycle_steps,
            )
            if resume.status is GenerationStatus.COMPLETE:
                complete_checkpoint = resume
                run = replace(resume, status=GenerationStatus.RUNNING)
            else:
                run = replace(resume, status=GenerationStatus.RUNNING)
                run = self._event(
                    run, "run-resumed", None, input_identity=input_identity.uri
                )

        try:
            self._require_readiness(
                readiness,
                request.component_revision,
            )
            expected_effective = (
                request.build_request_declaration.effective_revision_digest
            )
            if expected_effective != component_revision.identity.uri:
                raise GenerationFailure(
                    "generation.build-revision-mismatch",
                    "build request does not target the selected locked Component",
                    run,
                )

            prior_outputs: dict[str, object] = {}
            for stage, route in zip(request.model_stages, routes, strict=True):
                dependency_outputs = {
                    dependency: prior_outputs[dependency]
                    for dependency in stage.dependencies
                }
                stage_input = {
                    "run_input_identity": input_identity.to_dict(),
                    "component_revision_identity": (
                        component_revision.identity.to_dict()
                    ),
                    "bounded_generation_context": request.generation_context.to_dict(),
                    "source_snapshot_ids": list(readiness.source_snapshot_ids),
                    "evidence_ids": list(readiness.evidence_ids),
                    "route_decision": route_decision_dict(route),
                    "prior_stage_outputs": dependency_outputs,
                }
                stage_input_identity = canonical_identity(stage_input)
                existing = run.stage(stage.stage_id)
                if existing is not None:
                    if existing.input_identity != stage_input_identity:
                        raise GenerationFailure(
                            "generation.stage-input-mismatch",
                            "completed stage "
                            f"{stage.stage_id!r} has different immutable inputs",
                            run,
                        )
                    prior_outputs[stage.stage_id] = existing.response
                    continue
                run = self._event(
                    run,
                    "model-stage-started",
                    stage.stage_id,
                    input_identity=stage_input_identity.uri,
                    route_decision_digest=route.digest,
                )
                model_request = {
                    "stage_id": stage.stage_id,
                    "input_identity": stage_input_identity.to_dict(),
                    **stage_input,
                    "instructions": stage.instructions,
                    "input": stage_input,
                    "response_schema_name": stage.response_schema_name,
                    "response_schema": dict(stage.response_schema),
                    "content_kind": stage.content_kind,
                    "metadata": {
                        "run_id": run.run_id,
                        "stage_id": stage.stage_id,
                        "input_identity": stage_input_identity.uri,
                    },
                }
                if stage.max_output_tokens is not None:
                    model_request["max_output_tokens"] = stage.max_output_tokens
                try:
                    response, response_json = _safe_mapping(
                        self.model_provider.complete_structured(model_request),
                        f"model stage {stage.stage_id}",
                    )
                except Exception as error:
                    run = self._failed_event(
                        run, "model-stage-failed", stage.stage_id, error
                    )
                    raise GenerationFailure(
                        "generation.model-stage-failed",
                        f"model stage {stage.stage_id!r} failed",
                        run,
                    ) from error
                execution = StageExecution(
                    stage.stage_id,
                    stage_input_identity,
                    canonical_identity(response),
                    route,
                    response_json,
                )
                run = run.with_stage(execution)
                run = self._event(
                    run,
                    "model-stage-completed",
                    stage.stage_id,
                    output_identity=execution.output_identity.uri,
                )
                prior_outputs[stage.stage_id] = response

            final_response = run.stage(request.model_stages[-1].stage_id)
            assert final_response is not None
            files = _proposed_files(final_response.response)
            tree_material = {
                "files": [
                    {
                        "path": path,
                        "content_identity": canonical_identity(content).to_dict(),
                    }
                    for path, content in files.items()
                ]
            }
            tree_identity = canonical_identity(tree_material)
            source_bundle_digest = _source_bundle_digest(files)
            generated_test_suite_content = files.get(GENERATED_TEST_SUITE_PATH)
            if generated_test_suite_content is None:
                raise GenerationFailure(
                    "generation.generated-test-suite-missing",
                    "generated tree lacks source/tests/manifest.json",
                    run,
                )
            try:
                generated_test_suite = validate_generated_test_suite(
                    generated_test_suite_content,
                    recipe_identity=(
                        request.generated_test_suite_policy.recipe_identity
                    ),
                    specification_references=(
                        request.generated_test_suite_policy.specification_references
                    ),
                    acceptance_arguments=(
                        request.generated_test_suite_policy.acceptance_arguments
                    ),
                )
            except GeneratedTestSuiteError as error:
                raise GenerationFailure(
                    "generation.generated-test-suite-invalid",
                    "generated test suite does not match the exact generation recipe",
                    run,
                ) from error
            artifact = {
                "effective_revision_digest": component_revision.identity.uri,
                "tree_identity": tree_identity.uri,
                "source_bundle_digest": source_bundle_digest,
                "generated_test_suite_path": GENERATED_TEST_SUITE_PATH,
                "generated_test_suite_identity": (
                    generated_test_suite.content_identity
                ),
                "source_sbom_path": CYCLONEDX_SOURCE_SBOM_PATH,
                "files": dict(files),
                "source_snapshot_ids": list(readiness.source_snapshot_ids),
                "evidence_ids": list(readiness.evidence_ids),
            }
            source_bom_content = files.get(CYCLONEDX_SOURCE_SBOM_PATH)
            if source_bom_content is None:
                raise GenerationFailure(
                    "generation.source-sbom-missing",
                    f"generated tree lacks {CYCLONEDX_SOURCE_SBOM_PATH}",
                    run,
                )
            source_bom_identity = (
                "sha256:"
                + hashlib.sha256(source_bom_content.encode("utf-8")).hexdigest()
            )

            realized_build_request = request.build_request_declaration.realize(
                source_bundle_digest
            )
            realized_build_request_identity = canonical_identity(
                realized_build_request.to_dict()
            )
            if run.realized_build_request is None:
                run = replace(run, realized_build_request=realized_build_request)
                run = self._event(
                    run,
                    "build-request-realized",
                    None,
                    build_request_declaration_identity=(
                        request.build_request_declaration.identity
                    ),
                    realized_build_request_identity=(
                        realized_build_request_identity.uri
                    ),
                    source_bundle_digest=source_bundle_digest,
                    component_revision_identity=component_revision.identity.uri,
                )
            elif run.realized_build_request != realized_build_request:
                raise GenerationFailure(
                    "generation.realized-build-request-mismatch",
                    "generation checkpoint contains another realized build request",
                    run,
                )

            if stop_after == "generate" and not run.lifecycle_executions:
                return self._pause(run, stop_after)

            run = self._execute_guarded_lifecycle(
                run,
                request=request,
                build_request=realized_build_request,
                artifact=artifact,
                files=files,
                tree_identity=tree_identity.uri,
                source_bundle_digest=source_bundle_digest,
                generated_test_suite=generated_test_suite,
                source_bom_identity=source_bom_identity,
                stop_after=stop_after,
            )
            if run.status is GenerationStatus.PAUSED:
                return run
            provenance = GenerationProvenance(
                input_identity,
                lock.identity,
                lock.root_revision,
                component_revision.identity,
                request.generation_context.component_generation_plan_identity,
                request.generation_context.generation_key_identity,
                request.generation_context.context_manifest_identity,
                request.generation_context.prompt_identity,
                request.generation_context.generation_recipe_identity,
                request.generation_context.workspace_allocation_identity,
                ContentIdentity.parse_uri(request.build_request_declaration.identity),
                realized_build_request_identity,
                readiness.identity,
                tuple(route.digest for route in routes),
                tuple(item.output_identity for item in run.stage_executions),
                tuple(item.output_identity for item in run.lifecycle_executions),
                tree_identity,
            )
            if complete_checkpoint is not None:
                self._require_complete_checkpoint(
                    complete_checkpoint,
                    expected_provenance=provenance,
                    accepted_tree_identity=tree_identity.uri,
                    component_revision_identity=component_revision.identity.uri,
                )
                return complete_checkpoint
            run = replace(run, provenance=provenance)
            run = self._event(
                run,
                "provenance-recorded",
                None,
                provenance_identity=provenance.identity.uri,
                accepted_tree_identity=tree_identity.uri,
                realized_build_request_identity=realized_build_request_identity.uri,
            )
            run = replace(run, status=GenerationStatus.COMPLETE)
            return self._event(
                run,
                "run-completed",
                None,
                component_revision_identity=component_revision.identity.uri,
                accepted_tree_identity=tree_identity.uri,
                realized_build_request_identity=realized_build_request_identity.uri,
            )
        except GenerationFailure as failure:
            failed = failure.run or run
            if failed.status is not GenerationStatus.FAILED:
                failed = replace(failed, status=GenerationStatus.FAILED)
            if not failed.events or failed.events[-1].event_type != "run-failed":
                cause = failure.__cause__
                failed = self._event(
                    failed,
                    "run-failed",
                    None,
                    code=failure.code,
                    error_type=_stable_failure_type(
                        cause if isinstance(cause, Exception) else failure
                    ),
                )
            failure.run = failed
            raise

    def _require_resume_checkpoint(
        self,
        run: GenerationRun,
        *,
        input_identity: object,
        locked_authority: object,
        readiness: ComponentSourceEvidenceReadiness,
        route_decisions: tuple[object, ...],
        build_request_declaration: BuildRequestDeclaration,
        model_stage_ids: tuple[str, ...],
        lifecycle_step_ids: tuple[str, ...],
    ) -> None:
        """Reject caller-created, reordered, or stale resumable state."""

        if self.event_store is None:
            raise GenerationFailure(
                "generation.resume-checkpoint-untrusted",
                "generation resume requires its configured durable event store",
                run,
            )
        try:
            persisted_events = tuple(self.event_store.stream(run.run_id))
            persisted_json = canonical_json_bytes(persisted_events)
            checkpoint_json = canonical_json_bytes(
                tuple(item.to_dict() for item in run.events)
            )
        except Exception as error:
            raise GenerationFailure(
                "generation.resume-checkpoint-untrusted",
                "generation checkpoint event evidence cannot be read canonically",
                run,
            ) from error
        if persisted_json != checkpoint_json:
            raise GenerationFailure(
                "generation.resume-checkpoint-untrusted",
                "generation checkpoint does not exactly match its durable event stream",
                run,
            )
        try:
            for execution in run.stage_executions:
                StageExecution(
                    execution.stage_id,
                    execution.input_identity,
                    execution.output_identity,
                    execution.route_decision,
                    execution.response_json,
                )
            for execution in run.lifecycle_executions:
                LifecycleExecution(
                    execution.step_id,
                    execution.input_identity,
                    execution.output_identity,
                    execution.output_json,
                )
            GenerationRun(
                run_id=run.run_id,
                input_identity=run.input_identity,
                status=run.status,
                locked_authority=run.locked_authority,
                readiness=run.readiness,
                route_decisions=run.route_decisions,
                realized_build_request=run.realized_build_request,
                stage_executions=run.stage_executions,
                lifecycle_executions=run.lifecycle_executions,
                events=run.events,
                provenance=run.provenance,
            )
        except (TypeError, ValueError) as error:
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint contains invalid canonical evidence",
                run,
            ) from error
        self._require_execution_events(run)
        self._require_realized_build_request(
            run,
            build_request_declaration=build_request_declaration,
            model_stage_ids=model_stage_ids,
        )
        if run.status not in {
            GenerationStatus.PAUSED,
            GenerationStatus.FAILED,
            GenerationStatus.COMPLETE,
        }:
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "only failed or complete generation checkpoints can be resumed",
                run,
            )
        if (
            run.input_identity != input_identity
            or run.locked_authority != locked_authority
            or run.readiness != readiness
            or run.route_decisions != route_decisions
        ):
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint state does not match its immutable input",
                run,
            )
        stage_ids = tuple(item.stage_id for item in run.stage_executions)
        if stage_ids != model_stage_ids[: len(stage_ids)]:
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint model executions are reordered or unknown",
                run,
            )
        for execution, route in zip(
            run.stage_executions, route_decisions, strict=False
        ):
            if execution.route_decision != route:
                raise GenerationFailure(
                    "generation.resume-checkpoint-invalid",
                    "generation checkpoint model route differs from the exact plan",
                    run,
                )
        step_ids = tuple(item.step_id for item in run.lifecycle_executions)
        if step_ids != lifecycle_step_ids[: len(step_ids)] or (
            step_ids and len(stage_ids) != len(model_stage_ids)
        ):
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint lifecycle executions are reordered or premature",
                run,
            )
        if not run.events or run.events[0].event_type != "run-started":
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint lacks its authentic start event",
                run,
            )
        if run.events[0].data.get("input_identity") != run.input_identity.uri:
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint start event targets another input",
                run,
            )
        if run.status is GenerationStatus.FAILED:
            if run.provenance is not None or run.events[-1].event_type != "run-failed":
                raise GenerationFailure(
                    "generation.resume-checkpoint-invalid",
                    "failed generation checkpoint has impossible terminal state",
                    run,
                )
            return
        if run.status is GenerationStatus.PAUSED:
            if run.provenance is not None or run.events[-1].event_type != "run-paused":
                raise GenerationFailure(
                    "generation.resume-checkpoint-invalid",
                    "paused generation checkpoint has impossible terminal state",
                    run,
                )
            stop_after = run.events[-1].data.get("stop_after")
            completed = (
                step_ids[-1]
                if step_ids
                else (stage_ids[-1] if len(stage_ids) == len(model_stage_ids) else None)
            )
            if stop_after != completed:
                raise GenerationFailure(
                    "generation.resume-checkpoint-invalid",
                    "paused generation checkpoint does not follow a completed step",
                    run,
                )
            return
        if (
            len(stage_ids) != len(model_stage_ids)
            or len(step_ids) != len(lifecycle_step_ids)
            or run.provenance is None
            or run.events[-1].event_type != "run-completed"
        ):
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "complete generation checkpoint omits required execution evidence",
                run,
            )

    @staticmethod
    def _require_realized_build_request(
        run: GenerationRun,
        *,
        build_request_declaration: BuildRequestDeclaration,
        model_stage_ids: tuple[str, ...],
    ) -> None:
        events = tuple(
            item for item in run.events if item.event_type == "build-request-realized"
        )
        stages_complete = len(run.stage_executions) == len(model_stage_ids)
        if run.realized_build_request is None:
            if events or (
                stages_complete and run.status is not GenerationStatus.FAILED
            ):
                raise GenerationFailure(
                    "generation.resume-checkpoint-invalid",
                    "generation checkpoint omits its realized build request",
                    run,
                )
            return
        if not stages_complete or len(events) != 1:
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint has impossible build-request realization",
                run,
            )
        realized = run.realized_build_request
        expected = build_request_declaration.realize(realized.source_bundle_digest)
        identity = canonical_identity(realized.to_dict()).uri
        event = events[0]
        if (
            realized != expected
            or event.stage_id is not None
            or event.data.get("build_request_declaration_identity")
            != build_request_declaration.identity
            or event.data.get("realized_build_request_identity") != identity
            or event.data.get("source_bundle_digest") != realized.source_bundle_digest
            or event.data.get("component_revision_identity")
            != build_request_declaration.effective_revision_digest
        ):
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint build-request realization is invalid",
                run,
            )
        final_model_event = max(
            item.sequence
            for item in run.events
            if item.event_type == "model-stage-completed"
        )
        first_lifecycle_event = min(
            (
                item.sequence
                for item in run.events
                if item.event_type == "lifecycle-step-started"
            ),
            default=len(run.events) + 1,
        )
        if not final_model_event < event.sequence < first_lifecycle_event:
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint realized build request is out of order",
                run,
            )

    @staticmethod
    def _require_execution_events(run: GenerationRun) -> None:
        model_completions = tuple(
            item for item in run.events if item.event_type == "model-stage-completed"
        )
        lifecycle_completions = tuple(
            item for item in run.events if item.event_type == "lifecycle-step-completed"
        )
        if tuple(item.stage_id for item in model_completions) != tuple(
            item.stage_id for item in run.stage_executions
        ) or tuple(item.stage_id for item in lifecycle_completions) != tuple(
            item.step_id for item in run.lifecycle_executions
        ):
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "generation checkpoint executions do not match completion events",
                run,
            )
        for execution, completed in zip(
            run.stage_executions, model_completions, strict=True
        ):
            starts = tuple(
                item
                for item in run.events
                if item.sequence < completed.sequence
                and item.event_type == "model-stage-started"
                and item.stage_id == execution.stage_id
            )
            if (
                completed.data.get("output_identity") != execution.output_identity.uri
                or not starts
                or starts[-1].data.get("input_identity") != execution.input_identity.uri
                or starts[-1].data.get("route_decision_digest")
                != execution.route_decision.digest
            ):
                raise GenerationFailure(
                    "generation.resume-checkpoint-invalid",
                    "model execution is not bound to its authenticated events",
                    run,
                )
        for execution, completed in zip(
            run.lifecycle_executions, lifecycle_completions, strict=True
        ):
            starts = tuple(
                item
                for item in run.events
                if item.sequence < completed.sequence
                and item.event_type == "lifecycle-step-started"
                and item.stage_id == execution.step_id
            )
            if (
                completed.data.get("output_identity") != execution.output_identity.uri
                or not starts
                or starts[-1].data.get("input_identity") != execution.input_identity.uri
            ):
                raise GenerationFailure(
                    "generation.resume-checkpoint-invalid",
                    "lifecycle execution is not bound to its authenticated events",
                    run,
                )

    @staticmethod
    def _require_complete_checkpoint(
        run: GenerationRun,
        *,
        expected_provenance: GenerationProvenance,
        accepted_tree_identity: str,
        component_revision_identity: str,
    ) -> None:
        if run.provenance != expected_provenance:
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "complete generation checkpoint has stale or fabricated provenance",
                run,
            )
        provenance_events = tuple(
            item for item in run.events if item.event_type == "provenance-recorded"
        )
        if (
            len(provenance_events) != 1
            or provenance_events[0].data.get("provenance_identity")
            != expected_provenance.identity.uri
            or provenance_events[0].data.get("accepted_tree_identity")
            != accepted_tree_identity
        ):
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "complete generation checkpoint does not bind exact provenance",
                run,
            )
        completed = run.events[-1]
        if (
            completed.stage_id is not None
            or completed.data.get("component_revision_identity")
            != component_revision_identity
            or completed.data.get("accepted_tree_identity") != accepted_tree_identity
            or completed.data.get("realized_build_request_identity")
            != expected_provenance.realized_build_request_identity.uri
        ):
            raise GenerationFailure(
                "generation.resume-checkpoint-invalid",
                "complete generation checkpoint targets another accepted tree",
                run,
            )

    def _validate_source_before_build(
        self,
        artifact: Mapping[str, object],
        *,
        request: GenerationRequest,
        source_bom_identity: str,
    ) -> Mapping[str, object]:
        dependency_validation = require_dependency_source_validation_result(
            self.dependency_resolver.validate_source(artifact),
            resolver_id=self.dependency_resolver.resolver_id,
            effective_revision_digest=str(artifact["effective_revision_digest"]),
            source_bundle_digest=str(artifact["source_bundle_digest"]),
            source_bom_identity=source_bom_identity,
            managed_graph_identity=request.managed_sbom_graph.identity.uri,
            composition_identity=(
                request.managed_sbom_graph.resolved_graph_identity.uri
            ),
            root_ref=request.managed_sbom_graph.root_ref,
        )
        return {
            **dict(self.validator.validate(artifact)),
            "dependency_source_validation": dependency_validation,
        }

    def _execute_guarded_lifecycle(
        self,
        run: GenerationRun,
        *,
        request: GenerationRequest,
        build_request: BuildRequest,
        artifact: Mapping[str, object],
        files: Mapping[str, str],
        tree_identity: str,
        source_bundle_digest: str,
        generated_test_suite: ValidatedGeneratedTestSuite,
        source_bom_identity: str,
        stop_after: str | None,
    ) -> GenerationRun:
        """Execute only the lifecycle sequence authorized by the exact plan."""

        try:
            build_request_document = dict(
                require_build_request_document(build_request.to_dict())
            )
        except PortContractError as error:
            raise GenerationFailure(
                "generation.build-request-invalid",
                "build request is incomplete or malformed",
                run,
            ) from error
        validation: dict[str, object] | None = None
        classification: dict[str, object] | None = None
        authorization: dict[str, object] | None = None
        build: dict[str, object] | None = None
        dependency_resolution: dict[str, object] | None = None
        generated_tests: dict[str, object] | None = None
        acceptance: dict[str, object] | None = None
        prepared: dict[str, object] | None = None
        encoded_files = {
            path: content.encode("utf-8") for path, content in files.items()
        }

        def validation_contract(value: Mapping[str, object]) -> Mapping[str, object]:
            checked = require_validation_result(value)
            dependency_validation = checked.get("dependency_source_validation")
            if not isinstance(dependency_validation, Mapping):
                raise PortContractError(
                    "ports.dependencies.source-validation-missing",
                    "validation evidence omits pre-build dependency evidence",
                )
            checked_dependency = require_dependency_source_validation_result(
                dependency_validation,
                resolver_id=self.dependency_resolver.resolver_id,
                effective_revision_digest=str(artifact["effective_revision_digest"]),
                source_bundle_digest=source_bundle_digest,
                source_bom_identity=source_bom_identity,
                managed_graph_identity=request.managed_sbom_graph.identity.uri,
                composition_identity=(
                    request.managed_sbom_graph.resolved_graph_identity.uri
                ),
                root_ref=request.managed_sbom_graph.root_ref,
            )
            return {
                **dict(checked),
                "dependency_source_validation": checked_dependency,
            }

        for step_id in request.execution_plan.lifecycle_steps:
            if step_id == "validate":
                run, validation = self._step(
                    run,
                    step_id,
                    artifact,
                    lambda: self._validate_source_before_build(
                        artifact,
                        request=request,
                        source_bom_identity=source_bom_identity,
                    ),
                    output_contract=validation_contract,
                )
                if validation.get("passed") is not True:
                    run = self._event(
                        run,
                        "tree-rejected",
                        step_id,
                        reason="validation did not pass",
                        tree_identity=tree_identity,
                    )
                    raise GenerationFailure(
                        "generation.validation-rejected",
                        "proposed tree failed validation",
                        run,
                    )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "classify":
                if validation is None:
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "classification requires completed validation",
                        run,
                    )
                findings_value = validation.get("findings", ())
                findings = (
                    tuple(findings_value)
                    if isinstance(findings_value, Sequence)
                    and not isinstance(findings_value, (str, bytes, bytearray))
                    else ()
                )
                run, classification = self._step(
                    run,
                    step_id,
                    {"artifact": artifact, "validation": validation},
                    lambda findings=findings: self.classifier.classify(
                        artifact, findings
                    ),
                    output_contract=lambda value: require_classification_result(
                        value,
                        effective_revision_digest=str(
                            artifact["effective_revision_digest"]
                        ),
                        source_bundle_digest=source_bundle_digest,
                    ),
                )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "authorize-build":
                if classification is None:
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "build authorization requires classification",
                        run,
                    )
                classification_digest = str(classification["classification_digest"])
                effective_revision_digest = str(
                    build_request_document["effective_revision_digest"]
                )
                build_request_digest = canonical_identity(build_request_document).uri
                run, authorization = self._step(
                    run,
                    step_id,
                    {
                        "classification": classification,
                        "build_request": build_request_document,
                    },
                    lambda classification=classification: (
                        self.build_authorizer.authorize(
                            classification, build_request_document
                        )
                    ),
                    output_contract=partial(
                        require_build_authorization_result,
                        classification_digest=classification_digest,
                        effective_revision_digest=effective_revision_digest,
                        request_digest=build_request_digest,
                    ),
                )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "build":
                if authorization is None:
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "build requires exact authorization",
                        run,
                    )
                builder_request = {**build_request_document, "artifact": artifact}
                run, build = self._step(
                    run,
                    step_id,
                    {
                        "build_request": build_request_document,
                        "authorization": authorization,
                    },
                    partial(self.builder.build, builder_request, authorization),
                    output_contract=partial(
                        require_build_result,
                        source_bundle_digest=source_bundle_digest,
                        authorization_id=str(authorization["authorization_id"]),
                        toolchain_identity=str(
                            build_request_document["toolchain_digest"]
                        ),
                    ),
                )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "resolve-dependencies":
                if build is None:
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "dependency resolution requires the exact completed build",
                        run,
                    )
                if validation is None or not isinstance(
                    validation.get("dependency_source_validation"), Mapping
                ):
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "dependency resolution requires exact pre-build dependency "
                        "evidence",
                        run,
                    )
                run, dependency_resolution = self._step(
                    run,
                    step_id,
                    {
                        "artifact": artifact,
                        "build": build,
                        "resolver_id": self.dependency_resolver.resolver_id,
                        "source_bom_identity": source_bom_identity,
                        "source_validation_identity": validation[
                            "dependency_source_validation"
                        ]["validation_identity"],
                    },
                    partial(self.dependency_resolver.resolve, artifact, build),
                    output_contract=partial(
                        require_dependency_resolution_result,
                        resolver_id=self.dependency_resolver.resolver_id,
                        effective_revision_digest=str(
                            artifact["effective_revision_digest"]
                        ),
                        source_bundle_digest=source_bundle_digest,
                        artifact_digest=str(build["artifact_digest"]),
                        source_bom_identity=source_bom_identity,
                        source_validation=validation["dependency_source_validation"],
                        managed_graph_identity=(
                            request.managed_sbom_graph.identity.uri
                        ),
                        composition_identity=(
                            request.managed_sbom_graph.resolved_graph_identity.uri
                        ),
                        root_ref=request.managed_sbom_graph.root_ref,
                    ),
                )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "test-generated":
                if (
                    build is None
                    or classification is None
                    or dependency_resolution is None
                ):
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "generated tests require exact classified build dependency "
                        "evidence",
                        run,
                    )
                run, generated_tests = self._step(
                    run,
                    step_id,
                    {
                        "artifact": artifact,
                        "build": build,
                        "classification": classification,
                        "dependency_resolution": dependency_resolution,
                        "runner_id": self.generated_test_runner.runner_id,
                        "test_suite_identity": artifact[
                            "generated_test_suite_identity"
                        ],
                        "test_suite_policy_identity": (
                            request.generated_test_suite_policy.identity.uri
                        ),
                    },
                    partial(
                        self.generated_test_runner.run,
                        artifact,
                        build,
                        classification,
                        dependency_resolution,
                        generated_test_suite,
                    ),
                    output_contract=partial(
                        require_generated_test_result,
                        runner_id=self.generated_test_runner.runner_id,
                        classification_digest=str(
                            classification["classification_digest"]
                        ),
                        effective_revision_digest=str(
                            artifact["effective_revision_digest"]
                        ),
                        source_bundle_digest=source_bundle_digest,
                        artifact_digest=str(build["artifact_digest"]),
                        dependency_resolution_identity=str(
                            dependency_resolution["resolution_identity"]
                        ),
                        tree_identity=tree_identity,
                        test_suite_identity=str(
                            artifact["generated_test_suite_identity"]
                        ),
                        expected_case_ids=generated_test_suite.case_ids,
                    ),
                )
                if generated_tests.get("passed") is not True:
                    run = self._event(
                        run,
                        "tree-rejected",
                        step_id,
                        reason="generated tests did not pass",
                        tree_identity=tree_identity,
                    )
                    raise GenerationFailure(
                        "generation.generated-tests-rejected",
                        "proposed tree failed its generated tests",
                        run,
                    )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "verify-independent":
                if (
                    build is None
                    or classification is None
                    or generated_tests is None
                    or dependency_resolution is None
                    or generated_tests.get("passed") is not True
                ):
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "independent acceptance requires passing generated tests",
                        run,
                    )
                run, acceptance = self._step(
                    run,
                    step_id,
                    {
                        "artifact": artifact,
                        "build": build,
                        "classification": classification,
                        "generated_tests": generated_tests,
                        "dependency_resolution": dependency_resolution,
                        "runner_id": self.acceptance_runner.runner_id,
                        "test_suite_identity": (
                            self.acceptance_runner.acceptance_suite_identity
                        ),
                    },
                    partial(
                        self.acceptance_runner.run,
                        artifact,
                        build,
                        classification,
                        generated_tests,
                        dependency_resolution,
                    ),
                    output_contract=partial(
                        require_acceptance_result,
                        runner_id=self.acceptance_runner.runner_id,
                        classification_digest=str(
                            classification["classification_digest"]
                        ),
                        effective_revision_digest=str(
                            artifact["effective_revision_digest"]
                        ),
                        source_bundle_digest=source_bundle_digest,
                        artifact_digest=str(build["artifact_digest"]),
                        dependency_resolution_identity=str(
                            dependency_resolution["resolution_identity"]
                        ),
                        tree_identity=tree_identity,
                        test_suite_identity=(
                            self.acceptance_runner.acceptance_suite_identity
                        ),
                        expected_execution_profile=(
                            request.execution_plan.independent_acceptance_policy.execution_profile
                        ),
                    ),
                )
                if acceptance.get("passed") is not True:
                    run = self._event(
                        run,
                        "tree-rejected",
                        step_id,
                        reason="independent acceptance did not pass",
                        tree_identity=tree_identity,
                    )
                    raise GenerationFailure(
                        "generation.acceptance-rejected",
                        "proposed tree failed independent acceptance",
                        run,
                    )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "prepare-tree":
                if (
                    generated_tests is None
                    or generated_tests.get("passed") is not True
                    or acceptance is None
                    or acceptance.get("passed") is not True
                    or dependency_resolution is None
                ):
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "workspace preparation requires passing generated tests and "
                        "independent acceptance",
                        run,
                    )
                run, prepared = self._step(
                    run,
                    step_id,
                    {
                        "tree_identity": tree_identity,
                        "files": dict(files),
                        "generated_tests": generated_tests,
                        "acceptance": acceptance,
                        "dependency_resolution": dependency_resolution,
                    },
                    partial(
                        self.workspace.prepare,
                        encoded_files,
                        dependency_resolution,
                    ),
                    output_contract=partial(
                        require_prepared_tree_result,
                        source_bundle_digest=source_bundle_digest,
                        files=encoded_files,
                        dependency_resolution=dependency_resolution,
                    ),
                )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            if step_id == "commit-tree":
                if (
                    prepared is None
                    or generated_tests is None
                    or generated_tests.get("passed") is not True
                    or acceptance is None
                    or acceptance.get("passed") is not True
                    or dependency_resolution is None
                ):
                    raise GenerationFailure(
                        "generation.lifecycle-plan-invalid",
                        "workspace commit requires generated tests and independent "
                        "acceptance to pass before preparing the tree",
                        run,
                    )
                run, _accepted = self._step(
                    run,
                    step_id,
                    {
                        "prepared": prepared,
                        "acceptance": acceptance,
                        "dependency_resolution": dependency_resolution,
                        "reference": request.workspace_reference,
                    },
                    partial(
                        self.workspace.commit,
                        prepared,
                        request.workspace_reference,
                        dependency_resolution,
                    ),
                    output_contract=partial(
                        require_committed_tree_result,
                        tree_digest=source_bundle_digest,
                        reference=request.workspace_reference,
                        dependency_resolution=dependency_resolution,
                    ),
                )
                if stop_after == step_id:
                    return self._pause(run, step_id)
                continue

            raise GenerationFailure(
                "generation.lifecycle-plan-invalid",
                f"unsupported lifecycle step {step_id!r}",
                run,
            )
        return run

    def _pause(self, run: GenerationRun, stop_after: str) -> GenerationRun:
        paused = replace(run, status=GenerationStatus.PAUSED)
        return self._event(
            paused,
            "run-paused",
            None,
            stop_after=stop_after,
            input_identity=run.input_identity.uri,
        )

    def _step(
        self,
        run: GenerationRun,
        step_id: str,
        inputs: Mapping[str, object],
        operation: Callable[[], Mapping[str, object]],
        *,
        output_contract: (
            Callable[[Mapping[str, object]], Mapping[str, object]] | None
        ) = None,
    ) -> tuple[GenerationRun, dict[str, object]]:
        input_identity = canonical_identity(inputs)
        existing = run.step(step_id)
        if existing is not None:
            if existing.input_identity != input_identity:
                raise GenerationFailure(
                    "generation.step-input-mismatch",
                    f"completed step {step_id!r} has different immutable inputs",
                    run,
                )
            if output_contract is None:
                return run, existing.output
            try:
                output, _ = _safe_mapping(
                    output_contract(existing.output),
                    f"stored lifecycle step {step_id}",
                )
            except Exception as error:
                raise GenerationFailure(
                    "generation.step-output-invalid",
                    f"completed step {step_id!r} has an invalid stored output",
                    run,
                ) from error
            return run, output
        run = self._event(
            run, "lifecycle-step-started", step_id, input_identity=input_identity.uri
        )
        from literate_ai.diagnostics import debug_stage

        try:
            with debug_stage(step_id):
                output, output_json = _safe_mapping(
                    operation(), f"lifecycle step {step_id}"
                )
                if output_contract is not None:
                    output, output_json = _safe_mapping(
                        output_contract(output), f"lifecycle step {step_id}"
                    )
        except Exception as error:
            run = self._failed_event(run, "lifecycle-step-failed", step_id, error)
            raise GenerationFailure(
                "generation.lifecycle-step-failed",
                f"lifecycle step {step_id!r} failed",
                run,
            ) from error
        execution = LifecycleExecution(
            step_id, input_identity, canonical_identity(output), output_json
        )
        run = run.with_step(execution)
        run = self._event(
            run,
            "lifecycle-step-completed",
            step_id,
            output_identity=execution.output_identity.uri,
        )
        return run, output

    @staticmethod
    def _require_readiness(
        readiness: ComponentSourceEvidenceReadiness,
        component_revision: object,
    ) -> None:
        if readiness.component_revision != component_revision:
            raise GenerationFailure(
                "generation.readiness-component-mismatch",
                "source/evidence readiness does not target the selected Component",
            )
        if (
            not readiness.source_ready
            or not readiness.evidence_ready
            or not readiness.evidence_ids
            or readiness.missing
        ):
            raise GenerationFailure(
                "generation.readiness-required",
                "exact available source and durable evidence must be ready before "
                "generation",
            )

    def _event(
        self,
        run: GenerationRun,
        event_type: str,
        stage_id: str | None,
        **data: object,
    ) -> GenerationRun:
        event = GenerationEvent.create(
            run.run_id, len(run.events) + 1, event_type, stage_id, data
        )
        updated = replace(run, events=run.events + (event,))
        if self.event_store is not None:
            self.event_store.append(run.run_id, event.to_dict())
        return updated

    def _failed_event(
        self, run: GenerationRun, event_type: str, stage_id: str, error: Exception
    ) -> GenerationRun:
        failure_code = _stable_failure_code(error)
        return self._event(
            replace(run, status=GenerationStatus.FAILED),
            event_type,
            stage_id,
            error_type=_stable_failure_type(error),
            **({"error_code": failure_code} if failure_code is not None else {}),
        )


def _require_managed_graph_matches_lock(managed, lock, component_revision) -> None:
    """Reject any SBOM projection other than the one exact resolved lock implies."""

    if not isinstance(managed, CycloneDxManagedGraph):
        raise TypeError("managed SBOM authority must be a CycloneDX managed graph")
    expected = project_component_lock_managed_graph(lock, component_revision)
    if managed != expected:
        raise ValueError("managed SBOM differs from the exact Component lock")


def _safe_mapping(value: object, context: str) -> tuple[dict[str, object], bytes]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must return an object")
    encoded = canonical_json_bytes(dict(value))
    return json.loads(encoded), encoded


def _proposed_files(response: Mapping[str, object]) -> dict[str, str]:
    value = response.get("files")
    if not isinstance(value, Mapping) or not value:
        raise GenerationFailure(
            "generation.tree-missing",
            "final model stage must return a non-empty files object",
        )
    for path, content in value.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise GenerationFailure(
                "generation.tree-invalid",
                "generated file paths and contents must be strings",
            )
    try:
        paths = canonical_relative_posix_paths(
            value.keys(), label="generated file path"
        )
    except (TypeError, ValueError) as error:
        raise GenerationFailure(
            "generation.tree-invalid",
            "generated file paths must form one canonical portable POSIX tree",
        ) from error
    if any(len(path.parts) < 2 or path.parts[0] != "source" for path in paths):
        raise GenerationFailure(
            "generation.tree-outside-source",
            "generated files must be beneath `source/`",
        )
    return {
        path.as_posix(): content
        for path, content in zip(paths, value.values(), strict=True)
    }


def _source_bundle_digest(files: Mapping[str, str]) -> str:
    """Use the same portable file-tree identity enforced by concrete builders."""

    entries = [
        {
            "path": path,
            "size": len(content.encode("utf-8")),
            "digest": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
        }
        for path, content in sorted(files.items())
    ]
    return f"sha256:{hashlib.sha256(canonical_json_bytes(entries)).hexdigest()}"


__all__ = ["GenerationFailure", "GenerationOrchestrator"]

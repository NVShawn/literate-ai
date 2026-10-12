"""Candidate-aware scheduling that stops at generated source and provenance."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Protocol

from literate_ai.application.component_generation_preparation import (
    ComponentGenerationWorkspaceDescriptor,
    PreparedComponentGenerationNode,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.executable_components import (
    ComponentActionPhase,
    ComponentExecutionPlan,
    ComponentGenerationPlan,
    ComponentGenerationRuntimeObservation,
    ComponentInvalidationDecision,
    SourceGenerationDisposition,
    SourceGenerationNodeResult,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    SourceGenerationScheduleResult,
)


class SourceGenerationSchedulingError(ValueError):
    """The source-only schedule was invalid before any generation call."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ComponentSourceGenerationRunError(RuntimeError):
    """A source runner's stable failure and any measured provider consumption."""

    def __init__(
        self,
        code: str,
        *,
        runtime_observation: ComponentGenerationRuntimeObservation | None = None,
    ) -> None:
        self.code = code
        self.runtime_observation = runtime_observation
        super().__init__(code)


class ComponentSourceGenerationRunner(Protocol):
    """Generate one source candidate from one complete prepared Component node."""

    def __call__(
        self,
        prepared: PreparedComponentGenerationNode[Any, Any],
        /,
    ) -> SourceGenerationRunOutput: ...


class ComponentContextEvidenceRecorder(Protocol):
    """Receive the final exact bounded request/result pair for durable projection."""

    def record(
        self,
        prepared: PreparedComponentGenerationNode[Any, Any],
        result: SourceGenerationNodeResult,
    ) -> None: ...


_RUNNER_FAILURE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")


def _runner_failure_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and _RUNNER_FAILURE_CODE.fullmatch(code) is not None:
        return code
    return "runner-failed"


@dataclass(frozen=True, slots=True)
class ComponentSourceGenerationExecution:
    """Application-only pairing of scheduler evidence with its full source output."""

    result: SourceGenerationNodeResult
    output: SourceGenerationRunOutput | None
    failure_cause: Exception | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.result, SourceGenerationNodeResult):
            raise TypeError("result must be a SourceGenerationNodeResult")
        successful = self.result.disposition in {
            SourceGenerationDisposition.GENERATED,
            SourceGenerationDisposition.RETAINED,
            SourceGenerationDisposition.REUSED,
        }
        if successful:
            if self.failure_cause is not None:
                raise SourceGenerationSchedulingError(
                    "source_schedule.success_has_failure_cause",
                    "successful source execution cannot retain a failure cause",
                )
            if not isinstance(self.output, SourceGenerationRunOutput):
                raise SourceGenerationSchedulingError(
                    "source_schedule.execution_output_missing",
                    "successful source execution requires its full typed output",
                )
            if (
                self.result.candidate_identity != self.output.candidate_identity
                or self.result.provenance_identity != self.output.provenance_identity
            ):
                raise SourceGenerationSchedulingError(
                    "source_schedule.execution_output_mismatch",
                    "source execution result does not bind its exact output",
                )
        else:
            if self.output is not None:
                raise SourceGenerationSchedulingError(
                    "source_schedule.failed_execution_has_output",
                    "failed source execution cannot expose an accepted output",
                )
            if self.failure_cause is not None and not isinstance(
                self.failure_cause, Exception
            ):
                raise SourceGenerationSchedulingError(
                    "source_schedule.failure_cause_invalid",
                    "source execution failure cause must be an Exception",
                )


def _recipe_identity(
    prepared: PreparedComponentGenerationNode[Any, Any],
) -> ContentIdentity:
    raw = getattr(prepared.recipe, "identity", None)
    if isinstance(raw, ContentIdentity):
        return raw
    if isinstance(raw, str):
        try:
            return ContentIdentity.parse_uri(raw)
        except ValueError as error:
            raise SourceGenerationSchedulingError(
                "source_schedule.recipe_identity_invalid",
                "prepared recipe identity is not a canonical content identity",
            ) from error
    raise SourceGenerationSchedulingError(
        "source_schedule.recipe_identity_missing",
        "complete node preparation requires an identity-bearing recipe",
    )


def _require_complete_node(
    prepared: PreparedComponentGenerationNode[Any, Any],
    *,
    expected_plan: ComponentGenerationPlan | None = None,
) -> tuple[ComponentGenerationPlan, ContentIdentity]:
    if not isinstance(prepared, PreparedComponentGenerationNode):
        raise TypeError("prepared must be a PreparedComponentGenerationNode")
    plan = prepared.plan
    if not isinstance(plan, ComponentGenerationPlan):
        raise SourceGenerationSchedulingError(
            "source_schedule.plan_invalid",
            "prepared node must contain a typed Component generation plan",
        )
    if expected_plan is not None and plan != expected_plan:
        raise SourceGenerationSchedulingError(
            "source_schedule.plan_mismatch",
            "prepared node does not contain the exact scheduled Component plan",
        )
    request = prepared.request.request
    workspace = prepared.workspace
    if not isinstance(workspace, ComponentGenerationWorkspaceDescriptor):
        raise SourceGenerationSchedulingError(
            "source_schedule.workspace_invalid",
            "prepared node must contain a typed workspace descriptor",
        )
    if (
        request.component_generation_plan_identity != plan.identity
        or request.generation_key_identity != plan.generation_key.identity
        or request.context_manifest.component_revision != plan.component_revision
        or workspace.component_revision != plan.component_revision
        or workspace.generation_plan_identity != plan.identity
        or workspace.generation_key_identity != plan.generation_key.identity
    ):
        raise SourceGenerationSchedulingError(
            "source_schedule.preparation_identity_mismatch",
            "prepared node does not bind its exact plan, request, and workspace",
        )
    prompt_identity = ContentIdentity.parse_uri(
        f"sha256:{hashlib.sha256(prepared.request.prompt).hexdigest()}"
    )
    if prompt_identity != request.prompt_identity:
        raise SourceGenerationSchedulingError(
            "source_schedule.prompt_identity_mismatch",
            "prepared prompt bytes do not match the bounded request identity",
        )
    return plan, _recipe_identity(prepared)


def validate_prepared_component_generation_node(
    prepared: PreparedComponentGenerationNode[Any, Any],
    *,
    expected_plan: ComponentGenerationPlan | None = None,
) -> None:
    """Fail closed unless one complete prepared node binds its exact plan."""

    _require_complete_node(prepared, expected_plan=expected_plan)


def _output_matches(
    output: SourceGenerationRunOutput,
    prepared: PreparedComponentGenerationNode[Any, Any],
    recipe_identity: ContentIdentity,
) -> bool:
    plan = prepared.plan
    request = prepared.request.request
    workspace = prepared.workspace
    candidate = output.candidate
    provenance = output.provenance
    common = (
        candidate.component_revision == plan.component_revision
        and candidate.source_generation_request_identity == request.identity
        and candidate.component_generation_plan_identity == plan.identity
        and candidate.generation_key_identity == plan.generation_key.identity
        and candidate.context_manifest_identity == request.context_manifest_identity
        and candidate.prompt_identity == request.prompt_identity
        and candidate.recipe_identity == recipe_identity
        and candidate.workspace_allocation_identity == workspace.allocation_identity
        and provenance.source_generation_request_identity == request.identity
        and provenance.generated_component_revision_identity == plan.component_revision
        and provenance.component_generation_plan_identity == plan.identity
        and provenance.generation_key_identity == plan.generation_key.identity
        and provenance.context_manifest_identity == request.context_manifest_identity
        and provenance.prompt_identity == request.prompt_identity
        and provenance.recipe_identity == recipe_identity
        and provenance.workspace_allocation_identity == workspace.allocation_identity
    )
    recipe_lock = getattr(prepared.recipe, "component_lock_identity", None)
    return common and (
        recipe_lock is None or provenance.component_lock_identity == recipe_lock
    )


def _candidate_matches(
    candidate: SourceGenerationResumeCandidate,
    prepared: PreparedComponentGenerationNode[Any, Any],
    recipe_identity: ContentIdentity,
) -> bool:
    request = prepared.request.request
    return (
        candidate.complexity_budget_identity == request.budget.identity
        and candidate.complexity_decision_identity
        == request.complexity_decision_identity
        and _output_matches(candidate.output, prepared, recipe_identity)
        and not _observation_violation(candidate.output.runtime_observation, prepared)
    )


def _observation_violation(
    observation: ComponentGenerationRuntimeObservation | None,
    prepared: PreparedComponentGenerationNode[Any, Any],
) -> bool:
    if observation is None:
        return False
    budget = prepared.request.request.budget
    pairs = (
        (observation.model_attempts, budget.max_model_attempts),
        (observation.wall_time_ms, budget.max_wall_time_ms),
        (observation.model_tokens, budget.max_model_tokens),
        (observation.cost_microunits, budget.max_cost_microunits),
    )
    return any(actual is not None and actual > limit for actual, limit in pairs)


def _result(
    prepared: PreparedComponentGenerationNode[Any, Any],
    recipe_identity: ContentIdentity,
    disposition: SourceGenerationDisposition,
    *,
    output: SourceGenerationRunOutput | None = None,
    runtime_observation: ComponentGenerationRuntimeObservation | None = None,
    failure_code: str | None = None,
) -> SourceGenerationNodeResult:
    plan = prepared.plan
    request = prepared.request.request
    return SourceGenerationNodeResult(
        component_revision=plan.component_revision,
        generation_plan_identity=plan.identity,
        generation_key_identity=plan.generation_key.identity,
        context_manifest_identity=request.context_manifest_identity,
        complexity_budget_identity=request.budget.identity,
        complexity_decision_identity=request.complexity_decision_identity,
        prompt_identity=request.prompt_identity,
        recipe_identity=recipe_identity,
        workspace_allocation_identity=prepared.workspace.allocation_identity,
        disposition=disposition,
        candidate_identity=None if output is None else output.candidate_identity,
        provenance_identity=None if output is None else output.provenance_identity,
        runtime_observation=runtime_observation,
        failure_code=failure_code,
    )


def source_generation_terminal_result(
    prepared: PreparedComponentGenerationNode[Any, Any],
    *,
    disposition: SourceGenerationDisposition,
    failure_code: str,
) -> SourceGenerationNodeResult:
    """Create typed failed/cancelled evidence without invoking a source runner."""

    if disposition not in {
        SourceGenerationDisposition.FAILED,
        SourceGenerationDisposition.CANCELLED,
    }:
        raise SourceGenerationSchedulingError(
            "source_schedule.terminal_disposition_invalid",
            "terminal evidence requires failed or cancelled disposition",
        )
    _, recipe_identity = _require_complete_node(prepared)
    return _result(
        prepared,
        recipe_identity,
        disposition,
        failure_code=failure_code,
    )


def reusable_source_generation_output(
    prepared: PreparedComponentGenerationNode[Any, Any],
    candidate: SourceGenerationResumeCandidate | None,
    *,
    explicitly_invalid: bool,
) -> SourceGenerationRunOutput | None:
    """Apply the same resume admission before reserving generation capacity."""
    _, recipe_identity = _require_complete_node(prepared)
    if candidate is not None and not isinstance(
        candidate, SourceGenerationResumeCandidate
    ):
        raise SourceGenerationSchedulingError(
            "source_schedule.candidate_invalid", "resume candidate must be typed"
        )
    if (
        not explicitly_invalid
        and candidate is not None
        and _candidate_matches(candidate, prepared, recipe_identity)
    ):
        return candidate.output
    return None


def execute_component_source_generation_node(
    prepared: PreparedComponentGenerationNode[Any, Any],
    *,
    candidate: SourceGenerationResumeCandidate | None,
    explicitly_invalid: bool,
    runner: ComponentSourceGenerationRunner,
    context_evidence_recorder: ComponentContextEvidenceRecorder | None = None,
) -> ComponentSourceGenerationExecution:
    """Reuse or generate source for one complete prepared node, then stop."""

    _, recipe_identity = _require_complete_node(prepared)
    reused = reusable_source_generation_output(
        prepared, candidate, explicitly_invalid=explicitly_invalid
    )
    if not callable(runner):
        raise TypeError("runner must be callable")
    if reused is not None:
        execution = ComponentSourceGenerationExecution(
            _result(
                prepared,
                recipe_identity,
                SourceGenerationDisposition.REUSED,
                output=reused,
            ),
            reused,
        )
        if context_evidence_recorder is not None:
            context_evidence_recorder.record(prepared, execution.result)
        return execution
    try:
        output = runner(prepared)
        if not isinstance(output, SourceGenerationRunOutput):
            raise ComponentSourceGenerationRunError("runner-output-invalid")
        if not _output_matches(output, prepared, recipe_identity):
            raise ComponentSourceGenerationRunError("runner-output-identity-mismatch")
        if _observation_violation(output.runtime_observation, prepared):
            raise ComponentSourceGenerationRunError(
                "runtime-budget-exceeded",
                runtime_observation=output.runtime_observation,
            )
        execution = ComponentSourceGenerationExecution(
            _result(
                prepared,
                recipe_identity,
                (
                    SourceGenerationDisposition.RETAINED
                    if output.provenance.retained_source_identity is not None
                    else SourceGenerationDisposition.GENERATED
                ),
                output=output,
                runtime_observation=output.runtime_observation,
            ),
            output,
        )
    except ComponentSourceGenerationRunError as error:
        execution = ComponentSourceGenerationExecution(
            _result(
                prepared,
                recipe_identity,
                SourceGenerationDisposition.FAILED,
                runtime_observation=error.runtime_observation,
                failure_code=error.code,
            ),
            None,
            error,
        )
    except Exception as error:
        execution = ComponentSourceGenerationExecution(
            _result(
                prepared,
                recipe_identity,
                SourceGenerationDisposition.FAILED,
                failure_code=_runner_failure_code(error),
            ),
            None,
            error,
        )
    if context_evidence_recorder is not None:
        context_evidence_recorder.record(prepared, execution.result)
    return execution


def _preflight(
    execution_plan: ComponentExecutionPlan,
    invalidation: ComponentInvalidationDecision,
    prepared_nodes: Mapping[str, PreparedComponentGenerationNode[Any, Any]],
    resume_candidates: Mapping[str, SourceGenerationResumeCandidate],
) -> dict[str, ComponentGenerationPlan]:
    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    if not isinstance(invalidation, ComponentInvalidationDecision):
        raise TypeError("invalidation must be a ComponentInvalidationDecision")
    plans = {
        item.component_revision.uri: item for item in execution_plan.generation_plans
    }
    if set(prepared_nodes) != set(plans):
        raise SourceGenerationSchedulingError(
            "source_schedule.preparation_incomplete",
            "prepared nodes must cover every and only planned Component",
        )
    if set(resume_candidates) - set(plans):
        raise SourceGenerationSchedulingError(
            "source_schedule.candidate_unknown",
            "resume candidates contain a Component outside the exact plan",
        )
    admitted = set(plans)
    referenced = {
        invalidation.changed_component.uri,
        *(item.uri for item in invalidation.regenerate),
        *(item.uri for item in invalidation.rebuild),
        *(item.uri for item in invalidation.retest),
    }
    if invalidation.changed_component.uri not in admitted or not referenced <= admitted:
        raise SourceGenerationSchedulingError(
            "source_schedule.invalidation_foreign",
            "invalidation references a Component outside the exact plan",
        )
    for uri, plan in plans.items():
        _require_complete_node(prepared_nodes[uri], expected_plan=plan)
    if any(
        not isinstance(candidate, SourceGenerationResumeCandidate)
        for candidate in resume_candidates.values()
    ):
        raise SourceGenerationSchedulingError(
            "source_schedule.candidate_invalid",
            "resume candidates must be typed",
        )
    return plans


def schedule_source_generation(
    execution_plan: ComponentExecutionPlan,
    *,
    invalidation: ComponentInvalidationDecision,
    prepared_nodes: Mapping[str, PreparedComponentGenerationNode[Any, Any]],
    resume_candidates: Mapping[str, SourceGenerationResumeCandidate] | None = None,
    runner: ComponentSourceGenerationRunner,
    max_parallelism: int = 1,
    context_evidence_recorder: ComponentContextEvidenceRecorder | None = None,
) -> SourceGenerationScheduleResult:
    """Schedule source-only work over stable dependency layers."""

    executions = schedule_source_generation_executions(
        execution_plan,
        invalidation=invalidation,
        prepared_nodes=prepared_nodes,
        resume_candidates=resume_candidates,
        runner=runner,
        max_parallelism=max_parallelism,
        context_evidence_recorder=context_evidence_recorder,
    )
    return SourceGenerationScheduleResult(
        execution_plan.identity,
        invalidation.identity,
        max_parallelism,
        tuple(item.result for item in executions),
    )


def schedule_source_generation_executions(
    execution_plan: ComponentExecutionPlan,
    *,
    invalidation: ComponentInvalidationDecision,
    prepared_nodes: Mapping[str, PreparedComponentGenerationNode[Any, Any]],
    resume_candidates: Mapping[str, SourceGenerationResumeCandidate] | None = None,
    runner: ComponentSourceGenerationRunner,
    max_parallelism: int = 1,
    context_evidence_recorder: ComponentContextEvidenceRecorder | None = None,
) -> tuple[ComponentSourceGenerationExecution, ...]:
    """Schedule source-only work while retaining each full typed runner output."""

    if (
        isinstance(max_parallelism, bool)
        or not isinstance(max_parallelism, int)
        or not 1 <= max_parallelism <= 256
    ):
        raise SourceGenerationSchedulingError(
            "source_schedule.parallelism_invalid",
            "max_parallelism must be between 1 and 256",
        )
    if not callable(runner):
        raise TypeError("runner must be callable")
    nodes = dict(prepared_nodes)
    candidates = {} if resume_candidates is None else dict(resume_candidates)
    plans = _preflight(execution_plan, invalidation, nodes, candidates)
    action = next(
        item
        for item in execution_plan.action_plans
        if item.phase is ComponentActionPhase.GENERATE
    )
    dependencies: dict[str, set[str]] = {uri: set() for uri in plans}
    for edge in action.dependency_edges:
        dependencies[edge.consumer_revision.uri].add(edge.provider_revision.uri)
    explicitly_invalid = {item.uri for item in invalidation.regenerate}
    executions: dict[str, ComponentSourceGenerationExecution] = {}

    for layer in action.layers:
        pending: dict[
            Future[ComponentSourceGenerationExecution],
            tuple[str, PreparedComponentGenerationNode[Any, Any], ContentIdentity],
        ] = {}
        with ThreadPoolExecutor(max_workers=max_parallelism) as executor:
            for revision in layer.component_revisions:
                uri = revision.uri
                prepared = nodes[uri]
                recipe_identity = _recipe_identity(prepared)
                if any(
                    executions[parent].result.disposition
                    in {
                        SourceGenerationDisposition.FAILED,
                        SourceGenerationDisposition.CANCELLED,
                    }
                    for parent in dependencies[uri]
                ):
                    executions[uri] = ComponentSourceGenerationExecution(
                        _result(
                            prepared,
                            recipe_identity,
                            SourceGenerationDisposition.CANCELLED,
                            failure_code="dependency-failed",
                        ),
                        None,
                    )
                    if context_evidence_recorder is not None:
                        context_evidence_recorder.record(
                            prepared, executions[uri].result
                        )
                    continue
                future = executor.submit(
                    execute_component_source_generation_node,
                    prepared,
                    candidate=candidates.get(uri),
                    explicitly_invalid=uri in explicitly_invalid,
                    runner=runner,
                    context_evidence_recorder=context_evidence_recorder,
                )
                pending[future] = (uri, prepared, recipe_identity)

            for future in as_completed(pending):
                uri, prepared, recipe_identity = pending[future]
                try:
                    executions[uri] = future.result()
                except Exception as error:
                    executions[uri] = ComponentSourceGenerationExecution(
                        _result(
                            prepared,
                            recipe_identity,
                            SourceGenerationDisposition.FAILED,
                            failure_code=_runner_failure_code(error),
                        ),
                        None,
                    )

    return tuple(executions[uri] for uri in sorted(executions))


__all__ = [
    "ComponentSourceGenerationExecution",
    "ComponentContextEvidenceRecorder",
    "ComponentSourceGenerationRunError",
    "ComponentSourceGenerationRunner",
    "SourceGenerationSchedulingError",
    "execute_component_source_generation_node",
    "schedule_source_generation",
    "schedule_source_generation_executions",
    "source_generation_terminal_result",
    "validate_prepared_component_generation_node",
]

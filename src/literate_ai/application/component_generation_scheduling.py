"""Bounded topological scheduling for independently generatable Components."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from literate_ai.application.component_generation_context import (
    PreparedComponentGenerationRequest,
)
from literate_ai.contracts.executable_components import (
    ComponentActionPhase,
    ComponentExecutionPlan,
    ComponentGenerationPlan,
    ComponentInvalidationDecision,
)
from literate_ai.contracts.executable_components.scheduling import (
    ComponentGenerationDisposition,
    ComponentGenerationNodeResult,
    ComponentGenerationResumeCandidate,
    ComponentGenerationRunOutput,
    ComponentGenerationRuntimeObservation,
    ComponentGenerationScheduleResult,
)


class ComponentGenerationSchedulingError(ValueError):
    """The schedule was structurally invalid before any generation call."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_RUNNER_FAILURE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")


def _runner_failure_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and _RUNNER_FAILURE_CODE.fullmatch(code) is not None:
        return code
    return "runner-failed"


class ComponentGenerationRunError(RuntimeError):
    """A runner-reported stable failure with optional exact provider metrics."""

    def __init__(
        self,
        code: str,
        *,
        runtime_observation: ComponentGenerationRuntimeObservation | None = None,
    ) -> None:
        self.code = code
        self.runtime_observation = runtime_observation
        super().__init__(code)


ComponentGenerationRunner = Callable[
    [ComponentGenerationPlan, PreparedComponentGenerationRequest],
    ComponentGenerationRunOutput,
]


def execute_component_generation_node(
    plan: ComponentGenerationPlan,
    prepared: PreparedComponentGenerationRequest,
    *,
    candidate: ComponentGenerationResumeCandidate | None,
    explicitly_invalid: bool,
    runner: ComponentGenerationRunner,
) -> ComponentGenerationNodeResult:
    """Execute or exactly reuse one already dependency-admitted Component node."""

    if not isinstance(plan, ComponentGenerationPlan):
        raise TypeError("plan must be a ComponentGenerationPlan")
    if not isinstance(prepared, PreparedComponentGenerationRequest):
        raise TypeError("prepared must be a PreparedComponentGenerationRequest")
    request = prepared.request
    if (
        request.component_generation_plan_identity != plan.identity
        or request.generation_key_identity != plan.generation_key.identity
        or request.context_manifest.component_revision != plan.component_revision
    ):
        raise ComponentGenerationSchedulingError(
            "component_schedule.context_identity_mismatch",
            "prepared request does not bind its exact current Component plan",
        )
    if candidate is not None and not isinstance(
        candidate, ComponentGenerationResumeCandidate
    ):
        raise ComponentGenerationSchedulingError(
            "component_schedule.candidate_invalid",
            "resume candidate must be typed",
        )
    if not callable(runner):
        raise TypeError("runner must be callable")
    if (
        not explicitly_invalid
        and candidate is not None
        and _candidate_matches(candidate, plan, prepared)
    ):
        return _result(
            plan,
            prepared,
            ComponentGenerationDisposition.REUSED,
            accepted_result_identity=candidate.accepted_result_identity,
        )
    try:
        output = runner(plan, prepared)
        if not isinstance(output, ComponentGenerationRunOutput):
            raise ComponentGenerationRunError("runner-output-invalid")
        if _observation_violation(output.runtime_observation, prepared):
            raise ComponentGenerationRunError(
                "runtime-budget-exceeded",
                runtime_observation=output.runtime_observation,
            )
        return _result(
            plan,
            prepared,
            ComponentGenerationDisposition.GENERATED,
            accepted_result_identity=output.accepted_result_identity,
            runtime_observation=output.runtime_observation,
        )
    except ComponentGenerationRunError as error:
        return _result(
            plan,
            prepared,
            ComponentGenerationDisposition.FAILED,
            runtime_observation=error.runtime_observation,
            failure_code=error.code,
        )
    except Exception as error:
        return _result(
            plan,
            prepared,
            ComponentGenerationDisposition.FAILED,
            failure_code=_runner_failure_code(error),
        )


def _result(
    plan: ComponentGenerationPlan,
    prepared: PreparedComponentGenerationRequest,
    disposition: ComponentGenerationDisposition,
    *,
    accepted_result_identity=None,
    runtime_observation=None,
    failure_code: str | None = None,
) -> ComponentGenerationNodeResult:
    request = prepared.request
    return ComponentGenerationNodeResult(
        component_revision=plan.component_revision,
        generation_plan_identity=plan.identity,
        generation_key_identity=plan.generation_key.identity,
        context_manifest_identity=request.context_manifest_identity,
        complexity_budget_identity=request.budget.identity,
        complexity_decision_identity=request.complexity_decision_identity,
        prompt_identity=request.prompt_identity,
        disposition=disposition,
        accepted_result_identity=accepted_result_identity,
        runtime_observation=runtime_observation,
        failure_code=failure_code,
    )


def _candidate_matches(
    candidate: ComponentGenerationResumeCandidate,
    plan: ComponentGenerationPlan,
    prepared: PreparedComponentGenerationRequest,
) -> bool:
    request = prepared.request
    return (
        candidate.generation_key_identity == plan.generation_key.identity
        and candidate.context_manifest_identity == request.context_manifest_identity
        and candidate.complexity_budget_identity == request.budget.identity
        and candidate.complexity_decision_identity
        == request.complexity_decision_identity
        and candidate.prompt_identity == request.prompt_identity
    )


def _observation_violation(
    observation: ComponentGenerationRuntimeObservation | None,
    prepared: PreparedComponentGenerationRequest,
) -> bool:
    if observation is None:
        return False
    budget = prepared.request.budget
    pairs = (
        (observation.model_attempts, budget.max_model_attempts),
        (observation.wall_time_ms, budget.max_wall_time_ms),
        (observation.model_tokens, budget.max_model_tokens),
        (observation.cost_microunits, budget.max_cost_microunits),
    )
    return any(actual is not None and actual > limit for actual, limit in pairs)


def _preflight(
    execution_plan: ComponentExecutionPlan,
    invalidation: ComponentInvalidationDecision,
    prepared_requests: Mapping[str, PreparedComponentGenerationRequest],
    resume_candidates: Mapping[str, ComponentGenerationResumeCandidate],
) -> dict[str, ComponentGenerationPlan]:
    if not isinstance(execution_plan, ComponentExecutionPlan):
        raise TypeError("execution_plan must be a ComponentExecutionPlan")
    if not isinstance(invalidation, ComponentInvalidationDecision):
        raise TypeError("invalidation must be a ComponentInvalidationDecision")
    plans = {
        item.component_revision.uri: item for item in execution_plan.generation_plans
    }
    if set(prepared_requests) != set(plans):
        raise ComponentGenerationSchedulingError(
            "component_schedule.context_incomplete",
            "prepared requests must cover every and only planned Component",
        )
    unknown_candidates = set(resume_candidates) - set(plans)
    if unknown_candidates:
        raise ComponentGenerationSchedulingError(
            "component_schedule.candidate_unknown",
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
        raise ComponentGenerationSchedulingError(
            "component_schedule.invalidation_foreign",
            "invalidation references a Component outside the exact plan",
        )
    for uri, plan in plans.items():
        prepared = prepared_requests[uri]
        if not isinstance(prepared, PreparedComponentGenerationRequest):
            raise ComponentGenerationSchedulingError(
                "component_schedule.context_invalid",
                "prepared request values must be typed",
            )
        request = prepared.request
        if (
            request.component_generation_plan_identity != plan.identity
            or request.generation_key_identity != plan.generation_key.identity
            or request.context_manifest.component_revision != plan.component_revision
        ):
            raise ComponentGenerationSchedulingError(
                "component_schedule.context_identity_mismatch",
                "prepared request does not bind its exact current Component plan",
            )
    for candidate in resume_candidates.values():
        if not isinstance(candidate, ComponentGenerationResumeCandidate):
            raise ComponentGenerationSchedulingError(
                "component_schedule.candidate_invalid",
                "resume candidates must be typed",
            )
    return plans


def schedule_component_generation(
    execution_plan: ComponentExecutionPlan,
    *,
    invalidation: ComponentInvalidationDecision,
    prepared_requests: Mapping[str, PreparedComponentGenerationRequest],
    resume_candidates: Mapping[str, ComponentGenerationResumeCandidate] | None = None,
    runner: ComponentGenerationRunner,
    max_parallelism: int = 1,
) -> ComponentGenerationScheduleResult:
    """Reuse exact candidates and generate invalidated nodes in stable DAG layers."""

    if (
        isinstance(max_parallelism, bool)
        or not isinstance(max_parallelism, int)
        or not 1 <= max_parallelism <= 256
    ):
        raise ComponentGenerationSchedulingError(
            "component_schedule.parallelism_invalid",
            "max_parallelism must be between 1 and 256",
        )
    if not callable(runner):
        raise TypeError("runner must be callable")
    requests = dict(prepared_requests)
    candidates = {} if resume_candidates is None else dict(resume_candidates)
    plans = _preflight(execution_plan, invalidation, requests, candidates)
    action = next(
        item
        for item in execution_plan.action_plans
        if item.phase is ComponentActionPhase.GENERATE
    )
    dependencies: dict[str, set[str]] = {uri: set() for uri in plans}
    for edge in action.dependency_edges:
        dependencies[edge.consumer_revision.uri].add(edge.provider_revision.uri)
    explicitly_invalid = {item.uri for item in invalidation.regenerate}
    results: dict[str, ComponentGenerationNodeResult] = {}

    for layer in action.layers:
        pending: dict[
            Future[ComponentGenerationNodeResult],
            tuple[str, ComponentGenerationPlan, PreparedComponentGenerationRequest],
        ] = {}
        with ThreadPoolExecutor(max_workers=max_parallelism) as executor:
            for revision in layer.component_revisions:
                uri = revision.uri
                plan = plans[uri]
                prepared = requests[uri]
                if any(
                    results[parent].disposition
                    in {
                        ComponentGenerationDisposition.FAILED,
                        ComponentGenerationDisposition.CANCELLED,
                    }
                    for parent in dependencies[uri]
                ):
                    results[uri] = _result(
                        plan,
                        prepared,
                        ComponentGenerationDisposition.CANCELLED,
                        failure_code="dependency-failed",
                    )
                    continue
                candidate = candidates.get(uri)
                future = executor.submit(
                    execute_component_generation_node,
                    plan,
                    prepared,
                    candidate=candidate,
                    explicitly_invalid=uri in explicitly_invalid,
                    runner=runner,
                )
                pending[future] = (uri, plan, prepared)

            for future in as_completed(pending):
                uri, plan, prepared = pending[future]
                try:
                    results[uri] = future.result()
                except Exception as error:
                    results[uri] = _result(
                        plan,
                        prepared,
                        ComponentGenerationDisposition.FAILED,
                        failure_code=_runner_failure_code(error),
                    )

    return ComponentGenerationScheduleResult(
        execution_plan.identity,
        invalidation.identity,
        max_parallelism,
        tuple(results[uri] for uri in sorted(results)),
    )


__all__ = [
    "ComponentGenerationRunError",
    "ComponentGenerationRunner",
    "ComponentGenerationSchedulingError",
    "execute_component_generation_node",
    "schedule_component_generation",
]

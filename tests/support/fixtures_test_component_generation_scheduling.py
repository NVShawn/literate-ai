from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_component_generation_scheduling``."""


from dataclasses import replace

from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    PreparedComponentGenerationRequest,
    prepare_component_generation_context,
)
from literate_ai.contracts.executable_components import (
    ComponentChangeSurface,
    ComponentInvalidationDecision,
    GenerationComplexityBudget,
)
from tests.support.fixtures_test_component_execution_planning import (
    _models,
)
from tests.support.fixtures_test_component_generation_context import _materialize


def _budget() -> GenerationComplexityBudget:
    return GenerationComplexityBudget(
        max_prompt_bytes=1_000_000,
        max_estimated_tokens=250_000,
        max_document_count=100,
        max_direct_interface_bytes=100_000,
        max_dependency_fan_in=100,
        max_model_attempts=3,
        max_wall_time_ms=600_000,
        max_model_tokens=100_000,
        max_cost_microunits=50_000_000,
    )


def _prepared_execution(lock):
    execution = plan_component_execution(lock, model_identities=_models(lock))
    prepared: dict[str, PreparedComponentGenerationRequest] = {}
    plans = []
    for original in execution.generation_plans:
        plan, segments = _materialize(original)
        request = prepare_component_generation_context(
            plan,
            framework_envelope=b"scheduler envelope\n",
            authority_segments=segments,
            budget=_budget(),
        )
        plans.append(plan)
        prepared[plan.component_revision.uri] = request
    return replace(execution, generation_plans=tuple(plans)), prepared


def _names(lock) -> dict[str, str]:
    return {
        item.revision.identity.uri: item.revision.coordinate.name for item in lock.nodes
    }


def _decision(execution, names, changed, regenerate=()):
    revisions = {
        names[item.component_revision.uri]: item.component_revision
        for item in execution.generation_plans
    }
    regenerated = tuple(
        sorted((revisions[name] for name in regenerate), key=lambda x: x.uri)
    )
    changed_revision = revisions[changed]
    surface = (
        ComponentChangeSurface.LOCAL_AUTHORITY
        if changed in regenerate
        else ComponentChangeSurface.SOURCE_REPLACEMENT
    )
    all_revisions = tuple(sorted(revisions.values(), key=lambda item: item.uri))
    return ComponentInvalidationDecision(
        f"change-{changed}",
        changed_revision,
        surface,
        regenerated,
        all_revisions,
        all_revisions,
    )

"""Bounded incremental scheduling over executable Component DAGs."""

from __future__ import annotations

import threading
import unittest
from dataclasses import replace

from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_generation_context import (
    PreparedComponentGenerationRequest,
    prepare_component_generation_context,
)
from literate_ai.application.component_generation_scheduling import (
    ComponentGenerationRunError,
    schedule_component_generation,
)
from literate_ai.contracts.executable_components import (
    ComponentChangeSurface,
    ComponentGenerationResumeCandidate,
    ComponentGenerationRunOutput,
    ComponentInvalidationDecision,
    GenerationComplexityBudget,
)
from literate_ai.contracts.identity import canonical_identity
from tests.support.fixtures_test_component_execution_planning import (
    _diamond_lock,
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


def _candidates(execution, prepared, names):
    return {
        uri: ComponentGenerationResumeCandidate(
            plan.generation_key.identity,
            prepared[uri].request.context_manifest_identity,
            prepared[uri].request.budget.identity,
            prepared[uri].request.complexity_decision_identity,
            prepared[uri].request.prompt_identity,
            canonical_identity({"accepted": names[uri]}),
        )
        for uri, plan in (
            (item.component_revision.uri, item) for item in execution.generation_plans
        )
    }


def _remap_candidates(baseline, baseline_names, current_names):
    by_name = {baseline_names[uri]: candidate for uri, candidate in baseline.items()}
    return {
        uri: by_name[name] for uri, name in current_names.items() if name in by_name
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


class RecordingRunner:
    def __init__(self, names, *, fail=(), observation=None):
        self.names = names
        self.fail = set(fail)
        self.observation = observation
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, plan, prepared):
        name = self.names[plan.component_revision.uri]
        with self._lock:
            self.calls.append(name)
        if name in self.fail:
            raise ComponentGenerationRunError(
                "model-failed", runtime_observation=self.observation
            )
        return ComponentGenerationRunOutput(
            canonical_identity({"generated": name}), self.observation
        )


class ComponentGenerationSchedulingTests(unittest.TestCase):
    def test_diamond_calls_are_proportional_to_local_and_interface_changes(
        self,
    ) -> None:
        baseline_lock = _diamond_lock()
        baseline, baseline_requests = _prepared_execution(baseline_lock)
        baseline_names = _names(baseline_lock)
        accepted = _candidates(baseline, baseline_requests, baseline_names)

        runner = RecordingRunner(baseline_names)
        result = schedule_component_generation(
            baseline,
            invalidation=_decision(baseline, baseline_names, "money"),
            prepared_requests=baseline_requests,
            resume_candidates=accepted,
            runner=runner,
            max_parallelism=2,
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(result.actual_generation_calls, 0)

        local_lock = _diamond_lock(invoice_spec="invoice-private-v2")
        local, local_requests = _prepared_execution(local_lock)
        local_names = _names(local_lock)
        runner = RecordingRunner(local_names)
        local_result = schedule_component_generation(
            local,
            invalidation=_decision(local, local_names, "invoice-cli", ("invoice-cli",)),
            prepared_requests=local_requests,
            resume_candidates=_remap_candidates(accepted, baseline_names, local_names),
            runner=runner,
            max_parallelism=2,
        )
        self.assertEqual(runner.calls, ["invoice-cli"])
        self.assertEqual(local_result.actual_generation_calls, 1)

        public_lock = _diamond_lock(money_interface="money-public-v2")
        public, public_requests = _prepared_execution(public_lock)
        public_names = _names(public_lock)
        runner = RecordingRunner(public_names)
        public_result = schedule_component_generation(
            public,
            invalidation=_decision(
                public,
                public_names,
                "money",
                ("money", "pricing", "reporting"),
            ),
            prepared_requests=public_requests,
            resume_candidates=_remap_candidates(accepted, baseline_names, public_names),
            runner=runner,
            max_parallelism=2,
        )
        self.assertEqual(set(runner.calls), {"money", "pricing", "reporting"})
        self.assertEqual(public_result.actual_generation_calls, 3)


if __name__ == "__main__":
    unittest.main()

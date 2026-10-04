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
    ComponentGenerationSchedulingError,
    execute_component_generation_node,
    schedule_component_generation,
)
from literate_ai.contracts.executable_components import (
    ComponentChangeSurface,
    ComponentGenerationDisposition,
    ComponentGenerationResumeCandidate,
    ComponentGenerationRunOutput,
    ComponentGenerationRuntimeObservation,
    ComponentGenerationScheduleResult,
    ComponentInvalidationDecision,
    GenerationComplexityBudget,
)
from literate_ai.contracts.identity import canonical_identity
from tests.support.vfi_scaling import PRIVATE_DESCENDANT, vfi_component_lock
from tests.support.fixtures_test_component_execution_planning import (
    _diamond_lock,
    _models,
)
from tests.support.fixtures_test_component_generation_context import _materialize
from tests.support.fixtures_test_component_lock_contracts import component_lock
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


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

    def test_chain_failure_cancels_dependent_without_calling_it(self) -> None:
        lock = component_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        accepted = _candidates(execution, requests, names)
        runner = RecordingRunner(names, fail={"pricing"})
        result = schedule_component_generation(
            execution,
            invalidation=_decision(execution, names, "pricing", ("pricing",)),
            prepared_requests=requests,
            resume_candidates=accepted,
            runner=runner,
            max_parallelism=2,
        )
        self.assertEqual(runner.calls, ["pricing"])
        by_name = {
            names[item.component_revision.uri]: item for item in result.node_results
        }
        self.assertEqual(
            by_name["pricing"].disposition, ComponentGenerationDisposition.FAILED
        )
        self.assertEqual(
            by_name["invoice-cli"].disposition,
            ComponentGenerationDisposition.CANCELLED,
        )

    def test_untyped_runner_exception_preserves_a_safe_adapter_code(self) -> None:
        lock = component_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        plan = next(
            item
            for item in execution.generation_plans
            if names[item.component_revision.uri] == "pricing"
        )
        prepared = requests[plan.component_revision.uri]

        class AdapterFailure(RuntimeError):
            code = "source_generation.asset_authority_mismatch"

        def adapter_failed(_plan, _prepared):
            raise AdapterFailure("locked asset custody changed")

        result = execute_component_generation_node(
            plan,
            prepared,
            candidate=None,
            explicitly_invalid=False,
            runner=adapter_failed,
        )
        self.assertEqual(
            result.failure_code, "source_generation.asset_authority_mismatch"
        )

        def uncoded_failure(_plan, _prepared):
            raise RuntimeError("uncoded runner failure")

        result = execute_component_generation_node(
            plan,
            prepared,
            candidate=None,
            explicitly_invalid=False,
            runner=uncoded_failure,
        )
        self.assertEqual(result.failure_code, "runner-failed")

        class InvalidAdapterFailure(RuntimeError):
            code = "INVALID CODE"

        def untrusted_code_failure(_plan, _prepared):
            raise InvalidAdapterFailure("untrusted diagnostic code")

        result = execute_component_generation_node(
            plan,
            prepared,
            candidate=None,
            explicitly_invalid=False,
            runner=untrusted_code_failure,
        )
        self.assertEqual(result.failure_code, "runner-failed")

    def test_supplied_runtime_over_budget_fails_and_preserves_actual_metrics(
        self,
    ) -> None:
        lock = component_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        accepted = _candidates(execution, requests, names)
        observation = ComponentGenerationRuntimeObservation(4, None, 17, None)
        runner = RecordingRunner(names, observation=observation)
        result = schedule_component_generation(
            execution,
            invalidation=_decision(execution, names, "pricing", ("pricing",)),
            prepared_requests=requests,
            resume_candidates=accepted,
            runner=runner,
            max_parallelism=1,
        )
        by_name = {
            names[item.component_revision.uri]: item for item in result.node_results
        }
        self.assertEqual(
            by_name["pricing"].disposition, ComponentGenerationDisposition.FAILED
        )
        self.assertEqual(by_name["pricing"].failure_code, "runtime-budget-exceeded")
        self.assertEqual(by_name["pricing"].runtime_observation, observation)
        self.assertIsNone(by_name["pricing"].runtime_observation.wall_time_ms)
        self.assertEqual(
            by_name["invoice-cli"].disposition,
            ComponentGenerationDisposition.CANCELLED,
        )

    def test_diamond_independent_nodes_run_with_bounded_parallelism(self) -> None:
        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        accepted = _candidates(execution, requests, names)
        barrier = threading.Barrier(2)
        active = 0
        maximum = 0
        guard = threading.Lock()
        calls = []

        def runner(plan, prepared):
            nonlocal active, maximum
            name = names[plan.component_revision.uri]
            calls.append(name)
            if name in {"pricing", "reporting"}:
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                barrier.wait(timeout=2)
                with guard:
                    active -= 1
            return ComponentGenerationRunOutput(canonical_identity({"run": name}))

        result = schedule_component_generation(
            execution,
            invalidation=_decision(
                execution,
                names,
                "pricing",
                ("pricing", "reporting"),
            ),
            prepared_requests=requests,
            resume_candidates=accepted,
            runner=runner,
            max_parallelism=2,
        )
        self.assertEqual(set(calls), {"pricing", "reporting"})
        self.assertEqual(maximum, 2)
        self.assertEqual(result.max_parallelism, 2)

    def test_vfi_fifty_siblings_regenerate_only_changed_feature(self) -> None:
        baseline_lock = vfi_component_lock()
        baseline, baseline_requests = _prepared_execution(baseline_lock)
        baseline_names = _names(baseline_lock)
        accepted = _candidates(baseline, baseline_requests, baseline_names)
        changed_lock = vfi_component_lock(
            specification_overrides={PRIVATE_DESCENDANT: "private-descendant-v2"}
        )
        changed, changed_requests = _prepared_execution(changed_lock)
        changed_names = _names(changed_lock)
        runner = RecordingRunner(changed_names)
        result = schedule_component_generation(
            changed,
            invalidation=_decision(
                changed,
                changed_names,
                PRIVATE_DESCENDANT,
                (PRIVATE_DESCENDANT,),
            ),
            prepared_requests=changed_requests,
            resume_candidates=_remap_candidates(
                accepted, baseline_names, changed_names
            ),
            runner=runner,
            max_parallelism=8,
        )
        self.assertEqual(runner.calls, [PRIVATE_DESCENDANT])
        self.assertEqual(result.actual_generation_calls, 1)
        self.assertEqual(len(result.node_results), 52)

    def test_resume_revalidation_metrics_and_canonical_result_are_exact(self) -> None:
        lock = component_lock()
        execution, requests = _prepared_execution(lock)
        names = _names(lock)
        accepted = _candidates(execution, requests, names)
        pricing_uri = next(uri for uri, name in names.items() if name == "pricing")
        accepted[pricing_uri] = replace(
            accepted[pricing_uri],
            context_manifest_identity=canonical_identity({"stale": "context"}),
        )
        observation = ComponentGenerationRuntimeObservation(2, 17, None, 43)
        runner = RecordingRunner(names, observation=observation)
        result = schedule_component_generation(
            execution,
            invalidation=_decision(execution, names, "invoice-cli"),
            prepared_requests=requests,
            resume_candidates=accepted,
            runner=runner,
            max_parallelism=2,
        )
        self.assertEqual(runner.calls, ["pricing"])
        pricing = next(
            item
            for item in result.node_results
            if item.component_revision.uri == pricing_uri
        )
        self.assertEqual(pricing.runtime_observation, observation)
        self.assertIsNone(pricing.runtime_observation.model_tokens)
        self.assertEqual(
            result, ComponentGenerationScheduleResult.from_dict(result.to_dict())
        )
        self.assertEqual(
            tuple(item.component_revision.uri for item in result.node_results),
            tuple(sorted(item.component_revision.uri for item in result.node_results)),
        )
        run_output = ComponentGenerationRunOutput(
            canonical_identity({"schema-fixture": "run-output"}), observation
        )
        schemas = SchemaCatalog()
        for contract in (
            observation,
            accepted[pricing_uri],
            run_output,
            pricing,
            result,
        ):
            with self.subTest(schema=contract.SCHEMA):
                schemas.validate(contract.SCHEMA, contract.to_dict())

        wrong = dict(requests)
        invoice_uri = next(uri for uri, name in names.items() if name == "invoice-cli")
        wrong[invoice_uri] = requests[pricing_uri]
        calls_before = list(runner.calls)
        with self.assertRaises(ComponentGenerationSchedulingError) as error:
            schedule_component_generation(
                execution,
                invalidation=_decision(execution, names, "invoice-cli"),
                prepared_requests=wrong,
                resume_candidates=accepted,
                runner=runner,
                max_parallelism=2,
            )
        self.assertEqual(
            error.exception.code, "component_schedule.context_identity_mismatch"
        )
        self.assertEqual(runner.calls, calls_before)


if __name__ == "__main__":
    unittest.main()

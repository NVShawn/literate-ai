"""Generation capacity preserves shared readiness, output admission, and resume."""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from literate_ai.contracts import (
    ComponentGenerationRuntimeObservation,
    SourceGenerationResumeCandidate,
)
from tests.support.fixtures_test_standard_project_lifecycle import (
    LifecyclePorts,
    _decision,
    _diamond_lock,
    _names,
    _prepared_execution,
    _prepared_nodes,
    _service,
)


class GenerationReadyQueueTests(unittest.TestCase):
    def setUp(self):
        self.lock = _diamond_lock()
        self.execution, requests = _prepared_execution(self.lock)
        self.names = _names(self.lock)
        self.nodes = _prepared_nodes(self.execution, requests)
        self.ports = LifecyclePorts(self.execution, self.names)
        self.service = _service(self.ports)

    def execute(self, *, resumes=None):
        return self.service.execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                () if resumes is not None else tuple(self.names.values()),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
            source_generation_resume_candidates=resumes,
        )

    def generator(self, reserve):
        class Generator:
            def __call__(self, prepared):
                raise AssertionError("GENERATION reservation bypassed")

            def try_reserve_generate(self, prepared):
                return reserve(prepared)

        self.service.generator = Generator()

    def test_exhausted_generation_capacity_allows_other_phases_to_progress(self):
        waits, released = [], []

        def reserve(prepared):
            revision = prepared.plan.component_revision
            if (
                self.names[revision.uri] != "money"
                and ("accept", "money") not in self.ports.events
            ):
                waits.append(revision)
                return None
            return SimpleNamespace(
                run=lambda: self.ports(prepared),
                release=lambda: released.append(revision),
            )

        self.generator(reserve)
        result = self.execute()
        self.assertTrue(result.successful)
        self.assertTrue(waits)
        self.assertEqual(len(released), len(self.names))
        self.assertEqual(len(set(released)), len(released))

    def test_invalid_reserved_output_releases_without_build_or_acceptance(self):
        self.check_refusal(lambda prepared: object())

    def test_reserved_exception_releases_without_build_or_acceptance(self):
        def fail(prepared):
            raise RuntimeError("generation failed")

        self.check_refusal(fail)

    def test_reserved_output_for_another_node_is_refused(self):
        def foreign(prepared):
            other = next(
                node for node in self.nodes.values() if node.plan != prepared.plan
            )
            return self.ports(other)

        self.check_refusal(foreign)

    def test_reserved_output_over_runtime_budget_is_refused(self):
        def over_budget(prepared):
            return replace(
                self.ports(prepared),
                runtime_observation=ComponentGenerationRuntimeObservation(
                    prepared.request.request.budget.max_model_attempts + 1,
                    1,
                    None,
                    None,
                ),
            )

        self.check_refusal(over_budget)

    def check_refusal(self, operation):
        released = []
        self.generator(
            lambda prepared: SimpleNamespace(
                run=lambda: operation(prepared),
                release=lambda: released.append(True),
            )
        )
        result = self.execute()
        self.assertFalse(result.successful)
        self.assertTrue(released)
        self.assertFalse(any(e[0] in {"build", "accept"} for e in self.ports.events))

    def test_current_resume_never_reserves_generation_capacity(self):
        first = self.execute()
        self.assertTrue(first.successful)
        resumes = {
            node.component_revision.uri: SourceGenerationResumeCandidate(
                node.source_output,
                node.source_output.identity,
                node.source_generation.complexity_budget_identity,
                node.source_generation.complexity_decision_identity,
            )
            for node in first.node_results
        }
        self.ports = LifecyclePorts(self.execution, self.names)
        self.service = _service(self.ports)

        def reserve(prepared):
            raise AssertionError("resumed source consumed worker capacity")

        self.generator(reserve)
        second = self.execute(resumes=resumes)
        self.assertTrue(second.successful, str(second.node_results))
        self.assertFalse(any(e[0] == "generate" for e in self.ports.events))

"""Public resumable host Component lifecycle parity and identity tests."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.application import (
    GenerationFailure,
    GenerationRequest,
    GenerationStatus,
    HostComponentLifecyclePhase,
    HostComponentLifecycleSession,
)
from literate_ai.contracts import canonical_identity


class HostComponentLifecycleSessionTests(unittest.TestCase):
    @staticmethod
    def system():
        from tests.support.fixtures_test_application_generation import GenerationOrchestratorTests

        fixture = GenerationOrchestratorTests(methodName="runTest")
        return fixture.make_system()

    def test_session_has_effect_and_provenance_parity_with_one_shot_lifecycle(self):
        direct_request, direct_orchestrator, direct_calls, *_ = self.system()
        direct = direct_orchestrator.execute(direct_request)

        request, orchestrator, calls, *_ = self.system()
        result = HostComponentLifecycleSession(orchestrator, request).run()
        resumed = result.run

        self.assertEqual(resumed.status, GenerationStatus.COMPLETE)
        self.assertEqual(calls, direct_calls)
        self.assertEqual(resumed.input_identity, direct.input_identity)
        self.assertEqual(resumed.provenance, direct.provenance)
        self.assertEqual(
            [item.output_identity for item in resumed.stage_executions],
            [item.output_identity for item in direct.stage_executions],
        )
        self.assertEqual(
            [item.output_identity for item in resumed.lifecycle_executions],
            [item.output_identity for item in direct.lifecycle_executions],
        )
        self.assertEqual(result.checkpoint.phase, HostComponentLifecyclePhase.COMMIT)

    def test_interrupted_session_resumes_without_repeating_completed_effects(self):
        request, orchestrator, calls, *_ = self.system()
        session = HostComponentLifecycleSession(orchestrator, request)
        checkpoint = session.prepare_generate()
        checkpoint = session.validate_classify(checkpoint)
        checkpoint = session.authorize(checkpoint)
        checkpoint = session.build_resolve(checkpoint)
        checkpoint = session.generated_test(checkpoint)
        checkpoint_identity = checkpoint.identity

        self.assertEqual(checkpoint.phase, HostComponentLifecyclePhase.GENERATED_TEST)
        self.assertEqual(checkpoint.run.status, GenerationStatus.PAUSED)
        self.assertEqual(calls.count("model:generate"), 1)
        self.assertEqual(calls.count("build"), 1)
        self.assertEqual(calls.count("test-generated"), 1)
        self.assertNotIn("verify-independent", calls)
        self.assertNotIn("prepare", calls)
        self.assertNotIn("commit", calls)

        result = session.run(checkpoint)

        self.assertEqual(checkpoint.identity, checkpoint_identity)
        self.assertEqual(result.run.status, GenerationStatus.COMPLETE)
        self.assertEqual(calls.count("model:generate"), 1)
        self.assertEqual(calls.count("build"), 1)
        self.assertEqual(calls.count("test-generated"), 1)
        self.assertEqual(calls.count("verify-independent"), 1)
        self.assertEqual(calls.count("prepare"), 1)
        self.assertEqual(calls.count("commit"), 1)

    def test_each_typed_phase_stops_after_its_exact_effect_boundary(self):
        request, orchestrator, calls, *_ = self.system()
        session = HostComponentLifecycleSession(orchestrator, request)

        checkpoint = session.prepare_generate()
        self.assertNotIn("validate", calls)
        checkpoint = session.validate_classify(checkpoint)
        self.assertEqual(calls.count("validate"), 1)
        self.assertEqual(calls.count("classify"), 1)
        self.assertNotIn("authorize", calls)
        checkpoint = session.authorize(checkpoint)
        self.assertEqual(calls.count("authorize"), 1)
        self.assertNotIn("build", calls)
        checkpoint = session.build_resolve(checkpoint)
        self.assertEqual(calls.count("build"), 1)
        self.assertEqual(calls.count("resolve-dependencies"), 1)
        self.assertNotIn("test-generated", calls)
        checkpoint = session.generated_test(checkpoint)
        self.assertEqual(calls.count("test-generated"), 1)
        self.assertNotIn("verify-independent", calls)
        checkpoint = session.independent_accept(checkpoint)
        self.assertEqual(calls.count("verify-independent"), 1)
        self.assertNotIn("prepare", calls)
        checkpoint = session.prepare_commit(checkpoint)
        self.assertEqual(calls.count("prepare"), 1)
        self.assertNotIn("commit", calls)
        result = session.commit(checkpoint)
        self.assertEqual(calls.count("commit"), 1)
        self.assertEqual(result.checkpoint.phase, HostComponentLifecyclePhase.COMMIT)

    def test_resume_rejects_another_exact_input_identity(self):
        request, orchestrator, _calls, *_ = self.system()
        session = HostComponentLifecycleSession(orchestrator, request)
        checkpoint = session.prepare_generate()
        changed_build_request_declaration = replace(
            request.build_request_declaration,
            toolchain_digest=canonical_identity({"toolchain": "changed"}).uri,
        )
        changed_request = GenerationRequest(
            locked_authority=request.locked_authority,
            component_revision=request.component_revision,
            generation_context=request.generation_context,
            execution_plan=request.execution_plan,
            build_request_declaration=changed_build_request_declaration,
            workspace_reference=request.workspace_reference,
            generated_test_suite_policy=request.generated_test_suite_policy,
            managed_sbom_graph=request.managed_sbom_graph,
        )

        with self.assertRaises(GenerationFailure) as raised:
            HostComponentLifecycleSession(
                orchestrator, changed_request
            ).validate_classify(checkpoint)

        self.assertEqual(raised.exception.code, "generation.resume-identity-mismatch")

    def test_orchestrator_can_stop_after_every_complete_typed_step(self):
        request, _orchestrator, _calls, *_ = self.system()
        stop_points = ("generate", *request.execution_plan.lifecycle_steps)
        for stop_after in stop_points:
            with self.subTest(stop_after=stop_after):
                current_request, orchestrator, calls, *_ = self.system()
                paused = orchestrator.execute(current_request, stop_after=stop_after)

                self.assertEqual(paused.status, GenerationStatus.PAUSED)
                self.assertEqual(paused.events[-1].event_type, "run-paused")
                self.assertEqual(paused.events[-1].data["stop_after"], stop_after)
                if stop_after == "generate":
                    self.assertEqual(paused.lifecycle_executions, ())
                    self.assertIsNotNone(paused.realized_build_request)
                    self.assertEqual(
                        sum(
                            event.event_type == "build-request-realized"
                            for event in paused.events
                        ),
                        1,
                    )
                else:
                    self.assertEqual(
                        paused.lifecycle_executions[-1].step_id, stop_after
                    )
                if stop_after == "prepare-tree":
                    self.assertNotIn("commit", calls)

                before = list(calls)
                complete = orchestrator.execute(current_request, resume=paused)

                self.assertEqual(complete.status, GenerationStatus.COMPLETE)
                for completed_effect in set(before):
                    self.assertEqual(
                        calls.count(completed_effect), before.count(completed_effect)
                    )


if __name__ == "__main__":
    unittest.main()

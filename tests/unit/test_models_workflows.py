"""Model routing and arbitrary durable workflow tests."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict

from literate_ai.contracts import canonical_identity
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    RoutingError,
    StageModelPolicy,
)
from literate_ai.workflows import (
    StageDefinition,
    StageResult,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowError,
)


class MemoryEventStore:
    def __init__(self) -> None:
        self.records: dict[str, list[dict[str, object]]] = {}

    def append(self, stream_id: str, event: Mapping[str, object]) -> None:
        self.records.setdefault(stream_id, []).append(deepcopy(dict(event)))

    def stream(self, stream_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(deepcopy(self.records.get(stream_id, ())))


class SimulatedProcessTermination(BaseException):
    """Leave a durable stage-started tail as a terminated process would."""


class ModelRoutingTests(unittest.TestCase):
    def test_local_claim_requires_local_transport(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            ModelEndpoint(
                endpoint_id="bad",
                provider="test",
                model="model",
                base_url="https://models.example.test",
                locality=Locality.LOCAL,
                capabilities=("structured",),
                context_tokens=1000,
            )

    def test_fallback_is_ordered_and_request_scoped(self) -> None:
        first = ModelEndpoint(
            endpoint_id="first",
            provider="local",
            model="small",
            base_url="http://127.0.0.1:8000",
            locality=Locality.LOCAL,
            capabilities=("structured",),
            context_tokens=1000,
            available=False,
        )
        second = ModelEndpoint(
            endpoint_id="second",
            provider="local",
            model="large",
            base_url="http://localhost:8001",
            locality=Locality.LOCAL,
            capabilities=("structured", "tools"),
            context_tokens=10000,
        )
        router = ModelRouter(
            endpoints=(first, second),
            groups=(ModelGroup("group", "1.0.0", ("first", "second")),),
        )
        decision = router.select(
            StageModelPolicy(
                policy_id="policy",
                stage_type="plan",
                group_id="group",
                required_capabilities=("structured",),
                required_locality=Locality.LOCAL,
                data_egress=DataEgress.NONE,
            )
        )
        self.assertEqual(decision.selected_endpoint_id, "second")
        self.assertTrue(decision.fallback_used)

    def test_remote_endpoint_is_rejected_when_egress_is_none(self) -> None:
        endpoint = ModelEndpoint(
            endpoint_id="remote",
            provider="test",
            model="remote",
            base_url="https://models.example.test",
            locality=Locality.REMOTE,
            capabilities=("structured",),
            context_tokens=10000,
        )
        router = ModelRouter(
            endpoints=(endpoint,),
            groups=(ModelGroup("group", "1.0.0", ("remote",)),),
        )
        with self.assertRaises(RoutingError):
            router.select(
                StageModelPolicy(
                    policy_id="policy",
                    stage_type="generate",
                    group_id="group",
                    data_egress=DataEgress.NONE,
                )
            )


class WorkflowTests(unittest.TestCase):
    def test_workflow_hashes_and_stage_outputs_use_contract_canonical_identity(
        self,
    ) -> None:
        definition = WorkflowDefinition(
            "workflow:canonical",
            "1.0.0",
            (StageDefinition("only", "generic"),),
        )
        self.assertEqual(definition.digest, canonical_identity(asdict(definition)).uri)

        input_digest = canonical_identity({"input": "café"}).uri
        output = {"result": "naïve"}
        output_digest = canonical_identity(output).uri
        result = StageResult("only", 1, input_digest, output_digest, output)
        self.assertEqual(result.output_digest, output_digest)
        with self.assertRaisesRegex(ValueError, "floating-point"):
            StageResult(
                "only",
                1,
                input_digest,
                output_digest,
                {"result": 1.5},
            )

    def test_arbitrary_dag_executes_in_dependency_order_and_resumes(self) -> None:
        calls: list[str] = []

        def handler(data: object) -> dict[str, object]:
            dependencies = data["dependencies"]  # type: ignore[index]
            calls.append(str(len(dependencies)))
            return {"dependency_count": len(dependencies)}

        definition = WorkflowDefinition(
            workflow_id="workflow:test",
            version="1.0.0",
            stages=(
                StageDefinition("resolve", "generic"),
                StageDefinition("inspect", "generic", ("resolve",)),
                StageDefinition("report", "generic", ("inspect",)),
            ),
        )
        engine = WorkflowEngine(handlers={"generic": handler})
        run = engine.execute(definition, inputs={"component": "test"})
        self.assertEqual(calls, ["0", "1", "1"])
        calls.clear()
        same = engine.execute(definition, inputs={"component": "test"}, run=run)
        self.assertIs(same, run)
        self.assertEqual(calls, [])

    def test_resume_rejects_different_inputs(self) -> None:
        definition = WorkflowDefinition(
            "workflow:test", "1.0.0", (StageDefinition("only", "generic"),)
        )
        engine = WorkflowEngine(handlers={"generic": lambda _: {"ok": True}})
        run = engine.execute(definition, inputs={"value": 1})
        with self.assertRaisesRegex(WorkflowError, "different immutable inputs"):
            engine.execute(definition, inputs={"value": 2}, run=run)

    def test_restart_rehydrates_results_and_only_retries_incomplete_work(self) -> None:
        definition = WorkflowDefinition(
            "workflow:restart",
            "1.0.0",
            (
                StageDefinition("resolve", "resolve"),
                StageDefinition("inspect", "inspect", ("resolve",)),
                StageDefinition("report", "report", ("inspect",), 2),
            ),
        )
        store = MemoryEventStore()
        calls: list[str] = []

        def resolve(_: Mapping[str, object]) -> Mapping[str, object]:
            calls.append("resolve")
            return {"component": "example"}

        def inspect(_: Mapping[str, object]) -> Mapping[str, object]:
            calls.append("inspect")
            return {"findings": 2}

        def fail_report(_: Mapping[str, object]) -> Mapping[str, object]:
            calls.append("report")
            raise SimulatedProcessTermination("interrupted")

        first_engine = WorkflowEngine(
            handlers={
                "resolve": resolve,
                "inspect": inspect,
                "report": fail_report,
            },
            event_sink=store,
        )
        with self.assertRaises(SimulatedProcessTermination):
            first_engine.execute(definition, inputs={"component": "test"})

        def report(_: Mapping[str, object]) -> Mapping[str, object]:
            calls.append("report")
            return {"summary": "2 findings"}

        restarted_engine = WorkflowEngine(
            handlers={"resolve": resolve, "inspect": inspect, "report": report},
            event_sink=store,
        )
        completed = restarted_engine.execute(definition, inputs={"component": "test"})

        self.assertEqual(completed.status.value, "complete")
        self.assertEqual(calls, ["resolve", "inspect", "report", "report"])
        self.assertEqual(set(completed.stage_results), {"resolve", "inspect", "report"})
        self.assertEqual(completed.stage_results["report"].attempt, 2)
        self.assertEqual(
            [
                event.stage_id
                for event in completed.events
                if event.event_type == "stage-reused"
            ],
            ["resolve", "inspect"],
        )

        event_count = len(store.records[completed.run_id])
        after_second_restart = WorkflowEngine(handlers={}, event_sink=store).execute(
            definition, inputs={"component": "test"}
        )
        self.assertEqual(after_second_restart.status.value, "complete")
        self.assertEqual(len(store.records[completed.run_id]), event_count)

    def test_interrupted_event_tail_retries_without_repeating_completed_stage(
        self,
    ) -> None:
        definition = WorkflowDefinition(
            "workflow:interrupted-tail",
            "1.0.0",
            (
                StageDefinition("first", "first"),
                StageDefinition("second", "second", ("first",), 2),
            ),
        )
        store = MemoryEventStore()
        WorkflowEngine(
            handlers={
                "first": lambda _: {"first": True},
                "second": lambda _: {"second": True},
            },
            event_sink=store,
        ).execute(definition, inputs={"value": 1})
        run_id = next(iter(store.records))
        stage_started = next(
            index
            for index, event in enumerate(store.records[run_id])
            if event["event_type"] == "stage-started" and event["stage_id"] == "second"
        )
        del store.records[run_id][stage_started + 1 :]
        calls: list[str] = []

        result = WorkflowEngine(
            handlers={
                "first": lambda _: calls.append("first") or {"first": True},
                "second": lambda _: calls.append("second") or {"second": True},
            },
            event_sink=store,
        ).execute(definition, inputs={"value": 1})

        self.assertEqual(result.status.value, "complete")
        self.assertEqual(calls, ["second"])
        self.assertEqual(result.stage_results["second"].attempt, 2)

    def test_attempt_budget_survives_multiple_process_restarts(self) -> None:
        definition = WorkflowDefinition(
            "workflow:durable-attempts",
            "1.0.0",
            (StageDefinition("only", "generic", maximum_attempts=3),),
        )
        store = MemoryEventStore()

        def terminate(_: Mapping[str, object]) -> Mapping[str, object]:
            raise SimulatedProcessTermination("process terminated")

        for expected_attempt in (1, 2):
            with self.assertRaises(SimulatedProcessTermination):
                WorkflowEngine(
                    handlers={"generic": terminate}, event_sink=store
                ).execute(definition, inputs={"input": 1})
            starts = [
                event
                for event in next(iter(store.records.values()))
                if event["event_type"] == "stage-started"
            ]
            self.assertEqual(
                [event["payload"]["attempt"] for event in starts],  # type: ignore[index]
                list(range(1, expected_attempt + 1)),
            )

        completed = WorkflowEngine(
            handlers={"generic": lambda _: {"attempt": "third"}}, event_sink=store
        ).execute(definition, inputs={"input": 1})

        self.assertEqual(completed.stage_results["only"].attempt, 3)
        self.assertEqual(
            [
                event.payload["attempt"]
                for event in completed.events
                if event.event_type == "stage-started"
            ],
            [1, 2, 3],
        )

    def test_exhausted_attempt_budget_cannot_be_reset_by_restart(self) -> None:
        definition = WorkflowDefinition(
            "workflow:exhausted-attempts",
            "1.0.0",
            (StageDefinition("only", "generic", maximum_attempts=2),),
        )
        store = MemoryEventStore()

        def terminate(_: Mapping[str, object]) -> Mapping[str, object]:
            raise SimulatedProcessTermination("process terminated")

        for _ in range(2):
            with self.assertRaises(SimulatedProcessTermination):
                WorkflowEngine(
                    handlers={"generic": terminate}, event_sink=store
                ).execute(definition, inputs={"input": 1})

        run_id = next(iter(store.records))
        event_count = len(store.records[run_id])
        with self.assertRaises(WorkflowError) as exhausted:
            WorkflowEngine(
                handlers={"generic": lambda _: {"must": "not run"}},
                event_sink=store,
            ).execute(definition, inputs={"input": 1})
        self.assertEqual(exhausted.exception.code, "workflow.retry_budget_exhausted")
        self.assertEqual(len(store.records[run_id]), event_count)
        self.assertEqual(
            WorkflowEngine(handlers={}, event_sink=store)
            .rehydrate(definition, inputs={"input": 1})
            .status.value,
            "failed",
        )

        forged_restart = deepcopy(store.records[run_id][0])
        forged_restart["sequence"] = event_count + 1
        forged_restart["event_id"] = f"{run_id}:{event_count + 1}"
        store.records[run_id].append(forged_restart)
        with self.assertRaises(WorkflowError) as forged:
            WorkflowEngine(handlers={}, event_sink=store).rehydrate(
                definition, inputs={"input": 1}
            )
        self.assertEqual(forged.exception.code, "workflow.event_history_invalid")

    def test_rehydration_rejects_attempt_resets_gaps_and_overflow(self) -> None:
        definition = WorkflowDefinition(
            "workflow:attempt-history",
            "1.0.0",
            (StageDefinition("only", "generic", maximum_attempts=3),),
        )
        original = MemoryEventStore()

        def terminate(_: Mapping[str, object]) -> Mapping[str, object]:
            raise SimulatedProcessTermination("process terminated")

        for _ in range(2):
            with self.assertRaises(SimulatedProcessTermination):
                WorkflowEngine(
                    handlers={"generic": terminate}, event_sink=original
                ).execute(definition, inputs={"input": 1})

        run_id = next(iter(original.records))
        second_start = [
            index
            for index, event in enumerate(original.records[run_id])
            if event["event_type"] == "stage-started"
        ][1]
        variants: dict[str, int] = {
            "reset": 1,
            "gap": 3,
            "overflow": 4,
        }
        for name, forged_attempt in variants.items():
            with self.subTest(name=name):
                events = deepcopy(original.records[run_id])
                payload = events[second_start]["payload"]
                assert isinstance(payload, dict)
                payload["attempt"] = forged_attempt
                store = MemoryEventStore()
                store.records[run_id] = events
                with self.assertRaises(WorkflowError) as malformed:
                    WorkflowEngine(handlers={}, event_sink=store).rehydrate(
                        definition, inputs={"input": 1}
                    )
                self.assertEqual(
                    malformed.exception.code, "workflow.event_history_invalid"
                )

    def test_rehydration_rejects_corrupt_event_streams(self) -> None:
        definition = WorkflowDefinition(
            "workflow:corruption",
            "1.0.0",
            (StageDefinition("only", "generic"),),
        )
        original = MemoryEventStore()
        run = WorkflowEngine(
            handlers={"generic": lambda _: {"value": 1}}, event_sink=original
        ).execute(definition, inputs={"input": 1})
        valid = original.records[run.run_id]

        variants: dict[str, tuple[list[dict[str, object]], str]] = {}

        truncated = deepcopy(valid)
        truncated[0].pop("payload")
        variants["truncated record"] = (truncated, "workflow.event_invalid")

        missing_prefix = deepcopy(valid[1:])
        variants["truncated prefix"] = (
            missing_prefix,
            "workflow.event_sequence_invalid",
        )

        out_of_order = deepcopy(valid)
        out_of_order[0], out_of_order[1] = out_of_order[1], out_of_order[0]
        variants["out of order"] = (
            out_of_order,
            "workflow.event_sequence_invalid",
        )

        duplicate = deepcopy(valid)
        duplicate.insert(2, deepcopy(duplicate[1]))
        variants["duplicate"] = (duplicate, "workflow.event_sequence_invalid")

        tampered = deepcopy(valid)
        completed = next(
            event for event in tampered if event["event_type"] == "stage-completed"
        )
        completed_payload = completed["payload"]
        assert isinstance(completed_payload, dict)
        completed_payload["output"] = {"value": 999}
        variants["tampered output"] = (
            tampered,
            "workflow.event_history_invalid",
        )

        for name, (events, expected_code) in variants.items():
            with self.subTest(name=name):
                store = MemoryEventStore()
                store.records[run.run_id] = events
                with self.assertRaises(WorkflowError) as raised:
                    WorkflowEngine(handlers={}, event_sink=store).rehydrate(
                        definition,
                        inputs={"input": 1},
                        run_id=run.run_id,
                    )
                self.assertEqual(raised.exception.code, expected_code)

        ordered_definition = WorkflowDefinition(
            "workflow:semantic-order",
            "1.0.0",
            (
                StageDefinition("alpha", "generic"),
                StageDefinition("beta", "generic"),
            ),
        )
        ordered_store = MemoryEventStore()
        ordered_run = WorkflowEngine(
            handlers={"generic": lambda _: {"ok": True}},
            event_sink=ordered_store,
        ).execute(ordered_definition, inputs={"input": 1})
        ordered_events = ordered_store.records[ordered_run.run_id]
        run_started = ordered_events[0]
        run_completed = ordered_events[-1]
        alpha = [event for event in ordered_events if event["stage_id"] == "alpha"]
        beta = [event for event in ordered_events if event["stage_id"] == "beta"]
        semantic_reordering = deepcopy([run_started, *beta, *alpha, run_completed])
        for sequence, event in enumerate(semantic_reordering, start=1):
            event["sequence"] = sequence
            event["event_id"] = f"{ordered_run.run_id}:{sequence}"
        ordered_store.records[ordered_run.run_id] = semantic_reordering

        with self.assertRaises(WorkflowError) as reordered:
            WorkflowEngine(handlers={}, event_sink=ordered_store).rehydrate(
                ordered_definition,
                inputs={"input": 1},
                run_id=ordered_run.run_id,
            )
        self.assertEqual(reordered.exception.code, "workflow.event_history_invalid")

    def test_rehydration_binds_immutable_input_and_workflow_identities(self) -> None:
        definition = WorkflowDefinition(
            "workflow:identity",
            "1.0.0",
            (StageDefinition("only", "generic"),),
        )
        store = MemoryEventStore()
        run = WorkflowEngine(
            handlers={"generic": lambda _: {"value": 1}}, event_sink=store
        ).execute(definition, inputs={"input": 1})
        engine = WorkflowEngine(handlers={}, event_sink=store)

        with self.assertRaises(WorkflowError) as changed_input:
            engine.rehydrate(
                definition,
                inputs={"input": 2},
                run_id=run.run_id,
            )
        self.assertEqual(
            changed_input.exception.code, "workflow.resume_identity_mismatch"
        )

        changed_definition = WorkflowDefinition(
            "workflow:identity",
            "1.0.1",
            definition.stages,
        )
        with self.assertRaises(WorkflowError) as changed_workflow:
            engine.rehydrate(
                changed_definition,
                inputs={"input": 1},
                run_id=run.run_id,
            )
        self.assertEqual(
            changed_workflow.exception.code, "workflow.resume_identity_mismatch"
        )


if __name__ == "__main__":
    unittest.main()

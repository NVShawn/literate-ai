"""Model routing and arbitrary durable workflow tests."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from copy import deepcopy

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
    WorkflowDefinition,
    WorkflowEngine,
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


if __name__ == "__main__":
    unittest.main()

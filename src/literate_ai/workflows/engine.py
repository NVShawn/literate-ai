"""A small resumable DAG engine whose durable state is an append-only event stream."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Protocol

from literate_ai.contracts import (
    ContentIdentity,
    VersionedContentRef,
    canonical_identity,
    semantic_version,
)
from literate_ai.diagnostics import log_operation
from literate_ai.ports import EventStore


def _digest(value: object) -> str:
    return canonical_identity(value).uri


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StageDefinition:
    stage_id: str
    stage_type: str
    dependencies: tuple[str, ...] = ()
    maximum_attempts: int = 1
    model_policy_id: str | None = None

    def __post_init__(self) -> None:
        if not self.stage_id or not self.stage_type:
            raise ValueError("workflow stage identity cannot be empty")
        if self.maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        if self.stage_id in self.dependencies:
            raise ValueError("workflow stage cannot depend on itself")
        if len(set(self.dependencies)) != len(self.dependencies) or any(
            not item for item in self.dependencies
        ):
            raise ValueError("workflow stage dependencies must be unique non-empty IDs")


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    workflow_id: str
    version: str
    stages: tuple[StageDefinition, ...]

    def __post_init__(self) -> None:
        if not self.workflow_id or not self.stages:
            raise ValueError("workflow requires identity, version, and stages")
        semantic_version(self.version, "WorkflowDefinition.version")
        by_id = {stage.stage_id: stage for stage in self.stages}
        if len(by_id) != len(self.stages):
            raise ValueError("workflow stage IDs must be unique")
        for stage in self.stages:
            missing = set(stage.dependencies) - by_id.keys()
            if missing:
                raise ValueError(f"workflow stage has missing dependencies: {missing}")
        _topological_order(self.stages)

    @property
    def digest(self) -> str:
        return _digest(asdict(self))

    @property
    def ref(self) -> VersionedContentRef:
        return VersionedContentRef(
            "workflow",
            self.workflow_id,
            self.version,
            ContentIdentity.parse_uri(self.digest),
        )

    @property
    def order(self) -> tuple[str, ...]:
        return _topological_order(self.stages)


def _topological_order(stages: tuple[StageDefinition, ...]) -> tuple[str, ...]:
    dependencies = {stage.stage_id: set(stage.dependencies) for stage in stages}
    order: list[str] = []
    while dependencies:
        ready = sorted(
            stage_id for stage_id, required in dependencies.items() if not required
        )
        if not ready:
            raise ValueError("workflow contains a dependency cycle")
        order.extend(ready)
        for stage_id in ready:
            dependencies.pop(stage_id)
        for required in dependencies.values():
            required.difference_update(ready)
    return tuple(order)


@dataclass(frozen=True, slots=True)
class StageResult:
    stage_id: str
    attempt: int
    input_digest: str
    output_digest: str
    output: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.stage_id or self.attempt < 1:
            raise ValueError(
                "workflow stage result requires an ID and positive attempt"
            )
        ContentIdentity.parse_uri(self.input_digest)
        ContentIdentity.parse_uri(self.output_digest)
        _digest(dict(self.output))


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    stage_id: str | None
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.run_id or not self.event_type or self.sequence < 1:
            raise ValueError("workflow event requires run, type, and positive sequence")
        if self.event_id != f"{self.run_id}:{self.sequence}":
            raise ValueError("workflow event ID does not match its run and sequence")
        if self.stage_id is not None and not self.stage_id:
            raise ValueError("workflow event stage ID cannot be empty")
        if not isinstance(self.payload, Mapping):
            raise ValueError("workflow event payload must be an object")
        canonical_identity(dict(self.payload))


class EventSink(Protocol):
    def append(self, stream_id: str, event: Mapping[str, object]) -> None: ...


StageHandler = Callable[[Mapping[str, object]], Mapping[str, object]]


@dataclass(slots=True)
class WorkflowRun:
    run_id: str
    input_digest: str
    workflow_digest: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    stage_results: dict[str, StageResult] = field(default_factory=dict)
    events: list[WorkflowEvent] = field(default_factory=list)

    def event(self, event_type: str, stage_id: str | None, **payload: object) -> None:
        sequence = len(self.events) + 1
        self.events.append(
            WorkflowEvent(
                event_id=f"{self.run_id}:{sequence}",
                run_id=self.run_id,
                sequence=sequence,
                event_type=event_type,
                stage_id=stage_id,
                payload=payload,
            )
        )


def _run_id(workflow_digest: str, input_digest: str) -> str:
    return f"run:{_digest([workflow_digest, input_digest])[7:31]}"


def _stage_input(
    inputs: Mapping[str, object],
    stage: StageDefinition,
    stage_results: Mapping[str, StageResult],
) -> dict[str, object]:
    try:
        dependencies = {
            dependency: stage_results[dependency].output
            for dependency in stage.dependencies
        }
    except KeyError as exc:
        raise WorkflowError(
            "workflow.event_history_invalid",
            f"Stage {stage.stage_id!r} ran before its dependencies completed",
        ) from exc
    return {"run_input": inputs, "dependencies": dependencies}


def _event_from_mapping(
    value: Mapping[str, object], *, run_id: str, expected_sequence: int
) -> WorkflowEvent:
    required = {
        "event_id",
        "run_id",
        "sequence",
        "event_type",
        "stage_id",
        "payload",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {expected_sequence} is incomplete or has unknown fields",
        )
    sequence = value["sequence"]
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence != expected_sequence
    ):
        raise WorkflowError(
            "workflow.event_sequence_invalid",
            f"Expected event sequence {expected_sequence}, received {sequence!r}",
        )
    if value["run_id"] != run_id:
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {expected_sequence} belongs to another run",
        )
    event_id = value["event_id"]
    event_type = value["event_type"]
    stage_id = value["stage_id"]
    payload = value["payload"]
    if (
        not isinstance(event_id, str)
        or not isinstance(event_type, str)
        or not isinstance(stage_id, (str, type(None)))
        or not isinstance(payload, Mapping)
    ):
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {expected_sequence} has invalid field types",
        )
    try:
        return WorkflowEvent(
            event_id,
            run_id,
            sequence,
            event_type,
            stage_id,
            dict(payload),
        )
    except (TypeError, ValueError) as exc:
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {expected_sequence} is invalid: {exc}",
        ) from exc


def _payload(event: WorkflowEvent, required: set[str]) -> Mapping[str, object]:
    if set(event.payload) != required:
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {event.sequence} payload is incomplete or has unknown fields",
        )
    return event.payload


def _positive_attempt(event: WorkflowEvent, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {event.sequence} has an invalid stage attempt",
        )
    return value


def _attempt_history(
    events: Iterable[WorkflowEvent],
    stages: Mapping[str, StageDefinition],
) -> dict[str, int]:
    """Project cumulative attempt counters from their durable start events."""

    attempts: dict[str, int] = {}
    for event in events:
        if event.event_type != "stage-started":
            continue
        stage_id = event.stage_id
        if stage_id is None or stage_id not in stages:
            raise WorkflowError(
                "workflow.event_history_invalid",
                f"Event {event.sequence} identifies an unknown workflow stage",
            )
        if "attempt" not in event.payload:
            raise WorkflowError(
                "workflow.event_invalid",
                f"Event {event.sequence} has no stage attempt",
            )
        attempt = _positive_attempt(event, event.payload["attempt"])
        expected = attempts.get(stage_id, 0) + 1
        if attempt != expected or attempt > stages[stage_id].maximum_attempts:
            raise WorkflowError(
                "workflow.event_history_invalid",
                f"Stage {stage_id!r} attempt order is invalid",
            )
        attempts[stage_id] = attempt
    return attempts


def _exhausted_incomplete_stage(
    stage_order: Iterable[str],
    stages: Mapping[str, StageDefinition],
    stage_results: Mapping[str, StageResult],
    attempts: Mapping[str, int],
) -> str | None:
    for stage_id in stage_order:
        if (
            stage_id not in stage_results
            and attempts.get(stage_id, 0) >= stages[stage_id].maximum_attempts
        ):
            return stage_id
    return None


def _identity_value(event: WorkflowEvent, name: str, value: object) -> str:
    if not isinstance(value, str):
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {event.sequence} has a non-string {name}",
        )
    try:
        ContentIdentity.parse_uri(value)
    except ValueError as exc:
        raise WorkflowError(
            "workflow.event_invalid",
            f"Event {event.sequence} has an invalid {name}",
        ) from exc
    return value


def _rehydrate_run(
    definition: WorkflowDefinition,
    inputs: Mapping[str, object],
    run_id: str,
    stored_events: Iterable[Mapping[str, object]],
) -> WorkflowRun:
    input_digest = _digest(inputs)
    workflow_digest = definition.digest
    by_id = {stage.stage_id: stage for stage in definition.stages}
    stage_results: dict[str, StageResult] = {}
    events: list[WorkflowEvent] = []
    status = WorkflowStatus.PENDING
    stage_order = definition.order
    session_stage_index = 0
    active_stage: tuple[str, int, str] | None = None
    attempts: dict[str, int] = {}
    saw_start = False

    for expected_sequence, raw_event in enumerate(stored_events, start=1):
        event = _event_from_mapping(
            raw_event, run_id=run_id, expected_sequence=expected_sequence
        )
        events.append(event)

        if event.event_type == "run-started":
            if event.stage_id is not None:
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    "A run-started event cannot identify a stage",
                )
            values = _payload(event, {"input_digest", "workflow_digest"})
            recorded_input = _identity_value(
                event, "input digest", values["input_digest"]
            )
            recorded_workflow = _identity_value(
                event, "workflow digest", values["workflow_digest"]
            )
            if recorded_input != input_digest or recorded_workflow != workflow_digest:
                raise WorkflowError(
                    "workflow.resume_identity_mismatch",
                    "Stored run uses different immutable inputs or workflow definition",
                )
            if saw_start and status is WorkflowStatus.COMPLETE:
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    "A completed workflow run cannot be restarted",
                )
            exhausted_stage = _exhausted_incomplete_stage(
                stage_order, by_id, stage_results, attempts
            )
            if saw_start and exhausted_stage is not None:
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {exhausted_stage!r} restarted after exhausting its "
                    "durable attempt budget",
                )
            active_stage = None
            session_stage_index = 0
            status = WorkflowStatus.RUNNING
            saw_start = True
            continue

        if not saw_start:
            raise WorkflowError(
                "workflow.event_history_invalid",
                "Workflow history does not begin with run-started",
            )
        if status in (WorkflowStatus.COMPLETE, WorkflowStatus.FAILED):
            raise WorkflowError(
                "workflow.event_history_invalid",
                "A terminal run must restart before recording more events",
            )

        if event.event_type == "run-completed":
            if event.stage_id is not None or _payload(event, set()):
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    "A run-completed event cannot identify a stage or carry payload",
                )
            if (
                active_stage is not None
                or session_stage_index != len(stage_order)
                or set(stage_results) != set(by_id)
            ):
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    "A run completed before every stage produced a durable result",
                )
            status = WorkflowStatus.COMPLETE
            continue

        if event.stage_id is None or event.stage_id not in by_id:
            raise WorkflowError(
                "workflow.event_history_invalid",
                f"Event {event.sequence} identifies an unknown workflow stage",
            )
        if (
            session_stage_index >= len(stage_order)
            or event.stage_id != stage_order[session_stage_index]
        ):
            raise WorkflowError(
                "workflow.event_history_invalid",
                f"Stage {event.stage_id!r} is out of workflow order",
            )
        stage = by_id[event.stage_id]

        if event.event_type == "stage-started":
            values = _payload(event, {"attempt", "input_digest"})
            attempt = _positive_attempt(event, values["attempt"])
            recorded_input = _identity_value(
                event, "stage input digest", values["input_digest"]
            )
            expected_input = _digest(_stage_input(inputs, stage, stage_results))
            if recorded_input != expected_input:
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} input identity was tampered",
                )
            if active_stage is not None or stage.stage_id in stage_results:
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} started in an invalid state",
                )
            expected_attempt = attempts.get(stage.stage_id, 0) + 1
            if attempt != expected_attempt or attempt > stage.maximum_attempts:
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} attempt order is invalid",
                )
            attempts[stage.stage_id] = attempt
            active_stage = (stage.stage_id, attempt, recorded_input)
            continue

        if event.event_type == "stage-attempt-failed":
            values = _payload(event, {"attempt", "error_type", "message"})
            attempt = _positive_attempt(event, values["attempt"])
            if not isinstance(values["error_type"], str) or not isinstance(
                values["message"], str
            ):
                raise WorkflowError(
                    "workflow.event_invalid",
                    f"Event {event.sequence} has invalid failure diagnostics",
                )
            if active_stage is None or active_stage[:2] != (stage.stage_id, attempt):
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} failed without a matching start",
                )
            active_stage = None
            status = (
                WorkflowStatus.FAILED
                if attempt == stage.maximum_attempts
                else WorkflowStatus.RUNNING
            )
            continue

        if event.event_type == "stage-completed":
            values = _payload(
                event,
                {"attempt", "input_digest", "output_digest", "output"},
            )
            attempt = _positive_attempt(event, values["attempt"])
            recorded_input = _identity_value(
                event, "stage input digest", values["input_digest"]
            )
            recorded_output = _identity_value(
                event, "stage output digest", values["output_digest"]
            )
            output_value = values["output"]
            if not isinstance(output_value, Mapping):
                raise WorkflowError(
                    "workflow.event_invalid",
                    f"Event {event.sequence} stage output is not an object",
                )
            output = dict(output_value)
            if recorded_output != _digest(output):
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} output identity was tampered",
                )
            if active_stage != (stage.stage_id, attempt, recorded_input):
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} completed without a matching start",
                )
            try:
                stage_results[stage.stage_id] = StageResult(
                    stage.stage_id,
                    attempt,
                    recorded_input,
                    recorded_output,
                    output,
                )
            except ValueError as exc:
                raise WorkflowError(
                    "workflow.event_invalid",
                    f"Event {event.sequence} has an invalid stage result",
                ) from exc
            active_stage = None
            session_stage_index += 1
            status = WorkflowStatus.RUNNING
            continue

        if event.event_type == "stage-reused":
            values = _payload(event, {"attempt", "input_digest", "output_digest"})
            attempt = _positive_attempt(event, values["attempt"])
            recorded_input = _identity_value(
                event, "stage input digest", values["input_digest"]
            )
            recorded_output = _identity_value(
                event, "stage output digest", values["output_digest"]
            )
            previous = stage_results.get(stage.stage_id)
            expected_input = _digest(_stage_input(inputs, stage, stage_results))
            if (
                active_stage is not None
                or previous is None
                or previous.attempt != attempt
                or previous.input_digest != recorded_input
                or previous.output_digest != recorded_output
                or recorded_input != expected_input
            ):
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} reused an unbound result",
                )
            session_stage_index += 1
            continue

        if event.event_type == "stage-failed":
            values = _payload(event, {"code"})
            if values["code"] != "handler-missing" or active_stage is not None:
                raise WorkflowError(
                    "workflow.event_history_invalid",
                    f"Stage {stage.stage_id!r} has an invalid terminal failure",
                )
            status = WorkflowStatus.FAILED
            continue

        raise WorkflowError(
            "workflow.event_history_invalid",
            f"Unsupported workflow event type {event.event_type!r}",
        )

    if not events:
        raise WorkflowError("workflow.run_not_found", f"No events exist for {run_id}")
    exhausted_stage = _exhausted_incomplete_stage(
        stage_order, by_id, stage_results, attempts
    )
    if active_stage is not None and exhausted_stage is not None:
        status = WorkflowStatus.FAILED
    return WorkflowRun(
        run_id,
        input_digest,
        workflow_digest,
        status,
        stage_results,
        events,
    )


class WorkflowEngine:
    def __init__(
        self,
        *,
        handlers: Mapping[str, StageHandler],
        event_sink: EventSink | EventStore | None = None,
    ) -> None:
        self.handlers = dict(handlers)
        self.event_sink = event_sink

    def rehydrate(
        self,
        definition: WorkflowDefinition,
        *,
        inputs: Mapping[str, object],
        run_id: str | None = None,
    ) -> WorkflowRun:
        """Rebuild one run solely from its validated append-only event history."""

        input_digest = _digest(inputs)
        selected_run_id = run_id or _run_id(definition.digest, input_digest)
        stored = self._stored_events(selected_run_id)
        if stored is None:
            raise WorkflowError(
                "workflow.event_source_unavailable",
                "The configured event sink does not support reading event streams",
            )
        return _rehydrate_run(definition, inputs, selected_run_id, stored)

    def execute(
        self,
        definition: WorkflowDefinition,
        *,
        inputs: Mapping[str, object],
        run: WorkflowRun | None = None,
    ) -> WorkflowRun:
        input_digest = _digest(inputs)
        workflow_digest = definition.digest
        if run is None:
            run_id = _run_id(workflow_digest, input_digest)
            stored = self._stored_events(run_id)
            run = (
                _rehydrate_run(definition, inputs, run_id, stored)
                if stored
                else WorkflowRun(
                    run_id=run_id,
                    input_digest=input_digest,
                    workflow_digest=workflow_digest,
                )
            )
        elif run.input_digest != input_digest or run.workflow_digest != workflow_digest:
            raise WorkflowError(
                "workflow.resume_identity_mismatch",
                "Cannot resume a run with different immutable inputs or workflow "
                "definition",
            )
        if run.status is WorkflowStatus.COMPLETE:
            return run
        by_id = {stage.stage_id: stage for stage in definition.stages}
        attempts = _attempt_history(run.events, by_id)
        exhausted_stage = _exhausted_incomplete_stage(
            definition.order, by_id, run.stage_results, attempts
        )
        if exhausted_stage is not None:
            run.status = WorkflowStatus.FAILED
            raise WorkflowError(
                "workflow.retry_budget_exhausted",
                f"Stage {exhausted_stage!r} exhausted its durable attempt budget",
            )
        run.status = WorkflowStatus.RUNNING
        self._record(
            run,
            "run-started",
            None,
            input_digest=input_digest,
            workflow_digest=workflow_digest,
        )
        for stage_id in definition.order:
            stage = by_id[stage_id]
            previous = run.stage_results.get(stage_id)
            stage_input = _stage_input(inputs, stage, run.stage_results)
            stage_input_digest = _digest(stage_input)
            if previous and previous.input_digest == stage_input_digest:
                self._record(
                    run,
                    "stage-reused",
                    stage_id,
                    attempt=previous.attempt,
                    input_digest=previous.input_digest,
                    output_digest=previous.output_digest,
                )
                continue
            handler = self.handlers.get(stage.stage_type)
            if handler is None:
                run.status = WorkflowStatus.FAILED
                self._record(run, "stage-failed", stage_id, code="handler-missing")
                raise WorkflowError(
                    "workflow.handler_missing", f"No handler for {stage.stage_type!r}"
                )
            next_attempt = attempts.get(stage_id, 0) + 1
            for attempt in range(next_attempt, stage.maximum_attempts + 1):
                attempts[stage_id] = attempt
                self._record(
                    run,
                    "stage-started",
                    stage_id,
                    attempt=attempt,
                    input_digest=stage_input_digest,
                )
                try:
                    with log_operation(
                        "workflow_stage",
                        stage_id=stage_id,
                        workflow_id=definition.workflow_id,
                        attempt=attempt,
                    ):
                        output = dict(handler(stage_input))
                except Exception as exc:
                    self._record(
                        run,
                        "stage-attempt-failed",
                        stage_id,
                        attempt=attempt,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                    if attempt == stage.maximum_attempts:
                        run.status = WorkflowStatus.FAILED
                        raise WorkflowError(
                            "workflow.stage_failed", f"Stage {stage_id!r} failed"
                        ) from exc
                    continue
                result = StageResult(
                    stage_id=stage_id,
                    attempt=attempt,
                    input_digest=stage_input_digest,
                    output_digest=_digest(output),
                    output=output,
                )
                run.stage_results[stage_id] = result
                self._record(
                    run,
                    "stage-completed",
                    stage_id,
                    attempt=attempt,
                    input_digest=result.input_digest,
                    output_digest=result.output_digest,
                    output=dict(result.output),
                )
                break
        run.status = WorkflowStatus.COMPLETE
        self._record(run, "run-completed", None)
        return run

    def _record(
        self,
        run: WorkflowRun,
        event_type: str,
        stage_id: str | None,
        **payload: object,
    ) -> None:
        run.event(event_type, stage_id, **payload)
        if self.event_sink is not None:
            event = run.events[-1]
            self.event_sink.append(event.run_id, asdict(event))

    def _stored_events(self, run_id: str) -> tuple[Mapping[str, object], ...] | None:
        if self.event_sink is None:
            return None
        stream = getattr(self.event_sink, "stream", None)
        if not callable(stream):
            return None
        stored = stream(run_id)
        return tuple(stored)

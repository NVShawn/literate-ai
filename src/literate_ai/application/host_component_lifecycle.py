"""Resumable host Component lifecycle over authenticated generation checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from literate_ai.contracts import ContentIdentity, canonical_identity

from .models import GenerationRequest, GenerationRun, GenerationStatus


class HostComponentLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HostComponentLifecyclePhase(StrEnum):
    PREPARE_GENERATE = "prepare-generate"
    VALIDATE_CLASSIFY = "validate-classify"
    AUTHORIZE = "authorize"
    BUILD_RESOLVE = "build-resolve"
    GENERATED_TEST = "generated-test"
    INDEPENDENT_ACCEPT = "independent-accept"
    PREPARE_COMMIT = "prepare-commit"
    COMMIT = "commit"

    VALIDATE_INDEX = VALIDATE_CLASSIFY
    EXECUTE = INDEPENDENT_ACCEPT


HOST_COMPONENT_LIFECYCLE_PHASES = tuple(HostComponentLifecyclePhase)

_STOP_AFTER = {
    HostComponentLifecyclePhase.PREPARE_GENERATE: "generate",
    HostComponentLifecyclePhase.VALIDATE_CLASSIFY: "classify",
    HostComponentLifecyclePhase.AUTHORIZE: "authorize-build",
    HostComponentLifecyclePhase.BUILD_RESOLVE: "resolve-dependencies",
    HostComponentLifecyclePhase.GENERATED_TEST: "test-generated",
    HostComponentLifecyclePhase.INDEPENDENT_ACCEPT: "verify-independent",
    HostComponentLifecyclePhase.PREPARE_COMMIT: "prepare-tree",
    HostComponentLifecyclePhase.COMMIT: None,
}


class ResumableGenerationLifecycle(Protocol):
    def execute(
        self,
        request: GenerationRequest,
        *,
        resume: GenerationRun | None = None,
        stop_after: str | None = None,
    ) -> GenerationRun: ...


@dataclass(frozen=True, slots=True)
class HostComponentLifecycleCheckpoint:
    phase: HostComponentLifecyclePhase
    run: GenerationRun

    def __post_init__(self) -> None:
        if not isinstance(self.phase, HostComponentLifecyclePhase):
            raise TypeError("host lifecycle checkpoint phase must be typed")
        expected_status = (
            GenerationStatus.COMPLETE
            if self.phase is HostComponentLifecyclePhase.COMMIT
            else GenerationStatus.PAUSED
        )
        if self.run.status is not expected_status:
            raise HostComponentLifecycleError(
                "host_lifecycle.checkpoint_status_mismatch",
                "host lifecycle checkpoint has an impossible generation status",
            )
        if self.run.realized_build_request is None:
            raise HostComponentLifecycleError(
                "host_lifecycle.build_request_missing",
                "host lifecycle checkpoint requires realized build authority",
            )
        if self.phase is not HostComponentLifecyclePhase.COMMIT:
            if (
                not self.run.events
                or self.run.events[-1].event_type != "run-paused"
                or self.run.events[-1].data.get("stop_after") != _STOP_AFTER[self.phase]
            ):
                raise HostComponentLifecycleError(
                    "host_lifecycle.checkpoint_phase_mismatch",
                    "host lifecycle checkpoint does not follow its exact stop point",
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/host-component-lifecycle-checkpoint@3",
                "phase": self.phase.value,
                "run_id": self.run.run_id,
                "run_input_identity": self.run.input_identity.uri,
                "realized_build_request_identity": canonical_identity(
                    self.run.realized_build_request.to_dict()
                ).uri,
                "stage_output_identities": [
                    item.output_identity.uri for item in self.run.stage_executions
                ],
                "lifecycle_output_identities": [
                    item.output_identity.uri for item in self.run.lifecycle_executions
                ],
                "terminal_event": self.run.events[-1].to_dict(),
            }
        )


@dataclass(frozen=True, slots=True)
class HostComponentLifecycleResult:
    checkpoint: HostComponentLifecycleCheckpoint

    def __post_init__(self) -> None:
        if self.checkpoint.phase is not HostComponentLifecyclePhase.COMMIT:
            raise HostComponentLifecycleError(
                "host_lifecycle.result_incomplete",
                "host lifecycle result requires an admitted complete checkpoint",
            )

    @property
    def run(self) -> GenerationRun:
        return self.checkpoint.run

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/host-component-lifecycle-result@3",
                "checkpoint_identity": self.checkpoint.identity.uri,
                "provenance_identity": self.run.provenance.identity.uri,
            }
        )


class HostComponentLifecycleSession:
    """Advance one exact host lifecycle only at durable typed step boundaries."""

    def __init__(
        self,
        lifecycle: ResumableGenerationLifecycle,
        request: GenerationRequest,
    ) -> None:
        required = tuple(
            stop
            for phase, stop in _STOP_AFTER.items()
            if phase is not HostComponentLifecyclePhase.PREPARE_GENERATE
            and stop is not None
        ) + ("commit-tree",)
        if any(step not in request.execution_plan.lifecycle_steps for step in required):
            raise HostComponentLifecycleError(
                "host_lifecycle.plan_incomplete",
                "host lifecycle requires build, test, execution, acceptance, and "
                "admission stop points",
            )
        self.lifecycle = lifecycle
        self.request = request

    def prepare_generate(self) -> HostComponentLifecycleCheckpoint:
        return self._advance(HostComponentLifecyclePhase.PREPARE_GENERATE, None)

    def validate_classify(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        return self._advance(HostComponentLifecyclePhase.VALIDATE_CLASSIFY, checkpoint)

    def validate_index(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        """Compatibility alias for the validate-and-classify boundary."""

        return self.validate_classify(checkpoint)

    def authorize(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        return self._advance(HostComponentLifecyclePhase.AUTHORIZE, checkpoint)

    def build_resolve(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        return self._advance(HostComponentLifecyclePhase.BUILD_RESOLVE, checkpoint)

    def generated_test(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        return self._advance(HostComponentLifecyclePhase.GENERATED_TEST, checkpoint)

    def execute(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        """Compatibility alias: execution occurs inside independent acceptance."""

        return self.independent_accept(checkpoint)

    def independent_accept(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        return self._advance(HostComponentLifecyclePhase.INDEPENDENT_ACCEPT, checkpoint)

    def prepare_commit(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleCheckpoint:
        return self._advance(HostComponentLifecyclePhase.PREPARE_COMMIT, checkpoint)

    def commit(
        self, checkpoint: HostComponentLifecycleCheckpoint
    ) -> HostComponentLifecycleResult:
        admitted = self._advance(HostComponentLifecyclePhase.COMMIT, checkpoint)
        return HostComponentLifecycleResult(admitted)

    def run(
        self, resume: HostComponentLifecycleCheckpoint | None = None
    ) -> HostComponentLifecycleResult:
        checkpoint = resume or self.prepare_generate()
        start = HOST_COMPONENT_LIFECYCLE_PHASES.index(checkpoint.phase) + 1
        for phase in HOST_COMPONENT_LIFECYCLE_PHASES[start:]:
            if phase is HostComponentLifecyclePhase.VALIDATE_CLASSIFY:
                checkpoint = self.validate_classify(checkpoint)
            elif phase is HostComponentLifecyclePhase.AUTHORIZE:
                checkpoint = self.authorize(checkpoint)
            elif phase is HostComponentLifecyclePhase.BUILD_RESOLVE:
                checkpoint = self.build_resolve(checkpoint)
            elif phase is HostComponentLifecyclePhase.GENERATED_TEST:
                checkpoint = self.generated_test(checkpoint)
            elif phase is HostComponentLifecyclePhase.INDEPENDENT_ACCEPT:
                checkpoint = self.independent_accept(checkpoint)
            elif phase is HostComponentLifecyclePhase.PREPARE_COMMIT:
                checkpoint = self.prepare_commit(checkpoint)
            elif phase is HostComponentLifecyclePhase.COMMIT:
                return self.commit(checkpoint)
        if checkpoint.phase is HostComponentLifecyclePhase.COMMIT:
            return HostComponentLifecycleResult(checkpoint)
        raise AssertionError("host lifecycle phase sequence did not admit a result")

    def _advance(
        self,
        phase: HostComponentLifecyclePhase,
        checkpoint: HostComponentLifecycleCheckpoint | None,
    ) -> HostComponentLifecycleCheckpoint:
        index = HOST_COMPONENT_LIFECYCLE_PHASES.index(phase)
        if index == 0:
            if checkpoint is not None:
                raise HostComponentLifecycleError(
                    "host_lifecycle.phase_order_invalid",
                    "prepare/generate cannot resume a later checkpoint",
                )
        elif (
            checkpoint is None
            or checkpoint.phase is not HOST_COMPONENT_LIFECYCLE_PHASES[index - 1]
        ):
            raise HostComponentLifecycleError(
                "host_lifecycle.phase_order_invalid",
                "host lifecycle phases must advance in exact order",
            )
        run = self.lifecycle.execute(
            self.request,
            resume=None if checkpoint is None else checkpoint.run,
            stop_after=_STOP_AFTER[phase],
        )
        return HostComponentLifecycleCheckpoint(phase, run)


__all__ = [
    "HOST_COMPONENT_LIFECYCLE_PHASES",
    "HostComponentLifecycleCheckpoint",
    "HostComponentLifecycleError",
    "HostComponentLifecyclePhase",
    "HostComponentLifecycleResult",
    "HostComponentLifecycleSession",
    "ResumableGenerationLifecycle",
]

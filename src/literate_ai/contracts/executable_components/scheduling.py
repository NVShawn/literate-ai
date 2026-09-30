"""Canonical evidence for bounded per-Component generation scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import contract_fields, enum_value, fail, int_value, parse_tuple
from ..identity import ContentIdentity, contract_identity
from ._common import identity, portable_name, tuple_value

COMPONENT_GENERATION_RUNTIME_OBSERVATION_SCHEMA = (
    "urn:literate-ai:schema:v2:component-generation-runtime-observation"
)
COMPONENT_GENERATION_RESUME_CANDIDATE_SCHEMA = (
    "urn:literate-ai:schema:v2:component-generation-resume-candidate"
)
COMPONENT_GENERATION_RUN_OUTPUT_SCHEMA = (
    "urn:literate-ai:schema:v2:component-generation-run-output"
)
COMPONENT_GENERATION_NODE_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v2:component-generation-node-result"
)
COMPONENT_GENERATION_SCHEDULE_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v2:component-generation-schedule-result"
)


def _optional_count(value: object, path: str) -> int | None:
    if value is None:
        return None
    return int_value(value, path, maximum=2**63 - 1)


@dataclass(frozen=True, slots=True)
class ComponentGenerationRuntimeObservation:
    """Actual provider metrics; unavailable measurements remain explicit nulls."""

    model_attempts: int | None
    wall_time_ms: int | None
    model_tokens: int | None
    cost_microunits: int | None

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_RUNTIME_OBSERVATION_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "model_attempts",
            "wall_time_ms",
            "model_tokens",
            "cost_microunits",
        ):
            _optional_count(
                getattr(self, name), f"ComponentGenerationRuntimeObservation.{name}"
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "model_attempts": self.model_attempts,
            "wall_time_ms": self.wall_time_ms,
            "model_tokens": self.model_tokens,
            "cost_microunits": self.cost_microunits,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationRuntimeObservation"
    ) -> ComponentGenerationRuntimeObservation:
        names = frozenset(
            {"model_attempts", "wall_time_ms", "model_tokens", "cost_microunits"}
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            model_attempts=_optional_count(
                data["model_attempts"], f"{path}.model_attempts"
            ),
            wall_time_ms=_optional_count(data["wall_time_ms"], f"{path}.wall_time_ms"),
            model_tokens=_optional_count(data["model_tokens"], f"{path}.model_tokens"),
            cost_microunits=_optional_count(
                data["cost_microunits"], f"{path}.cost_microunits"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentGenerationResumeCandidate:
    """Accepted node source plus every identity required for fail-closed reuse."""

    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_budget_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    prompt_identity: ContentIdentity
    accepted_result_identity: ContentIdentity

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_RESUME_CANDIDATE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "generation_key_identity",
            "context_manifest_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
            "prompt_identity",
            "accepted_result_identity",
        ):
            identity(getattr(self, name), f"ComponentGenerationResumeCandidate.{name}")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "generation_key_identity",
                    "context_manifest_identity",
                    "complexity_budget_identity",
                    "complexity_decision_identity",
                    "prompt_identity",
                    "accepted_result_identity",
                )
            },
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationResumeCandidate"
    ) -> ComponentGenerationResumeCandidate:
        names = frozenset(
            {
                "generation_key_identity",
                "context_manifest_identity",
                "complexity_budget_identity",
                "complexity_decision_identity",
                "prompt_identity",
                "accepted_result_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in names
            }
        )


@dataclass(frozen=True, slots=True)
class ComponentGenerationRunOutput:
    accepted_result_identity: ContentIdentity
    runtime_observation: ComponentGenerationRuntimeObservation | None = None

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_RUN_OUTPUT_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.accepted_result_identity,
            "ComponentGenerationRunOutput.accepted_result_identity",
        )
        if self.runtime_observation is not None and not isinstance(
            self.runtime_observation, ComponentGenerationRuntimeObservation
        ):
            fail(
                "ComponentGenerationRunOutput.runtime_observation",
                "must be a ComponentGenerationRuntimeObservation or null",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "accepted_result_identity": self.accepted_result_identity.to_dict(),
            "runtime_observation": (
                None
                if self.runtime_observation is None
                else self.runtime_observation.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationRunOutput"
    ) -> ComponentGenerationRunOutput:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"accepted_result_identity", "runtime_observation"}),
        )
        observation = data["runtime_observation"]
        return cls(
            ContentIdentity.from_dict(
                data["accepted_result_identity"],
                path=f"{path}.accepted_result_identity",
            ),
            (
                None
                if observation is None
                else ComponentGenerationRuntimeObservation.from_dict(
                    observation, path=f"{path}.runtime_observation"
                )
            ),
        )


class ComponentGenerationDisposition(StrEnum):
    REUSED = "reused"
    GENERATED = "generated"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ComponentGenerationNodeResult:
    """Journal/benchmark/receipt-facing result for one exact bounded request."""

    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_budget_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    prompt_identity: ContentIdentity
    disposition: ComponentGenerationDisposition
    accepted_result_identity: ContentIdentity | None
    runtime_observation: ComponentGenerationRuntimeObservation | None
    failure_code: str | None

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_NODE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
            "prompt_identity",
        ):
            identity(getattr(self, name), f"ComponentGenerationNodeResult.{name}")
        if not isinstance(self.disposition, ComponentGenerationDisposition):
            fail(
                "ComponentGenerationNodeResult.disposition",
                "must be a ComponentGenerationDisposition",
            )
        if self.accepted_result_identity is not None:
            identity(
                self.accepted_result_identity,
                "ComponentGenerationNodeResult.accepted_result_identity",
            )
        if self.runtime_observation is not None and not isinstance(
            self.runtime_observation, ComponentGenerationRuntimeObservation
        ):
            fail(
                "ComponentGenerationNodeResult.runtime_observation",
                "must be a ComponentGenerationRuntimeObservation or null",
            )
        successful = self.disposition in {
            ComponentGenerationDisposition.REUSED,
            ComponentGenerationDisposition.GENERATED,
        }
        if successful != (self.accepted_result_identity is not None):
            fail(
                "ComponentGenerationNodeResult.accepted_result_identity",
                "must exist exactly for reused or generated results",
            )
        if (
            self.disposition
            in {
                ComponentGenerationDisposition.REUSED,
                ComponentGenerationDisposition.CANCELLED,
            }
            and self.runtime_observation is not None
        ):
            fail(
                "ComponentGenerationNodeResult.runtime_observation",
                "a reused or cancelled result cannot claim current generation runtime",
            )
        if successful and self.failure_code is not None:
            fail(
                "ComponentGenerationNodeResult.failure_code",
                "successful results cannot have a failure code",
            )
        if not successful:
            portable_name(
                self.failure_code, "ComponentGenerationNodeResult.failure_code"
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generation_plan_identity": self.generation_plan_identity.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "context_manifest_identity": self.context_manifest_identity.to_dict(),
            "complexity_budget_identity": self.complexity_budget_identity.to_dict(),
            "complexity_decision_identity": self.complexity_decision_identity.to_dict(),
            "prompt_identity": self.prompt_identity.to_dict(),
            "disposition": self.disposition.value,
            "accepted_result_identity": (
                None
                if self.accepted_result_identity is None
                else self.accepted_result_identity.to_dict()
            ),
            "runtime_observation": (
                None
                if self.runtime_observation is None
                else self.runtime_observation.to_dict()
            ),
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationNodeResult"
    ) -> ComponentGenerationNodeResult:
        names = frozenset(
            {
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
                "context_manifest_identity",
                "complexity_budget_identity",
                "complexity_decision_identity",
                "prompt_identity",
                "disposition",
                "accepted_result_identity",
                "runtime_observation",
                "failure_code",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        accepted = data["accepted_result_identity"]
        observation = data["runtime_observation"]
        failure = data["failure_code"]
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            generation_plan_identity=ContentIdentity.from_dict(
                data["generation_plan_identity"],
                path=f"{path}.generation_plan_identity",
            ),
            generation_key_identity=ContentIdentity.from_dict(
                data["generation_key_identity"], path=f"{path}.generation_key_identity"
            ),
            context_manifest_identity=ContentIdentity.from_dict(
                data["context_manifest_identity"],
                path=f"{path}.context_manifest_identity",
            ),
            complexity_budget_identity=ContentIdentity.from_dict(
                data["complexity_budget_identity"],
                path=f"{path}.complexity_budget_identity",
            ),
            complexity_decision_identity=ContentIdentity.from_dict(
                data["complexity_decision_identity"],
                path=f"{path}.complexity_decision_identity",
            ),
            prompt_identity=ContentIdentity.from_dict(
                data["prompt_identity"], path=f"{path}.prompt_identity"
            ),
            disposition=enum_value(
                ComponentGenerationDisposition,
                data["disposition"],
                f"{path}.disposition",
            ),
            accepted_result_identity=(
                None
                if accepted is None
                else ContentIdentity.from_dict(
                    accepted, path=f"{path}.accepted_result_identity"
                )
            ),
            runtime_observation=(
                None
                if observation is None
                else ComponentGenerationRuntimeObservation.from_dict(
                    observation, path=f"{path}.runtime_observation"
                )
            ),
            failure_code=(
                None
                if failure is None
                else portable_name(failure, f"{path}.failure_code")
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentGenerationScheduleResult:
    execution_plan_identity: ContentIdentity
    invalidation_decision_identity: ContentIdentity
    max_parallelism: int
    node_results: tuple[ComponentGenerationNodeResult, ...]

    SCHEMA: ClassVar[str] = COMPONENT_GENERATION_SCHEDULE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.execution_plan_identity,
            "ComponentGenerationScheduleResult.execution_plan_identity",
        )
        identity(
            self.invalidation_decision_identity,
            "ComponentGenerationScheduleResult.invalidation_decision_identity",
        )
        int_value(
            self.max_parallelism,
            "ComponentGenerationScheduleResult.max_parallelism",
            minimum=1,
            maximum=256,
        )
        values = tuple_value(
            self.node_results, "ComponentGenerationScheduleResult.node_results"
        )
        if (
            not values
            or len(values) > 4096
            or any(
                not isinstance(item, ComponentGenerationNodeResult) for item in values
            )
        ):
            fail(
                "ComponentGenerationScheduleResult.node_results",
                "must contain 1 to 4096 ComponentGenerationNodeResult values",
            )
        uris = tuple(item.component_revision.uri for item in values)
        if uris != tuple(sorted(set(uris))):
            fail(
                "ComponentGenerationScheduleResult.node_results",
                "must use unique canonical Component revision order",
            )

    @property
    def actual_generation_calls(self) -> int:
        return sum(
            item.disposition
            in {
                ComponentGenerationDisposition.GENERATED,
                ComponentGenerationDisposition.FAILED,
            }
            for item in self.node_results
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "invalidation_decision_identity": (
                self.invalidation_decision_identity.to_dict()
            ),
            "max_parallelism": self.max_parallelism,
            "node_results": [item.to_dict() for item in self.node_results],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentGenerationScheduleResult"
    ) -> ComponentGenerationScheduleResult:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "execution_plan_identity",
                    "invalidation_decision_identity",
                    "max_parallelism",
                    "node_results",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            ContentIdentity.from_dict(
                data["invalidation_decision_identity"],
                path=f"{path}.invalidation_decision_identity",
            ),
            int_value(
                data["max_parallelism"],
                f"{path}.max_parallelism",
                minimum=1,
                maximum=256,
            ),
            parse_tuple(
                data["node_results"],
                f"{path}.node_results",
                ComponentGenerationNodeResult.from_dict,
            ),
        )


__all__ = [
    "COMPONENT_GENERATION_NODE_RESULT_SCHEMA",
    "COMPONENT_GENERATION_RESUME_CANDIDATE_SCHEMA",
    "COMPONENT_GENERATION_RUN_OUTPUT_SCHEMA",
    "COMPONENT_GENERATION_RUNTIME_OBSERVATION_SCHEMA",
    "COMPONENT_GENERATION_SCHEDULE_RESULT_SCHEMA",
    "ComponentGenerationDisposition",
    "ComponentGenerationNodeResult",
    "ComponentGenerationResumeCandidate",
    "ComponentGenerationRunOutput",
    "ComponentGenerationRuntimeObservation",
    "ComponentGenerationScheduleResult",
]

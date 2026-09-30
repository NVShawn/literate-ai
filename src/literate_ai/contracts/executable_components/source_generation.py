"""Immutable contracts for source candidates, provenance, and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    enum_value,
    fail,
    int_value,
    optional_string,
    parse_tuple,
    string_tuple,
    string_value,
)
from ..identity import ContentIdentity, contract_identity
from ._common import identity, portable_name
from .scheduling import ComponentGenerationRuntimeObservation

GENERATED_SOURCE_CANDIDATE_SCHEMA = (
    "urn:literate-ai:schema:v4:generated-source-candidate"
)
SOURCE_GENERATION_RUN_OUTPUT_SCHEMA = (
    "urn:literate-ai:schema:v3:source-generation-run-output"
)
SOURCE_GENERATION_PROVENANCE_SCHEMA = (
    "urn:literate-ai:schema:v5:source-generation-provenance"
)
SOURCE_GENERATION_CHECKPOINT_SCHEMA = (
    "urn:literate-ai:schema:v4:source-generation-checkpoint"
)
SOURCE_GENERATION_RESUME_CANDIDATE_SCHEMA = (
    "urn:literate-ai:schema:v3:source-generation-resume-candidate"
)
SOURCE_GENERATION_NODE_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v3:source-generation-node-result"
)
SOURCE_GENERATION_SCHEDULE_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v3:source-generation-schedule-result"
)


def _identity_tuple(
    values: tuple[ContentIdentity, ...], path: str, *, required: bool = False
) -> None:
    if required and not values:
        fail(path, "must not be empty")
    for index, value in enumerate(values):
        identity(value, f"{path}[{index}]")
    uris = tuple(value.uri for value in values)
    if len(set(uris)) != len(uris):
        fail(path, "must not repeat identities")


@dataclass(frozen=True, slots=True)
class GeneratedSourceCandidate:
    """One immutable source candidate awaiting the current lifecycle gates."""

    component_revision: ContentIdentity
    # The component-orchestration request and the exact coding-CLI prompt request are
    # intentionally different identities. The latter keys deterministic source reuse.
    source_generation_request_identity: ContentIdentity
    planned_coding_cli_request_identity: ContentIdentity
    component_generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    prompt_identity: ContentIdentity
    recipe_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    tree_identity: ContentIdentity
    source_bundle_identity: ContentIdentity
    source_manifest_identity: ContentIdentity
    source_bom_identity: ContentIdentity
    generated_test_suite_identity: ContentIdentity

    SCHEMA: ClassVar[str] = GENERATED_SOURCE_CANDIDATE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "source_generation_request_identity",
            "planned_coding_cli_request_identity",
            "component_generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "prompt_identity",
            "recipe_identity",
            "workspace_allocation_identity",
            "tree_identity",
            "source_bundle_identity",
            "source_manifest_identity",
            "source_bom_identity",
            "generated_test_suite_identity",
        ):
            identity(getattr(self, name), f"GeneratedSourceCandidate.{name}")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "component_revision",
                    "source_generation_request_identity",
                    "planned_coding_cli_request_identity",
                    "component_generation_plan_identity",
                    "generation_key_identity",
                    "context_manifest_identity",
                    "prompt_identity",
                    "recipe_identity",
                    "workspace_allocation_identity",
                    "tree_identity",
                    "source_bundle_identity",
                    "source_manifest_identity",
                    "source_bom_identity",
                    "generated_test_suite_identity",
                )
            },
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "GeneratedSourceCandidate"
    ) -> GeneratedSourceCandidate:
        names = frozenset(
            {
                "component_revision",
                "source_generation_request_identity",
                "planned_coding_cli_request_identity",
                "component_generation_plan_identity",
                "generation_key_identity",
                "context_manifest_identity",
                "prompt_identity",
                "recipe_identity",
                "workspace_allocation_identity",
                "tree_identity",
                "source_bundle_identity",
                "source_manifest_identity",
                "source_bom_identity",
                "generated_test_suite_identity",
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
class SourceGenerationProvenance:
    """Origin evidence ending at an unadmitted generated or retained candidate."""

    source_generation_request_identity: ContentIdentity
    planned_coding_cli_request_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    application_root_revision_identity: ContentIdentity
    generated_component_revision_identity: ContentIdentity
    component_generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    prompt_identity: ContentIdentity
    recipe_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    readiness_identity: ContentIdentity
    route_decision_identities: tuple[ContentIdentity, ...]
    model_stage_output_identities: tuple[ContentIdentity, ...]
    candidate_identity: ContentIdentity
    provider_evidence_identities: tuple[ContentIdentity, ...] = ()
    retained_source_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = SOURCE_GENERATION_PROVENANCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "source_generation_request_identity",
            "planned_coding_cli_request_identity",
            "component_lock_identity",
            "application_root_revision_identity",
            "generated_component_revision_identity",
            "component_generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "prompt_identity",
            "recipe_identity",
            "workspace_allocation_identity",
            "readiness_identity",
            "candidate_identity",
        ):
            identity(getattr(self, name), f"SourceGenerationProvenance.{name}")
        _identity_tuple(
            self.route_decision_identities,
            "SourceGenerationProvenance.route_decision_identities",
            required=self.retained_source_identity is None,
        )
        _identity_tuple(
            self.model_stage_output_identities,
            "SourceGenerationProvenance.model_stage_output_identities",
            required=self.retained_source_identity is None,
        )
        _identity_tuple(
            self.provider_evidence_identities,
            "SourceGenerationProvenance.provider_evidence_identities",
        )
        if self.retained_source_identity is not None:
            identity(
                self.retained_source_identity,
                "SourceGenerationProvenance.retained_source_identity",
            )
            if (
                self.route_decision_identities
                or self.model_stage_output_identities
                or self.provider_evidence_identities
            ):
                fail(
                    "SourceGenerationProvenance",
                    "retained source must not claim model generation",
                )
        if len(self.route_decision_identities) != len(
            self.model_stage_output_identities
        ):
            fail(
                "SourceGenerationProvenance",
                "route decisions and model stage outputs must have equal length",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "source_generation_request_identity",
                    "planned_coding_cli_request_identity",
                    "component_lock_identity",
                    "application_root_revision_identity",
                    "generated_component_revision_identity",
                    "component_generation_plan_identity",
                    "generation_key_identity",
                    "context_manifest_identity",
                    "prompt_identity",
                    "recipe_identity",
                    "workspace_allocation_identity",
                    "readiness_identity",
                    "candidate_identity",
                )
            },
            "route_decision_identities": [
                item.to_dict() for item in self.route_decision_identities
            ],
            "model_stage_output_identities": [
                item.to_dict() for item in self.model_stage_output_identities
            ],
            **(
                {"retained_source_identity": self.retained_source_identity.to_dict()}
                if self.retained_source_identity is not None
                else {}
            ),
            **(
                {
                    "provider_evidence_identities": [
                        item.to_dict() for item in self.provider_evidence_identities
                    ]
                }
                if self.provider_evidence_identities
                else {}
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceGenerationProvenance"
    ) -> SourceGenerationProvenance:
        singular = frozenset(
            {
                "source_generation_request_identity",
                "planned_coding_cli_request_identity",
                "component_lock_identity",
                "application_root_revision_identity",
                "generated_component_revision_identity",
                "component_generation_plan_identity",
                "generation_key_identity",
                "context_manifest_identity",
                "prompt_identity",
                "recipe_identity",
                "workspace_allocation_identity",
                "readiness_identity",
                "candidate_identity",
            }
        )
        repeated = frozenset(
            {"route_decision_identities", "model_stage_output_identities"}
        )
        optional_repeated = frozenset({"provider_evidence_identities"})
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=singular | repeated,
            optional=optional_repeated | {"retained_source_identity"},
        )
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in singular
            },
            **{
                name: parse_tuple(
                    data[name], f"{path}.{name}", ContentIdentity.from_dict
                )
                for name in repeated
            },
            provider_evidence_identities=parse_tuple(
                data.get("provider_evidence_identities", ()),
                f"{path}.provider_evidence_identities",
                ContentIdentity.from_dict,
            ),
            retained_source_identity=(
                None
                if "retained_source_identity" not in data
                else ContentIdentity.from_dict(data["retained_source_identity"])
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceGenerationRunOutput:
    """Typed generator-port output; it grants no later lifecycle authority."""

    candidate: GeneratedSourceCandidate
    candidate_identity: ContentIdentity
    provenance: SourceGenerationProvenance
    provenance_identity: ContentIdentity
    runtime_observation: ComponentGenerationRuntimeObservation | None = None

    SCHEMA: ClassVar[str] = SOURCE_GENERATION_RUN_OUTPUT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, GeneratedSourceCandidate):
            fail("SourceGenerationRunOutput.candidate", "must be a typed candidate")
        if not isinstance(self.provenance, SourceGenerationProvenance):
            fail("SourceGenerationRunOutput.provenance", "must be typed provenance")
        identity(
            self.candidate_identity, "SourceGenerationRunOutput.candidate_identity"
        )
        identity(
            self.provenance_identity, "SourceGenerationRunOutput.provenance_identity"
        )
        if self.candidate_identity != self.candidate.identity:
            fail(
                "SourceGenerationRunOutput.candidate_identity",
                "must bind the exact candidate",
            )
        if (
            self.provenance_identity != self.provenance.identity
            or self.provenance.candidate_identity != self.candidate_identity
        ):
            fail(
                "SourceGenerationRunOutput.provenance_identity",
                "must bind provenance for the exact candidate",
            )
        if (
            self.provenance.source_generation_request_identity
            != self.candidate.source_generation_request_identity
            or self.provenance.planned_coding_cli_request_identity
            != self.candidate.planned_coding_cli_request_identity
        ):
            fail(
                "SourceGenerationRunOutput.provenance",
                "must bind both orchestration and planned coding-CLI requests",
            )
        if self.runtime_observation is not None and not isinstance(
            self.runtime_observation, ComponentGenerationRuntimeObservation
        ):
            fail(
                "SourceGenerationRunOutput.runtime_observation",
                "must be a ComponentGenerationRuntimeObservation or null",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "candidate": self.candidate.to_dict(),
            "candidate_identity": self.candidate_identity.to_dict(),
            "provenance": self.provenance.to_dict(),
            "provenance_identity": self.provenance_identity.to_dict(),
            "runtime_observation": (
                None
                if self.runtime_observation is None
                else self.runtime_observation.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceGenerationRunOutput"
    ) -> SourceGenerationRunOutput:
        names = frozenset(
            {
                "candidate",
                "candidate_identity",
                "provenance",
                "provenance_identity",
                "runtime_observation",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        observation = data["runtime_observation"]
        return cls(
            GeneratedSourceCandidate.from_dict(
                data["candidate"], path=f"{path}.candidate"
            ),
            ContentIdentity.from_dict(
                data["candidate_identity"], path=f"{path}.candidate_identity"
            ),
            SourceGenerationProvenance.from_dict(
                data["provenance"], path=f"{path}.provenance"
            ),
            ContentIdentity.from_dict(
                data["provenance_identity"], path=f"{path}.provenance_identity"
            ),
            (
                None
                if observation is None
                else ComponentGenerationRuntimeObservation.from_dict(
                    observation, path=f"{path}.runtime_observation"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class SourceGenerationResumeCandidate:
    """A prior source-only output plus the runtime policy identities it used."""

    output: SourceGenerationRunOutput
    output_identity: ContentIdentity
    complexity_budget_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity

    SCHEMA: ClassVar[str] = SOURCE_GENERATION_RESUME_CANDIDATE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.output, SourceGenerationRunOutput):
            fail("SourceGenerationResumeCandidate.output", "must be typed output")
        for name in (
            "output_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
        ):
            identity(getattr(self, name), f"SourceGenerationResumeCandidate.{name}")
        if self.output_identity != self.output.identity:
            fail(
                "SourceGenerationResumeCandidate.output_identity",
                "must bind the exact source-generation output",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "output": self.output.to_dict(),
            "output_identity": self.output_identity.to_dict(),
            "complexity_budget_identity": self.complexity_budget_identity.to_dict(),
            "complexity_decision_identity": (
                self.complexity_decision_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceGenerationResumeCandidate"
    ) -> SourceGenerationResumeCandidate:
        names = frozenset(
            {
                "output",
                "output_identity",
                "complexity_budget_identity",
                "complexity_decision_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            SourceGenerationRunOutput.from_dict(data["output"], path=f"{path}.output"),
            ContentIdentity.from_dict(
                data["output_identity"], path=f"{path}.output_identity"
            ),
            ContentIdentity.from_dict(
                data["complexity_budget_identity"],
                path=f"{path}.complexity_budget_identity",
            ),
            ContentIdentity.from_dict(
                data["complexity_decision_identity"],
                path=f"{path}.complexity_decision_identity",
            ),
        )


class SourceGenerationDisposition(StrEnum):
    REUSED = "reused"
    GENERATED = "generated"
    RETAINED = "retained"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SourceGenerationNodeResult:
    """Source-only result for one complete, independently prepared Component node."""

    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    complexity_budget_identity: ContentIdentity
    complexity_decision_identity: ContentIdentity
    prompt_identity: ContentIdentity
    recipe_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    disposition: SourceGenerationDisposition
    candidate_identity: ContentIdentity | None
    provenance_identity: ContentIdentity | None
    runtime_observation: ComponentGenerationRuntimeObservation | None
    failure_code: str | None

    SCHEMA: ClassVar[str] = SOURCE_GENERATION_NODE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
            "context_manifest_identity",
            "complexity_budget_identity",
            "complexity_decision_identity",
            "prompt_identity",
            "recipe_identity",
            "workspace_allocation_identity",
        ):
            identity(getattr(self, name), f"SourceGenerationNodeResult.{name}")
        if not isinstance(self.disposition, SourceGenerationDisposition):
            fail(
                "SourceGenerationNodeResult.disposition",
                "must be a SourceGenerationDisposition",
            )
        for name in ("candidate_identity", "provenance_identity"):
            value = getattr(self, name)
            if value is not None:
                identity(value, f"SourceGenerationNodeResult.{name}")
        if self.runtime_observation is not None and not isinstance(
            self.runtime_observation, ComponentGenerationRuntimeObservation
        ):
            fail(
                "SourceGenerationNodeResult.runtime_observation",
                "must be a ComponentGenerationRuntimeObservation or null",
            )
        successful = self.disposition in {
            SourceGenerationDisposition.REUSED,
            SourceGenerationDisposition.GENERATED,
            SourceGenerationDisposition.RETAINED,
        }
        bound = (
            self.candidate_identity is not None and self.provenance_identity is not None
        )
        if successful != bound or (
            (self.candidate_identity is None) != (self.provenance_identity is None)
        ):
            fail(
                "SourceGenerationNodeResult",
                "candidate and provenance must exist exactly for successful results",
            )
        if (
            self.disposition
            in {
                SourceGenerationDisposition.REUSED,
                SourceGenerationDisposition.CANCELLED,
            }
            and self.runtime_observation is not None
        ):
            fail(
                "SourceGenerationNodeResult.runtime_observation",
                "reused or cancelled results cannot claim current generation runtime",
            )
        if successful and self.failure_code is not None:
            fail(
                "SourceGenerationNodeResult.failure_code",
                "successful results cannot have a failure code",
            )
        if not successful:
            portable_name(self.failure_code, "SourceGenerationNodeResult.failure_code")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema": self.SCHEMA,
            **{
                name: getattr(self, name).to_dict()
                for name in (
                    "component_revision",
                    "generation_plan_identity",
                    "generation_key_identity",
                    "context_manifest_identity",
                    "complexity_budget_identity",
                    "complexity_decision_identity",
                    "prompt_identity",
                    "recipe_identity",
                    "workspace_allocation_identity",
                )
            },
            "disposition": self.disposition.value,
            "candidate_identity": (
                None
                if self.candidate_identity is None
                else self.candidate_identity.to_dict()
            ),
            "provenance_identity": (
                None
                if self.provenance_identity is None
                else self.provenance_identity.to_dict()
            ),
            "runtime_observation": (
                None
                if self.runtime_observation is None
                else self.runtime_observation.to_dict()
            ),
            "failure_code": self.failure_code,
        }
        return document

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceGenerationNodeResult"
    ) -> SourceGenerationNodeResult:
        identity_names = frozenset(
            {
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
                "context_manifest_identity",
                "complexity_budget_identity",
                "complexity_decision_identity",
                "prompt_identity",
                "recipe_identity",
                "workspace_allocation_identity",
            }
        )
        names = identity_names | {
            "disposition",
            "candidate_identity",
            "provenance_identity",
            "runtime_observation",
            "failure_code",
        }
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)

        def optional_identity(name: str) -> ContentIdentity | None:
            raw = data[name]
            return (
                None
                if raw is None
                else ContentIdentity.from_dict(raw, path=f"{path}.{name}")
            )

        observation = data["runtime_observation"]
        failure = data["failure_code"]
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            disposition=enum_value(
                SourceGenerationDisposition,
                data["disposition"],
                f"{path}.disposition",
            ),
            candidate_identity=optional_identity("candidate_identity"),
            provenance_identity=optional_identity("provenance_identity"),
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
class SourceGenerationScheduleResult:
    execution_plan_identity: ContentIdentity
    invalidation_decision_identity: ContentIdentity
    max_parallelism: int
    node_results: tuple[SourceGenerationNodeResult, ...]

    SCHEMA: ClassVar[str] = SOURCE_GENERATION_SCHEDULE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.execution_plan_identity,
            "SourceGenerationScheduleResult.execution_plan_identity",
        )
        identity(
            self.invalidation_decision_identity,
            "SourceGenerationScheduleResult.invalidation_decision_identity",
        )
        int_value(
            self.max_parallelism,
            "SourceGenerationScheduleResult.max_parallelism",
            minimum=1,
            maximum=256,
        )
        if (
            not self.node_results
            or len(self.node_results) > 4096
            or any(
                not isinstance(item, SourceGenerationNodeResult)
                for item in self.node_results
            )
        ):
            fail(
                "SourceGenerationScheduleResult.node_results",
                "must contain 1 to 4096 SourceGenerationNodeResult values",
            )
        uris = tuple(item.component_revision.uri for item in self.node_results)
        if uris != tuple(sorted(set(uris))):
            fail(
                "SourceGenerationScheduleResult.node_results",
                "must use unique canonical Component revision order",
            )

    @property
    def actual_generation_calls(self) -> int:
        return sum(
            item.disposition
            in {
                SourceGenerationDisposition.GENERATED,
                SourceGenerationDisposition.FAILED,
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
        cls, value: Any, *, path: str = "SourceGenerationScheduleResult"
    ) -> SourceGenerationScheduleResult:
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
                SourceGenerationNodeResult.from_dict,
            ),
        )


class SourceGenerationCheckpointStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    CANDIDATE_READY = "candidate-ready"


@dataclass(frozen=True, slots=True)
class SourceGenerationCheckpoint:
    """Immutable resumable generation state that ends when a candidate is ready."""

    run_id: str
    source_generation_request_identity: ContentIdentity
    status: SourceGenerationCheckpointStatus
    event_stream_identity: ContentIdentity
    completed_model_stage_ids: tuple[str, ...]
    completed_model_output_identities: tuple[ContentIdentity, ...]
    candidate_identity: ContentIdentity | None = None
    provenance_identity: ContentIdentity | None = None
    failure_code: str | None = None

    SCHEMA: ClassVar[str] = SOURCE_GENERATION_CHECKPOINT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.run_id, "SourceGenerationCheckpoint.run_id")
        identity(
            self.source_generation_request_identity,
            "SourceGenerationCheckpoint.source_generation_request_identity",
        )
        if not isinstance(self.status, SourceGenerationCheckpointStatus):
            fail(
                "SourceGenerationCheckpoint.status",
                "must be a SourceGenerationCheckpointStatus",
            )
        identity(
            self.event_stream_identity,
            "SourceGenerationCheckpoint.event_stream_identity",
        )
        stages = self.completed_model_stage_ids
        for index, stage in enumerate(stages):
            portable_name(stage, f"SourceGenerationCheckpoint.stage_ids[{index}]")
        if len(set(stages)) != len(stages):
            fail(
                "SourceGenerationCheckpoint.completed_model_stage_ids",
                "must not repeat stage IDs",
            )
        _identity_tuple(
            self.completed_model_output_identities,
            "SourceGenerationCheckpoint.completed_model_output_identities",
        )
        if len(stages) != len(self.completed_model_output_identities):
            fail(
                "SourceGenerationCheckpoint",
                "completed stages and outputs must have equal length",
            )
        if self.candidate_identity is not None:
            identity(
                self.candidate_identity,
                "SourceGenerationCheckpoint.candidate_identity",
            )
        if self.provenance_identity is not None:
            identity(
                self.provenance_identity,
                "SourceGenerationCheckpoint.provenance_identity",
            )
        if self.failure_code is not None:
            portable_name(self.failure_code, "SourceGenerationCheckpoint.failure_code")
        terminal_ready = self.status is SourceGenerationCheckpointStatus.CANDIDATE_READY
        terminal_bindings = (
            self.candidate_identity is not None,
            self.provenance_identity is not None,
        )
        if terminal_bindings != ((True, True) if terminal_ready else (False, False)):
            fail(
                "SourceGenerationCheckpoint",
                "only candidate-ready checkpoints bind candidate and provenance",
            )
        failed = self.status is SourceGenerationCheckpointStatus.FAILED
        if failed != (self.failure_code is not None):
            fail(
                "SourceGenerationCheckpoint.failure_code",
                "must exist exactly for failed checkpoints",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "run_id": self.run_id,
            "source_generation_request_identity": (
                self.source_generation_request_identity.to_dict()
            ),
            "status": self.status.value,
            "event_stream_identity": self.event_stream_identity.to_dict(),
            "completed_model_stage_ids": list(self.completed_model_stage_ids),
            "completed_model_output_identities": [
                item.to_dict() for item in self.completed_model_output_identities
            ],
            "candidate_identity": (
                None
                if self.candidate_identity is None
                else self.candidate_identity.to_dict()
            ),
            "provenance_identity": (
                None
                if self.provenance_identity is None
                else self.provenance_identity.to_dict()
            ),
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SourceGenerationCheckpoint"
    ) -> SourceGenerationCheckpoint:
        names = frozenset(
            {
                "run_id",
                "source_generation_request_identity",
                "status",
                "event_stream_identity",
                "completed_model_stage_ids",
                "completed_model_output_identities",
                "candidate_identity",
                "provenance_identity",
                "failure_code",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)

        def optional_identity(name: str) -> ContentIdentity | None:
            raw = data[name]
            return (
                None
                if raw is None
                else ContentIdentity.from_dict(raw, path=f"{path}.{name}")
            )

        return cls(
            string_value(data["run_id"], f"{path}.run_id"),
            ContentIdentity.from_dict(
                data["source_generation_request_identity"],
                path=f"{path}.source_generation_request_identity",
            ),
            enum_value(
                SourceGenerationCheckpointStatus,
                data["status"],
                f"{path}.status",
            ),
            ContentIdentity.from_dict(
                data["event_stream_identity"],
                path=f"{path}.event_stream_identity",
            ),
            string_tuple(
                data["completed_model_stage_ids"],
                f"{path}.completed_model_stage_ids",
            ),
            parse_tuple(
                data["completed_model_output_identities"],
                f"{path}.completed_model_output_identities",
                ContentIdentity.from_dict,
            ),
            optional_identity("candidate_identity"),
            optional_identity("provenance_identity"),
            optional_string(data["failure_code"], f"{path}.failure_code"),
        )


__all__ = [
    "GENERATED_SOURCE_CANDIDATE_SCHEMA",
    "SOURCE_GENERATION_CHECKPOINT_SCHEMA",
    "SOURCE_GENERATION_NODE_RESULT_SCHEMA",
    "SOURCE_GENERATION_PROVENANCE_SCHEMA",
    "SOURCE_GENERATION_RESUME_CANDIDATE_SCHEMA",
    "SOURCE_GENERATION_RUN_OUTPUT_SCHEMA",
    "SOURCE_GENERATION_SCHEDULE_RESULT_SCHEMA",
    "GeneratedSourceCandidate",
    "SourceGenerationCheckpoint",
    "SourceGenerationCheckpointStatus",
    "SourceGenerationDisposition",
    "SourceGenerationNodeResult",
    "SourceGenerationProvenance",
    "SourceGenerationResumeCandidate",
    "SourceGenerationRunOutput",
    "SourceGenerationScheduleResult",
]

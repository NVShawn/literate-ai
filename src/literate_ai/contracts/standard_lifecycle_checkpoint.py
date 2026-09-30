"""Durable, replay-safe checkpoints for the Standard Component lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import contract_fields, enum_value, fail, int_value, parse_tuple
from .executable_components import SourceGenerationResumeCandidate
from .generation_cache import CachedSourceFile
from .identity import ContentIdentity, contract_identity

STANDARD_LIFECYCLE_STAGE_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-lifecycle-stage-evidence"
)
STANDARD_LIFECYCLE_CHECKPOINT_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-lifecycle-checkpoint"
)
STANDARD_LIFECYCLE_ATTEMPT_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-lifecycle-attempt-evidence"
)


class StandardLifecycleStage(StrEnum):
    SOURCE_GENERATION = "source-generation"
    BUILD_INTENT = "build-intent"
    SOURCE_INDEX = "source-index"
    BUILD_AUTHORIZATION = "build-authorization"
    BUILD_PLAN = "build-plan"
    BUILD = "build"
    TEST = "test"
    EXECUTE = "execute"
    ACCEPT = "accept"
    SOURCE_CACHE_PUBLICATION = "source-cache-publication"


STANDARD_LIFECYCLE_STAGES = tuple(StandardLifecycleStage)


class StandardLifecycleCheckpointOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


@dataclass(frozen=True, slots=True)
class StandardLifecycleStageEvidence:
    """One completed or failed stage, always bound to reusable source custody."""

    execution_plan_identity: ContentIdentity
    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    recipe_identity: ContentIdentity
    source_generation: SourceGenerationResumeCandidate
    stage: StandardLifecycleStage
    subject_identity: ContentIdentity
    outcome: StandardLifecycleCheckpointOutcome
    failure_code: str | None = None

    SCHEMA: ClassVar[str] = STANDARD_LIFECYCLE_STAGE_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "execution_plan_identity",
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
            "recipe_identity",
            "subject_identity",
        ):
            _identity(getattr(self, name), f"StandardLifecycleStageEvidence.{name}")
        if not isinstance(self.source_generation, SourceGenerationResumeCandidate):
            fail(
                "StandardLifecycleStageEvidence.source_generation",
                "must be a SourceGenerationResumeCandidate",
            )
        candidate = self.source_generation.output.candidate
        if (
            candidate.component_revision != self.component_revision
            or candidate.component_generation_plan_identity
            != self.generation_plan_identity
            or candidate.generation_key_identity != self.generation_key_identity
            or candidate.recipe_identity != self.recipe_identity
        ):
            fail(
                "StandardLifecycleStageEvidence.source_generation",
                "must bind the exact Component generation identities",
            )
        if not isinstance(self.stage, StandardLifecycleStage):
            fail("StandardLifecycleStageEvidence.stage", "must be typed")
        if not isinstance(self.outcome, StandardLifecycleCheckpointOutcome):
            fail("StandardLifecycleStageEvidence.outcome", "must be typed")
        if (self.failure_code is None) != (
            self.outcome is StandardLifecycleCheckpointOutcome.PASSED
        ):
            fail(
                "StandardLifecycleStageEvidence.failure_code",
                "must exist exactly for failed stage evidence",
            )
        if self.failure_code is not None and (
            not isinstance(self.failure_code, str)
            or not self.failure_code
            or len(self.failure_code) > 128
        ):
            fail(
                "StandardLifecycleStageEvidence.failure_code",
                "must be a non-empty string",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "component_revision": self.component_revision.to_dict(),
            "generation_plan_identity": self.generation_plan_identity.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "recipe_identity": self.recipe_identity.to_dict(),
            "source_generation": self.source_generation.to_dict(),
            "stage": self.stage.value,
            "subject_identity": self.subject_identity.to_dict(),
            "outcome": self.outcome.value,
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardLifecycleStageEvidence"
    ) -> StandardLifecycleStageEvidence:
        names = frozenset(
            {
                "execution_plan_identity",
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
                "recipe_identity",
                "source_generation",
                "stage",
                "subject_identity",
                "outcome",
                "failure_code",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            ContentIdentity.from_dict(
                data["generation_plan_identity"],
                path=f"{path}.generation_plan_identity",
            ),
            ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            ContentIdentity.from_dict(
                data["recipe_identity"], path=f"{path}.recipe_identity"
            ),
            SourceGenerationResumeCandidate.from_dict(
                data["source_generation"], path=f"{path}.source_generation"
            ),
            enum_value(StandardLifecycleStage, data["stage"], f"{path}.stage"),
            ContentIdentity.from_dict(
                data["subject_identity"], path=f"{path}.subject_identity"
            ),
            enum_value(
                StandardLifecycleCheckpointOutcome,
                data["outcome"],
                f"{path}.outcome",
            ),
            data["failure_code"],
        )


@dataclass(frozen=True, slots=True)
class StandardLifecycleAttemptEvidence:
    """One process attempt, including attempts that stop before source exists."""

    execution_plan_identity: ContentIdentity
    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    recipe_identity: ContentIdentity
    attempt: int
    predecessor_attempt_identity: ContentIdentity | None
    source_predecessor_identity: ContentIdentity | None

    SCHEMA: ClassVar[str] = STANDARD_LIFECYCLE_ATTEMPT_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "execution_plan_identity",
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
            "recipe_identity",
        ):
            _identity(getattr(self, name), f"StandardLifecycleAttemptEvidence.{name}")
        int_value(self.attempt, "StandardLifecycleAttemptEvidence.attempt", minimum=1)
        for name in (
            "predecessor_attempt_identity",
            "source_predecessor_identity",
        ):
            value = getattr(self, name)
            if value is not None:
                _identity(value, f"StandardLifecycleAttemptEvidence.{name}")
        if (self.attempt == 1) != (self.predecessor_attempt_identity is None):
            fail(
                "StandardLifecycleAttemptEvidence.predecessor_attempt_identity",
                "must be absent exactly for the first attempt",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "component_revision": self.component_revision.to_dict(),
            "generation_plan_identity": self.generation_plan_identity.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "recipe_identity": self.recipe_identity.to_dict(),
            "attempt": self.attempt,
            "predecessor_attempt_identity": (
                None
                if self.predecessor_attempt_identity is None
                else self.predecessor_attempt_identity.to_dict()
            ),
            "source_predecessor_identity": (
                None
                if self.source_predecessor_identity is None
                else self.source_predecessor_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardLifecycleAttemptEvidence"
    ) -> StandardLifecycleAttemptEvidence:
        names = frozenset(
            {
                "execution_plan_identity",
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
                "recipe_identity",
                "attempt",
                "predecessor_attempt_identity",
                "source_predecessor_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)

        def optional_identity(name: str) -> ContentIdentity | None:
            item = data[name]
            return (
                None
                if item is None
                else ContentIdentity.from_dict(item, path=f"{path}.{name}")
            )

        return cls(
            ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            ContentIdentity.from_dict(
                data["generation_plan_identity"],
                path=f"{path}.generation_plan_identity",
            ),
            ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            ContentIdentity.from_dict(
                data["recipe_identity"], path=f"{path}.recipe_identity"
            ),
            int_value(data["attempt"], f"{path}.attempt", minimum=1),
            optional_identity("predecessor_attempt_identity"),
            optional_identity("source_predecessor_identity"),
        )


@dataclass(frozen=True, slots=True)
class StandardLifecycleCheckpoint:
    """CAS-backed source snapshot plus append-only attempt and stage lineage."""

    attempt: int
    attempt_evidence: StandardLifecycleAttemptEvidence
    attempt_sequence: int
    sequence: int
    predecessor_identity: ContentIdentity | None
    evidence: StandardLifecycleStageEvidence
    source_files: tuple[CachedSourceFile, ...]

    SCHEMA: ClassVar[str] = STANDARD_LIFECYCLE_CHECKPOINT_SCHEMA

    def __post_init__(self) -> None:
        int_value(self.attempt, "StandardLifecycleCheckpoint.attempt", minimum=1)
        if (
            not isinstance(self.attempt_evidence, StandardLifecycleAttemptEvidence)
            or self.attempt_evidence.attempt != self.attempt
        ):
            fail(
                "StandardLifecycleCheckpoint.attempt_evidence",
                "must bind the exact checkpoint attempt",
            )
        int_value(
            self.attempt_sequence,
            "StandardLifecycleCheckpoint.attempt_sequence",
            minimum=1,
        )
        int_value(self.sequence, "StandardLifecycleCheckpoint.sequence", minimum=1)
        if self.predecessor_identity is not None:
            _identity(
                self.predecessor_identity,
                "StandardLifecycleCheckpoint.predecessor_identity",
            )
        if self.sequence == 1 and self.predecessor_identity is not None:
            fail(
                "StandardLifecycleCheckpoint.predecessor_identity",
                "must be absent for the first checkpoint",
            )
        if self.sequence > 1 and self.predecessor_identity is None:
            fail(
                "StandardLifecycleCheckpoint.predecessor_identity",
                "must bind the previous checkpoint",
            )
        if self.attempt_sequence == 1 and (
            self.attempt_evidence.source_predecessor_identity
            != self.predecessor_identity
        ):
            fail(
                "StandardLifecycleCheckpoint.predecessor_identity",
                "must equal the attempt's exact source predecessor",
            )
        if self.attempt_sequence > 1 and self.predecessor_identity is None:
            fail(
                "StandardLifecycleCheckpoint.predecessor_identity",
                "later attempt stages must bind their preceding stage",
            )
        if not isinstance(self.evidence, StandardLifecycleStageEvidence):
            fail("StandardLifecycleCheckpoint.evidence", "must be typed")
        if (
            self.attempt_evidence.execution_plan_identity
            != self.evidence.execution_plan_identity
            or self.attempt_evidence.component_revision
            != self.evidence.component_revision
            or self.attempt_evidence.generation_plan_identity
            != self.evidence.generation_plan_identity
            or self.attempt_evidence.generation_key_identity
            != self.evidence.generation_key_identity
            or self.attempt_evidence.recipe_identity != self.evidence.recipe_identity
        ):
            fail(
                "StandardLifecycleCheckpoint.attempt_evidence",
                "must bind the same execution node as its stage evidence",
            )
        if (
            not isinstance(self.source_files, tuple)
            or not self.source_files
            or len(self.source_files) > 100_000
        ):
            fail(
                "StandardLifecycleCheckpoint.source_files",
                "must contain between 1 and 100000 files",
            )
        if any(not isinstance(item, CachedSourceFile) for item in self.source_files):
            fail(
                "StandardLifecycleCheckpoint.source_files",
                "must contain only CachedSourceFile values",
            )
        paths = tuple(item.path for item in self.source_files)
        if paths != tuple(sorted(set(paths))):
            fail(
                "StandardLifecycleCheckpoint.source_files",
                "must use unique canonical path order",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "attempt": self.attempt,
            "attempt_evidence": self.attempt_evidence.to_dict(),
            "attempt_sequence": self.attempt_sequence,
            "sequence": self.sequence,
            "predecessor_identity": (
                None
                if self.predecessor_identity is None
                else self.predecessor_identity.to_dict()
            ),
            "evidence": self.evidence.to_dict(),
            "source_files": [item.to_dict() for item in self.source_files],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardLifecycleCheckpoint"
    ) -> StandardLifecycleCheckpoint:
        names = frozenset(
            {
                "attempt",
                "attempt_evidence",
                "attempt_sequence",
                "sequence",
                "predecessor_identity",
                "evidence",
                "source_files",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        predecessor = data["predecessor_identity"]
        return cls(
            int_value(data["attempt"], f"{path}.attempt", minimum=1),
            StandardLifecycleAttemptEvidence.from_dict(
                data["attempt_evidence"], path=f"{path}.attempt_evidence"
            ),
            int_value(
                data["attempt_sequence"],
                f"{path}.attempt_sequence",
                minimum=1,
            ),
            int_value(data["sequence"], f"{path}.sequence", minimum=1),
            (
                None
                if predecessor is None
                else ContentIdentity.from_dict(
                    predecessor, path=f"{path}.predecessor_identity"
                )
            ),
            StandardLifecycleStageEvidence.from_dict(
                data["evidence"], path=f"{path}.evidence"
            ),
            parse_tuple(
                data["source_files"],
                f"{path}.source_files",
                CachedSourceFile.from_dict,
            ),
        )


__all__ = [
    "STANDARD_LIFECYCLE_CHECKPOINT_SCHEMA",
    "STANDARD_LIFECYCLE_ATTEMPT_EVIDENCE_SCHEMA",
    "STANDARD_LIFECYCLE_STAGE_EVIDENCE_SCHEMA",
    "STANDARD_LIFECYCLE_STAGES",
    "StandardLifecycleCheckpoint",
    "StandardLifecycleAttemptEvidence",
    "StandardLifecycleCheckpointOutcome",
    "StandardLifecycleStage",
    "StandardLifecycleStageEvidence",
]

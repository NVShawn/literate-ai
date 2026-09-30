"""Bounded, content-addressed replacement-candidate attempt chains."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    enum_value,
    fail,
    int_value,
    parse_tuple,
    string_value,
)
from ..identity import ContentIdentity, contract_identity
from ..standard_lifecycle_membership import StandardNodeFailurePhase
from ._common import identity, portable_name

CANDIDATE_REPAIR_DIAGNOSTIC_SCHEMA = (
    "urn:literate-ai:schema:v2:candidate-repair-diagnostic"
)
CANDIDATE_REPAIR_REQUEST_SCHEMA = "urn:literate-ai:schema:v2:candidate-repair-request"
CANDIDATE_ATTEMPT_SCHEMA = "urn:literate-ai:schema:v2:candidate-attempt"
CANDIDATE_ATTEMPT_CHAIN_SCHEMA = "urn:literate-ai:schema:v2:candidate-attempt-chain"

_ABSOLUTE_PATH = re.compile(r"(?:^|\s)(?:/|[A-Za-z]:[\\/]|\\\\)")
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|bearer|password|private[_-]?key)"
)


class CandidateFailureClassification(StrEnum):
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


class CandidateAttemptDisposition(StrEnum):
    ACCEPTED = "accepted"
    RETRYABLE_REJECTED = "retryable-rejected"
    TERMINAL_REJECTED = "terminal-rejected"


class CandidateAttemptChainDisposition(StrEnum):
    ACCEPTED = "accepted"
    TERMINAL = "terminal"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class CandidateRepairDiagnostic:
    component_revision: ContentIdentity
    phase: StandardNodeFailurePhase
    code: str
    sanitized_text: str
    classification: CandidateFailureClassification
    failure_evidence_identity: ContentIdentity

    SCHEMA: ClassVar[str] = CANDIDATE_REPAIR_DIAGNOSTIC_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_revision, "CandidateRepairDiagnostic.component_revision"
        )
        identity(
            self.failure_evidence_identity,
            "CandidateRepairDiagnostic.failure_evidence_identity",
        )
        if not isinstance(self.phase, StandardNodeFailurePhase):
            fail(
                "CandidateRepairDiagnostic.phase", "must be a StandardNodeFailurePhase"
            )
        portable_name(self.code, "CandidateRepairDiagnostic.code")
        text = string_value(
            self.sanitized_text,
            "CandidateRepairDiagnostic.sanitized_text",
            max_length=8192,
        )
        if any(ord(character) < 32 and character not in "\n\t" for character in text):
            fail("CandidateRepairDiagnostic.sanitized_text", "contains control bytes")
        if _ABSOLUTE_PATH.search(text) or _SECRET.search(text):
            fail(
                "CandidateRepairDiagnostic.sanitized_text",
                "must exclude absolute paths and secret-bearing material",
            )
        if not isinstance(self.classification, CandidateFailureClassification):
            fail("CandidateRepairDiagnostic.classification", "must be typed")
        if (
            self.classification is CandidateFailureClassification.RETRYABLE
            and self.phase
            not in {
                StandardNodeFailurePhase.BUILD,
                StandardNodeFailurePhase.TEST,
            }
        ):
            fail(
                "CandidateRepairDiagnostic.classification",
                "only compiler/linker or generated-test failures may be retryable",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "phase": self.phase.value,
            "code": self.code,
            "sanitized_text": self.sanitized_text,
            "classification": self.classification.value,
            "failure_evidence_identity": self.failure_evidence_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CandidateRepairDiagnostic"
    ) -> CandidateRepairDiagnostic:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "phase",
                    "code",
                    "sanitized_text",
                    "classification",
                    "failure_evidence_identity",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            enum_value(StandardNodeFailurePhase, data["phase"], f"{path}.phase"),
            portable_name(data["code"], f"{path}.code"),
            string_value(
                data["sanitized_text"], f"{path}.sanitized_text", max_length=8192
            ),
            enum_value(
                CandidateFailureClassification,
                data["classification"],
                f"{path}.classification",
            ),
            ContentIdentity.from_dict(
                data["failure_evidence_identity"],
                path=f"{path}.failure_evidence_identity",
            ),
        )


@dataclass(frozen=True, slots=True)
class CandidateRepairRequest:
    component_revision: ContentIdentity
    attempt_index: int
    original_recipe_identity: ContentIdentity
    predecessor_attempt_identities: tuple[ContentIdentity, ...]
    diagnostic_identity: ContentIdentity
    bounded_generation_request_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity

    SCHEMA: ClassVar[str] = CANDIDATE_REPAIR_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "original_recipe_identity",
            "diagnostic_identity",
            "bounded_generation_request_identity",
            "workspace_allocation_identity",
        ):
            identity(getattr(self, name), f"CandidateRepairRequest.{name}")
        int_value(
            self.attempt_index,
            "CandidateRepairRequest.attempt_index",
            minimum=1,
            maximum=2,
        )
        if len(self.predecessor_attempt_identities) != self.attempt_index:
            fail(
                "CandidateRepairRequest.predecessor_attempt_identities",
                "must contain the exact predecessor sequence",
            )
        for index, value in enumerate(self.predecessor_attempt_identities):
            identity(
                value, f"CandidateRepairRequest.predecessor_attempt_identities[{index}]"
            )
        if len({item.uri for item in self.predecessor_attempt_identities}) != len(
            self.predecessor_attempt_identities
        ):
            fail(
                "CandidateRepairRequest.predecessor_attempt_identities",
                "must be unique",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "attempt_index": self.attempt_index,
            "original_recipe_identity": self.original_recipe_identity.to_dict(),
            "predecessor_attempt_identities": [
                item.to_dict() for item in self.predecessor_attempt_identities
            ],
            "diagnostic_identity": self.diagnostic_identity.to_dict(),
            "bounded_generation_request_identity": (
                self.bounded_generation_request_identity.to_dict()
            ),
            "workspace_allocation_identity": (
                self.workspace_allocation_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CandidateRepairRequest"
    ) -> CandidateRepairRequest:
        names = frozenset(
            {
                "component_revision",
                "attempt_index",
                "original_recipe_identity",
                "predecessor_attempt_identities",
                "diagnostic_identity",
                "bounded_generation_request_identity",
                "workspace_allocation_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            int_value(
                data["attempt_index"], f"{path}.attempt_index", minimum=1, maximum=2
            ),
            ContentIdentity.from_dict(
                data["original_recipe_identity"],
                path=f"{path}.original_recipe_identity",
            ),
            parse_tuple(
                data["predecessor_attempt_identities"],
                f"{path}.predecessor_attempt_identities",
                ContentIdentity.from_dict,
            ),
            ContentIdentity.from_dict(
                data["diagnostic_identity"], path=f"{path}.diagnostic_identity"
            ),
            ContentIdentity.from_dict(
                data["bounded_generation_request_identity"],
                path=f"{path}.bounded_generation_request_identity",
            ),
            ContentIdentity.from_dict(
                data["workspace_allocation_identity"],
                path=f"{path}.workspace_allocation_identity",
            ),
        )


@dataclass(frozen=True, slots=True)
class CandidateAttempt:
    component_revision: ContentIdentity
    attempt_index: int
    generation_request_identity: ContentIdentity
    generation_response_identity: ContentIdentity
    complete_tree_identity: ContentIdentity
    source_bundle_identity: ContentIdentity
    workspace_allocation_identity: ContentIdentity
    lifecycle_result_identity: ContentIdentity
    predecessor_attempt_identities: tuple[ContentIdentity, ...]
    disposition: CandidateAttemptDisposition
    diagnostic: CandidateRepairDiagnostic | None
    repair_request: CandidateRepairRequest | None = None

    SCHEMA: ClassVar[str] = CANDIDATE_ATTEMPT_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_request_identity",
            "generation_response_identity",
            "complete_tree_identity",
            "source_bundle_identity",
            "workspace_allocation_identity",
            "lifecycle_result_identity",
        ):
            identity(getattr(self, name), f"CandidateAttempt.{name}")
        int_value(self.attempt_index, "CandidateAttempt.attempt_index", maximum=2)
        if len(self.predecessor_attempt_identities) != self.attempt_index:
            fail(
                "CandidateAttempt.predecessor_attempt_identities",
                "must contain the exact predecessor sequence",
            )
        for index, value in enumerate(self.predecessor_attempt_identities):
            identity(value, f"CandidateAttempt.predecessor_attempt_identities[{index}]")
        if self.attempt_index == 0 and self.repair_request is not None:
            fail(
                "CandidateAttempt.repair_request",
                "initial attempt cannot claim a repair request",
            )
        if self.attempt_index > 0:
            if not isinstance(self.repair_request, CandidateRepairRequest):
                fail(
                    "CandidateAttempt.repair_request",
                    "replacement attempt requires a typed repair request",
                )
            if (
                self.repair_request.component_revision != self.component_revision
                or self.repair_request.attempt_index != self.attempt_index
                or self.repair_request.predecessor_attempt_identities
                != self.predecessor_attempt_identities
                or self.repair_request.bounded_generation_request_identity
                != self.generation_request_identity
                or self.repair_request.workspace_allocation_identity
                != self.workspace_allocation_identity
            ):
                fail(
                    "CandidateAttempt.repair_request",
                    "must bind this exact replacement attempt",
                )
        if not isinstance(self.disposition, CandidateAttemptDisposition):
            fail("CandidateAttempt.disposition", "must be typed")
        rejected = self.disposition is not CandidateAttemptDisposition.ACCEPTED
        if rejected != (self.diagnostic is not None):
            fail(
                "CandidateAttempt.diagnostic",
                "must exist exactly for rejected attempts",
            )
        if self.diagnostic is not None:
            if self.diagnostic.component_revision != self.component_revision:
                fail("CandidateAttempt.diagnostic", "names another Component")
            retryable = (
                self.diagnostic.classification
                is CandidateFailureClassification.RETRYABLE
            )
            if retryable != (
                self.disposition is CandidateAttemptDisposition.RETRYABLE_REJECTED
            ):
                fail(
                    "CandidateAttempt.disposition",
                    "must match diagnostic classification",
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
                    "component_revision",
                    "generation_request_identity",
                    "generation_response_identity",
                    "complete_tree_identity",
                    "source_bundle_identity",
                    "workspace_allocation_identity",
                    "lifecycle_result_identity",
                )
            },
            "attempt_index": self.attempt_index,
            "predecessor_attempt_identities": [
                item.to_dict() for item in self.predecessor_attempt_identities
            ],
            "disposition": self.disposition.value,
            "diagnostic": None
            if self.diagnostic is None
            else self.diagnostic.to_dict(),
            "repair_request": None
            if self.repair_request is None
            else self.repair_request.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CandidateAttempt"
    ) -> CandidateAttempt:
        names = frozenset(
            {
                "component_revision",
                "attempt_index",
                "generation_request_identity",
                "generation_response_identity",
                "complete_tree_identity",
                "source_bundle_identity",
                "workspace_allocation_identity",
                "lifecycle_result_identity",
                "predecessor_attempt_identities",
                "disposition",
                "diagnostic",
                "repair_request",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            attempt_index=int_value(
                data["attempt_index"], f"{path}.attempt_index", maximum=2
            ),
            generation_request_identity=ContentIdentity.from_dict(
                data["generation_request_identity"],
                path=f"{path}.generation_request_identity",
            ),
            generation_response_identity=ContentIdentity.from_dict(
                data["generation_response_identity"],
                path=f"{path}.generation_response_identity",
            ),
            complete_tree_identity=ContentIdentity.from_dict(
                data["complete_tree_identity"], path=f"{path}.complete_tree_identity"
            ),
            source_bundle_identity=ContentIdentity.from_dict(
                data["source_bundle_identity"], path=f"{path}.source_bundle_identity"
            ),
            workspace_allocation_identity=ContentIdentity.from_dict(
                data["workspace_allocation_identity"],
                path=f"{path}.workspace_allocation_identity",
            ),
            lifecycle_result_identity=ContentIdentity.from_dict(
                data["lifecycle_result_identity"],
                path=f"{path}.lifecycle_result_identity",
            ),
            predecessor_attempt_identities=parse_tuple(
                data["predecessor_attempt_identities"],
                f"{path}.predecessor_attempt_identities",
                ContentIdentity.from_dict,
            ),
            disposition=enum_value(
                CandidateAttemptDisposition, data["disposition"], f"{path}.disposition"
            ),
            diagnostic=None
            if data["diagnostic"] is None
            else CandidateRepairDiagnostic.from_dict(
                data["diagnostic"], path=f"{path}.diagnostic"
            ),
            repair_request=None
            if data["repair_request"] is None
            else CandidateRepairRequest.from_dict(
                data["repair_request"], path=f"{path}.repair_request"
            ),
        )


@dataclass(frozen=True, slots=True)
class CandidateAttemptChain:
    component_revision: ContentIdentity
    attempts: tuple[CandidateAttempt, ...]
    disposition: CandidateAttemptChainDisposition

    SCHEMA: ClassVar[str] = CANDIDATE_ATTEMPT_CHAIN_SCHEMA

    def __post_init__(self) -> None:
        identity(self.component_revision, "CandidateAttemptChain.component_revision")
        if not 1 <= len(self.attempts) <= 3:
            fail(
                "CandidateAttemptChain.attempts",
                "must contain one initial attempt and at most two repairs",
            )
        for index, attempt in enumerate(self.attempts):
            if (
                not isinstance(attempt, CandidateAttempt)
                or attempt.component_revision != self.component_revision
            ):
                fail(
                    "CandidateAttemptChain.attempts",
                    "must contain typed attempts for one Component",
                )
            if (
                attempt.attempt_index != index
                or attempt.predecessor_attempt_identities
                != tuple(item.identity for item in self.attempts[:index])
            ):
                fail(
                    "CandidateAttemptChain.attempts",
                    "must preserve the exact predecessor sequence",
                )
            if (
                index < len(self.attempts) - 1
                and attempt.disposition
                is not CandidateAttemptDisposition.RETRYABLE_REJECTED
            ):
                fail(
                    "CandidateAttemptChain.attempts",
                    "only retryable rejection may have a successor",
                )
            if index > 0:
                prior_diagnostic = self.attempts[index - 1].diagnostic
                if (
                    prior_diagnostic is None
                    or attempt.repair_request is None
                    or attempt.repair_request.diagnostic_identity
                    != prior_diagnostic.identity
                ):
                    fail(
                        "CandidateAttemptChain.attempts",
                        "replacement request must bind the exact preceding diagnostic",
                    )
        if len(
            {item.workspace_allocation_identity.uri for item in self.attempts}
        ) != len(self.attempts):
            fail(
                "CandidateAttemptChain.attempts",
                "every attempt requires a fresh workspace",
            )
        if not isinstance(self.disposition, CandidateAttemptChainDisposition):
            fail("CandidateAttemptChain.disposition", "must be typed")
        last = self.attempts[-1]
        expected = (
            CandidateAttemptChainDisposition.ACCEPTED
            if last.disposition is CandidateAttemptDisposition.ACCEPTED
            else CandidateAttemptChainDisposition.EXHAUSTED
            if last.disposition is CandidateAttemptDisposition.RETRYABLE_REJECTED
            and len(self.attempts) == 3
            else CandidateAttemptChainDisposition.TERMINAL
        )
        if self.disposition is not expected:
            fail(
                "CandidateAttemptChain.disposition",
                "does not match the final bounded attempt",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "attempts": [item.to_dict() for item in self.attempts],
            "disposition": self.disposition.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CandidateAttemptChain"
    ) -> CandidateAttemptChain:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"component_revision", "attempts", "disposition"}),
        )
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            parse_tuple(
                data["attempts"], f"{path}.attempts", CandidateAttempt.from_dict
            ),
            enum_value(
                CandidateAttemptChainDisposition,
                data["disposition"],
                f"{path}.disposition",
            ),
        )


__all__ = [
    "CandidateAttempt",
    "CandidateAttemptChain",
    "CandidateAttemptChainDisposition",
    "CandidateAttemptDisposition",
    "CandidateFailureClassification",
    "CandidateRepairDiagnostic",
    "CandidateRepairRequest",
]

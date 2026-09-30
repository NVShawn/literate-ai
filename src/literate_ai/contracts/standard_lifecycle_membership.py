"""Exact cache-decision, lifecycle-membership, and aggregate-receipt contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    optional_string,
    parse_tuple,
)
from .identity import ContentIdentity, contract_identity

STANDARD_PLANNED_LIFECYCLE_NODE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-planned-lifecycle-node"
)
STANDARD_NODE_CACHE_DECISION_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-node-cache-decision"
)
STANDARD_PROJECT_LIFECYCLE_MEMBERSHIP_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-project-lifecycle-membership"
)
STANDARD_AGGREGATE_RECEIPT_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-aggregate-receipt"
)
STANDARD_NODE_FAILURE_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-node-failure-evidence"
)

_FAILURE_CODE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:\\|/(?:[^/\s]+/)+)")
_SECRET = re.compile(
    r"(?i)(?:authorization|cookie|credential|password|secret|token)"
    r"[\"'\s:=]+(?!<redacted>(?:\s|$))\S+"
)


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _optional_identity(value: object, path: str) -> ContentIdentity | None:
    if value is None:
        return None
    return _identity(value, path)


def _parse_optional_identity(value: object, path: str) -> ContentIdentity | None:
    if value is None:
        return None
    return ContentIdentity.from_dict(value, path=path)


class StandardNodeCacheOutcome(StrEnum):
    HIT = "hit"
    MISS = "miss"
    FORCED_REGENERATION = "forced-regeneration"


class StandardNodeFailurePhase(StrEnum):
    DEPENDENCY = "dependency"
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
    LIFECYCLE = "lifecycle"


@dataclass(frozen=True, slots=True)
class StandardNodeFailureEvidence:
    """Portable failure evidence with one bounded, explicitly redacted diagnostic."""

    component_revision: ContentIdentity
    phase: StandardNodeFailurePhase
    code: str
    subject_identity: ContentIdentity
    diagnostic: str | None = None

    SCHEMA: ClassVar[str] = STANDARD_NODE_FAILURE_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        _identity(
            self.component_revision, "StandardNodeFailureEvidence.component_revision"
        )
        _identity(self.subject_identity, "StandardNodeFailureEvidence.subject_identity")
        if not isinstance(self.phase, StandardNodeFailurePhase):
            fail(
                "StandardNodeFailureEvidence.phase",
                "must be a StandardNodeFailurePhase",
            )
        if not isinstance(self.code, str) or _FAILURE_CODE.fullmatch(self.code) is None:
            fail(
                "StandardNodeFailureEvidence.code",
                "must be a lowercase portable code of at most 128 characters",
            )
        if self.diagnostic is not None:
            if (
                not isinstance(self.diagnostic, str)
                or not self.diagnostic
                or len(self.diagnostic) > 8192
                or _PRIVATE_PATH.search(self.diagnostic)
                or _SECRET.search(self.diagnostic)
            ):
                fail(
                    "StandardNodeFailureEvidence.diagnostic",
                    "must be bounded and exclude secrets and private paths",
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
            "subject_identity": self.subject_identity.to_dict(),
            "diagnostic": self.diagnostic,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardNodeFailureEvidence"
    ) -> StandardNodeFailureEvidence:
        names = frozenset({"component_revision", "phase", "code", "subject_identity"})
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names,
            optional=frozenset({"diagnostic"}),
        )
        return cls(
            ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            enum_value(StandardNodeFailurePhase, data["phase"], f"{path}.phase"),
            data["code"],
            ContentIdentity.from_dict(
                data["subject_identity"], path=f"{path}.subject_identity"
            ),
            optional_string(data.get("diagnostic"), f"{path}.diagnostic"),
        )


@dataclass(frozen=True, slots=True)
class StandardPlannedLifecycleNode:
    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity

    SCHEMA: ClassVar[str] = STANDARD_PLANNED_LIFECYCLE_NODE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
        ):
            _identity(getattr(self, name), f"StandardPlannedLifecycleNode.{name}")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generation_plan_identity": self.generation_plan_identity.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardPlannedLifecycleNode"
    ) -> StandardPlannedLifecycleNode:
        names = frozenset(
            {
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
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
class StandardNodeCacheDecision:
    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    outcome: StandardNodeCacheOutcome
    input_membership_identity: ContentIdentity | None
    lifecycle_result_identity: ContentIdentity
    accepted_membership_identity: ContentIdentity | None
    publication_identity: ContentIdentity | None
    failure_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = STANDARD_NODE_CACHE_DECISION_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
            "lifecycle_result_identity",
        ):
            _identity(getattr(self, name), f"StandardNodeCacheDecision.{name}")
        if not isinstance(self.outcome, StandardNodeCacheOutcome):
            fail(
                "StandardNodeCacheDecision.outcome",
                "must be a StandardNodeCacheOutcome",
            )
        for name in (
            "input_membership_identity",
            "accepted_membership_identity",
            "publication_identity",
            "failure_identity",
        ):
            _optional_identity(getattr(self, name), f"StandardNodeCacheDecision.{name}")
        hit = self.outcome is StandardNodeCacheOutcome.HIT
        if hit != (self.input_membership_identity is not None):
            fail(
                "StandardNodeCacheDecision.input_membership_identity",
                "must exist exactly for a cache hit",
            )
        if self.accepted_membership_identity is None:
            publication_valid = self.publication_identity is None
        elif hit:
            publication_valid = self.publication_identity is None
        else:
            publication_valid = self.publication_identity in {
                None,
                self.accepted_membership_identity,
            }
        if not publication_valid:
            fail(
                "StandardNodeCacheDecision.publication_identity",
                "must be absent until project acceptance or identify the exact "
                "accepted non-hit membership",
            )
        if self.failure_identity is not None and (
            self.accepted_membership_identity is not None
            or self.publication_identity is not None
        ):
            fail(
                "StandardNodeCacheDecision.failure_identity",
                "failed nodes cannot claim acceptance or publication",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def planned_node(self) -> StandardPlannedLifecycleNode:
        return StandardPlannedLifecycleNode(
            self.component_revision,
            self.generation_plan_identity,
            self.generation_key_identity,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generation_plan_identity": self.generation_plan_identity.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "outcome": self.outcome.value,
            "input_membership_identity": (
                None
                if self.input_membership_identity is None
                else self.input_membership_identity.to_dict()
            ),
            "lifecycle_result_identity": self.lifecycle_result_identity.to_dict(),
            "accepted_membership_identity": (
                None
                if self.accepted_membership_identity is None
                else self.accepted_membership_identity.to_dict()
            ),
            "publication_identity": (
                None
                if self.publication_identity is None
                else self.publication_identity.to_dict()
            ),
            "failure_identity": (
                None
                if self.failure_identity is None
                else self.failure_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardNodeCacheDecision"
    ) -> StandardNodeCacheDecision:
        names = frozenset(
            {
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
                "outcome",
                "input_membership_identity",
                "lifecycle_result_identity",
                "accepted_membership_identity",
                "publication_identity",
                "failure_identity",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
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
            enum_value(StandardNodeCacheOutcome, data["outcome"], f"{path}.outcome"),
            _parse_optional_identity(
                data["input_membership_identity"],
                f"{path}.input_membership_identity",
            ),
            ContentIdentity.from_dict(
                data["lifecycle_result_identity"],
                path=f"{path}.lifecycle_result_identity",
            ),
            _parse_optional_identity(
                data["accepted_membership_identity"],
                f"{path}.accepted_membership_identity",
            ),
            _parse_optional_identity(
                data["publication_identity"], f"{path}.publication_identity"
            ),
            _parse_optional_identity(
                data["failure_identity"], f"{path}.failure_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardProjectLifecycleMembership:
    execution_plan_identity: ContentIdentity
    planned_nodes: tuple[StandardPlannedLifecycleNode, ...]
    cache_decisions: tuple[StandardNodeCacheDecision, ...]

    SCHEMA: ClassVar[str] = STANDARD_PROJECT_LIFECYCLE_MEMBERSHIP_SCHEMA

    def __post_init__(self) -> None:
        _identity(
            self.execution_plan_identity,
            "StandardProjectLifecycleMembership.execution_plan_identity",
        )
        for values, expected, path in (
            (self.planned_nodes, StandardPlannedLifecycleNode, "planned_nodes"),
            (self.cache_decisions, StandardNodeCacheDecision, "cache_decisions"),
        ):
            if not values or any(not isinstance(item, expected) for item in values):
                fail(
                    f"StandardProjectLifecycleMembership.{path}",
                    f"must be a non-empty tuple of {expected.__name__}",
                )
        planned_uris = tuple(item.component_revision.uri for item in self.planned_nodes)
        decision_uris = tuple(
            item.component_revision.uri for item in self.cache_decisions
        )
        canonical = tuple(sorted(set(planned_uris)))
        if planned_uris != canonical or decision_uris != canonical:
            fail(
                "StandardProjectLifecycleMembership",
                "plan nodes and cache decisions must have exact canonical membership",
            )
        if (
            tuple(item.planned_node for item in self.cache_decisions)
            != self.planned_nodes
        ):
            fail(
                "StandardProjectLifecycleMembership.cache_decisions",
                "must bind every exact planned node without substitution",
            )
        result_uris = tuple(
            item.lifecycle_result_identity.uri for item in self.cache_decisions
        )
        if len(set(result_uris)) != len(result_uris):
            fail(
                "StandardProjectLifecycleMembership.cache_decisions",
                "lifecycle result identities must be unique",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def lifecycle_result_identities(self) -> tuple[ContentIdentity, ...]:
        return tuple(item.lifecycle_result_identity for item in self.cache_decisions)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "planned_nodes": [item.to_dict() for item in self.planned_nodes],
            "cache_decisions": [item.to_dict() for item in self.cache_decisions],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardProjectLifecycleMembership"
    ) -> StandardProjectLifecycleMembership:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"execution_plan_identity", "planned_nodes", "cache_decisions"}
            ),
        )
        return cls(
            ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            parse_tuple(
                data["planned_nodes"],
                f"{path}.planned_nodes",
                StandardPlannedLifecycleNode.from_dict,
            ),
            parse_tuple(
                data["cache_decisions"],
                f"{path}.cache_decisions",
                StandardNodeCacheDecision.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardAggregateReceipt:
    execution_plan_identity: ContentIdentity
    lifecycle_membership_identity: ContentIdentity
    lifecycle_result_identities: tuple[ContentIdentity, ...]
    admission_identity: ContentIdentity
    context_prompt_journal_identities: tuple[ContentIdentity, ...]
    context_benchmark_record_identities: tuple[ContentIdentity, ...]
    context_cache_report_identity: ContentIdentity
    root_integration_evidence_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = STANDARD_AGGREGATE_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "execution_plan_identity",
            "lifecycle_membership_identity",
            "admission_identity",
            "context_cache_report_identity",
        ):
            _identity(getattr(self, name), f"StandardAggregateReceipt.{name}")
        _optional_identity(
            self.root_integration_evidence_identity,
            "StandardAggregateReceipt.root_integration_evidence_identity",
        )
        if not self.lifecycle_result_identities:
            fail(
                "StandardAggregateReceipt.lifecycle_result_identities",
                "must not be empty",
            )
        for index, identity in enumerate(self.lifecycle_result_identities):
            _identity(
                identity,
                f"StandardAggregateReceipt.lifecycle_result_identities[{index}]",
            )
        uris = tuple(item.uri for item in self.lifecycle_result_identities)
        if len(set(uris)) != len(uris):
            fail(
                "StandardAggregateReceipt.lifecycle_result_identities",
                "must be unique",
            )
        for name in (
            "context_prompt_journal_identities",
            "context_benchmark_record_identities",
        ):
            values = getattr(self, name)
            if len(values) != len(self.lifecycle_result_identities):
                fail(
                    f"StandardAggregateReceipt.{name}",
                    "must cover every exact lifecycle result",
                )
            for index, value in enumerate(values):
                _identity(value, f"StandardAggregateReceipt.{name}[{index}]")
            if len({value.uri for value in values}) != len(values):
                fail(f"StandardAggregateReceipt.{name}", "must be unique")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "lifecycle_membership_identity": (
                self.lifecycle_membership_identity.to_dict()
            ),
            "lifecycle_result_identities": [
                item.to_dict() for item in self.lifecycle_result_identities
            ],
            "admission_identity": self.admission_identity.to_dict(),
            "context_prompt_journal_identities": [
                item.to_dict() for item in self.context_prompt_journal_identities
            ],
            "context_benchmark_record_identities": [
                item.to_dict() for item in self.context_benchmark_record_identities
            ],
            "context_cache_report_identity": (
                self.context_cache_report_identity.to_dict()
            ),
            "root_integration_evidence_identity": (
                None
                if self.root_integration_evidence_identity is None
                else self.root_integration_evidence_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardAggregateReceipt"
    ) -> StandardAggregateReceipt:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "execution_plan_identity",
                    "lifecycle_membership_identity",
                    "lifecycle_result_identities",
                    "admission_identity",
                    "context_prompt_journal_identities",
                    "context_benchmark_record_identities",
                    "context_cache_report_identity",
                }
            ),
            optional=frozenset({"root_integration_evidence_identity"}),
        )
        return cls(
            ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            ContentIdentity.from_dict(
                data["lifecycle_membership_identity"],
                path=f"{path}.lifecycle_membership_identity",
            ),
            parse_tuple(
                data["lifecycle_result_identities"],
                f"{path}.lifecycle_result_identities",
                ContentIdentity.from_dict,
            ),
            ContentIdentity.from_dict(
                data["admission_identity"], path=f"{path}.admission_identity"
            ),
            parse_tuple(
                data["context_prompt_journal_identities"],
                f"{path}.context_prompt_journal_identities",
                ContentIdentity.from_dict,
            ),
            parse_tuple(
                data["context_benchmark_record_identities"],
                f"{path}.context_benchmark_record_identities",
                ContentIdentity.from_dict,
            ),
            ContentIdentity.from_dict(
                data["context_cache_report_identity"],
                path=f"{path}.context_cache_report_identity",
            ),
            _parse_optional_identity(
                data.get("root_integration_evidence_identity"),
                f"{path}.root_integration_evidence_identity",
            ),
        )


__all__ = [
    "STANDARD_AGGREGATE_RECEIPT_SCHEMA",
    "STANDARD_NODE_CACHE_DECISION_SCHEMA",
    "STANDARD_NODE_FAILURE_EVIDENCE_SCHEMA",
    "STANDARD_PLANNED_LIFECYCLE_NODE_SCHEMA",
    "STANDARD_PROJECT_LIFECYCLE_MEMBERSHIP_SCHEMA",
    "StandardAggregateReceipt",
    "StandardNodeCacheDecision",
    "StandardNodeCacheOutcome",
    "StandardNodeFailureEvidence",
    "StandardNodeFailurePhase",
    "StandardPlannedLifecycleNode",
    "StandardProjectLifecycleMembership",
]

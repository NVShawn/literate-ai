"""Normalized, fail-closed semantic comparison for bidirectional qualification."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from .contracts import DraftScenario, DraftStatement, canonical_digest, canonical_value
from .errors import SourceToSpecificationError

SEMANTIC_REQUIREMENT_GRAPH_SCHEMA = (
    "urn:literate-ai:schema:v1:semantic-requirement-graph"
)
SEMANTIC_COMPARISON_OUTPUT_SCHEMA = (
    "urn:literate-ai:schema:v1:semantic-comparison-model-output"
)
SEMANTIC_COMPARISON_RESULT_SCHEMA = (
    "urn:literate-ai:schema:v1:semantic-comparison-result"
)

_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceToSpecificationError(
            "semantic_comparison.text_invalid", f"{field} must be non-empty text"
        )
    return value.strip()


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise SourceToSpecificationError(
            "semantic_comparison.array_invalid", f"{field} must be a string array"
        )
    normalized = tuple(item.strip() for item in value)
    if normalized != tuple(sorted(set(normalized))):
        raise SourceToSpecificationError(
            "semantic_comparison.array_invalid",
            f"{field} must be unique and canonically ordered",
        )
    return normalized


def _model_string_set(value: object, field: str) -> tuple[str, ...]:
    """Admit an unordered model-emitted set and canonicalize it at the boundary."""

    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise SourceToSpecificationError(
            "semantic_comparison.array_invalid", f"{field} must be a string array"
        )
    normalized = tuple(item.strip() for item in value)
    if len(normalized) != len(set(normalized)):
        raise SourceToSpecificationError(
            "semantic_comparison.array_invalid", f"{field} must be unique"
        )
    return tuple(sorted(normalized))


def _identity(value: object, field: str) -> str:
    normalized = _text(value, field)
    if not _IDENTITY.fullmatch(normalized):
        raise SourceToSpecificationError(
            "semantic_comparison.identity_invalid",
            f"{field} must be a sha256 content identity",
        )
    return normalized


class RequirementDimension(StrEnum):
    INPUTS = "inputs"
    OUTPUTS = "outputs"
    INVARIANTS = "invariants"
    NORMALIZATION = "normalization"
    AGGREGATION = "aggregation"
    ORDERING = "ordering"
    TIE_BREAKING = "tie-breaking"
    ERRORS = "errors"
    INVALID_INPUTS = "invalid-inputs"


class SemanticDisposition(StrEnum):
    EQUIVALENT = "equivalent"
    MISSING = "missing"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class NormalizedRequirement:
    requirement_id: str
    dimension: RequirementDimension
    statement: str
    scenarios: tuple[DraftScenario, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requirement_id", _text(self.requirement_id, "requirement_id")
        )
        object.__setattr__(self, "statement", _text(self.statement, "statement"))
        if not isinstance(self.dimension, RequirementDimension):
            raise SourceToSpecificationError(
                "semantic_comparison.dimension_invalid",
                "requirement dimension is unsupported",
            )
        if not self.scenarios or any(
            not isinstance(item, DraftScenario) for item in self.scenarios
        ):
            raise SourceToSpecificationError(
                "semantic_comparison.scenario_required",
                "each normalized requirement needs an observable scenario",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "dimension": self.dimension.value,
            "statement": self.statement,
            "scenarios": canonical_value(self.scenarios),
        }

    @classmethod
    def from_dict(cls, value: object) -> NormalizedRequirement:
        if not isinstance(value, Mapping) or set(value) != {
            "requirement_id",
            "dimension",
            "statement",
            "scenarios",
        }:
            raise SourceToSpecificationError(
                "semantic_comparison.requirement_invalid",
                "normalized requirement has missing or unknown fields",
            )
        raw_scenarios = value["scenarios"]
        if not isinstance(raw_scenarios, list):
            raise SourceToSpecificationError(
                "semantic_comparison.scenario_required", "scenarios must be an array"
            )
        if any(
            not isinstance(item, Mapping) or set(item) != {"name", "when", "then"}
            for item in raw_scenarios
        ):
            raise SourceToSpecificationError(
                "semantic_comparison.scenario_invalid",
                "every scenario must contain exactly name, when, and then",
            )
        try:
            dimension = RequirementDimension(value["dimension"])
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "semantic_comparison.dimension_invalid",
                "requirement dimension is unsupported",
            ) from exc
        return cls(
            requirement_id=_text(value["requirement_id"], "requirement_id"),
            dimension=dimension,
            statement=_text(value["statement"], "statement"),
            scenarios=tuple(
                DraftScenario(
                    _text(item.get("name"), "scenario.name"),
                    _text(item.get("when"), "scenario.when"),
                    _text(item.get("then"), "scenario.then"),
                )
                for item in raw_scenarios
            ),
        )


@dataclass(frozen=True, slots=True)
class NormalizedRequirementGraph:
    graph_id: str
    requirements: tuple[NormalizedRequirement, ...]

    SCHEMA: ClassVar[str] = SEMANTIC_REQUIREMENT_GRAPH_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", _text(self.graph_id, "graph_id"))
        ids = tuple(item.requirement_id for item in self.requirements)
        if not self.requirements or ids != tuple(sorted(set(ids))):
            raise SourceToSpecificationError(
                "semantic_comparison.graph_invalid",
                "requirements must be non-empty, unique, and canonically ordered",
            )
        missing = set(RequirementDimension) - {
            item.dimension for item in self.requirements
        }
        if missing:
            raise SourceToSpecificationError(
                "semantic_comparison.dimension_incomplete",
                "normalized graph omits required dimensions: "
                + ", ".join(sorted(item.value for item in missing)),
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "graph_id": self.graph_id,
            "requirements": [item.to_dict() for item in self.requirements],
        }

    @classmethod
    def from_dict(cls, value: object) -> NormalizedRequirementGraph:
        if (
            not isinstance(value, Mapping)
            or set(value)
            != {
                "schema",
                "graph_id",
                "requirements",
            }
            or value.get("schema") != cls.SCHEMA
        ):
            raise SourceToSpecificationError(
                "semantic_comparison.graph_invalid",
                "semantic requirement graph has an unsupported shape or schema",
            )
        raw = value["requirements"]
        if not isinstance(raw, list):
            raise SourceToSpecificationError(
                "semantic_comparison.graph_invalid", "requirements must be an array"
            )
        return cls(
            graph_id=_text(value["graph_id"], "graph_id"),
            requirements=tuple(NormalizedRequirement.from_dict(item) for item in raw),
        )


@dataclass(frozen=True, slots=True)
class RequirementMatch:
    requirement_id: str
    disposition: SemanticDisposition
    inverse_statement_ids: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requirement_id", _text(self.requirement_id, "requirement_id")
        )
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))
        if not isinstance(self.disposition, SemanticDisposition):
            raise SourceToSpecificationError(
                "semantic_comparison.disposition_invalid",
                "semantic disposition is unsupported",
            )
        if self.inverse_statement_ids != tuple(
            sorted(set(self.inverse_statement_ids))
        ) or any(not item.strip() for item in self.inverse_statement_ids):
            raise SourceToSpecificationError(
                "semantic_comparison.statement_binding_invalid",
                "inverse statement IDs must be unique and canonically ordered",
            )
        if (
            self.disposition is SemanticDisposition.EQUIVALENT
            and not self.inverse_statement_ids
        ):
            raise SourceToSpecificationError(
                "semantic_comparison.statement_binding_required",
                "equivalent requirements need at least one inverse statement",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "disposition": self.disposition.value,
            "inverse_statement_ids": list(self.inverse_statement_ids),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class SemanticComparison:
    reference_graph_identity: str
    inverse_draft_identity: str
    comparator_identity: str
    matches: tuple[RequirementMatch, ...]

    SCHEMA: ClassVar[str] = SEMANTIC_COMPARISON_RESULT_SCHEMA

    def __post_init__(self) -> None:
        for field in (
            "reference_graph_identity",
            "inverse_draft_identity",
            "comparator_identity",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    @property
    def complete(self) -> bool:
        return all(
            item.disposition is SemanticDisposition.EQUIVALENT for item in self.matches
        )

    def require_complete(self) -> None:
        failures = tuple(
            item
            for item in self.matches
            if item.disposition is not SemanticDisposition.EQUIVALENT
        )
        if failures:
            first = failures[0]
            raise SourceToSpecificationError(
                "semantic_comparison.incomplete",
                f"{first.requirement_id} is {first.disposition.value}: "
                f"{first.rationale}",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "reference_graph_identity": self.reference_graph_identity,
            "inverse_draft_identity": self.inverse_draft_identity,
            "comparator_identity": self.comparator_identity,
            "matches": [item.to_dict() for item in self.matches],
            "complete": self.complete,
        }


def semantic_comparison_from_response(
    *,
    response: object,
    graph: NormalizedRequirementGraph,
    inverse_draft_identity: str,
    inverse_statements: tuple[DraftStatement, ...],
    comparator_identity: str,
) -> SemanticComparison:
    """Strictly admit a complete mapping; no reference requirement may disappear."""

    if (
        not isinstance(response, Mapping)
        or set(response)
        != {
            "schema",
            "reference_graph_identity",
            "inverse_draft_identity",
            "matches",
        }
        or response.get("schema") != SEMANTIC_COMPARISON_OUTPUT_SCHEMA
    ):
        raise SourceToSpecificationError(
            "semantic_comparison.output_invalid",
            "semantic comparison response has missing or unknown fields",
        )
    inverse_draft_identity = _identity(inverse_draft_identity, "inverse_draft_identity")
    comparator_identity = _identity(comparator_identity, "comparator_identity")
    if (
        response.get("reference_graph_identity") != graph.identity
        or response.get("inverse_draft_identity") != inverse_draft_identity
    ):
        raise SourceToSpecificationError(
            "semantic_comparison.binding_mismatch",
            "semantic comparison describes another graph or inverse draft",
        )
    raw_matches = response["matches"]
    if not isinstance(raw_matches, list):
        raise SourceToSpecificationError(
            "semantic_comparison.output_invalid", "matches must be an array"
        )
    known_statements = {item.statement_id for item in inverse_statements}
    if len(known_statements) != len(inverse_statements):
        raise SourceToSpecificationError(
            "semantic_comparison.inverse_statement_invalid",
            "inverse statement IDs must be unique",
        )
    matches: list[RequirementMatch] = []
    for raw in raw_matches:
        if not isinstance(raw, Mapping) or set(raw) != {
            "requirement_id",
            "disposition",
            "inverse_statement_ids",
            "rationale",
        }:
            raise SourceToSpecificationError(
                "semantic_comparison.output_invalid",
                "semantic match has missing or unknown fields",
            )
        try:
            disposition = SemanticDisposition(raw["disposition"])
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "semantic_comparison.disposition_invalid",
                "semantic disposition is unsupported",
            ) from exc
        statement_ids = _model_string_set(
            raw["inverse_statement_ids"], "inverse_statement_ids"
        )
        if not set(statement_ids).issubset(known_statements):
            raise SourceToSpecificationError(
                "semantic_comparison.statement_unknown",
                "semantic match cites an unknown inverse statement",
            )
        matches.append(
            RequirementMatch(
                requirement_id=_text(raw["requirement_id"], "requirement_id"),
                disposition=disposition,
                inverse_statement_ids=statement_ids,
                rationale=_text(raw["rationale"], "rationale"),
            )
        )
    expected_ids = tuple(item.requirement_id for item in graph.requirements)
    if tuple(item.requirement_id for item in matches) != expected_ids:
        raise SourceToSpecificationError(
            "semantic_comparison.coverage_incomplete",
            "semantic comparison must map every reference requirement exactly once",
        )
    return SemanticComparison(
        graph.identity,
        inverse_draft_identity,
        comparator_identity,
        tuple(matches),
    )


__all__ = [
    "SEMANTIC_COMPARISON_OUTPUT_SCHEMA",
    "SEMANTIC_COMPARISON_RESULT_SCHEMA",
    "SEMANTIC_REQUIREMENT_GRAPH_SCHEMA",
    "NormalizedRequirement",
    "NormalizedRequirementGraph",
    "RequirementDimension",
    "RequirementMatch",
    "SemanticComparison",
    "SemanticDisposition",
    "semantic_comparison_from_response",
]

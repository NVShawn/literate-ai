"""Provider-neutral, immutable behavioral specification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    fail,
    fields,
    optional_string,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .identity import SCHEMA_PREFIX, ContentIdentity, contract_identity

SPECIFICATION_SET_SCHEMA = f"{SCHEMA_PREFIX}specification-set"


@dataclass(frozen=True, slots=True)
class SpecificationArtifact:
    uri: str
    identity: ContentIdentity

    def __post_init__(self) -> None:
        string_value(self.uri, "SpecificationArtifact.uri")

    def to_dict(self) -> dict[str, Any]:
        return {"uri": self.uri, "identity": self.identity.to_dict()}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SpecificationArtifact"
    ) -> SpecificationArtifact:
        data = fields(
            value,
            path=path,
            required=frozenset({"uri", "identity"}),
        )
        return cls(
            uri=string_value(data["uri"], f"{path}.uri"),
            identity=ContentIdentity.from_dict(
                data["identity"], path=f"{path}.identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class SpecificationScenario:
    scenario_id: str
    title: str
    given: tuple[str, ...]
    when: tuple[str, ...]
    then: tuple[str, ...]

    def __post_init__(self) -> None:
        string_value(self.scenario_id, "SpecificationScenario.scenario_id")
        string_value(self.title, "SpecificationScenario.title")
        if not self.when:
            fail("SpecificationScenario.when", "must contain at least one condition")
        if not self.then:
            fail("SpecificationScenario.then", "must contain at least one outcome")
        for field_name in ("given", "when", "then"):
            for index, item in enumerate(getattr(self, field_name)):
                string_value(item, f"SpecificationScenario.{field_name}[{index}]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "given": list(self.given),
            "when": list(self.when),
            "then": list(self.then),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SpecificationScenario"
    ) -> SpecificationScenario:
        data = fields(
            value,
            path=path,
            required=frozenset({"scenario_id", "title", "given", "when", "then"}),
        )
        return cls(
            scenario_id=string_value(data["scenario_id"], f"{path}.scenario_id"),
            title=string_value(data["title"], f"{path}.title"),
            given=string_tuple(data["given"], f"{path}.given"),
            when=string_tuple(data["when"], f"{path}.when"),
            then=string_tuple(data["then"], f"{path}.then"),
        )


@dataclass(frozen=True, slots=True)
class SpecificationRequirement:
    requirement_id: str
    title: str
    statement: str
    scenarios: tuple[SpecificationScenario, ...]

    def __post_init__(self) -> None:
        string_value(self.requirement_id, "SpecificationRequirement.requirement_id")
        string_value(self.title, "SpecificationRequirement.title")
        string_value(
            self.statement, "SpecificationRequirement.statement", max_length=65536
        )
        if not self.scenarios:
            fail("SpecificationRequirement.scenarios", "must not be empty")
        unique(
            tuple(scenario.scenario_id for scenario in self.scenarios),
            "SpecificationRequirement.scenarios",
            "scenario IDs",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "title": self.title,
            "statement": self.statement,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SpecificationRequirement"
    ) -> SpecificationRequirement:
        data = fields(
            value,
            path=path,
            required=frozenset({"requirement_id", "title", "statement", "scenarios"}),
        )
        return cls(
            requirement_id=string_value(
                data["requirement_id"], f"{path}.requirement_id"
            ),
            title=string_value(data["title"], f"{path}.title"),
            statement=string_value(
                data["statement"], f"{path}.statement", max_length=65536
            ),
            scenarios=parse_tuple(
                data["scenarios"],
                f"{path}.scenarios",
                SpecificationScenario.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class SpecificationSet:
    provider_kind: str
    provider_version: str
    artifacts: tuple[SpecificationArtifact, ...]
    requirements: tuple[SpecificationRequirement, ...]
    baseline_id: str | None = None
    active_change_id: str | None = None

    SCHEMA: ClassVar[str] = SPECIFICATION_SET_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.provider_kind, "SpecificationSet.provider_kind")
        string_value(self.provider_version, "SpecificationSet.provider_version")
        if not self.artifacts:
            fail("SpecificationSet.artifacts", "must not be empty")
        if not self.requirements:
            fail("SpecificationSet.requirements", "must not be empty")
        optional_string(self.baseline_id, "SpecificationSet.baseline_id")
        optional_string(self.active_change_id, "SpecificationSet.active_change_id")
        unique(
            tuple(artifact.uri for artifact in self.artifacts),
            "SpecificationSet.artifacts",
            "artifact URIs",
        )
        unique(
            tuple(requirement.requirement_id for requirement in self.requirements),
            "SpecificationSet.requirements",
            "requirement IDs",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "provider_kind": self.provider_kind,
            "provider_version": self.provider_version,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "requirements": [
                requirement.to_dict() for requirement in self.requirements
            ],
            "baseline_id": self.baseline_id,
            "active_change_id": self.active_change_id,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SpecificationSet"
    ) -> SpecificationSet:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "provider_kind",
                    "provider_version",
                    "artifacts",
                    "requirements",
                    "baseline_id",
                    "active_change_id",
                }
            ),
        )
        return cls(
            provider_kind=string_value(data["provider_kind"], f"{path}.provider_kind"),
            provider_version=string_value(
                data["provider_version"], f"{path}.provider_version"
            ),
            artifacts=parse_tuple(
                data["artifacts"], f"{path}.artifacts", SpecificationArtifact.from_dict
            ),
            requirements=parse_tuple(
                data["requirements"],
                f"{path}.requirements",
                SpecificationRequirement.from_dict,
            ),
            baseline_id=optional_string(data["baseline_id"], f"{path}.baseline_id"),
            active_change_id=optional_string(
                data["active_change_id"], f"{path}.active_change_id"
            ),
        )


__all__ = [
    "SPECIFICATION_SET_SCHEMA",
    "SpecificationArtifact",
    "SpecificationRequirement",
    "SpecificationScenario",
    "SpecificationSet",
]

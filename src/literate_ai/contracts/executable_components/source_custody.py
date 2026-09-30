"""Versioned host custody for independently generated Component source trees."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from .._validation import contract_fields, fail, parse_tuple, string_value
from ..identity import ContentIdentity, canonical_identity, contract_identity
from .source_generation import SourceGenerationNodeResult, SourceGenerationRunOutput

COMPONENT_SOURCE_WORKSPACE_CUSTODY_SCHEMA = (
    "urn:literate-ai:schema:v1:component-source-workspace-custody"
)
PROJECT_SOURCE_GENERATION_CUSTODY_SCHEMA = (
    "urn:literate-ai:schema:v1:project-source-generation-custody"
)


@dataclass(frozen=True, slots=True)
class ComponentSourceWorkspaceCustody:
    """One generated node's evidence and the host workspace retaining its bytes."""

    component_revision: ContentIdentity
    generation_plan_identity: ContentIdentity
    generation_key_identity: ContentIdentity
    workspace_identity: ContentIdentity
    workspace_locator: str
    result: SourceGenerationNodeResult
    output: SourceGenerationRunOutput | None

    SCHEMA: ClassVar[str] = COMPONENT_SOURCE_WORKSPACE_CUSTODY_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_plan_identity",
            "generation_key_identity",
            "workspace_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"ComponentSourceWorkspaceCustody.{name}", "must be an identity")
        string_value(
            self.workspace_locator,
            "ComponentSourceWorkspaceCustody.workspace_locator",
        )
        if not isinstance(self.result, SourceGenerationNodeResult):
            fail("ComponentSourceWorkspaceCustody.result", "must be typed")
        if (
            self.result.component_revision != self.component_revision
            or self.result.generation_plan_identity != self.generation_plan_identity
            or self.result.generation_key_identity != self.generation_key_identity
        ):
            fail(
                "ComponentSourceWorkspaceCustody.result",
                "must bind the exact Component plan and generation key",
            )
        expected_workspace_identity = canonical_identity(
            {
                "schema": "literate-ai/component-generation-workspace@1",
                "component_revision": self.component_revision.uri,
                "generation_plan_identity": self.generation_plan_identity.uri,
                "generation_key_identity": self.generation_key_identity.uri,
                "allocation_identity": self.result.workspace_allocation_identity.uri,
                "locator": self.workspace_locator,
                "workspace": "fresh-empty",
            }
        )
        if self.workspace_identity != expected_workspace_identity:
            fail(
                "ComponentSourceWorkspaceCustody.workspace_identity",
                "must identify the exact retained workspace locator and allocation",
            )
        successful = self.result.failure_code is None
        if successful != isinstance(self.output, SourceGenerationRunOutput):
            fail(
                "ComponentSourceWorkspaceCustody.output",
                "must be present exactly when source generation succeeded",
            )
        if self.output is not None and (
            self.output.candidate.component_revision != self.component_revision
            or self.output.candidate.component_generation_plan_identity
            != self.generation_plan_identity
            or self.output.candidate.generation_key_identity
            != self.generation_key_identity
            or self.output.candidate.workspace_allocation_identity
            != self.result.workspace_allocation_identity
            or self.output.candidate_identity != self.result.candidate_identity
            or self.output.provenance_identity != self.result.provenance_identity
        ):
            fail(
                "ComponentSourceWorkspaceCustody.output",
                "must bind the exact result and prepared workspace allocation",
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
            "workspace_identity": self.workspace_identity.to_dict(),
            "workspace_locator": self.workspace_locator,
            "result": self.result.to_dict(),
            "output": None if self.output is None else self.output.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentSourceWorkspaceCustody"
    ) -> ComponentSourceWorkspaceCustody:
        names = frozenset(
            {
                "component_revision",
                "generation_plan_identity",
                "generation_key_identity",
                "workspace_identity",
                "workspace_locator",
                "result",
                "output",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        output = data["output"]
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            generation_plan_identity=ContentIdentity.from_dict(
                data["generation_plan_identity"],
                path=f"{path}.generation_plan_identity",
            ),
            generation_key_identity=ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            workspace_identity=ContentIdentity.from_dict(
                data["workspace_identity"], path=f"{path}.workspace_identity"
            ),
            workspace_locator=string_value(
                data["workspace_locator"], f"{path}.workspace_locator"
            ),
            result=SourceGenerationNodeResult.from_dict(
                data["result"], path=f"{path}.result"
            ),
            output=(
                None
                if output is None
                else SourceGenerationRunOutput.from_dict(output, path=f"{path}.output")
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectSourceGenerationCustody:
    """Canonical source-only result for every Component in one execution plan."""

    execution_plan_identity: ContentIdentity
    invalidation_decision_identity: ContentIdentity
    root_revision: ContentIdentity
    components: tuple[ComponentSourceWorkspaceCustody, ...]

    SCHEMA: ClassVar[str] = PROJECT_SOURCE_GENERATION_CUSTODY_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "execution_plan_identity",
            "invalidation_decision_identity",
            "root_revision",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(f"ProjectSourceGenerationCustody.{name}", "must be an identity")
        if not self.components:
            fail("ProjectSourceGenerationCustody.components", "must not be empty")
        if any(
            not isinstance(item, ComponentSourceWorkspaceCustody)
            for item in self.components
        ):
            fail("ProjectSourceGenerationCustody.components", "must be typed")
        uris = tuple(item.component_revision.uri for item in self.components)
        if uris != tuple(sorted(set(uris))):
            fail(
                "ProjectSourceGenerationCustody.components",
                "must be unique and canonically ordered",
            )
        if self.root_revision.uri not in uris:
            fail(
                "ProjectSourceGenerationCustody.root_revision",
                "must identify one generated Component",
            )

    @property
    def successful(self) -> bool:
        return all(item.result.failure_code is None for item in self.components)

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
            "root_revision": self.root_revision.to_dict(),
            "components": [item.to_dict() for item in self.components],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectSourceGenerationCustody"
    ) -> ProjectSourceGenerationCustody:
        names = frozenset(
            {
                "execution_plan_identity",
                "invalidation_decision_identity",
                "root_revision",
                "components",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            execution_plan_identity=ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            invalidation_decision_identity=ContentIdentity.from_dict(
                data["invalidation_decision_identity"],
                path=f"{path}.invalidation_decision_identity",
            ),
            root_revision=ContentIdentity.from_dict(
                data["root_revision"], path=f"{path}.root_revision"
            ),
            components=parse_tuple(
                data["components"],
                f"{path}.components",
                ComponentSourceWorkspaceCustody.from_dict,
            ),
        )


__all__ = [
    "COMPONENT_SOURCE_WORKSPACE_CUSTODY_SCHEMA",
    "PROJECT_SOURCE_GENERATION_CUSTODY_SCHEMA",
    "ComponentSourceWorkspaceCustody",
    "ProjectSourceGenerationCustody",
]

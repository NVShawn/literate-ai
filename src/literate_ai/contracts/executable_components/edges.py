"""Phase-specific executable meanings for Component dependency edges."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import bool_value, contract_fields, enum_value, fail, string_value
from ..capabilities import DependencyKind
from ..identity import ContentIdentity, contract_identity
from ._common import identity, optional_identity, portable_name

EXECUTABLE_COMPONENT_EDGE_SCHEMA = "urn:literate-ai:schema:v2:executable-component-edge"
EXECUTABLE_EDGE_SEMANTICS_VERSION = "executable-component-edge-semantics@1"


class ComponentActionPhase(StrEnum):
    GENERATE = "generate"
    VALIDATE = "validate"
    BUILD = "build"
    RUN = "run"
    PACKAGE = "package"
    DEPLOY = "deploy"


class DependencyInputKind(StrEnum):
    PUBLIC_INTERFACE = "public-interface"
    ARTIFACT_EXPORT = "artifact-export"
    VALIDATION_EVIDENCE = "validation-evidence"
    TOOLCHAIN = "toolchain"
    PACKAGE = "package"
    DEPLOYMENT_EVIDENCE = "deployment-evidence"


@dataclass(frozen=True, slots=True)
class ExecutableEdgeSemantics:
    consumer_phase: ComponentActionPhase
    provider_phase: ComponentActionPhase | None
    consumed_input: DependencyInputKind
    visible_to_generation: bool


EXECUTABLE_EDGE_SEMANTICS: dict[DependencyKind, ExecutableEdgeSemantics] = {
    DependencyKind.GENERATION: ExecutableEdgeSemantics(
        ComponentActionPhase.GENERATE,
        None,
        DependencyInputKind.PUBLIC_INTERFACE,
        True,
    ),
    DependencyKind.BUILD: ExecutableEdgeSemantics(
        ComponentActionPhase.BUILD,
        ComponentActionPhase.BUILD,
        DependencyInputKind.ARTIFACT_EXPORT,
        False,
    ),
    DependencyKind.RUNTIME: ExecutableEdgeSemantics(
        ComponentActionPhase.RUN,
        ComponentActionPhase.BUILD,
        DependencyInputKind.ARTIFACT_EXPORT,
        False,
    ),
    DependencyKind.VALIDATION: ExecutableEdgeSemantics(
        ComponentActionPhase.VALIDATE,
        ComponentActionPhase.VALIDATE,
        DependencyInputKind.VALIDATION_EVIDENCE,
        False,
    ),
    DependencyKind.TOOLCHAIN: ExecutableEdgeSemantics(
        ComponentActionPhase.BUILD,
        ComponentActionPhase.BUILD,
        DependencyInputKind.TOOLCHAIN,
        False,
    ),
    DependencyKind.PACKAGING: ExecutableEdgeSemantics(
        ComponentActionPhase.PACKAGE,
        ComponentActionPhase.PACKAGE,
        DependencyInputKind.PACKAGE,
        False,
    ),
    DependencyKind.DEPLOYMENT: ExecutableEdgeSemantics(
        ComponentActionPhase.DEPLOY,
        ComponentActionPhase.DEPLOY,
        DependencyInputKind.DEPLOYMENT_EVIDENCE,
        False,
    ),
}


@dataclass(frozen=True, slots=True)
class ExecutableComponentEdge:
    """One exact, phase-specific edge between two Component revisions."""

    consumer_revision: ContentIdentity
    provider_revision: ContentIdentity
    requirement_id: str
    capability: str
    kind: DependencyKind
    public_interface_identity: ContentIdentity | None
    optional: bool = False
    semantics_version: str = EXECUTABLE_EDGE_SEMANTICS_VERSION

    SCHEMA: ClassVar[str] = EXECUTABLE_COMPONENT_EDGE_SCHEMA

    def __post_init__(self) -> None:
        identity(self.consumer_revision, "ExecutableComponentEdge.consumer_revision")
        identity(self.provider_revision, "ExecutableComponentEdge.provider_revision")
        if self.consumer_revision == self.provider_revision:
            fail(
                "ExecutableComponentEdge.provider_revision",
                "must identify another Component revision",
            )
        portable_name(self.requirement_id, "ExecutableComponentEdge.requirement_id")
        portable_name(self.capability, "ExecutableComponentEdge.capability")
        if not isinstance(self.kind, DependencyKind):
            fail("ExecutableComponentEdge.kind", "must be a DependencyKind")
        if self.semantics_version != EXECUTABLE_EDGE_SEMANTICS_VERSION:
            fail(
                "ExecutableComponentEdge.semantics_version",
                f"must be {EXECUTABLE_EDGE_SEMANTICS_VERSION!r}",
            )
        _ = EXECUTABLE_EDGE_SEMANTICS[self.kind]
        if self.kind is DependencyKind.GENERATION:
            identity(
                self.public_interface_identity,
                "ExecutableComponentEdge.public_interface_identity",
            )
        elif self.public_interface_identity is not None:
            fail(
                "ExecutableComponentEdge.public_interface_identity",
                "is permitted only on a generation edge; declare separate edge kinds",
            )
        bool_value(self.optional, "ExecutableComponentEdge.optional")

    @property
    def semantics(self) -> ExecutableEdgeSemantics:
        return EXECUTABLE_EDGE_SEMANTICS[self.kind]

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "semantics_version": self.semantics_version,
            "consumer_revision": self.consumer_revision.to_dict(),
            "provider_revision": self.provider_revision.to_dict(),
            "requirement_id": self.requirement_id,
            "capability": self.capability,
            "kind": self.kind.value,
            "public_interface_identity": (
                None
                if self.public_interface_identity is None
                else self.public_interface_identity.to_dict()
            ),
            "optional": self.optional,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExecutableComponentEdge"
    ) -> ExecutableComponentEdge:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "semantics_version",
                    "consumer_revision",
                    "provider_revision",
                    "requirement_id",
                    "capability",
                    "kind",
                    "public_interface_identity",
                    "optional",
                }
            ),
        )
        return cls(
            consumer_revision=ContentIdentity.from_dict(
                data["consumer_revision"], path=f"{path}.consumer_revision"
            ),
            provider_revision=ContentIdentity.from_dict(
                data["provider_revision"], path=f"{path}.provider_revision"
            ),
            requirement_id=portable_name(
                data["requirement_id"], f"{path}.requirement_id"
            ),
            capability=portable_name(data["capability"], f"{path}.capability"),
            kind=enum_value(DependencyKind, data["kind"], f"{path}.kind"),
            public_interface_identity=optional_identity(
                data["public_interface_identity"],
                f"{path}.public_interface_identity",
            ),
            optional=bool_value(data["optional"], f"{path}.optional"),
            semantics_version=string_value(
                data["semantics_version"], f"{path}.semantics_version"
            ),
        )


__all__ = [
    "EXECUTABLE_COMPONENT_EDGE_SCHEMA",
    "EXECUTABLE_EDGE_SEMANTICS",
    "EXECUTABLE_EDGE_SEMANTICS_VERSION",
    "ComponentActionPhase",
    "DependencyInputKind",
    "ExecutableComponentEdge",
    "ExecutableEdgeSemantics",
]

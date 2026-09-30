"""Exact public routing and artifact custody for a single Component DAG."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, list_value
from .executable_components import ArtifactExport
from .executable_components._common import canonical_identities, identity, tuple_value
from .execution_dispatch import _identifier
from .identity import ContentIdentity, contract_identity


def _ordered(values: tuple, key, path: str, *, required: bool = False) -> None:
    tuple_value(values, path)
    keys = tuple(key(value) for value in values)
    if (required and not keys) or keys != tuple(sorted(set(keys))):
        fail(path, "must be unique and canonically ordered")


@dataclass(frozen=True, slots=True)
class ComponentWorkerAssignment:
    component_revision: ContentIdentity
    worker_id: str
    worker_identity: ContentIdentity
    target_profile: str
    target_flavor_selection_identity: ContentIdentity

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:component-worker-assignment"

    def __post_init__(self) -> None:
        for field in (
            "component_revision",
            "worker_identity",
            "target_flavor_selection_identity",
        ):
            identity(getattr(self, field), f"ComponentWorkerAssignment.{field}")
        for field in ("worker_id", "target_profile"):
            _identifier(getattr(self, field), f"ComponentWorkerAssignment.{field}")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "worker_id": self.worker_id,
            "worker_identity": self.worker_identity.to_dict(),
            "target_profile": self.target_profile,
            "target_flavor_selection_identity": (
                self.target_flavor_selection_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Any) -> ComponentWorkerAssignment:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "worker_id",
                    "worker_identity",
                    "target_profile",
                    "target_flavor_selection_identity",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["component_revision"]),
            data["worker_id"],
            ContentIdentity.from_dict(data["worker_identity"]),
            data["target_profile"],
            ContentIdentity.from_dict(data["target_flavor_selection_identity"]),
        )


@dataclass(frozen=True, slots=True)
class ComponentWorkerRouting:
    execution_plan_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    catalog_identity: ContentIdentity
    assignments: tuple[ComponentWorkerAssignment, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:component-worker-routing"

    def __post_init__(self) -> None:
        for field in (
            "execution_plan_identity",
            "component_lock_identity",
            "catalog_identity",
        ):
            identity(getattr(self, field), f"ComponentWorkerRouting.{field}")
        tuple_value(self.assignments, "ComponentWorkerRouting.assignments")
        if any(
            not isinstance(item, ComponentWorkerAssignment) for item in self.assignments
        ):
            fail("ComponentWorkerRouting.assignments", "must contain typed assignments")
        _ordered(
            self.assignments,
            lambda item: item.component_revision.uri,
            "ComponentWorkerRouting.assignments",
            required=True,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "catalog_identity": self.catalog_identity.to_dict(),
            "assignments": [item.to_dict() for item in self.assignments],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ComponentWorkerRouting:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "execution_plan_identity",
                    "component_lock_identity",
                    "catalog_identity",
                    "assignments",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["execution_plan_identity"]),
            ContentIdentity.from_dict(data["component_lock_identity"]),
            ContentIdentity.from_dict(data["catalog_identity"]),
            tuple(
                ComponentWorkerAssignment.from_dict(item)
                for item in list_value(
                    data["assignments"], f"{cls.__name__}.assignments"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentWorkerProduct:
    component_revision: ContentIdentity
    worker_identity: ContentIdentity
    acceptance_identity: ContentIdentity
    exports: tuple[ArtifactExport, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:component-worker-product"

    def __post_init__(self) -> None:
        for field in ("component_revision", "worker_identity", "acceptance_identity"):
            identity(getattr(self, field), f"ComponentWorkerProduct.{field}")
        tuple_value(self.exports, "ComponentWorkerProduct.exports")
        if any(
            not isinstance(item, ArtifactExport)
            or item.component_revision != self.component_revision
            for item in self.exports
        ):
            fail(
                "ComponentWorkerProduct.exports",
                "must belong to the accepted Component",
            )
        _ordered(
            self.exports,
            lambda item: item.identity.uri,
            "ComponentWorkerProduct.exports",
            required=True,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "worker_identity": self.worker_identity.to_dict(),
            "acceptance_identity": self.acceptance_identity.to_dict(),
            "exports": [item.to_dict() for item in self.exports],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ComponentWorkerProduct:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "worker_identity",
                    "acceptance_identity",
                    "exports",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["component_revision"]),
            ContentIdentity.from_dict(data["worker_identity"]),
            ContentIdentity.from_dict(data["acceptance_identity"]),
            tuple(
                ArtifactExport.from_dict(item)
                for item in list_value(data["exports"], f"{cls.__name__}.exports")
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentArtifactHandoff:
    routing_identity: ContentIdentity
    consumer: ComponentWorkerAssignment
    predecessors: tuple[ComponentWorkerProduct, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v1:component-artifact-handoff"

    def __post_init__(self) -> None:
        identity(self.routing_identity, "ComponentArtifactHandoff.routing_identity")
        if not isinstance(self.consumer, ComponentWorkerAssignment):
            fail("ComponentArtifactHandoff.consumer", "must be a typed assignment")
        tuple_value(self.predecessors, "ComponentArtifactHandoff.predecessors")
        if any(
            not isinstance(item, ComponentWorkerProduct)
            or item.component_revision == self.consumer.component_revision
            for item in self.predecessors
        ):
            fail(
                "ComponentArtifactHandoff.predecessors",
                "must be typed foreign predecessor products",
            )
        _ordered(
            self.predecessors,
            lambda item: item.component_revision.uri,
            "ComponentArtifactHandoff.predecessors",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def export_identities(self) -> tuple[ContentIdentity, ...]:
        return tuple(
            sorted(
                (
                    export.identity
                    for item in self.predecessors
                    for export in item.exports
                ),
                key=lambda item: item.uri,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "routing_identity": self.routing_identity.to_dict(),
            "consumer": self.consumer.to_dict(),
            "predecessors": [item.to_dict() for item in self.predecessors],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ComponentArtifactHandoff:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"routing_identity", "consumer", "predecessors"}),
        )
        return cls(
            ContentIdentity.from_dict(data["routing_identity"]),
            ComponentWorkerAssignment.from_dict(data["consumer"]),
            tuple(
                ComponentWorkerProduct.from_dict(item)
                for item in list_value(
                    data["predecessors"], f"{cls.__name__}.predecessors"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentArtifactImportReceipt:
    handoff_identity: ContentIdentity
    worker_identity: ContentIdentity
    export_identities: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = (
        "urn:literate-ai:schema:v1:component-artifact-import-receipt"
    )

    def __post_init__(self) -> None:
        identity(
            self.handoff_identity, "ComponentArtifactImportReceipt.handoff_identity"
        )
        identity(self.worker_identity, "ComponentArtifactImportReceipt.worker_identity")
        canonical_identities(
            self.export_identities, "ComponentArtifactImportReceipt.export_identities"
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "handoff_identity": self.handoff_identity.to_dict(),
            "worker_identity": self.worker_identity.to_dict(),
            "export_identities": [item.to_dict() for item in self.export_identities],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ComponentArtifactImportReceipt:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"handoff_identity", "worker_identity", "export_identities"}
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["handoff_identity"]),
            ContentIdentity.from_dict(data["worker_identity"]),
            tuple(
                ContentIdentity.from_dict(item)
                for item in list_value(
                    data["export_identities"], f"{cls.__name__}.export_identities"
                )
            ),
        )

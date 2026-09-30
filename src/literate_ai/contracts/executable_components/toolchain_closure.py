"""Locked command and observed host-toolchain closure for a Standard project."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar

from .._validation import contract_fields, fail, parse_tuple, string_value, unique
from ..identity import ContentIdentity, contract_identity
from ._common import identity, portable_name, tuple_value

STANDARD_COMPONENT_COMMAND_AUTHORITY_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-component-command-authority"
)
STANDARD_PROVIDER_ARTIFACT_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-provider-artifact-binding"
)
STANDARD_TOOLCHAIN_CLOSURE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-toolchain-closure"
)

_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,126}$")


@dataclass(frozen=True, slots=True)
class StandardComponentCommandAuthority:
    """One plan node bound to its selected Flavors and complete command authority."""

    component_revision: ContentIdentity
    generation_key_identity: ContentIdentity
    flavor_selection_identity: ContentIdentity
    command_contract_identity: ContentIdentity
    build_target_identity: ContentIdentity | None

    SCHEMA: ClassVar[str] = STANDARD_COMPONENT_COMMAND_AUTHORITY_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generation_key_identity",
            "flavor_selection_identity",
            "command_contract_identity",
        ):
            identity(getattr(self, name), f"StandardComponentCommandAuthority.{name}")
        if self.build_target_identity is not None:
            identity(
                self.build_target_identity,
                "StandardComponentCommandAuthority.build_target_identity",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generation_key_identity": self.generation_key_identity.to_dict(),
            "flavor_selection_identity": self.flavor_selection_identity.to_dict(),
            "command_contract_identity": self.command_contract_identity.to_dict(),
            "build_target_identity": (
                None
                if self.build_target_identity is None
                else self.build_target_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardComponentCommandAuthority",
    ) -> StandardComponentCommandAuthority:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "generation_key_identity",
                    "flavor_selection_identity",
                    "command_contract_identity",
                    "build_target_identity",
                }
            ),
        )
        target = data["build_target_identity"]
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            generation_key_identity=ContentIdentity.from_dict(
                data["generation_key_identity"],
                path=f"{path}.generation_key_identity",
            ),
            flavor_selection_identity=ContentIdentity.from_dict(
                data["flavor_selection_identity"],
                path=f"{path}.flavor_selection_identity",
            ),
            command_contract_identity=ContentIdentity.from_dict(
                data["command_contract_identity"],
                path=f"{path}.command_contract_identity",
            ),
            build_target_identity=(
                None
                if target is None
                else ContentIdentity.from_dict(
                    target, path=f"{path}.build_target_identity"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardProviderArtifactBinding:
    """One locked provider export projected into a consumer process environment."""

    export_id: str
    environment_name: str
    relative_path: str

    SCHEMA: ClassVar[str] = STANDARD_PROVIDER_ARTIFACT_BINDING_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.export_id, "StandardProviderArtifactBinding.export_id")
        string_value(
            self.environment_name,
            "StandardProviderArtifactBinding.environment_name",
        )
        if _ENVIRONMENT_NAME.fullmatch(self.environment_name) is None:
            fail(
                "StandardProviderArtifactBinding.environment_name",
                "must be one portable uppercase environment variable name",
            )
        string_value(
            self.relative_path,
            "StandardProviderArtifactBinding.relative_path",
        )
        relative = PurePosixPath(self.relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != self.relative_path
        ):
            fail(
                "StandardProviderArtifactBinding.relative_path",
                "must be a canonical relative POSIX path",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "export_id": self.export_id,
            "environment_name": self.environment_name,
            "relative_path": self.relative_path,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardProviderArtifactBinding",
    ) -> StandardProviderArtifactBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"export_id", "environment_name", "relative_path"}),
        )
        return cls(
            export_id=string_value(data["export_id"], f"{path}.export_id"),
            environment_name=string_value(
                data["environment_name"], f"{path}.environment_name"
            ),
            relative_path=string_value(data["relative_path"], f"{path}.relative_path"),
        )


@dataclass(frozen=True, slots=True)
class StandardToolchainClosure:
    """Portable identity spine for exact commands and one observed host closure."""

    component_lock_identity: ContentIdentity
    execution_plan_identity: ContentIdentity
    component_authorities: tuple[StandardComponentCommandAuthority, ...]
    toolchain_identities: tuple[ContentIdentity, ...]
    provider_bindings: tuple[StandardProviderArtifactBinding, ...]
    dependency_graph_identity: ContentIdentity
    observer_identity: ContentIdentity

    SCHEMA: ClassVar[str] = STANDARD_TOOLCHAIN_CLOSURE_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_lock_identity,
            "StandardToolchainClosure.component_lock_identity",
        )
        identity(
            self.execution_plan_identity,
            "StandardToolchainClosure.execution_plan_identity",
        )
        authorities = tuple_value(
            self.component_authorities,
            "StandardToolchainClosure.component_authorities",
        )
        if not authorities or any(
            not isinstance(item, StandardComponentCommandAuthority)
            for item in authorities
        ):
            fail(
                "StandardToolchainClosure.component_authorities",
                "must contain StandardComponentCommandAuthority values",
            )
        revisions = tuple(item.component_revision.uri for item in authorities)
        if revisions != tuple(sorted(revisions)) or len(revisions) != len(
            set(revisions)
        ):
            fail(
                "StandardToolchainClosure.component_authorities",
                "must use unique canonical Component revision order",
            )
        toolchains = tuple_value(
            self.toolchain_identities,
            "StandardToolchainClosure.toolchain_identities",
        )
        if not toolchains:
            fail(
                "StandardToolchainClosure.toolchain_identities",
                "must not be empty",
            )
        for index, item in enumerate(toolchains):
            identity(item, f"StandardToolchainClosure.toolchain_identities[{index}]")
        toolchain_uris = tuple(item.uri for item in toolchains)
        if toolchain_uris != tuple(sorted(toolchain_uris)):
            fail(
                "StandardToolchainClosure.toolchain_identities",
                "must use unique canonical identity order",
            )
        unique(
            toolchain_uris,
            "StandardToolchainClosure.toolchain_identities",
            "toolchain identities",
        )
        providers = tuple_value(
            self.provider_bindings,
            "StandardToolchainClosure.provider_bindings",
        )
        if any(
            not isinstance(item, StandardProviderArtifactBinding) for item in providers
        ):
            fail(
                "StandardToolchainClosure.provider_bindings",
                "must contain StandardProviderArtifactBinding values",
            )
        export_ids = tuple(item.export_id for item in providers)
        if export_ids != tuple(sorted(export_ids)):
            fail(
                "StandardToolchainClosure.provider_bindings",
                "must use unique canonical export order",
            )
        unique(
            export_ids,
            "StandardToolchainClosure.provider_bindings",
            "provider export IDs",
        )
        unique(
            tuple(item.environment_name for item in providers),
            "StandardToolchainClosure.provider_bindings",
            "provider environment names",
        )
        identity(
            self.dependency_graph_identity,
            "StandardToolchainClosure.dependency_graph_identity",
        )
        identity(self.observer_identity, "StandardToolchainClosure.observer_identity")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_lock_identity": self.component_lock_identity.to_dict(),
            "execution_plan_identity": self.execution_plan_identity.to_dict(),
            "component_authorities": [
                item.to_dict() for item in self.component_authorities
            ],
            "toolchain_identities": [
                item.to_dict() for item in self.toolchain_identities
            ],
            "provider_bindings": [item.to_dict() for item in self.provider_bindings],
            "dependency_graph_identity": self.dependency_graph_identity.to_dict(),
            "observer_identity": self.observer_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardToolchainClosure",
    ) -> StandardToolchainClosure:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_lock_identity",
                    "execution_plan_identity",
                    "component_authorities",
                    "toolchain_identities",
                    "provider_bindings",
                    "dependency_graph_identity",
                    "observer_identity",
                }
            ),
        )
        return cls(
            component_lock_identity=ContentIdentity.from_dict(
                data["component_lock_identity"],
                path=f"{path}.component_lock_identity",
            ),
            execution_plan_identity=ContentIdentity.from_dict(
                data["execution_plan_identity"],
                path=f"{path}.execution_plan_identity",
            ),
            component_authorities=parse_tuple(
                data["component_authorities"],
                f"{path}.component_authorities",
                StandardComponentCommandAuthority.from_dict,
            ),
            toolchain_identities=parse_tuple(
                data["toolchain_identities"],
                f"{path}.toolchain_identities",
                ContentIdentity.from_dict,
            ),
            provider_bindings=parse_tuple(
                data["provider_bindings"],
                f"{path}.provider_bindings",
                StandardProviderArtifactBinding.from_dict,
            ),
            dependency_graph_identity=ContentIdentity.from_dict(
                data["dependency_graph_identity"],
                path=f"{path}.dependency_graph_identity",
            ),
            observer_identity=ContentIdentity.from_dict(
                data["observer_identity"], path=f"{path}.observer_identity"
            ),
        )


__all__ = [
    "STANDARD_COMPONENT_COMMAND_AUTHORITY_SCHEMA",
    "STANDARD_PROVIDER_ARTIFACT_BINDING_SCHEMA",
    "STANDARD_TOOLCHAIN_CLOSURE_SCHEMA",
    "StandardComponentCommandAuthority",
    "StandardProviderArtifactBinding",
    "StandardToolchainClosure",
]

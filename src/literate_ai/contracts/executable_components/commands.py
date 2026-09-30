"""Locked, provider-neutral Component build/test/execute command authority."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    enum_value,
    fail,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from ..cpp_libraries import CppLibraryLayout, is_cpp_name, validate_cpp_header
from ..identity import ContentIdentity, canonical_identity, contract_identity
from ._common import identity, portable_name, tuple_value

COMPONENT_LIFECYCLE_COMMAND_SCHEMA = (
    "urn:literate-ai:schema:v2:component-lifecycle-command"
)
COMPONENT_COMMAND_CONTRACT_SCHEMA = (
    "urn:literate-ai:schema:v2:component-command-contract"
)
COMPONENT_ENTRYPOINT_COMMAND_CONTRACT_SCHEMA = (
    "urn:literate-ai:schema:v2:multi-entrypoint-component-semantics@1"
)
LIBRARY_CAPABILITY_IMPORT_SCHEMA = (
    "urn:literate-ai:schema:v2:library-capability-import@1"
)
LIBRARY_IMPORT_SURFACE_SCHEMA = "urn:literate-ai:schema:v2:library-import-surface@1"
LIBRARY_CONSUMER_BINDING_SCHEMA = "urn:literate-ai:schema:v2:library-consumer-binding@1"
_LIBRARY_SYMBOL = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]{0,126}$")


class ComponentCommandPhase(StrEnum):
    BUILD = "build"
    TEST = "test"
    EXECUTE = "execute"


# Per-entrypoint fan-out covers only the runtime phases. BUILD stays a single
# per-Component-revision command that produces the whole set of entrypoint
# artifacts, so it is never repeated inside a per-entrypoint contract.
_ENTRYPOINT_COMMAND_PHASES = (
    ComponentCommandPhase.TEST,
    ComponentCommandPhase.EXECUTE,
)


class ComponentCommandRole(StrEnum):
    TOOL = "tool"
    SOURCE_ROOT = "source-root"
    OBJECT_ROOT = "object-root"
    ARTIFACT_ROOT = "artifact-root"
    EXPORT_PATH = "export-path"
    PROVIDER_ARTIFACTS = "provider-artifacts"
    NATIVE_SDK_INPUTS = "native-sdk-inputs"

    @property
    def placeholder(self) -> str:
        return "{" + self.value.replace("-", "_") + "}"


_PLACEHOLDER_ROLES = {role.placeholder: role for role in ComponentCommandRole}
_REQUIRED_ROLES = {
    ComponentCommandPhase.BUILD: frozenset(
        {
            ComponentCommandRole.TOOL,
            ComponentCommandRole.SOURCE_ROOT,
            ComponentCommandRole.OBJECT_ROOT,
            ComponentCommandRole.EXPORT_PATH,
        }
    ),
    ComponentCommandPhase.TEST: frozenset(
        {ComponentCommandRole.TOOL, ComponentCommandRole.ARTIFACT_ROOT}
    ),
    ComponentCommandPhase.EXECUTE: frozenset(
        {ComponentCommandRole.TOOL, ComponentCommandRole.ARTIFACT_ROOT}
    ),
}


@dataclass(frozen=True, slots=True)
class LibraryCapabilityImport:
    """One public capability's exact language-native import mapping."""

    capability: str
    interface_identity: ContentIdentity
    module: str
    symbols: tuple[str, ...]

    SCHEMA: ClassVar[str] = LIBRARY_CAPABILITY_IMPORT_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.capability, "LibraryCapabilityImport.capability")
        identity(self.interface_identity, "LibraryCapabilityImport.interface_identity")
        string_value(self.module, "LibraryCapabilityImport.module", max_length=512)
        if any(character in self.module for character in ("\x00", "\\")):
            fail(
                "LibraryCapabilityImport.module",
                "must be a bounded portable language import name",
            )
        symbols = string_tuple(self.symbols, "LibraryCapabilityImport.symbols")
        if not symbols or len(symbols) > 256:
            fail(
                "LibraryCapabilityImport.symbols",
                "must contain between 1 and 256 exported symbols",
            )
        for index, symbol in enumerate(symbols):
            if _LIBRARY_SYMBOL.fullmatch(symbol) is None and not is_cpp_name(symbol):
                fail(
                    f"LibraryCapabilityImport.symbols[{index}]",
                    "must be a portable language export identifier",
                )
        unique(symbols, "LibraryCapabilityImport.symbols")
        if symbols != tuple(sorted(symbols)):
            fail("LibraryCapabilityImport.symbols", "must use canonical order")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "capability": self.capability,
            "interface_identity": self.interface_identity.to_dict(),
            "module": self.module,
            "symbols": list(self.symbols),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LibraryCapabilityImport"
    ) -> LibraryCapabilityImport:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"capability", "interface_identity", "module", "symbols"}
            ),
        )
        return cls(
            capability=portable_name(data["capability"], f"{path}.capability"),
            interface_identity=ContentIdentity.from_dict(
                data["interface_identity"], path=f"{path}.interface_identity"
            ),
            module=string_value(data["module"], f"{path}.module", max_length=512),
            symbols=string_tuple(data["symbols"], f"{path}.symbols"),
        )


@dataclass(frozen=True, slots=True)
class LibraryImportSurface:
    """Canonical import authority for one immutable package-shaped export."""

    language: str
    package: str
    capabilities: tuple[LibraryCapabilityImport, ...]

    SCHEMA: ClassVar[str] = LIBRARY_IMPORT_SURFACE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.language, "LibraryImportSurface.language")
        string_value(self.package, "LibraryImportSurface.package", max_length=255)
        if any(character in self.package for character in ("\x00", "\\", "/")):
            fail(
                "LibraryImportSurface.package",
                "must be one bounded language-native package name",
            )
        capabilities = tuple_value(
            self.capabilities, "LibraryImportSurface.capabilities"
        )
        if not capabilities or any(
            not isinstance(item, LibraryCapabilityImport) for item in capabilities
        ):
            fail(
                "LibraryImportSurface.capabilities",
                "must contain at least one LibraryCapabilityImport",
            )
        for capability in capabilities:
            if self.language == "cpp":
                validate_cpp_header(
                    capability.module, label="LibraryImportSurface.header"
                )
                if not capability.module.startswith(self.package + "/"):
                    fail(
                        "LibraryImportSurface.header",
                        "must remain below the named package",
                    )
                if any(not is_cpp_name(symbol) for symbol in capability.symbols):
                    fail(
                        "LibraryImportSurface.symbols",
                        "requires qualified C++ identifiers",
                    )
            elif any(
                _LIBRARY_SYMBOL.fullmatch(symbol) is None
                for symbol in capability.symbols
            ):
                fail(
                    "LibraryImportSurface.symbols", "requires native export identifiers"
                )
        names = tuple(item.capability for item in capabilities)
        unique(names, "LibraryImportSurface.capabilities", "capability names")
        if names != tuple(sorted(names)):
            fail("LibraryImportSurface.capabilities", "must use canonical order")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def capability(self, name: str) -> LibraryCapabilityImport:
        portable_name(name, "LibraryImportSurface.capability")
        for item in self.capabilities:
            if item.capability == name:
                return item
        fail(
            "LibraryImportSurface.capability",
            f"no import mapping exists for capability {name!r}",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "language": self.language,
            "package": self.package,
            "capabilities": [item.to_dict() for item in self.capabilities],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LibraryImportSurface"
    ) -> LibraryImportSurface:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"language", "package", "capabilities"}),
        )
        return cls(
            language=portable_name(data["language"], f"{path}.language"),
            package=string_value(data["package"], f"{path}.package", max_length=255),
            capabilities=parse_tuple(
                data["capabilities"],
                f"{path}.capabilities",
                LibraryCapabilityImport.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class LibraryConsumerBinding:
    """Exact accepted provider artifact selected for one consumer capability edge."""

    consumer_component_revision: ContentIdentity
    provider_component_revision: ContentIdentity
    capability: str
    interface_identity: ContentIdentity
    artifact_identity: ContentIdentity
    import_surface_identity: ContentIdentity
    target_identity: ContentIdentity
    dependency_artifact_identities: tuple[ContentIdentity, ...] = ()

    SCHEMA: ClassVar[str] = LIBRARY_CONSUMER_BINDING_SCHEMA

    def __post_init__(self) -> None:
        for field_name in (
            "consumer_component_revision",
            "provider_component_revision",
            "interface_identity",
            "artifact_identity",
            "import_surface_identity",
            "target_identity",
        ):
            identity(getattr(self, field_name), f"LibraryConsumerBinding.{field_name}")
        portable_name(self.capability, "LibraryConsumerBinding.capability")
        dependencies = tuple_value(
            self.dependency_artifact_identities,
            "LibraryConsumerBinding.dependency_artifact_identities",
        )
        if any(not isinstance(item, ContentIdentity) for item in dependencies):
            fail(
                "LibraryConsumerBinding.dependency_artifact_identities",
                "must contain ContentIdentity values",
            )
        uris = tuple(item.uri for item in dependencies)
        unique(uris, "LibraryConsumerBinding.dependency_artifact_identities")
        if uris != tuple(sorted(uris)):
            fail(
                "LibraryConsumerBinding.dependency_artifact_identities",
                "must use canonical order",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "consumer_component_revision": self.consumer_component_revision.to_dict(),
            "provider_component_revision": self.provider_component_revision.to_dict(),
            "capability": self.capability,
            "interface_identity": self.interface_identity.to_dict(),
            "artifact_identity": self.artifact_identity.to_dict(),
            "import_surface_identity": self.import_surface_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "dependency_artifact_identities": [
                item.to_dict() for item in self.dependency_artifact_identities
            ],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "LibraryConsumerBinding"
    ) -> LibraryConsumerBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "consumer_component_revision",
                    "provider_component_revision",
                    "capability",
                    "interface_identity",
                    "artifact_identity",
                    "import_surface_identity",
                    "target_identity",
                    "dependency_artifact_identities",
                }
            ),
        )
        return cls(
            consumer_component_revision=ContentIdentity.from_dict(
                data["consumer_component_revision"],
                path=f"{path}.consumer_component_revision",
            ),
            provider_component_revision=ContentIdentity.from_dict(
                data["provider_component_revision"],
                path=f"{path}.provider_component_revision",
            ),
            capability=portable_name(data["capability"], f"{path}.capability"),
            interface_identity=ContentIdentity.from_dict(
                data["interface_identity"], path=f"{path}.interface_identity"
            ),
            artifact_identity=ContentIdentity.from_dict(
                data["artifact_identity"], path=f"{path}.artifact_identity"
            ),
            import_surface_identity=ContentIdentity.from_dict(
                data["import_surface_identity"],
                path=f"{path}.import_surface_identity",
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            dependency_artifact_identities=parse_tuple(
                data["dependency_artifact_identities"],
                f"{path}.dependency_artifact_identities",
                ContentIdentity.from_dict,
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentLifecycleCommand:
    """One shell-free argv vector whose path roles are explicit placeholders."""

    phase: ComponentCommandPhase
    argv: tuple[str, ...]

    SCHEMA: ClassVar[str] = COMPONENT_LIFECYCLE_COMMAND_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ComponentCommandPhase):
            fail("ComponentLifecycleCommand.phase", "must be a ComponentCommandPhase")
        values = tuple_value(self.argv, "ComponentLifecycleCommand.argv")
        if not values or len(values) > 128:
            fail(
                "ComponentLifecycleCommand.argv",
                "must contain between 1 and 128 argv tokens",
            )
        roles: list[ComponentCommandRole] = []
        for index, raw in enumerate(values):
            argument = string_value(
                raw, f"ComponentLifecycleCommand.argv[{index}]", max_length=4096
            )
            if "\x00" in argument:
                fail(
                    f"ComponentLifecycleCommand.argv[{index}]",
                    "must not contain a NUL byte",
                )
            if "{" in argument or "}" in argument:
                try:
                    roles.append(_PLACEHOLDER_ROLES[argument])
                except KeyError:
                    fail(
                        f"ComponentLifecycleCommand.argv[{index}]",
                        "must use one complete known portable placeholder",
                    )
        if self.argv[0] != ComponentCommandRole.TOOL.placeholder:
            fail(
                "ComponentLifecycleCommand.argv[0]",
                "must be {tool}; shell strings and inferred launchers are forbidden",
            )
        if len(roles) != len(set(roles)):
            fail(
                "ComponentLifecycleCommand.argv",
                "must not repeat portable placeholder roles",
            )
        missing = _REQUIRED_ROLES[self.phase] - set(roles)
        if missing:
            fail(
                "ComponentLifecycleCommand.argv",
                "omits required roles: "
                + ", ".join(sorted(role.value for role in missing)),
            )

    @property
    def roles(self) -> frozenset[ComponentCommandRole]:
        return frozenset(
            _PLACEHOLDER_ROLES[item] for item in self.argv if item in _PLACEHOLDER_ROLES
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def substitute(
        self, bindings: Mapping[ComponentCommandRole, tuple[str, ...]]
    ) -> tuple[str, ...]:
        """Substitute every and only declared role without invoking a shell."""

        if not isinstance(bindings, Mapping) or any(
            not isinstance(role, ComponentCommandRole) for role in bindings
        ):
            fail(
                "ComponentLifecycleCommand.bindings",
                "must map ComponentCommandRole values to argv tuples",
            )
        if set(bindings) != set(self.roles):
            fail(
                "ComponentLifecycleCommand.bindings",
                "must bind every and only the command's declared roles",
            )
        normalized: dict[ComponentCommandRole, tuple[str, ...]] = {}
        for role, raw_values in bindings.items():
            values = tuple_value(
                raw_values, f"ComponentLifecycleCommand.bindings.{role.value}"
            )
            if (
                role
                not in {
                    ComponentCommandRole.TOOL,
                    ComponentCommandRole.PROVIDER_ARTIFACTS,
                }
                and len(values) != 1
            ):
                fail(
                    f"ComponentLifecycleCommand.bindings.{role.value}",
                    "must contain one value; tool and provider-artifacts may expand",
                )
            if role is ComponentCommandRole.TOOL and not values:
                fail(
                    f"ComponentLifecycleCommand.bindings.{role.value}",
                    "must contain at least one argv token",
                )
            normalized[role] = tuple(
                string_value(
                    value,
                    f"ComponentLifecycleCommand.bindings.{role.value}[{index}]",
                    max_length=4096,
                )
                for index, value in enumerate(values)
            )
            if any("\x00" in value for value in normalized[role]):
                fail(
                    f"ComponentLifecycleCommand.bindings.{role.value}",
                    "must not contain a NUL byte",
                )
        result: list[str] = []
        for argument in self.argv:
            role = _PLACEHOLDER_ROLES.get(argument)
            result.extend((argument,) if role is None else normalized[role])
        return tuple(result)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "phase": self.phase.value,
            "argv": list(self.argv),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentLifecycleCommand"
    ) -> ComponentLifecycleCommand:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"phase", "argv"}),
        )
        raw_argv = data["argv"]
        if not isinstance(raw_argv, list):
            fail(f"{path}.argv", "must be an argv array, never a shell string")
        return cls(
            enum_value(ComponentCommandPhase, data["phase"], f"{path}.phase"),
            tuple(raw_argv),
        )


@dataclass(frozen=True, slots=True)
class ComponentArtifactExportShape:
    """Static locked output shape before generated and authorization identities."""

    export_id: str
    role: str
    abi_identity: ContentIdentity
    target_identity: ContentIdentity
    media_type: str
    producer_identity: ContentIdentity

    def __post_init__(self) -> None:
        portable_name(self.export_id, "ComponentArtifactExportShape.export_id")
        portable_name(self.role, "ComponentArtifactExportShape.role")
        identity(self.abi_identity, "ComponentArtifactExportShape.abi_identity")
        identity(self.target_identity, "ComponentArtifactExportShape.target_identity")
        identity(
            self.producer_identity, "ComponentArtifactExportShape.producer_identity"
        )
        string_value(
            self.media_type, "ComponentArtifactExportShape.media_type", max_length=255
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "export_id": self.export_id,
            "role": self.role,
            "abi_identity": self.abi_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "media_type": self.media_type,
            "producer_identity": self.producer_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentArtifactExportShape"
    ) -> ComponentArtifactExportShape:
        names = frozenset(
            {
                "export_id",
                "role",
                "abi_identity",
                "target_identity",
                "media_type",
                "producer_identity",
            }
        )
        if not isinstance(value, dict):
            fail(path, "must be an object")
        if set(value) != names:
            fail(path, "must contain exactly the static export-shape fields")
        data = value
        return cls(
            export_id=string_value(data["export_id"], f"{path}.export_id"),
            role=string_value(data["role"], f"{path}.role"),
            abi_identity=ContentIdentity.from_dict(
                data["abi_identity"], path=f"{path}.abi_identity"
            ),
            target_identity=ContentIdentity.from_dict(
                data["target_identity"], path=f"{path}.target_identity"
            ),
            media_type=string_value(data["media_type"], f"{path}.media_type"),
            producer_identity=ContentIdentity.from_dict(
                data["producer_identity"], path=f"{path}.producer_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentCommandToolBinding:
    """Exact external tool selected for one lifecycle phase."""

    phase: ComponentCommandPhase
    toolchain_identity: ContentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.phase, ComponentCommandPhase):
            fail("ComponentCommandToolBinding.phase", "must be a ComponentCommandPhase")
        identity(
            self.toolchain_identity,
            "ComponentCommandToolBinding.toolchain_identity",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "toolchain_identity": self.toolchain_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentCommandToolBinding"
    ) -> ComponentCommandToolBinding:
        if not isinstance(value, dict):
            fail(path, "must be an object")
        if set(value) != {"phase", "toolchain_identity"}:
            fail(path, "must contain exactly phase and toolchain_identity")
        return cls(
            enum_value(ComponentCommandPhase, value["phase"], f"{path}.phase"),
            ContentIdentity.from_dict(
                value["toolchain_identity"], path=f"{path}.toolchain_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentEntrypointCommandContract:
    """Locked TEST/EXECUTE authority and export shape for one entrypoint.

    Additive wire contract (``multi-entrypoint-component-semantics@1``) introduced
    by ADR 0026. BUILD is not repeated here -- one Component revision has one
    build that produces the set of entrypoint artifacts. Each entrypoint carries
    its own runtime commands, tool bindings, export shape, and the deployment
    unit it belongs to, keyed by ``entrypoint_identity``.
    """

    entrypoint_identity: ContentIdentity
    deployment_unit: str
    commands: tuple[ComponentLifecycleCommand, ...]
    tool_bindings: tuple[ComponentCommandToolBinding, ...]
    artifact_export: ComponentArtifactExportShape

    SCHEMA: ClassVar[str] = COMPONENT_ENTRYPOINT_COMMAND_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.entrypoint_identity,
            "ComponentEntrypointCommandContract.entrypoint_identity",
        )
        string_value(
            self.deployment_unit,
            "ComponentEntrypointCommandContract.deployment_unit",
        )
        commands = tuple_value(
            self.commands, "ComponentEntrypointCommandContract.commands"
        )
        if any(not isinstance(item, ComponentLifecycleCommand) for item in commands):
            fail(
                "ComponentEntrypointCommandContract.commands",
                "must contain ComponentLifecycleCommand values",
            )
        if tuple(item.phase for item in commands) != _ENTRYPOINT_COMMAND_PHASES:
            fail(
                "ComponentEntrypointCommandContract.commands",
                "must contain test and execute exactly once in order",
            )
        bindings = tuple_value(
            self.tool_bindings, "ComponentEntrypointCommandContract.tool_bindings"
        )
        if any(not isinstance(item, ComponentCommandToolBinding) for item in bindings):
            fail(
                "ComponentEntrypointCommandContract.tool_bindings",
                "must contain ComponentCommandToolBinding values",
            )
        if tuple(item.phase for item in bindings) != _ENTRYPOINT_COMMAND_PHASES:
            fail(
                "ComponentEntrypointCommandContract.tool_bindings",
                "must bind test and execute exactly once in order",
            )
        if not isinstance(self.artifact_export, ComponentArtifactExportShape):
            fail(
                "ComponentEntrypointCommandContract.artifact_export",
                "must be a ComponentArtifactExportShape",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def command(self, phase: ComponentCommandPhase) -> ComponentLifecycleCommand:
        if phase not in _ENTRYPOINT_COMMAND_PHASES:
            fail(
                "ComponentEntrypointCommandContract.phase",
                "must be a per-entrypoint test or execute phase",
            )
        return self.commands[_ENTRYPOINT_COMMAND_PHASES.index(phase)]

    def tool_binding(self, phase: ComponentCommandPhase) -> ComponentCommandToolBinding:
        if phase not in _ENTRYPOINT_COMMAND_PHASES:
            fail(
                "ComponentEntrypointCommandContract.phase",
                "must be a per-entrypoint test or execute phase",
            )
        return self.tool_bindings[_ENTRYPOINT_COMMAND_PHASES.index(phase)]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "entrypoint_identity": self.entrypoint_identity.to_dict(),
            "deployment_unit": self.deployment_unit,
            "commands": [item.to_dict() for item in self.commands],
            "tool_bindings": [item.to_dict() for item in self.tool_bindings],
            "artifact_export": self.artifact_export.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentEntrypointCommandContract"
    ) -> ComponentEntrypointCommandContract:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "entrypoint_identity",
                    "deployment_unit",
                    "commands",
                    "tool_bindings",
                    "artifact_export",
                }
            ),
        )
        return cls(
            entrypoint_identity=ContentIdentity.from_dict(
                data["entrypoint_identity"], path=f"{path}.entrypoint_identity"
            ),
            deployment_unit=string_value(
                data["deployment_unit"], f"{path}.deployment_unit"
            ),
            commands=parse_tuple(
                data["commands"],
                f"{path}.commands",
                ComponentLifecycleCommand.from_dict,
            ),
            tool_bindings=parse_tuple(
                data["tool_bindings"],
                f"{path}.tool_bindings",
                ComponentCommandToolBinding.from_dict,
            ),
            artifact_export=ComponentArtifactExportShape.from_dict(
                data["artifact_export"], path=f"{path}.artifact_export"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentCommandContract:
    """Exact locked command authority for one independently executable Component."""

    component_revision: ContentIdentity
    locked_build_authority_identity: ContentIdentity
    build_system_resolver_identity: ContentIdentity
    build_system_toolchain_identity: ContentIdentity
    language_compiler_identity: ContentIdentity
    language_runtime_identity: ContentIdentity
    commands: tuple[ComponentLifecycleCommand, ...]
    tool_bindings: tuple[ComponentCommandToolBinding, ...]
    artifact_export: ComponentArtifactExportShape
    # ADR-0026 additive extension. ``None`` for the single-entrypoint common case
    # (which uses ``commands``/``tool_bindings``/``artifact_export`` verbatim), so
    # a single-entrypoint contract serializes and hashes byte-identically to
    # before this field existed. Present only for genuine multi-entrypoint
    # Components, carrying one ComponentEntrypointCommandContract per entrypoint.
    entrypoint_contracts: tuple[ComponentEntrypointCommandContract, ...] | None = None
    # ADR-0038 additive extension. Absent for every executable Component so their
    # wire bytes and identities remain unchanged; present only for `kind: library`.
    library_import_surface: LibraryImportSurface | None = None
    native_layout: CppLibraryLayout | None = None

    SCHEMA: ClassVar[str] = COMPONENT_COMMAND_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        identity(self.component_revision, "ComponentCommandContract.component_revision")
        identity(
            self.locked_build_authority_identity,
            "ComponentCommandContract.locked_build_authority_identity",
        )
        identity(
            self.build_system_resolver_identity,
            "ComponentCommandContract.build_system_resolver_identity",
        )
        identity(
            self.build_system_toolchain_identity,
            "ComponentCommandContract.build_system_toolchain_identity",
        )
        identity(
            self.language_compiler_identity,
            "ComponentCommandContract.language_compiler_identity",
        )
        identity(
            self.language_runtime_identity,
            "ComponentCommandContract.language_runtime_identity",
        )
        commands = tuple_value(self.commands, "ComponentCommandContract.commands")
        if any(not isinstance(item, ComponentLifecycleCommand) for item in commands):
            fail(
                "ComponentCommandContract.commands",
                "must contain ComponentLifecycleCommand values",
            )
        phases = tuple(item.phase for item in commands)
        expected = tuple(ComponentCommandPhase)
        if phases != expected:
            fail(
                "ComponentCommandContract.commands",
                "must contain build, test, and execute exactly once in order",
            )
        if not isinstance(self.artifact_export, ComponentArtifactExportShape):
            fail(
                "ComponentCommandContract.artifact_export",
                "must be a ComponentArtifactExportShape",
            )
        bindings = tuple_value(
            self.tool_bindings, "ComponentCommandContract.tool_bindings"
        )
        if any(not isinstance(item, ComponentCommandToolBinding) for item in bindings):
            fail(
                "ComponentCommandContract.tool_bindings",
                "must contain ComponentCommandToolBinding values",
            )
        if tuple(item.phase for item in bindings) != tuple(ComponentCommandPhase):
            fail(
                "ComponentCommandContract.tool_bindings",
                "must bind build, test, and execute exactly once in order",
            )
        if self.entrypoint_contracts is not None:
            per_entrypoint = tuple_value(
                self.entrypoint_contracts,
                "ComponentCommandContract.entrypoint_contracts",
            )
            if not per_entrypoint:
                fail(
                    "ComponentCommandContract.entrypoint_contracts",
                    "must be absent (None) rather than empty; a single implicit "
                    "unit uses the top-level command fields",
                )
            if any(
                not isinstance(item, ComponentEntrypointCommandContract)
                for item in per_entrypoint
            ):
                fail(
                    "ComponentCommandContract.entrypoint_contracts",
                    "must contain ComponentEntrypointCommandContract values",
                )
            uris = tuple(item.entrypoint_identity.uri for item in per_entrypoint)
            if len(set(uris)) != len(uris):
                fail(
                    "ComponentCommandContract.entrypoint_contracts",
                    "must key each entrypoint contract by a unique entrypoint identity",
                )
        if self.library_import_surface is not None:
            if not isinstance(self.library_import_surface, LibraryImportSurface):
                fail(
                    "ComponentCommandContract.library_import_surface",
                    "must be a LibraryImportSurface or null",
                )
            if self.artifact_export.role != "library":
                fail(
                    "ComponentCommandContract.library_import_surface",
                    "requires an artifact export with role 'library'",
                )
            if self.entrypoint_contracts is not None:
                fail(
                    "ComponentCommandContract.library_import_surface",
                    "cannot be combined with executable entrypoint contracts",
                )

        cpp = (
            self.library_import_surface is not None
            and self.library_import_surface.language == "cpp"
        )
        if cpp:
            if not isinstance(self.native_layout, CppLibraryLayout):
                fail(
                    "ComponentCommandContract.native_layout",
                    "C++ requires a typed native layout",
                )
            if any(
                "include/" + item.module not in self.native_layout.headers
                for item in self.library_import_surface.capabilities
            ):
                fail(
                    "ComponentCommandContract.native_layout",
                    "must contain every declared public header",
                )
        elif self.native_layout is not None:
            fail("ComponentCommandContract.native_layout", "requires a C++ library")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def is_multi_entrypoint(self) -> bool:
        """True when this contract fans TEST/EXECUTE out per entrypoint."""

        return self.entrypoint_contracts is not None

    @property
    def is_library(self) -> bool:
        """True when this contract describes an importable package.

        Such a package deliberately has no product entrypoint.
        """

        return self.library_import_surface is not None

    @property
    def library_acceptance_toolchain_identity(self) -> ContentIdentity | None:
        """Exact launcher for the verifier-owned in-language library harness."""

        if self.library_import_surface is None:
            return None
        if self.library_import_surface.language == "rust":
            return self.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity
        return self.language_compiler_identity

    @property
    def execution_toolchain_identities(self) -> tuple[ContentIdentity, ...]:
        """Every launcher needed by commands and independent library acceptance."""

        identities = {
            binding.toolchain_identity.uri: binding.toolchain_identity
            for binding in self.tool_bindings
        }
        acceptance = self.library_acceptance_toolchain_identity
        if acceptance is not None:
            identities[acceptance.uri] = acceptance
        return tuple(identities[key] for key in sorted(identities))

    def command(self, phase: ComponentCommandPhase) -> ComponentLifecycleCommand:
        if not isinstance(phase, ComponentCommandPhase):
            fail("ComponentCommandContract.phase", "must be a ComponentCommandPhase")
        return self.commands[tuple(ComponentCommandPhase).index(phase)]

    def tool_binding(self, phase: ComponentCommandPhase) -> ComponentCommandToolBinding:
        if not isinstance(phase, ComponentCommandPhase):
            fail("ComponentCommandContract.phase", "must be a ComponentCommandPhase")
        return self.tool_bindings[tuple(ComponentCommandPhase).index(phase)]

    def entrypoint_command_contract(
        self, entrypoint_identity: ContentIdentity | None = None
    ) -> ComponentEntrypointCommandContract:
        """Resolve one entrypoint's TEST/EXECUTE authority for dispatch.

        With no argument, returns the primary/root entrypoint -- the same surface
        the top-level ``artifact_export``/``command()`` fields describe -- so a
        single-entrypoint dispatcher is unchanged. A multi-entrypoint dispatcher
        (#216/#218) passes an ``entrypoint_identity`` to route TEST/EXECUTE to that
        deployment unit's own commands and export shape.
        """

        contracts = self.entrypoint_command_contracts()
        if entrypoint_identity is None:
            return contracts[0]
        if not isinstance(entrypoint_identity, ContentIdentity):
            fail(
                "ComponentCommandContract.entrypoint_identity",
                "must be a ContentIdentity",
            )
        for item in contracts:
            if item.entrypoint_identity.uri == entrypoint_identity.uri:
                return item
        fail(
            "ComponentCommandContract.entrypoint_identity",
            f"no entrypoint contract for {entrypoint_identity.uri!r}",
        )

    def entrypoint_command_contracts(
        self,
    ) -> tuple[ComponentEntrypointCommandContract, ...]:
        """The per-entrypoint contracts, or the single implicit unit as one.

        Callers that fan out over deployment units iterate this uniformly: a
        single-entrypoint Component yields exactly one synthetic entrypoint
        contract derived from the top-level TEST/EXECUTE fields, so dispatch code
        need not special-case cardinality.
        """

        if self.is_library:
            fail(
                "ComponentCommandContract.entrypoint_contracts",
                "an importable library has no executable entrypoint contract",
            )
        if self.entrypoint_contracts is not None:
            return self.entrypoint_contracts
        return (
            ComponentEntrypointCommandContract(
                entrypoint_identity=self.artifact_export.identity,
                deployment_unit=self.artifact_export.export_id,
                commands=(
                    self.command(ComponentCommandPhase.TEST),
                    self.command(ComponentCommandPhase.EXECUTE),
                ),
                tool_bindings=(
                    self.tool_binding(ComponentCommandPhase.TEST),
                    self.tool_binding(ComponentCommandPhase.EXECUTE),
                ),
                artifact_export=self.artifact_export,
            ),
        )

    def artifact_export_shapes(self) -> tuple[ComponentArtifactExportShape, ...]:
        """Return every declared artifact without inventing a library entrypoint."""

        if self.is_library:
            return (self.artifact_export,)
        return tuple(
            item.artifact_export for item in self.entrypoint_command_contracts()
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "locked_build_authority_identity": (
                self.locked_build_authority_identity.to_dict()
            ),
            "build_system_resolver_identity": (
                self.build_system_resolver_identity.to_dict()
            ),
            "build_system_toolchain_identity": (
                self.build_system_toolchain_identity.to_dict()
            ),
            "language_compiler_identity": self.language_compiler_identity.to_dict(),
            "language_runtime_identity": self.language_runtime_identity.to_dict(),
            "commands": [item.to_dict() for item in self.commands],
            "tool_bindings": [item.to_dict() for item in self.tool_bindings],
            "artifact_export": self.artifact_export.to_dict(),
        }
        # Additive: absent (not present-and-empty) for the single-entrypoint case
        # so its wire bytes and identity are unchanged from before ADR 0026.
        if self.entrypoint_contracts is not None:
            value["entrypoint_contracts"] = [
                item.to_dict() for item in self.entrypoint_contracts
            ]
        if self.library_import_surface is not None:
            value["library_import_surface"] = self.library_import_surface.to_dict()
        if self.native_layout is not None:
            value["native_layout"] = self.native_layout.to_dict()
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentCommandContract"
    ) -> ComponentCommandContract:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "component_revision",
                    "locked_build_authority_identity",
                    "build_system_resolver_identity",
                    "build_system_toolchain_identity",
                    "language_compiler_identity",
                    "language_runtime_identity",
                    "commands",
                    "tool_bindings",
                    "artifact_export",
                }
            ),
            optional=frozenset(
                {"entrypoint_contracts", "library_import_surface", "native_layout"}
            ),
        )
        raw_entrypoint_contracts = data.get("entrypoint_contracts")
        raw_library_import_surface = data.get("library_import_surface")
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            locked_build_authority_identity=ContentIdentity.from_dict(
                data["locked_build_authority_identity"],
                path=f"{path}.locked_build_authority_identity",
            ),
            build_system_resolver_identity=ContentIdentity.from_dict(
                data["build_system_resolver_identity"],
                path=f"{path}.build_system_resolver_identity",
            ),
            build_system_toolchain_identity=ContentIdentity.from_dict(
                data["build_system_toolchain_identity"],
                path=f"{path}.build_system_toolchain_identity",
            ),
            language_compiler_identity=ContentIdentity.from_dict(
                data["language_compiler_identity"],
                path=f"{path}.language_compiler_identity",
            ),
            language_runtime_identity=ContentIdentity.from_dict(
                data["language_runtime_identity"],
                path=f"{path}.language_runtime_identity",
            ),
            commands=parse_tuple(
                data["commands"],
                f"{path}.commands",
                ComponentLifecycleCommand.from_dict,
            ),
            tool_bindings=parse_tuple(
                data["tool_bindings"],
                f"{path}.tool_bindings",
                ComponentCommandToolBinding.from_dict,
            ),
            artifact_export=ComponentArtifactExportShape.from_dict(
                data["artifact_export"], path=f"{path}.artifact_export"
            ),
            entrypoint_contracts=(
                None
                if raw_entrypoint_contracts is None
                else parse_tuple(
                    raw_entrypoint_contracts,
                    f"{path}.entrypoint_contracts",
                    ComponentEntrypointCommandContract.from_dict,
                )
            ),
            library_import_surface=(
                None
                if raw_library_import_surface is None
                else LibraryImportSurface.from_dict(
                    raw_library_import_surface,
                    path=f"{path}.library_import_surface",
                )
            ),
            native_layout=(
                CppLibraryLayout.from_dict(
                    data["native_layout"], path=f"{path}.native_layout"
                )
                if "native_layout" in data
                else None
            ),
        )


__all__ = [
    "COMPONENT_COMMAND_CONTRACT_SCHEMA",
    "COMPONENT_ENTRYPOINT_COMMAND_CONTRACT_SCHEMA",
    "COMPONENT_LIFECYCLE_COMMAND_SCHEMA",
    "LIBRARY_CAPABILITY_IMPORT_SCHEMA",
    "LIBRARY_CONSUMER_BINDING_SCHEMA",
    "LIBRARY_IMPORT_SURFACE_SCHEMA",
    "ComponentArtifactExportShape",
    "ComponentCommandContract",
    "ComponentCommandPhase",
    "ComponentCommandRole",
    "ComponentCommandToolBinding",
    "ComponentEntrypointCommandContract",
    "ComponentLifecycleCommand",
    "LibraryCapabilityImport",
    "LibraryConsumerBinding",
    "LibraryImportSurface",
]

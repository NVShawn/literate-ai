"""Generation-visible public interface and intentional re-export contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from .._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from ..identity import ContentIdentity, contract_identity
from ..versioning import semantic_version
from ._common import identity, portable_name, texts, tuple_value

PUBLIC_INTERFACE_CONTRACT_SCHEMA = "urn:literate-ai:schema:v2:public-interface-contract"
COMPONENT_INTERFACE_BINDING_SCHEMA = (
    "urn:literate-ai:schema:v2:component-interface-binding"
)


class CompatibilityPolicy(StrEnum):
    """The review promise attached to a public interface revision."""

    SEMVER_STABLE = "semver-stable"
    EXPLICIT_MIGRATION = "explicit-migration"
    FIXED_REVISION = "fixed-revision"


@dataclass(frozen=True, slots=True)
class CompatibilityPromise:
    policy: CompatibilityPolicy
    compatible_versions: str
    statement: str

    def __post_init__(self) -> None:
        if not isinstance(self.policy, CompatibilityPolicy):
            fail("CompatibilityPromise.policy", "must be a CompatibilityPolicy")
        string_value(
            self.compatible_versions,
            "CompatibilityPromise.compatible_versions",
            max_length=256,
        )
        string_value(self.statement, "CompatibilityPromise.statement", max_length=4096)

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy.value,
            "compatible_versions": self.compatible_versions,
            "statement": self.statement,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CompatibilityPromise"
    ) -> CompatibilityPromise:
        data = fields(
            value,
            path=path,
            required=frozenset({"policy", "compatible_versions", "statement"}),
        )
        return cls(
            enum_value(CompatibilityPolicy, data["policy"], f"{path}.policy"),
            string_value(data["compatible_versions"], f"{path}.compatible_versions"),
            string_value(data["statement"], f"{path}.statement"),
        )


@dataclass(frozen=True, slots=True)
class ExportedType:
    name: str
    contract: str

    def __post_init__(self) -> None:
        portable_name(self.name, "ExportedType.name")
        string_value(self.contract, "ExportedType.contract", max_length=16384)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "contract": self.contract}

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "ExportedType") -> ExportedType:
        data = fields(
            value,
            path=path,
            required=frozenset({"name", "contract"}),
        )
        return cls(
            portable_name(data["name"], f"{path}.name"),
            string_value(data["contract"], f"{path}.contract", max_length=16384),
        )


@dataclass(frozen=True, slots=True)
class ExportedProtocol:
    name: str
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        portable_name(self.name, "ExportedProtocol.name")
        texts(
            self.operations,
            "ExportedProtocol.operations",
            required=True,
            maximum_items=128,
        )

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "operations": list(self.operations)}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ExportedProtocol"
    ) -> ExportedProtocol:
        data = fields(
            value,
            path=path,
            required=frozenset({"name", "operations"}),
        )
        return cls(
            portable_name(data["name"], f"{path}.name"),
            string_tuple(data["operations"], f"{path}.operations"),
        )


@dataclass(frozen=True, slots=True)
class InterfaceError:
    code: str
    condition: str
    caller_obligation: str

    def __post_init__(self) -> None:
        portable_name(self.code, "InterfaceError.code")
        string_value(self.condition, "InterfaceError.condition", max_length=4096)
        string_value(
            self.caller_obligation,
            "InterfaceError.caller_obligation",
            max_length=4096,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "condition": self.condition,
            "caller_obligation": self.caller_obligation,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "InterfaceError") -> InterfaceError:
        data = fields(
            value,
            path=path,
            required=frozenset({"code", "condition", "caller_obligation"}),
        )
        return cls(
            portable_name(data["code"], f"{path}.code"),
            string_value(data["condition"], f"{path}.condition"),
            string_value(data["caller_obligation"], f"{path}.caller_obligation"),
        )


@dataclass(frozen=True, slots=True)
class IntentionalInterfaceReExport:
    """An exact direct dependency interface deliberately exposed by a provider."""

    capability: str
    version: str
    interface_identity: ContentIdentity
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        portable_name(self.capability, "IntentionalInterfaceReExport.capability")
        semantic_version(self.version, "IntentionalInterfaceReExport.version")
        identity(
            self.interface_identity,
            "IntentionalInterfaceReExport.interface_identity",
        )
        symbols = texts(
            self.symbols,
            "IntentionalInterfaceReExport.symbols",
            required=True,
            maximum_items=256,
        )
        for index, symbol in enumerate(symbols):
            portable_name(symbol, f"IntentionalInterfaceReExport.symbols[{index}]")
        if symbols != tuple(sorted(symbols)):
            fail(
                "IntentionalInterfaceReExport.symbols",
                "must use canonical symbol order",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "version": self.version,
            "interface_identity": self.interface_identity.to_dict(),
            "symbols": list(self.symbols),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "IntentionalInterfaceReExport"
    ) -> IntentionalInterfaceReExport:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {
                    "capability",
                    "version",
                    "interface_identity",
                    "symbols",
                }
            ),
        )
        return cls(
            portable_name(data["capability"], f"{path}.capability"),
            semantic_version(data["version"], f"{path}.version"),
            ContentIdentity.from_dict(
                data["interface_identity"], path=f"{path}.interface_identity"
            ),
            string_tuple(data["symbols"], f"{path}.symbols"),
        )


@dataclass(frozen=True, slots=True)
class ComponentInterfaceBinding:
    """Bind one revision to stable public-interface semantics without polluting them."""

    component_revision: ContentIdentity
    capability: str
    interface_identity: ContentIdentity

    SCHEMA: ClassVar[str] = COMPONENT_INTERFACE_BINDING_SCHEMA

    def __post_init__(self) -> None:
        identity(
            self.component_revision,
            "ComponentInterfaceBinding.component_revision",
        )
        portable_name(self.capability, "ComponentInterfaceBinding.capability")
        identity(
            self.interface_identity,
            "ComponentInterfaceBinding.interface_identity",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "capability": self.capability,
            "interface_identity": self.interface_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentInterfaceBinding"
    ) -> ComponentInterfaceBinding:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"component_revision", "capability", "interface_identity"}
            ),
        )
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            capability=portable_name(data["capability"], f"{path}.capability"),
            interface_identity=ContentIdentity.from_dict(
                data["interface_identity"], path=f"{path}.interface_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class PublicInterfaceContract:
    """Stable semantics and the only dependency material visible to generation."""

    capability: str
    version: str
    exported_types: tuple[ExportedType, ...]
    exported_protocols: tuple[ExportedProtocol, ...]
    preconditions: tuple[str, ...]
    postconditions: tuple[str, ...]
    errors: tuple[InterfaceError, ...]
    compatibility: CompatibilityPromise
    re_exports: tuple[IntentionalInterfaceReExport, ...] = ()

    SCHEMA: ClassVar[str] = PUBLIC_INTERFACE_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.capability, "PublicInterfaceContract.capability")
        semantic_version(self.version, "PublicInterfaceContract.version")
        for field_name, expected_type in (
            ("exported_types", ExportedType),
            ("exported_protocols", ExportedProtocol),
            ("errors", InterfaceError),
            ("re_exports", IntentionalInterfaceReExport),
        ):
            values = tuple_value(
                getattr(self, field_name), f"PublicInterfaceContract.{field_name}"
            )
            if len(values) > 256:
                fail(
                    f"PublicInterfaceContract.{field_name}",
                    "must contain at most 256 values",
                )
            if any(not isinstance(item, expected_type) for item in values):
                fail(
                    f"PublicInterfaceContract.{field_name}",
                    f"must contain only {expected_type.__name__} values",
                )
        if (
            not self.exported_types
            and not self.exported_protocols
            and not self.re_exports
        ):
            fail(
                "PublicInterfaceContract",
                "must export at least one type, protocol, or intentional re-export",
            )
        texts(
            self.preconditions,
            "PublicInterfaceContract.preconditions",
            required=True,
        )
        texts(
            self.postconditions,
            "PublicInterfaceContract.postconditions",
            required=True,
        )
        if not isinstance(self.compatibility, CompatibilityPromise):
            fail(
                "PublicInterfaceContract.compatibility",
                "must be a CompatibilityPromise",
            )
        type_names = tuple(item.name for item in self.exported_types)
        protocol_names = tuple(item.name for item in self.exported_protocols)
        error_codes = tuple(item.code for item in self.errors)
        re_export_ids = tuple(item.interface_identity.uri for item in self.re_exports)
        for values, field_name in (
            (type_names, "exported_types"),
            (protocol_names, "exported_protocols"),
            (error_codes, "errors"),
            (re_export_ids, "re_exports"),
        ):
            unique(values, f"PublicInterfaceContract.{field_name}")
            if values != tuple(sorted(values)):
                fail(
                    f"PublicInterfaceContract.{field_name}",
                    "must use canonical order",
                )
        own_symbols = set(type_names) | set(protocol_names)
        exported_symbols = {
            symbol for re_export in self.re_exports for symbol in re_export.symbols
        }
        if own_symbols & exported_symbols:
            fail(
                "PublicInterfaceContract.re_exports",
                "cannot shadow a locally exported type or protocol",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "capability": self.capability,
            "version": self.version,
            "exported_types": [item.to_dict() for item in self.exported_types],
            "exported_protocols": [item.to_dict() for item in self.exported_protocols],
            "preconditions": list(self.preconditions),
            "postconditions": list(self.postconditions),
            "errors": [item.to_dict() for item in self.errors],
            "compatibility": self.compatibility.to_dict(),
            "re_exports": [item.to_dict() for item in self.re_exports],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "PublicInterfaceContract"
    ) -> PublicInterfaceContract:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "capability",
                    "version",
                    "exported_types",
                    "exported_protocols",
                    "preconditions",
                    "postconditions",
                    "errors",
                    "compatibility",
                    "re_exports",
                }
            ),
        )
        return cls(
            capability=portable_name(data["capability"], f"{path}.capability"),
            version=semantic_version(data["version"], f"{path}.version"),
            exported_types=parse_tuple(
                data["exported_types"], f"{path}.exported_types", ExportedType.from_dict
            ),
            exported_protocols=parse_tuple(
                data["exported_protocols"],
                f"{path}.exported_protocols",
                ExportedProtocol.from_dict,
            ),
            preconditions=string_tuple(data["preconditions"], f"{path}.preconditions"),
            postconditions=string_tuple(
                data["postconditions"], f"{path}.postconditions"
            ),
            errors=parse_tuple(
                data["errors"], f"{path}.errors", InterfaceError.from_dict
            ),
            compatibility=CompatibilityPromise.from_dict(
                data["compatibility"], path=f"{path}.compatibility"
            ),
            re_exports=parse_tuple(
                data["re_exports"],
                f"{path}.re_exports",
                IntentionalInterfaceReExport.from_dict,
            ),
        )


__all__ = [
    "COMPONENT_INTERFACE_BINDING_SCHEMA",
    "PUBLIC_INTERFACE_CONTRACT_SCHEMA",
    "ComponentInterfaceBinding",
    "CompatibilityPolicy",
    "CompatibilityPromise",
    "ExportedProtocol",
    "ExportedType",
    "IntentionalInterfaceReExport",
    "InterfaceError",
    "PublicInterfaceContract",
]

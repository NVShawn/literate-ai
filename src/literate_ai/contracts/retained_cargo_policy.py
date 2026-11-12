"""Reviewed v2 retained gate policy with explicit, identity-bound input custody.

The common command/tool fields retain their existing validation. This separate
codec cannot be parsed as a v1 policy and never implicitly widens v1 input walks.
Version-matched plans, receiving bindings, execution custody and independent
acceptance remain required before this record can participate in admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail
from .identity import ContentIdentity, canonical_identity
from .repositories import RepositoryBuildCommand
from .retained_cargo_custody import RetainedCargoCustodyPolicy
from .retained_cargo_native_custody import RetainedCargoNativeCustodyPolicy
from .retained_libraries import RetainedLibraryGatePolicy


@dataclass(frozen=True, slots=True)
class RetainedLibraryGatePolicyV2:
    common: RetainedLibraryGatePolicy
    input_custody: RetainedCargoCustodyPolicy
    native_custody: RetainedCargoNativeCustodyPolicy | None = None

    SCHEMA: ClassVar[str] = "literate-ai/retained-library-gate-policy@2"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "importer_project_id",
            "commands",
            "toolchains",
            "external_input_variables",
            "input_custody",
        }
    )

    def __post_init__(self) -> None:
        if type(self.common) is not RetainedLibraryGatePolicy:
            fail(self.__class__.__name__, "requires exact common gate declarations")
        if not isinstance(self.input_custody, RetainedCargoCustodyPolicy):
            fail(self.__class__.__name__, "requires explicit typed v2 input custody")
        self.input_custody.require_external_variables(
            self.common.external_input_variables
        )
        if (
            self.native_custody is not None
            and type(self.native_custody) is not RetainedCargoNativeCustodyPolicy
        ):
            fail(self.__class__.__name__, "requires exact native output custody")

    @property
    def importer_project_id(self) -> str:
        return self.common.importer_project_id

    @property
    def commands(self) -> tuple[RepositoryBuildCommand, ...]:
        return self.common.commands

    @property
    def toolchains(self) -> tuple[ContentIdentity, ...]:
        return self.common.toolchains

    @property
    def external_input_variables(self) -> tuple[str, ...]:
        return self.common.external_input_variables

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        result = self.common.to_dict()
        result.update(
            schema=self.SCHEMA,
            external_input_variables=list(self.external_input_variables),
            input_custody=self.input_custody.to_dict(),
        )
        if self.native_custody is not None:
            result["native_custody"] = self.native_custody.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Any) -> RetainedLibraryGatePolicyV2:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=cls.FIELDS,
            optional=frozenset({"native_custody"}),
        )
        common = {key: data[key] for key in cls.FIELDS if key != "input_custody"}
        common["schema"] = RetainedLibraryGatePolicy.SCHEMA
        return cls(
            RetainedLibraryGatePolicy.from_dict(common),
            RetainedCargoCustodyPolicy.from_dict(data["input_custody"]),
            RetainedCargoNativeCustodyPolicy.from_dict(data["native_custody"])
            if "native_custody" in data
            else None,
        )

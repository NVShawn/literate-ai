"""Explicit v2 Cargo input budgets; values never grant traversal or execution.

The receiving gate must bind the complete reviewed owner set to its declared
workspace, provisioned CARGO_HOME and exact external-input variables. Physical
ownership, closed topology and current bytes remain the capture owner's job.
These values do not reinterpret v1 readers or supply an implicit larger default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import contract_fields, fail, fields, int_value, list_value
from .identity import ContentIdentity, canonical_identity


@dataclass(frozen=True, slots=True)
class RetainedCargoInputBudget:
    maximum_files: int
    maximum_entries: int
    maximum_file_bytes: int
    maximum_total_bytes: int

    MAXIMUM_FILES: ClassVar[int] = 1_000_000
    MAXIMUM_ENTRIES: ClassVar[int] = 4_000_000
    MAXIMUM_FILE_BYTES: ClassVar[int] = 256 * 1024**2
    MAXIMUM_TOTAL_BYTES: ClassVar[int] = 4 * 1024**3
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "maximum_files",
            "maximum_entries",
            "maximum_file_bytes",
            "maximum_total_bytes",
        }
    )

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("maximum_files", self.MAXIMUM_FILES),
            ("maximum_entries", self.MAXIMUM_ENTRIES),
            ("maximum_file_bytes", self.MAXIMUM_FILE_BYTES),
            ("maximum_total_bytes", self.MAXIMUM_TOTAL_BYTES),
        ):
            int_value(
                getattr(self, name),
                "RetainedCargoInputBudget." + name,
                minimum=1,
                maximum=ceiling,
            )
        if (
            self.maximum_files > self.maximum_entries
            or self.maximum_file_bytes > self.maximum_total_bytes
            or self.maximum_total_bytes > self.maximum_files * self.maximum_file_bytes
        ):
            fail("RetainedCargoInputBudget", "requires consistent finite allowances")

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in sorted(self.FIELDS)}

    @classmethod
    def from_dict(cls, value: Any) -> RetainedCargoInputBudget:
        data = fields(value, path=cls.__name__, required=cls.FIELDS)
        return cls(
            data["maximum_files"],
            data["maximum_entries"],
            data["maximum_file_bytes"],
            data["maximum_total_bytes"],
        )


@dataclass(frozen=True, slots=True)
class RetainedCargoOwnerBudget:
    scope: str
    budget: RetainedCargoInputBudget

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or (
            self.scope not in {"workspace", "cargo-home"}
            and re.fullmatch(r"external:[A-Z_][A-Z0-9_]{0,127}", self.scope) is None
        ):
            fail("RetainedCargoOwnerBudget.scope", "requires a declared input owner")
        if not isinstance(self.budget, RetainedCargoInputBudget):
            fail("RetainedCargoOwnerBudget.budget", "requires a typed finite budget")

    def to_dict(self) -> dict[str, object]:
        return {"scope": self.scope, "budget": self.budget.to_dict()}

    @classmethod
    def from_dict(cls, value: Any) -> RetainedCargoOwnerBudget:
        data = fields(value, path=cls.__name__, required=frozenset({"scope", "budget"}))
        return cls(data["scope"], RetainedCargoInputBudget.from_dict(data["budget"]))


@dataclass(frozen=True, slots=True)
class RetainedCargoCustodyPolicy:
    owners: tuple[RetainedCargoOwnerBudget, ...]
    aggregate: RetainedCargoInputBudget

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-custody-policy@2"
    MAXIMUM_OWNERS: ClassVar[int] = 66

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owners, tuple)
            or not 2 <= len(self.owners) <= self.MAXIMUM_OWNERS
            or any(
                not isinstance(item, RetainedCargoOwnerBudget) for item in self.owners
            )
        ):
            fail("RetainedCargoCustodyPolicy.owners", "requires bounded typed owners")
        scopes = tuple(item.scope for item in self.owners)
        if scopes != tuple(sorted(set(scopes))) or not {
            "workspace",
            "cargo-home",
        } <= set(scopes):
            fail(
                "RetainedCargoCustodyPolicy.owners",
                "requires distinct ordered workspace and Cargo home owners",
            )
        if not isinstance(self.aggregate, RetainedCargoInputBudget):
            fail(
                "RetainedCargoCustodyPolicy.aggregate", "requires a typed finite budget"
            )
        for item in self.owners:
            if any(
                getattr(item.budget, name) > getattr(self.aggregate, name)
                for name in RetainedCargoInputBudget.FIELDS
            ):
                fail(
                    "RetainedCargoCustodyPolicy.aggregate",
                    "an owner allowance exceeds the aggregate allowance",
                )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def require_external_variables(self, variables: tuple[str, ...]) -> None:
        """Require the same exact owner set at the receiving v2 policy boundary."""
        if (
            not isinstance(variables, tuple)
            or len(variables) > 64
            or any(
                not isinstance(name, str)
                or re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) is None
                for name in variables
            )
            or variables != tuple(sorted(set(variables)))
        ):
            fail(
                "RetainedCargoCustodyPolicy.variables", "requires exact declared names"
            )
        expected = {"workspace", "cargo-home"} | {
            "external:" + name for name in variables
        }
        if {item.scope for item in self.owners} != expected:
            fail("RetainedCargoCustodyPolicy.owners", "input owner declarations differ")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "owners": [item.to_dict() for item in self.owners],
            "aggregate": self.aggregate.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> RetainedCargoCustodyPolicy:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"owners", "aggregate"}),
        )
        owners = list_value(data["owners"], cls.__name__ + ".owners")
        if not 2 <= len(owners) <= cls.MAXIMUM_OWNERS:
            fail(cls.__name__ + ".owners", "owner count exceeds its finite allowance")
        return cls(
            tuple(RetainedCargoOwnerBudget.from_dict(item) for item in owners),
            RetainedCargoInputBudget.from_dict(data["aggregate"]),
        )

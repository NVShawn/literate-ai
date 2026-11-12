"""Opt-in byte allowances for complete generated native output custody.

These limits grant no source exclusions, publication, execution or admission.
Existing input-tree budgets and native defaults remain separate and unchanged.
"""

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail, fields, int_value
from .identity import canonical_identity


@dataclass(frozen=True, slots=True)
class RetainedCargoOutputBudget:
    maximum_file_bytes: int
    maximum_total_bytes: int

    MAXIMUM_FILE_BYTES: ClassVar[int] = 1024**3
    MAXIMUM_TOTAL_BYTES: ClassVar[int] = 64 * 1024**3
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"maximum_file_bytes", "maximum_total_bytes"}
    )

    def __post_init__(self):
        for name, ceiling in (
            ("maximum_file_bytes", self.MAXIMUM_FILE_BYTES),
            ("maximum_total_bytes", self.MAXIMUM_TOTAL_BYTES),
        ):
            int_value(getattr(self, name), name, minimum=1, maximum=ceiling)
        if self.maximum_file_bytes > self.maximum_total_bytes:
            fail(self.__class__.__name__, "member allowance exceeds aggregate")

    def to_dict(self):
        return {name: getattr(self, name) for name in sorted(self.FIELDS)}

    @classmethod
    def from_dict(cls, value):
        data = fields(value, path=cls.__name__, required=cls.FIELDS)
        return cls(data["maximum_file_bytes"], data["maximum_total_bytes"])


@dataclass(frozen=True, slots=True)
class RetainedCargoNativeCustodyPolicy:
    executables: RetainedCargoOutputBudget
    runtime: RetainedCargoOutputBudget
    build_outputs: RetainedCargoOutputBudget

    SCHEMA: ClassVar[str] = "literate-ai/retained-cargo-native-custody@1"
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"executables", "runtime", "build_outputs"}
    )

    def __post_init__(self):
        if any(
            type(getattr(self, name)) is not RetainedCargoOutputBudget
            for name in self.FIELDS
        ):
            fail(self.__class__.__name__, "requires exact bounded output declarations")
        if (
            sum(getattr(self, name).maximum_total_bytes for name in self.FIELDS)
            > RetainedCargoOutputBudget.MAXIMUM_TOTAL_BYTES
        ):
            fail(self.__class__.__name__, "combined allowances exceed hard ceiling")

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            **{name: getattr(self, name).to_dict() for name in sorted(self.FIELDS)},
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value, path=cls.__name__, schema_uri=cls.SCHEMA, required=cls.FIELDS
        )
        return cls(
            *(
                RetainedCargoOutputBudget.from_dict(data[name])
                for name in ("executables", "runtime", "build_outputs")
            )
        )

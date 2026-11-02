"""Explicit operator package budgets, separate from source inventory limits."""

from dataclasses import dataclass, fields
from typing import ClassVar

from ._validation import contract_fields
from .identity import contract_identity
from .native_tree_shards import MAX_TREE_MEMBERS, _integer
from .retained_project import RetainedProjectLimits


@dataclass(frozen=True, slots=True)
class RetainedUnitPackagePolicy:
    limits: RetainedProjectLimits
    per_unit_limits: RetainedProjectLimits
    max_units: int
    max_dependencies: int
    max_metadata_bytes: int
    max_staging_bytes: int
    max_transport_bytes: int
    max_archive_bytes: int
    max_total_archive_bytes: int
    max_descriptor_bytes: int = 8 * 1024**2
    max_descriptor_total_bytes: int = 512 * 1024**2
    max_index_bytes: int = 2 * 1024**2
    max_metadata_memory_bytes: int = 1024**3
    max_closure_steps: int = 16_000_000
    max_closure_memory_bytes: int = 1024**3
    max_provider_metadata_bytes: int = 64 * 1024**2
    max_provider_memory_bytes: int = 256 * 1024**2
    max_verified_record_cache_bytes: int = 1024**3

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-policy@1"

    def __post_init__(self):
        for limits in (self.limits, self.per_unit_limits):
            if not isinstance(limits, RetainedProjectLimits):
                raise TypeError("retained.unit.explicit-package-limits-required")
            if limits.max_entries > MAX_TREE_MEMBERS:
                raise ValueError("retained.unit.package-tree-hard-limit-exceeded")
        ceilings = {
            "max_units": 1_000_000,
            "max_dependencies": 4_000_000,
            "max_metadata_bytes": 512 * 1024**2,
            "max_descriptor_bytes": 8 * 1024**2,
            "max_descriptor_total_bytes": 512 * 1024**2,
            "max_index_bytes": 2 * 1024**2,
            "max_metadata_memory_bytes": 16 * 1024**3,
            "max_closure_steps": 16_000_000,
            "max_closure_memory_bytes": 16 * 1024**3,
            "max_provider_metadata_bytes": 512 * 1024**2,
            "max_provider_memory_bytes": 16 * 1024**3,
            "max_verified_record_cache_bytes": 16 * 1024**3,
        }
        for name, value in self.owner_arguments().items():
            if name not in {"limits", "per_unit_limits"}:
                _integer(value, 1, ceilings.get(name, 2**63 - 1), "unit-policy-" + name)

    def owner_arguments(self):
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            **self.owner_arguments(),
            "limits": self.limits.to_dict(),
            "per_unit_limits": self.per_unit_limits.to_dict(),
        }

    @property
    def identity(self):
        return contract_identity(self)

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(field.name for field in fields(cls)),
        )
        return cls(
            **{
                key: RetainedProjectLimits.from_dict(item)
                if key in {"limits", "per_unit_limits"}
                else item
                for key, item in data.items()
                if key != "schema"
            }
        )

"""Exact native coordinates and retained evidence inputs for one package unit.

This descriptor is mechanical. The live retained owner must establish the source,
lock, recipe, BOM and license bindings before construction; decoding cannot do so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar

from literate_ai.adapters import freebsd_packaging as native
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts._validation import contract_fields
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
)
from literate_ai.contracts.identity import contract_identity
from literate_ai.contracts.paths import canonical_relative_posix_paths

UNIT_PACKAGE_EVIDENCE_FIELDS = MappingProxyType(
    {
        "component-authoring": "component_authoring_identity",
        "component-lock": "component_lock_identity",
        "retained-project-profile": "recipe_identity",
        "retained-source-manifest": "retained_source_identity",
        "cyclonedx-source-sbom": "source_bom_identity",
        "cyclonedx-resolved-sbom": "resolved_bom_identity",
        "retained-receipt": "receipt_identity",
        "license": "license_identity",
    }
)


@dataclass(frozen=True, slots=True)
class FreeBsdRetainedUnitParameters:
    package: str
    origin: str
    prefix: str
    architecture: str
    evidence: tuple[PackageInput, ...]

    SCHEMA: ClassVar[str] = "literate-ai/freebsd-retained-unit-parameters@1"

    def __post_init__(self):
        if (
            not isinstance(self.package, str)
            or native._NAME.fullmatch(self.package) is None
        ):
            raise PackagingError("Retained native unit package name is invalid")
        native._origin(self.origin)
        native._absolute(self.prefix)
        if (
            not isinstance(self.architecture, str)
            or re.fullmatch(r"[A-Za-z0-9:._-]{1,96}", self.architecture) is None
        ):
            raise PackagingError("Retained native unit architecture is invalid")
        if (
            type(self.evidence) is not tuple
            or not 1 <= len(self.evidence) <= 64
            or any(not isinstance(value, PackageInput) for value in self.evidence)
        ):
            raise PackagingError(
                "Retained native unit requires bounded typed evidence inputs"
            )
        paths = tuple(value.path for value in self.evidence)
        if paths != tuple(sorted(set(paths))):
            raise PackagingError(
                "Retained native unit evidence paths are not canonical"
            )
        canonical_relative_posix_paths(paths, label="retained native unit evidence")
        if any(
            value.kind is not PackageFileKind.RESOURCE or value.executable
            for value in self.evidence
        ):
            raise PackagingError(
                "Retained native unit evidence is a nonexecutable resource"
            )
        if any(
            value.role not in {*UNIT_PACKAGE_EVIDENCE_FIELDS, "component-specification"}
            for value in self.evidence
        ):
            raise PackagingError("Retained native unit evidence role is unsupported")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "package": self.package,
            "origin": self.origin,
            "prefix": self.prefix,
            "architecture": self.architecture,
            "evidence": [value.to_dict() for value in self.evidence],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"package", "origin", "prefix", "architecture", "evidence"}
            ),
        )
        if type(data["evidence"]) is not list or not 1 <= len(data["evidence"]) <= 64:
            raise PackagingError(
                "Retained native unit requires bounded evidence inputs"
            )
        return cls(
            *(data[name] for name in ("package", "origin", "prefix", "architecture")),
            tuple(PackageInput.from_dict(value) for value in data["evidence"]),
        )

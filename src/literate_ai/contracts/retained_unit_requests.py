"""Authored output ownership; only exact locked supporting specifications apply.

Requests contain intent, not computed lock/graph/archive identities. The live
retained owner resolves their selected Component, actual payload, target and
source license bytes before creating non-circular unit definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields
from .executable_components._common import portable_name
from .identity import contract_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths

MAX_UNIT_REQUEST_ROWS = 16384


@dataclass(frozen=True, slots=True)
class RetainedUnitDependencyRequest:
    unit_name: str
    kind: str
    required_abi: str | None = None
    required_version: str | None = None

    def __post_init__(self):
        portable_name(self.unit_name, "retained unit dependency")
        if self.kind not in {"build-input", "runtime", "tree-link"}:
            raise ValueError("retained.unit.typed-dependency-kind-required")
        for value in (self.required_abi, self.required_version):
            if value is None and self.kind == "tree-link":
                continue
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 255
                or any(character in value for character in "\r\n\x00")
            ):
                raise ValueError("retained.unit.exact-abi-version-binding-required")

    def to_dict(self):
        return {
            "unit_name": self.unit_name,
            "kind": self.kind,
            "required_abi": self.required_abi,
            "required_version": self.required_version,
        }

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {
            "unit_name",
            "kind",
            "required_abi",
            "required_version",
        }:
            raise ValueError("retained.unit.dependency-request-fields-invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class RetainedUnitPackageRequest:
    unit_name: str
    output_prefixes: tuple[str, ...]
    whole_payload: bool
    license_path: str
    package: str
    version: str
    origin: str
    prefix: str
    dependencies: tuple[RetainedUnitDependencyRequest, ...] = ()

    SCHEMA: ClassVar[str] = "literate-ai/retained-unit-package-request@1"

    def __post_init__(self):
        portable_name(self.unit_name, "retained unit request")
        if (
            type(self.whole_payload) is not bool
            or type(self.output_prefixes) is not tuple
            or len(self.output_prefixes) > MAX_UNIT_REQUEST_ROWS
            or bool(self.output_prefixes) == self.whole_payload
        ):
            raise ValueError("retained.unit.explicit-output-selection-required")
        if self.output_prefixes != tuple(sorted(set(self.output_prefixes))):
            raise ValueError("retained.unit.canonical-output-prefixes-required")
        canonical_relative_posix_paths(
            self.output_prefixes, label="retained unit output prefixes"
        )
        canonical_relative_posix_path(
            self.license_path, label="retained unit source license"
        )
        # Native syntax is checked again by the selected provider. Intent must
        # already be bounded nonempty text without control characters.
        for value in (self.package, self.version, self.origin, self.prefix):
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 4096
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError("retained.unit.bounded-native-coordinate-required")
        if (
            type(self.dependencies) is not tuple
            or len(self.dependencies) > MAX_UNIT_REQUEST_ROWS
            or any(
                not isinstance(value, RetainedUnitDependencyRequest)
                for value in self.dependencies
            )
        ):
            raise ValueError("retained.unit.bounded-typed-dependency-requests-required")
        keys = tuple((value.unit_name, value.kind) for value in self.dependencies)
        if keys != tuple(sorted(set(keys))) or any(
            value.unit_name == self.unit_name for value in self.dependencies
        ):
            raise ValueError(
                "retained.unit.canonical-nonself-dependency-requests-required"
            )

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "unit_name": self.unit_name,
            "output_prefixes": list(self.output_prefixes),
            "whole_payload": self.whole_payload,
            "license_path": self.license_path,
            "package": self.package,
            "version": self.version,
            "origin": self.origin,
            "prefix": self.prefix,
            "dependencies": [value.to_dict() for value in self.dependencies],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "unit_name",
                    "output_prefixes",
                    "whole_payload",
                    "license_path",
                    "package",
                    "version",
                    "origin",
                    "prefix",
                    "dependencies",
                }
            ),
        )
        for name in ("output_prefixes", "dependencies"):
            if type(data[name]) is not list or len(data[name]) > MAX_UNIT_REQUEST_ROWS:
                raise ValueError("retained.unit.bounded-request-rows-required")
        return cls(
            data["unit_name"],
            tuple(data["output_prefixes"]),
            data["whole_payload"],
            *(
                data[name]
                for name in ("license_path", "package", "version", "origin", "prefix")
            ),
            tuple(
                RetainedUnitDependencyRequest.from_dict(value)
                for value in data["dependencies"]
            ),
        )

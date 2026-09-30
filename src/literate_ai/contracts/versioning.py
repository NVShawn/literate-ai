"""Strict Semantic Versioning shared by every first-class domain object.

Distribution releases deliberately do not use this contract: Python package
versions remain PEP 440 values (for example ``0.1.1a1``).  Component-domain
versions use the complete SemVer 2.0.0 grammar and comparison rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from ._validation import fail, string_value

_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True, slots=True)
class SemanticVersion:
    """A parsed, canonical SemVer 2.0.0 value."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(self.major, self.minor, self.patch) < 0:
            raise ValueError("semantic version numbers cannot be negative")
        # Construction and parsing must have identical validation behavior.
        reparsed = type(self).parse(str(self), _construction=True)
        if reparsed != self:
            raise ValueError("semantic version is not canonical")

    @classmethod
    def parse(cls, value: str, *, _construction: bool = False) -> SemanticVersion:
        if not isinstance(value, str) or not value:
            raise ValueError("semantic version must be a non-empty string")
        match = _SEMVER_RE.fullmatch(value)
        if match is None:
            raise ValueError(f"invalid semantic version {value!r}")
        major, minor, patch, prerelease, build = match.groups()
        parsed = object.__new__(cls) if _construction else None
        values = (
            int(major),
            int(minor),
            int(patch),
            tuple(prerelease.split(".")) if prerelease else (),
            tuple(build.split(".")) if build else (),
        )
        if parsed is not None:
            for name, item in zip(
                ("major", "minor", "patch", "prerelease", "build"),
                values,
                strict=True,
            ):
                object.__setattr__(parsed, name, item)
            return parsed
        return cls(*values)

    @property
    def precedence_key(self) -> tuple[object, ...]:
        prerelease: tuple[tuple[int, object], ...]
        if not self.prerelease:
            prerelease = ((2, 0),)
        else:
            prerelease = tuple(
                (0, int(item)) if item.isdigit() else (1, item)
                for item in self.prerelease
            )
        return self.major, self.minor, self.patch, prerelease

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self.precedence_key < other.precedence_key

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value


def semantic_version(value: object, path: str) -> str:
    """Validate and return an exact canonical SemVer string for a contract."""

    raw = string_value(value, path)
    try:
        parsed = SemanticVersion.parse(raw)
    except ValueError as exc:
        fail(path, str(exc))
    if str(parsed) != raw:
        fail(path, "must use canonical Semantic Versioning 2.0.0 form")
    return raw


__all__ = ["SemanticVersion", "semantic_version"]

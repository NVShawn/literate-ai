"""Opt-in verifier-owned filesystem cases; these records grant no execution."""

import base64
import binascii
import re
from dataclasses import dataclass

from .identity import canonical_identity
from .paths import canonical_relative_posix_paths
from .retained_project import RetainedProjectLimits
from .retained_project_lifecycle import _fields


def _ordered(values, kind):
    if (
        type(values) is not tuple
        or len(values) > 128
        or any(not isinstance(v, kind) for v in values)
    ):
        raise ValueError("retained.filesystem.typed-bounded-entries-required")
    names = tuple(v.path for v in values)
    if names != tuple(sorted(names)) or len(set(names)) != len(names):
        raise ValueError("retained.filesystem.canonical-order-required")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("retained.filesystem.aliased-paths")
    prefixes = {}
    for name in names:
        while name:
            if prefixes.setdefault(name.casefold(), name) != name:
                raise ValueError("retained.filesystem.aliased-paths")
            name = name.rpartition("/")[0]


@dataclass(frozen=True, slots=True)
class RetainedFilesystemInput:
    path: str
    content: bytes

    def __post_init__(self):
        canonical_relative_posix_paths((self.path,), label="verifier fixture")
        if type(self.content) is not bytes or len(self.content) > 64 * 1024:
            raise ValueError("retained.filesystem.fixture-budget")

    def to_dict(self):
        return {
            "path": self.path,
            "content": base64.b64encode(self.content).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"path", "content"})
        encoded = value["content"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((64 * 1024 + 2) // 3):
            raise ValueError("retained.filesystem.fixture-budget")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("retained.filesystem.fixture-encoding") from None
        if base64.b64encode(content).decode("ascii") != encoded:
            raise ValueError("retained.filesystem.fixture-encoding")
        return cls(value["path"], content)


@dataclass(frozen=True, slots=True)
class RetainedFilesystemState:
    path: str
    kind: str
    size: int | None = None
    sha256: str | None = None
    mode: int | None = None

    def __post_init__(self):
        canonical_relative_posix_paths((self.path,), label="verifier file state")
        if self.kind not in ("file", "directory", "absent"):
            raise ValueError("retained.filesystem.unsupported-kind")
        if self.kind == "file":
            if (
                type(self.size) is not int
                or not 0 <= self.size < 2**63
                or (
                    not isinstance(self.sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
                )
            ):
                raise ValueError("retained.filesystem.exact-file-content-required")
        elif self.size is not None or self.sha256 is not None:
            raise ValueError("retained.filesystem.nonfile-content")
        if self.mode is not None and (
            self.kind == "absent"
            or type(self.mode) is not int
            or not 0 <= self.mode <= 0o7777
        ):
            raise ValueError("retained.filesystem.invalid-mode")

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, value):
        return cls(**_fields(value, set(cls.__slots__)))


@dataclass(frozen=True, slots=True)
class RetainedFilesystemCase:
    inputs: tuple[RetainedFilesystemInput, ...]
    expected: tuple[RetainedFilesystemState, ...]
    limits: RetainedProjectLimits

    def __post_init__(self):
        _ordered(self.inputs, RetainedFilesystemInput)
        _ordered(self.expected, RetainedFilesystemState)
        if not self.expected or not isinstance(self.limits, RetainedProjectLimits):
            raise ValueError("retained.filesystem.expectations-and-limits-required")
        expected = {v.path: v for v in self.expected}
        if not {v.path for v in self.inputs} <= set(expected):
            raise ValueError("retained.filesystem.fixture-final-state-required")
        for values in (self.inputs, self.expected):
            by_path = {v.path: v for v in values}
            for value in values:
                parent = value.path.rpartition("/")[0]
                while parent:
                    if parent in by_path and (
                        isinstance(by_path[parent], RetainedFilesystemInput)
                        or by_path[parent].kind != "directory"
                    ):
                        raise ValueError("retained.filesystem.path-collision")
                    parent = parent.rpartition("/")[0]
        inventories = []
        for values in (self.inputs, self.expected):
            nodes = set()
            for value in values:
                name = value.path
                while name:
                    nodes.add(name)
                    name = name.rpartition("/")[0]
            inventories.append(nodes)
        if max(len(v) for v in inventories) > self.limits.max_entries or (
            any(len(v.content) > self.limits.max_file_bytes for v in self.inputs)
            or sum(len(v.content) for v in self.inputs) > self.limits.max_total_bytes
            or any(
                v.kind == "file" and v.size > self.limits.max_file_bytes
                for v in self.expected
            )
            or sum(v.size or 0 for v in self.expected) > self.limits.max_total_bytes
        ):
            raise ValueError("retained.filesystem.declared-budget-exceeded")

    def to_dict(self):
        return {
            "schema": "literate-ai/retained-case-filesystem@1",
            "inputs": [v.to_dict() for v in self.inputs],
            "expected": [v.to_dict() for v in self.expected],
            "limits": self.limits.to_dict(),
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"schema", "inputs", "expected", "limits"})
        if value["schema"] != "literate-ai/retained-case-filesystem@1" or any(
            not isinstance(value[k], list) or len(value[k]) > 128
            for k in ("inputs", "expected")
        ):
            raise ValueError("retained.filesystem.invalid-case")
        return cls(
            tuple(RetainedFilesystemInput.from_dict(v) for v in value["inputs"]),
            tuple(RetainedFilesystemState.from_dict(v) for v in value["expected"]),
            RetainedProjectLimits.from_dict(value["limits"]),
        )

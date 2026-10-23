"""Inventoried SDK trees; validation reads no host paths and grants no admission."""

from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    fields,
    list_value,
    string_tuple,
    string_value,
)
from .identity import ContentIdentity, contract_identity
from .native_sdks import NativeSdkFile
from .paths import canonical_relative_posix_path

_MAX_MEMBERS = 16384
_MAX_EXPANSIONS = 64
_MAX_RESOLUTION_STEPS = 1048576


@dataclass(slots=True)
class _ResolutionBudget:
    remaining: int = _MAX_RESOLUTION_STEPS

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ValueError("SDK alias resolution work limit exceeded")


@dataclass(frozen=True, slots=True)
class NativeSdkAlias:
    """Symbolic target text or canonical root-relative hard-link ownership."""

    path: str
    target: str
    kind: str = "symlink"

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.path, label="SDK alias path")
        string_value(self.target, "SDK alias target")
        if len(self.target.encode("utf-8")) > 4096:
            raise ValueError("SDK alias target exceeds 4096 bytes")
        if self.kind not in {"symlink", "hardlink"}:
            raise ValueError("SDK alias kind must be symlink or hardlink")
        if self.kind == "hardlink":
            canonical_relative_posix_path(self.target, label="SDK hard-link target")
            if self.target >= self.path:
                raise ValueError(
                    "SDK hard-link target must be its first canonical name"
                )
            return
        for part in self.target.split("/"):
            if part not in {".", ".."}:
                canonical_relative_posix_path(part, label="SDK alias target segment")

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "target": self.target, "kind": self.kind}

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkAlias:
        data = fields(
            value, path=cls.__name__, required=frozenset({"path", "target", "kind"})
        )
        return cls(
            string_value(data["path"], "SDK alias path"),
            string_value(data["target"], "SDK alias target"),
            string_value(data["kind"], "SDK alias kind"),
        )


@dataclass(frozen=True, slots=True)
class NativeSdkTree:
    """Exact physical inventory with bounded internal alias resolution.

    The root directory is implicit. Physical members never descend through aliases.
    This contract is deliberately separate from the legacy regular-file snapshot:
    constructing a tree does not enable alias capture or authenticate its producer.
    """

    directories: tuple[str, ...]
    files: tuple[NativeSdkFile, ...]
    aliases: tuple[NativeSdkAlias, ...]
    case_sensitive: bool = False

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:native-sdk-tree@1"

    def __post_init__(self) -> None:
        if type(self.case_sensitive) is not bool:
            raise TypeError("SDK case sensitivity requires explicit boolean selection")
        if (
            not isinstance(self.directories, tuple)
            or not isinstance(self.files, tuple)
            or not isinstance(self.aliases, tuple)
            or not self.files
            or sum(map(len, (self.directories, self.files, self.aliases)))
            > _MAX_MEMBERS
        ):
            raise ValueError("SDK tree requires tuples and 1 to 16384 physical members")
        if any(not isinstance(item, NativeSdkFile) for item in self.files) or any(
            not isinstance(item, NativeSdkAlias) for item in self.aliases
        ):
            raise ValueError("SDK tree requires typed files and aliases")
        groups = (
            self.directories,
            tuple(item.path for item in self.files),
            tuple(item.path for item in self.aliases),
        )
        names: dict[str, str] = {}
        for group in groups:
            for name in group:
                canonical_relative_posix_path(name, label="SDK tree member")
                key = (
                    name
                    if self.case_sensitive
                    else unicodedata.normalize("NFC", name.casefold())
                )
                if key in names:
                    raise ValueError("SDK tree has ambiguous member ownership")
                names[key] = name
            if group != tuple(sorted(group)):
                raise ValueError("SDK tree members must be canonically ordered")
        directories = set(self.directories)
        for name in names.values():
            for parent in PurePosixPath(name).parents:
                if (
                    parent != PurePosixPath(".")
                    and parent.as_posix() not in directories
                ):
                    raise ValueError(
                        "SDK tree member lacks a physical directory ancestor"
                    )
        index = self._index()
        budget = _ResolutionBudget()
        for alias in self.aliases:
            if alias.kind == "hardlink" and alias.target not in index[1]:
                raise ValueError("SDK hard-link target must be a physical regular file")
            self._resolve(alias.path, *index, budget)

    def _index(self) -> tuple[set[str], set[str], dict[str, NativeSdkAlias]]:
        return (
            set(self.directories),
            {item.path for item in self.files},
            {item.path: item for item in self.aliases},
        )

    @staticmethod
    def _resolve(
        path: str,
        directories: set[str],
        files: set[str],
        aliases: dict[str, NativeSdkAlias],
        budget: _ResolutionBudget,
    ) -> str:
        pending = deque(path.split("/"))
        resolved: list[str] = []
        expansions = 0
        while pending:
            budget.consume()
            part = pending.popleft()
            current = "/".join(resolved)
            if resolved and current not in directories:
                raise ValueError("SDK alias traverses a non-directory")
            if part == ".":
                continue
            if part == "..":
                if not resolved:
                    raise ValueError("SDK alias escapes its inventoried root")
                resolved.pop()
                continue
            candidate = "/".join((*resolved, part))
            if candidate in aliases:
                expansions += 1
                if expansions > _MAX_EXPANSIONS:
                    raise ValueError("SDK alias cycle or expansion limit exceeded")
                alias = aliases[candidate]
                if alias.kind == "hardlink":
                    if alias.target not in files:
                        raise ValueError(
                            "SDK hard-link target must be a physical regular file"
                        )
                    resolved = alias.target.split("/")
                else:
                    pending.extendleft(reversed(alias.target.split("/")))
            elif candidate in directories or candidate in files:
                resolved.append(part)
            else:
                raise ValueError("SDK alias target is absent from its inventoried tree")
        return "/".join(resolved) or "."

    def resolve(self, path: str) -> str:
        """Resolve an inventoried logical path without consulting the filesystem."""
        return self.resolve_all((path,))[0]

    def resolve_all(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        """Resolve a bounded batch with one local index and aggregate work budget."""
        if not isinstance(paths, tuple) or len(paths) > _MAX_MEMBERS:
            raise ValueError("SDK logical paths require a bounded tuple")
        index = self._index()
        budget = _ResolutionBudget()
        result = []
        for path in paths:
            canonical_relative_posix_path(path, label="SDK logical path")
            result.append(self._resolve(path, *index, budget))
        return tuple(result)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "directories": list(self.directories),
            "files": [item.to_dict() for item in self.files],
            "aliases": [item.to_dict() for item in self.aliases],
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkTree:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"directories", "files", "aliases", "case_sensitive"}),
        )
        return cls(
            string_tuple(data["directories"], "SDK directories"),
            tuple(
                NativeSdkFile.from_dict(item)
                for item in list_value(data["files"], "SDK files")
            ),
            tuple(
                NativeSdkAlias.from_dict(item)
                for item in list_value(data["aliases"], "SDK aliases")
            ),
            bool_value(data["case_sensitive"], "SDK case sensitivity"),
        )

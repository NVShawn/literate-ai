"""Explicit package tree provenance; no producer, execution or package admission."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import contract_fields, string_value
from .identity import ContentIdentity, contract_identity
from .native_sdk_trees import NativeSdkTree
from .paths import canonical_relative_posix_path


class PackageTreeKind(StrEnum):
    ARTIFACT = "artifact"
    RESOURCE = "resource"


@dataclass(frozen=True, slots=True)
class PackageTreeInput:
    """A complete tree owns one prefix; physical files are never its whole closure."""

    prefix: str
    role: str
    source_identity: ContentIdentity
    target_identity: ContentIdentity
    tree: NativeSdkTree
    kind: PackageTreeKind = PackageTreeKind.RESOURCE

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:package-tree-input@1"

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.prefix, label="PackageTreeInput.prefix")
        string_value(self.role, "PackageTreeInput.role", max_length=255)
        if not isinstance(self.kind, PackageTreeKind):
            raise TypeError("Package tree requires explicit artifact or resource kind")
        if any(
            not isinstance(value, ContentIdentity)
            for value in (self.source_identity, self.target_identity)
        ):
            raise TypeError("Package tree requires typed source and target identities")
        if not isinstance(self.tree, NativeSdkTree):
            raise TypeError("Package tree requires complete canonical tree authority")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "prefix": self.prefix,
            "role": self.role,
            "kind": self.kind.value,
            "source_identity": self.source_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "tree": self.tree.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "PackageTreeInput"
    ) -> PackageTreeInput:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"prefix", "role", "kind", "source_identity", "target_identity", "tree"}
            ),
        )
        return cls(
            data["prefix"],
            data["role"],
            ContentIdentity.from_dict(data["source_identity"]),
            ContentIdentity.from_dict(data["target_identity"]),
            NativeSdkTree.from_dict(data["tree"]),
            PackageTreeKind(data["kind"]),
        )


def require_package_tree_namespace(
    trees: tuple[PackageTreeInput, ...],
    regular_paths: tuple[str, ...],
    *,
    target_identity: ContentIdentity,
    case_sensitive: bool = False,
) -> None:
    """Reserve complete tree prefixes; never merge independent alias closures."""
    if (
        type(trees) is not tuple
        or type(regular_paths) is not tuple
        or type(case_sensitive) is not bool
        or not isinstance(target_identity, ContentIdentity)
        or any(not isinstance(tree, PackageTreeInput) for tree in trees)
    ):
        raise TypeError("Package tree namespace requires typed explicit authority")
    if any(tree.target_identity != target_identity for tree in trees):
        raise ValueError("Package tree target differs from its package")
    if not case_sensitive and any(tree.tree.case_sensitive for tree in trees):
        raise ValueError("Package tree requires case-sensitive namespace selection")
    names = (*regular_paths, *(tree.prefix for tree in trees))
    keys = set()
    spellings = {}
    for name in names:
        path = canonical_relative_posix_path(name, label="package namespace")
        key = tuple(
            part if case_sensitive else unicodedata.normalize("NFC", part.casefold())
            for part in path.parts
        )
        for depth in range(1, len(key) + 1):
            ancestor = key[:depth]
            spelling = path.parts[:depth]
            if ancestor in spellings and spellings[ancestor] != spelling:
                raise ValueError(
                    "Package tree namespace has aliased ancestor ownership"
                )
            spellings[ancestor] = spelling
        if key in keys:
            raise ValueError(
                "Package tree namespace has duplicate or aliased ownership"
            )
        keys.add(key)
    for key in keys:
        if any(key[:depth] in keys for depth in range(1, len(key))):
            raise ValueError("Package tree namespace has overlapping prefix ownership")

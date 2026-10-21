"""Bounded binary-capable input custody for retained-project execution.

These contracts identify inputs, never acceptance or conversion authority.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass
from typing import Any

from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.paths import (
    canonical_relative_posix_path,
    canonical_relative_posix_paths,
)
from literate_ai.contracts.retained_component_ownership import (
    RetainedComponentOwner,
    validate_retained_ownership,
)


@dataclass(frozen=True, slots=True)
class RetainedProjectLimits:
    max_entries: int
    max_file_bytes: int
    max_total_bytes: int

    def __post_init__(self) -> None:
        for value in (self.max_entries, self.max_file_bytes, self.max_total_bytes):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError("Retained input bounds must be positive integers")

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, value: Any) -> RetainedProjectLimits:
        _fields(value, set(cls.__slots__))
        return cls(**value)


def _fields(value: Any, fields: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Unexpected retained input contract fields")


@dataclass(frozen=True, slots=True)
class RetainedProjectTreePolicy:
    """Explicit faithful topology capability; no admission authority."""

    case_sensitive: bool = False

    def __post_init__(self) -> None:
        if type(self.case_sensitive) is not bool:
            raise TypeError("Retained tree naming requires a boolean selection")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/retained-project-tree-policy@1",
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, value: Any) -> RetainedProjectTreePolicy:
        _fields(value, {"schema", "case_sensitive"})
        if value["schema"] != "literate-ai/retained-project-tree-policy@1":
            raise ValueError("Unsupported retained tree policy")
        return cls(value["case_sensitive"])


def _retained_paths(values, *, label, case_sensitive=False):
    if not case_sensitive:
        return canonical_relative_posix_paths(values, label=label)
    paths = tuple(canonical_relative_posix_path(v, label=label) for v in values)
    names = {p.as_posix() for p in paths}
    if len(names) != len(paths):
        raise ValueError("Duplicate retained paths")
    for path in paths:
        parent = path.as_posix().rpartition("/")[0]
        while parent:
            if parent in names:
                raise ValueError("Retained file ancestor collision")
            parent = parent.rpartition("/")[0]
    return paths


@dataclass(frozen=True, slots=True)
class RetainedProjectMember:
    path: str
    size: int
    mode: int
    sha256: str
    role: str
    symlink_target: str | None = None
    hardlink_target: str | None = None

    def __post_init__(self) -> None:
        canonical_relative_posix_paths((self.path,), label="retained member")
        if type(self.size) is not int or not 0 <= self.size < 2**63:
            raise ValueError("Invalid retained input size")
        if self.symlink_target is None:
            if type(self.mode) is not int or self.mode not in (0o644, 0o755):
                raise ValueError("Retained mode must be normalized 0644 or 0755")
        else:
            target = self.symlink_target
            if (
                not isinstance(target, str)
                or not target
                or target.startswith("/")
                or any(c in target for c in ("\\", ":", "\x00"))
                or any(ord(c) < 32 for c in target)
                or len(target.encode("utf-8")) > 4096
                or type(self.mode) is not int
                or self.mode != 0o120000
            ):
                raise ValueError(
                    "Retained symlink requires bounded relative target text"
                )
            data = target.encode("utf-8")
            if (
                self.size != len(data)
                or self.sha256 != hashlib.sha256(data).hexdigest()
            ):
                raise ValueError("Retained symlink target identity does not match")
        if self.hardlink_target is not None:
            canonical_relative_posix_path(
                self.hardlink_target, label="retained hard link"
            )
            if self.symlink_target is not None or self.hardlink_target >= self.path:
                raise ValueError(
                    "Retained hard link requires its first regular-file representative"
                )
        ContentIdentity.parse_uri("sha256:" + self.sha256)
        if not isinstance(self.role, str) or not self.role.strip():
            raise ValueError("Retained input requires an ownership role")

    def to_dict(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__slots__
            if name not in {"symlink_target", "hardlink_target"}
            or getattr(self, name) is not None
        }

    @property
    def symlink_destination(self) -> str | None:
        if self.symlink_target is None:
            return None
        return posixpath.normpath(
            posixpath.join(posixpath.dirname(self.path), self.symlink_target)
        )

    @classmethod
    def from_dict(cls, value: Any) -> RetainedProjectMember:
        fields = set(cls.__slots__) - {"symlink_target", "hardlink_target"}
        if isinstance(value, dict) and "symlink_target" in value:
            if not isinstance(value["symlink_target"], str):
                raise ValueError("Explicit retained symlink target must be text")
            fields.add("symlink_target")
        if isinstance(value, dict) and "hardlink_target" in value:
            if not isinstance(value["hardlink_target"], str):
                raise ValueError("Explicit retained hard link target must be text")
            fields.add("hardlink_target")
        _fields(value, fields)
        return cls(**value)


@dataclass(frozen=True, slots=True)
class RetainedProjectManifest:
    roots: tuple[str, ...]
    members: tuple[RetainedProjectMember, ...]
    limits: RetainedProjectLimits
    directories: tuple[str, ...] = ()
    component_ownership: tuple[RetainedComponentOwner, ...] = ()
    tree_policy: RetainedProjectTreePolicy | None = None

    def __post_init__(self) -> None:
        if self.tree_policy is not None and not isinstance(
            self.tree_policy, RetainedProjectTreePolicy
        ):
            raise TypeError("Typed retained tree policy required")
        case_sensitive = (
            self.tree_policy is not None and self.tree_policy.case_sensitive
        )
        key = (lambda name: name) if case_sensitive else str.casefold
        if not isinstance(self.limits, RetainedProjectLimits):
            raise TypeError("Typed retained input limits required")
        if type(self.roots) is not tuple or not self.roots:
            raise ValueError("Explicit retained input roots required")
        _retained_paths(
            self.roots, label="retained roots", case_sensitive=case_sensitive
        )
        if tuple(sorted(self.roots)) != self.roots:
            raise ValueError("Retained roots must be ordered")
        if type(self.members) is not tuple or not self.members:
            raise ValueError("Retained input manifest must not be empty")
        if not all(
            isinstance(member, RetainedProjectMember) for member in self.members
        ):
            raise TypeError("Typed retained members required")
        if type(self.directories) is not tuple or self.directories != tuple(
            sorted(self.directories)
        ):
            raise ValueError("Retained directories must be ordered")
        roots = set(self.roots)
        directories = set(self.directories)

        def declared_root(name: str) -> str | None:
            while name:
                if name in roots:
                    return name
                name = name.rpartition("/")[0]
            return None

        # Directories may be ancestors; validate aliases separately from files.
        aliases: set[str] = set()
        for directory in self.directories:
            canonical_relative_posix_paths((directory,), label="retained directory")
            if key(directory) in aliases:
                raise ValueError("Colliding retained directories")
            aliases.add(key(directory))
            if declared_root(directory) is None:
                raise ValueError("Retained directory outside declared roots")
        names = tuple(member.path for member in self.members)
        _retained_paths(names, label="retained members", case_sensitive=case_sensitive)
        if names != tuple(sorted(names)):
            raise ValueError("Retained members must be ordered")
        nodes = set(names) | set(self.directories)
        if any(root not in nodes for root in self.roots):
            raise ValueError("Retained root absent from manifest")
        for name in nodes:
            root = declared_root(name)
            if root is None:
                raise ValueError("Retained node outside declared roots")
            while name != root:
                name = name.rsplit("/", 1)[0]
                if name not in directories:
                    raise ValueError("Retained directory inventory is incomplete")
        # Checking every file against every directory is quadratic on complete
        # native trees. Walk directory ancestors against an indexed file set.
        files = {key(name) for name in names}
        for directory in self.directories:
            ancestor = key(directory)
            while ancestor:
                if ancestor in files:
                    raise ValueError("Retained file/directory collision")
                ancestor = ancestor.rpartition("/")[0]
        for member in self.members:
            if member.size > self.limits.max_file_bytes:
                raise ValueError("Retained input exceeds per-file bound")
        if (
            len(self.members) + len(self.directories) > self.limits.max_entries
            or sum(m.size for m in self.members) > self.limits.max_total_bytes
        ):
            raise ValueError("Retained input exceeds aggregate bound")
        by_path = {member.path: member for member in self.members}
        for member in self.members:
            if member.hardlink_target is None:
                continue
            target = by_path.get(member.hardlink_target)
            if (
                self.tree_policy is None
                or target is None
                or target.symlink_target is not None
                or target.hardlink_target is not None
                or (target.size, target.mode, target.sha256)
                != (member.size, member.mode, member.sha256)
            ):
                raise ValueError(
                    "Retained hard link lacks selected canonical file authority"
                )
        if type(self.component_ownership) is not tuple:
            raise TypeError("Typed retained Component ownership tuple required")
        if self.component_ownership:
            validate_retained_ownership(self.component_ownership, nodes)
        links = {
            member.path: member.symlink_destination
            for member in self.members
            if member.symlink_target is not None
        }
        if any(target not in nodes for target in links.values()):
            raise ValueError(
                "Retained symlink target is outside the declared input closure"
            )
        for member in self.members:
            if member.symlink_target is None:
                continue
            cursor = member.path.split("/")[:-1]
            parts = member.symlink_target.split("/")
            for index, part in enumerate(parts):
                if part in ("", "."):
                    continue
                if part == "..":
                    if not cursor:
                        raise ValueError(
                            "Retained symlink traverses outside the input tree"
                        )
                    cursor.pop()
                else:
                    cursor.append(part)
                    if index < len(parts) - 1 and "/".join(cursor) not in directories:
                        raise ValueError(
                            "Retained symlink traverses a link or non-directory"
                        )
        resolved: set[str] = set()
        for start in links:
            current = start
            chain: set[str] = set()
            while current in links and current not in resolved:
                if current in chain:
                    raise ValueError("Retained symlink target cycle")
                chain.add(current)
                current = links[current]
            resolved.update(chain)

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema": "literate-ai/retained-project-manifest@1",
            "roots": list(self.roots),
            "directories": list(self.directories),
            "members": [m.to_dict() for m in self.members],
            "limits": self.limits.to_dict(),
        }
        # Omitted legacy extension preserves existing manifest/plan identities.
        if self.component_ownership:
            value["component_ownership"] = [
                owner.to_dict() for owner in self.component_ownership
            ]
        if self.tree_policy is not None:
            value["tree_policy"] = self.tree_policy.to_dict()
        return value

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def role_identity(self, roles: set[str]) -> ContentIdentity:
        """Bind the exact declared closure for the selected ownership roles."""
        document = {
            "schema": "literate-ai/retained-role-closure@1",
            "roles": sorted(roles),
            "members": [
                member.to_dict() for member in self.members if member.role in roles
            ],
        }
        if self.tree_policy is not None:
            document["tree_policy"] = self.tree_policy.to_dict()
        return canonical_identity(document)

    @classmethod
    def from_dict(cls, value: Any) -> RetainedProjectManifest:
        required = {"schema", "roots", "members", "limits", "directories"}
        _fields(
            value,
            required
            | (
                {"tree_policy"}
                if isinstance(value, dict) and "tree_policy" in value
                else set()
            )
            | (
                {"component_ownership"}
                if isinstance(value, dict) and "component_ownership" in value
                else set()
            ),
        )
        if (
            value["schema"] != "literate-ai/retained-project-manifest@1"
            or not isinstance(value["roots"], list)
            or not isinstance(value["members"], list)
            or not isinstance(value["directories"], list)
        ):
            raise ValueError("Invalid retained input manifest")
        if "component_ownership" in value and (
            not isinstance(value["component_ownership"], list)
            or not value["component_ownership"]
        ):
            raise ValueError(
                "Explicit retained Component ownership must be a nonempty array"
            )
        return cls(
            tuple(value["roots"]),
            tuple(RetainedProjectMember.from_dict(m) for m in value["members"]),
            RetainedProjectLimits.from_dict(value["limits"]),
            tuple(value["directories"]),
            tuple(
                RetainedComponentOwner.from_dict(owner)
                for owner in value.get("component_ownership", ())
            ),
            RetainedProjectTreePolicy.from_dict(value["tree_policy"])
            if "tree_policy" in value
            else None,
        )

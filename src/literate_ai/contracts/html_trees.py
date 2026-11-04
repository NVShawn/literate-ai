"""Explicit retained-tree HTML intent and resource bounds.

These unused declaration records grant no source, publication, or execution
authority. They live alongside the frozen single-file HTML protocol; the tree
adapter must obtain current canonical input and output custody separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import ClassVar

from ._validation import contract_fields
from .identity import ContentIdentity, contract_identity
from .native_tree_shards import (
    MAX_SHARD_MEMBERS,
    MAX_TREE_MEMBERS,
    MAX_TREE_SHARDS,
    validate_native_tree_policy,
)
from .paths import canonical_relative_posix_path
from .retained_project import RetainedProjectLimits

MAX_TREE_HTML_PAGE_BYTES = 64 * 1024 * 1024
_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_RESERVED = frozenset({".git", ".hg", ".svn", ".codegraph", ".litai-locks"})


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"html.tree.{label}-out-of-bounds")


def _selection(value, label):
    """The selected root is explicit, without relaxing global path contracts."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"html.tree.invalid-{label}")
    if value == ".":
        return PurePosixPath(".")
    path = canonical_relative_posix_path(value, label=label)
    if _RESERVED.intersection(part.casefold() for part in path.parts):
        raise ValueError(f"html.tree.{label}-uses-repository-metadata")
    return path


@dataclass(frozen=True, slots=True)
class HtmlTreeLimits:
    """Every bound is explicit; estimates are not measured RSS claims."""

    source: RetainedProjectLimits
    max_descriptor_bytes: int
    max_descriptor_total_bytes: int
    max_index_bytes: int
    max_metadata_memory_bytes: int
    max_depth: int
    max_children_per_group: int
    max_groups_per_page: int
    max_pages: int
    max_page_bytes: int
    max_total_generated_bytes: int
    max_retained_generation_bytes: int

    SCHEMA: ClassVar[str] = "literate-ai/retained-tree-html-limits@1"

    def __post_init__(self):
        validate_native_tree_policy(
            self.source,
            True,
            self.max_descriptor_bytes,
            self.max_descriptor_total_bytes,
            self.max_index_bytes,
            self.max_metadata_memory_bytes,
        )
        _integer(self.source.max_entries, 1, MAX_TREE_MEMBERS, "source-entries")
        for name, maximum in (
            ("max_depth", 1024),
            ("max_children_per_group", MAX_SHARD_MEMBERS),
            ("max_groups_per_page", MAX_TREE_SHARDS),
            ("max_pages", 8 * MAX_TREE_MEMBERS),
            ("max_page_bytes", MAX_TREE_HTML_PAGE_BYTES),
            ("max_total_generated_bytes", 2**63 - 1),
            ("max_retained_generation_bytes", 2**63 - 1),
        ):
            _integer(getattr(self, name), 1, maximum, name)
        if not (
            self.max_page_bytes
            <= self.max_total_generated_bytes
            <= self.max_retained_generation_bytes
        ):
            raise ValueError("html.tree.output-and-storage-budgets-inconsistent")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "source": self.source.to_dict(),
            **{
                name: getattr(self, name) for name in self.__slots__ if name != "source"
            },
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(cls.__slots__),
        )
        return cls(
            source=RetainedProjectLimits.from_dict(data["source"]),
            **{name: data[name] for name in cls.__slots__ if name != "source"},
        )

    @property
    def identity(self):
        return contract_identity(self)


@dataclass(frozen=True, slots=True)
class HtmlTreeRenderRequest:
    """One declaration selects a complete tree, not thousands of old requests.

    Paths are relative to a separately selected physical root. In particular,
    `.` permits a standalone native tree's existing root and top-level index;
    it never authorizes access to an arbitrary absolute host path.
    """

    request_id: str
    source_root: str
    output_root: str
    limits: HtmlTreeLimits
    case_sensitive: bool
    renderer_identity: ContentIdentity
    view_id: str
    view_version: str

    SCHEMA: ClassVar[str] = "literate-ai/retained-tree-html-render-request@1"

    def __post_init__(self):
        if not isinstance(self.request_id, str) or not _ID.fullmatch(self.request_id):
            raise ValueError("html.tree.portable-request-id-required")
        _selection(self.source_root, "source-root")
        _selection(self.output_root, "output-root")
        if not isinstance(self.limits, HtmlTreeLimits):
            raise TypeError("html.tree.typed-explicit-limits-required")
        if type(self.case_sensitive) is not bool:
            raise TypeError("html.tree.explicit-case-policy-required")
        if not isinstance(self.renderer_identity, ContentIdentity):
            raise TypeError("html.tree.typed-renderer-identity-required")
        if (self.view_id, self.view_version) != ("information-cards", "1.0.0"):
            raise ValueError("html.tree.unsupported-view")

    @property
    def index_path(self):
        return (PurePosixPath(self.output_root) / "index.html").as_posix()

    @property
    def namespace_path(self):
        return (
            PurePosixPath(self.output_root) / f".litai-tree-{self.request_id}"
        ).as_posix()

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "request_id": self.request_id,
            "source_root": self.source_root,
            "output_root": self.output_root,
            "limits": self.limits.to_dict(),
            "case_sensitive": self.case_sensitive,
            "renderer_identity": self.renderer_identity.to_dict(),
            "view_id": self.view_id,
            "view_version": self.view_version,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(cls.__slots__),
        )
        return cls(
            limits=HtmlTreeLimits.from_dict(data["limits"]),
            renderer_identity=ContentIdentity.from_dict(data["renderer_identity"]),
            **{
                name: data[name]
                for name in cls.__slots__
                if name not in ("limits", "renderer_identity")
            },
        )


@dataclass(frozen=True, slots=True)
class HtmlTreeViewDeclaration:
    """Operator intent; resolving a measured renderer grants no live authority.

    The nominal renderer selects the supported view without asking operators to
    author an installed distribution digest. A separate current framework
    observation supplies that digest when resolving the existing request.
    """

    request_id: str
    source_root: str
    output_root: str
    limits: HtmlTreeLimits
    case_sensitive: bool

    SCHEMA: ClassVar[str] = "literate-ai/retained-tree-html-declaration@1"
    RENDERER: ClassVar[str] = "renderer://literate-ai/information-cards/1.0.0"

    def __post_init__(self):
        if not isinstance(self.request_id, str) or not _ID.fullmatch(self.request_id):
            raise ValueError("html.tree.portable-request-id-required")
        _selection(self.source_root, "source-root")
        _selection(self.output_root, "output-root")
        if not isinstance(self.limits, HtmlTreeLimits):
            raise TypeError("html.tree.typed-explicit-limits-required")
        if type(self.case_sensitive) is not bool:
            raise TypeError("html.tree.explicit-case-policy-required")

    @property
    def renderer(self):
        return self.RENDERER

    @property
    def identity(self):
        return contract_identity(self)

    def resolve(
        self, actual_renderer_identity: ContentIdentity
    ) -> HtmlTreeRenderRequest:
        """Bind a typed observation, without asserting its origin or currentness."""
        return HtmlTreeRenderRequest(
            self.request_id,
            self.source_root,
            self.output_root,
            self.limits,
            self.case_sensitive,
            actual_renderer_identity,
            "information-cards",
            "1.0.0",
        )

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            **{
                name: self.limits.to_dict() if name == "limits" else getattr(self, name)
                for name in self.__slots__
            },
            "renderer": self.renderer,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset((*cls.__slots__, "renderer")),
        )
        if data["renderer"] != cls.RENDERER:
            raise ValueError("html.tree.unsupported-renderer")
        return cls(
            limits=HtmlTreeLimits.from_dict(data["limits"]),
            **{name: data[name] for name in cls.__slots__ if name != "limits"},
        )

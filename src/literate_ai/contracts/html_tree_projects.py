"""Explicit project selection of bounded existing tree-view declarations.

These records are authored intent only. They cannot issue source exclusions,
publication custody, recovery, or any execution owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields
from .paths import canonical_relative_posix_paths

PROJECT_TREE_HTML_FILENAME = "literate.tree-html.json"
MAX_PROJECT_TREE_VIEWS = 128


@dataclass(frozen=True, slots=True)
class ProjectHtmlTreeViews:
    declarations: tuple[str, ...]

    SCHEMA: ClassVar[str] = "literate-ai/project-tree-html@1"

    def __post_init__(self):
        if (
            type(self.declarations) is not tuple
            or not 1 <= len(self.declarations) <= MAX_PROJECT_TREE_VIEWS
            or any(
                type(value) is not str or len(value.encode("utf-8")) > 4096
                for value in self.declarations
            )
        ):
            raise ValueError("html.tree.project-declarations-out-of-bounds")
        canonical_relative_posix_paths(
            self.declarations, label="project tree declaration"
        )

    @classmethod
    def from_dict(cls, value):
        fields = contract_fields(
            value,
            path="ProjectHtmlTreeViews",
            schema_uri=cls.SCHEMA,
            required=frozenset({"declarations"}),
        )
        values = fields["declarations"]
        if type(values) is not list:
            raise TypeError("html.tree.project-declarations-array-required")
        return cls(tuple(values))

    def to_dict(self):
        return {"schema": self.SCHEMA, "declarations": list(self.declarations)}

"""Exact parent selection and transitive repository-lineage evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    int_value,
    list_value,
    string_value,
)
from .identity import ContentIdentity, contract_identity

REPOSITORY_FETCH_DEADLINE_POLICY_SCHEMA = (
    "urn:literate-ai:schema:v1:repository-fetch-deadline-policy"
)
REPOSITORY_PARENT_REFERENCE_SCHEMA = (
    "urn:literate-ai:schema:v1:repository-parent-reference"
)
REPOSITORY_PARENT_SELECTION_SCHEMA = (
    "urn:literate-ai:schema:v1:repository-parent-selection"
)
REPOSITORY_LINEAGE_NODE_SCHEMA = "urn:literate-ai:schema:v1:repository-lineage-node"
REPOSITORY_LINEAGE_SCHEMA = "urn:literate-ai:schema:v1:repository-lineage"

MAX_REPOSITORY_PARENTS = 16
MAX_REPOSITORY_LINEAGE_NODES = 128
MIN_REPOSITORY_FETCH_TOTAL_SECONDS = 30
MAX_REPOSITORY_FETCH_TOTAL_SECONDS = 14_400
MIN_REPOSITORY_FETCH_NO_PROGRESS_SECONDS = 15
MAX_REPOSITORY_FETCH_NO_PROGRESS_SECONDS = 1_800
MIN_REPOSITORY_FETCH_CONNECT_SECONDS = 5
MAX_REPOSITORY_FETCH_CONNECT_SECONDS = 120

_GIT_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_DISALLOWED_REF_CHARACTER = re.compile(r"[\x00-\x20~^:?*\\[]")
_ALLOWED_REPOSITORY_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})


class RepositoryParentMode(StrEnum):
    ROOT = "root"
    INHERIT = "inherit"


@dataclass(frozen=True, slots=True)
class RepositoryFetchDeadlinePolicy:
    """Bound one lineage fetch without treating valid network latency as a hang."""

    total_seconds: int = 3_600
    no_progress_seconds: int = 600
    connect_seconds: int = 30

    SCHEMA: ClassVar[str] = REPOSITORY_FETCH_DEADLINE_POLICY_SCHEMA

    def __post_init__(self) -> None:
        int_value(
            self.total_seconds,
            "RepositoryFetchDeadlinePolicy.total_seconds",
            minimum=MIN_REPOSITORY_FETCH_TOTAL_SECONDS,
            maximum=MAX_REPOSITORY_FETCH_TOTAL_SECONDS,
        )
        int_value(
            self.no_progress_seconds,
            "RepositoryFetchDeadlinePolicy.no_progress_seconds",
            minimum=MIN_REPOSITORY_FETCH_NO_PROGRESS_SECONDS,
            maximum=MAX_REPOSITORY_FETCH_NO_PROGRESS_SECONDS,
        )
        int_value(
            self.connect_seconds,
            "RepositoryFetchDeadlinePolicy.connect_seconds",
            minimum=MIN_REPOSITORY_FETCH_CONNECT_SECONDS,
            maximum=MAX_REPOSITORY_FETCH_CONNECT_SECONDS,
        )
        if self.connect_seconds > self.no_progress_seconds:
            fail(
                "RepositoryFetchDeadlinePolicy.connect_seconds",
                "must not exceed no_progress_seconds",
            )
        if self.no_progress_seconds > self.total_seconds:
            fail(
                "RepositoryFetchDeadlinePolicy.no_progress_seconds",
                "must not exceed total_seconds",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "total_seconds": self.total_seconds,
            "no_progress_seconds": self.no_progress_seconds,
            "connect_seconds": self.connect_seconds,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "RepositoryFetchDeadlinePolicy",
    ) -> RepositoryFetchDeadlinePolicy:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"total_seconds", "no_progress_seconds", "connect_seconds"}
            ),
        )
        return cls(
            total_seconds=int_value(
                data["total_seconds"],
                f"{path}.total_seconds",
                minimum=MIN_REPOSITORY_FETCH_TOTAL_SECONDS,
                maximum=MAX_REPOSITORY_FETCH_TOTAL_SECONDS,
            ),
            no_progress_seconds=int_value(
                data["no_progress_seconds"],
                f"{path}.no_progress_seconds",
                minimum=MIN_REPOSITORY_FETCH_NO_PROGRESS_SECONDS,
                maximum=MAX_REPOSITORY_FETCH_NO_PROGRESS_SECONDS,
            ),
            connect_seconds=int_value(
                data["connect_seconds"],
                f"{path}.connect_seconds",
                minimum=MIN_REPOSITORY_FETCH_CONNECT_SECONDS,
                maximum=MAX_REPOSITORY_FETCH_CONNECT_SECONDS,
            ),
        )


def _repository_url(value: Any, path: str) -> str:
    raw = string_value(value, path)
    if any(ord(character) < 32 for character in raw):
        fail(path, "must not contain control characters")
    parsed = urlsplit(raw)
    if parsed.scheme not in _ALLOWED_REPOSITORY_SCHEMES:
        fail(path, "must use a file, git, http, https, or ssh URL")
    if parsed.password is not None or parsed.query or parsed.fragment:
        fail(path, "must not contain credentials, query parameters, or fragments")
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
            fail(path, "file repository URLs must identify an absolute local path")
    elif not parsed.hostname:
        fail(path, "network repository URLs must identify a host")
    return raw


def _requested_revision(value: Any, path: str) -> str:
    raw = string_value(value, path, max_length=256)
    if raw.startswith("-") or raw.endswith((".", "/")) or ".." in raw or "//" in raw:
        fail(path, "is not a safe Git revision selector")
    if raw != "HEAD" and _DISALLOWED_REF_CHARACTER.search(raw):
        fail(path, "is not a safe Git revision selector")
    return raw


def _resolved_revision(value: Any, path: str) -> str:
    raw = string_value(value, path)
    if not _GIT_REVISION.fullmatch(raw):
        fail(path, "must be an exact 40- or 64-character lower-case Git object ID")
    return raw


def _identity(value: Any, path: str) -> ContentIdentity:
    try:
        return ContentIdentity.from_dict(value, path=path)
    except (TypeError, ValueError) as exc:
        fail(path, str(exc))


@dataclass(frozen=True, slots=True)
class RepositoryParentReference:
    """One sanitized repository URL and the revision selector it tracks."""

    repository_url: str
    requested_revision: str

    SCHEMA: ClassVar[str] = REPOSITORY_PARENT_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        _repository_url(self.repository_url, "RepositoryParentReference.repository_url")
        _requested_revision(
            self.requested_revision,
            "RepositoryParentReference.requested_revision",
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "repository_url": self.repository_url,
            "requested_revision": self.requested_revision,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryParentReference"
    ) -> RepositoryParentReference:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"repository_url", "requested_revision"}),
        )
        return cls(
            _repository_url(data["repository_url"], f"{path}.repository_url"),
            _requested_revision(
                data["requested_revision"], f"{path}.requested_revision"
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryParentSelection:
    """Authored direct-parent authority; root is an explicit empty selection."""

    mode: RepositoryParentMode
    parents: tuple[RepositoryParentReference, ...]

    SCHEMA: ClassVar[str] = REPOSITORY_PARENT_SELECTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RepositoryParentMode):
            fail("RepositoryParentSelection.mode", "must be a RepositoryParentMode")
        if not isinstance(self.parents, tuple) or any(
            not isinstance(item, RepositoryParentReference) for item in self.parents
        ):
            fail(
                "RepositoryParentSelection.parents",
                "must be a tuple of RepositoryParentReference values",
            )
        if len(self.parents) > MAX_REPOSITORY_PARENTS:
            fail(
                "RepositoryParentSelection.parents",
                f"must contain at most {MAX_REPOSITORY_PARENTS} parents",
            )
        if self.mode is RepositoryParentMode.ROOT and self.parents:
            fail("RepositoryParentSelection.parents", "must be empty in root mode")
        if self.mode is RepositoryParentMode.INHERIT and not self.parents:
            fail(
                "RepositoryParentSelection.parents", "must be nonempty in inherit mode"
            )
        keys = tuple(
            (item.repository_url, item.requested_revision) for item in self.parents
        )
        if keys != tuple(sorted(set(keys))):
            fail(
                "RepositoryParentSelection.parents",
                "must be uniquely sorted by repository URL and requested revision",
            )
        urls = tuple(item.repository_url for item in self.parents)
        if len(urls) != len(set(urls)):
            fail(
                "RepositoryParentSelection.parents",
                "cannot select one repository URL at multiple revisions",
            )

    @classmethod
    def root(cls) -> RepositoryParentSelection:
        return cls(RepositoryParentMode.ROOT, ())

    @classmethod
    def inherit(
        cls, parents: tuple[RepositoryParentReference, ...]
    ) -> RepositoryParentSelection:
        return cls(
            RepositoryParentMode.INHERIT,
            tuple(
                sorted(parents, key=lambda p: (p.repository_url, p.requested_revision))
            ),
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "mode": self.mode.value,
            "parents": [item.to_dict() for item in self.parents],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryParentSelection"
    ) -> RepositoryParentSelection:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"mode", "parents"}),
        )
        return cls(
            enum_value(RepositoryParentMode, data["mode"], f"{path}.mode"),
            tuple(
                RepositoryParentReference.from_dict(
                    item, path=f"{path}.parents[{index}]"
                )
                for index, item in enumerate(
                    list_value(data["parents"], f"{path}.parents")
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryLineageNode:
    """One exact repository revision and its complete declared direct parents."""

    project_id: str
    project_identity: ContentIdentity
    repository_url: str
    requested_revision: str
    resolved_revision: str
    parent_selection: RepositoryParentSelection
    parents: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = REPOSITORY_LINEAGE_NODE_SCHEMA

    def __post_init__(self) -> None:
        string_value(
            self.project_id, "RepositoryLineageNode.project_id", max_length=256
        )
        if not isinstance(self.project_identity, ContentIdentity):
            fail("RepositoryLineageNode.project_identity", "must be a ContentIdentity")
        _repository_url(self.repository_url, "RepositoryLineageNode.repository_url")
        _requested_revision(
            self.requested_revision, "RepositoryLineageNode.requested_revision"
        )
        _resolved_revision(
            self.resolved_revision, "RepositoryLineageNode.resolved_revision"
        )
        if not isinstance(self.parent_selection, RepositoryParentSelection):
            fail(
                "RepositoryLineageNode.parent_selection",
                "must be a RepositoryParentSelection",
            )
        if not isinstance(self.parents, tuple) or any(
            not isinstance(item, ContentIdentity) for item in self.parents
        ):
            fail(
                "RepositoryLineageNode.parents",
                "must be a tuple of ContentIdentity values",
            )
        if len(self.parents) != len(self.parent_selection.parents):
            fail(
                "RepositoryLineageNode.parents",
                "must resolve every declared direct parent exactly once",
            )
        if tuple(item.uri for item in self.parents) != tuple(
            sorted({item.uri for item in self.parents})
        ):
            fail("RepositoryLineageNode.parents", "must be uniquely sorted")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "project_id": self.project_id,
            "project_identity": self.project_identity.to_dict(),
            "repository_url": self.repository_url,
            "requested_revision": self.requested_revision,
            "resolved_revision": self.resolved_revision,
            "parent_selection": self.parent_selection.to_dict(),
            "parents": [item.to_dict() for item in self.parents],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryLineageNode"
    ) -> RepositoryLineageNode:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "project_id",
                    "project_identity",
                    "repository_url",
                    "requested_revision",
                    "resolved_revision",
                    "parent_selection",
                    "parents",
                }
            ),
        )
        return cls(
            string_value(data["project_id"], f"{path}.project_id", max_length=256),
            _identity(data["project_identity"], f"{path}.project_identity"),
            _repository_url(data["repository_url"], f"{path}.repository_url"),
            _requested_revision(
                data["requested_revision"], f"{path}.requested_revision"
            ),
            _resolved_revision(data["resolved_revision"], f"{path}.resolved_revision"),
            RepositoryParentSelection.from_dict(
                data["parent_selection"], path=f"{path}.parent_selection"
            ),
            tuple(
                _identity(item, f"{path}.parents[{index}]")
                for index, item in enumerate(
                    list_value(data["parents"], f"{path}.parents")
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RepositoryLineage:
    """Canonical, complete ancestor-first resolution of one parent selection."""

    selection: RepositoryParentSelection
    nodes: tuple[RepositoryLineageNode, ...]
    selected_parents: tuple[ContentIdentity, ...]

    SCHEMA: ClassVar[str] = REPOSITORY_LINEAGE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.selection, RepositoryParentSelection):
            fail("RepositoryLineage.selection", "must be a RepositoryParentSelection")
        if not isinstance(self.nodes, tuple) or any(
            not isinstance(item, RepositoryLineageNode) for item in self.nodes
        ):
            fail("RepositoryLineage.nodes", "must be repository lineage nodes")
        if len(self.nodes) > MAX_REPOSITORY_LINEAGE_NODES:
            fail(
                "RepositoryLineage.nodes",
                f"must contain at most {MAX_REPOSITORY_LINEAGE_NODES} nodes",
            )
        if not isinstance(self.selected_parents, tuple) or any(
            not isinstance(item, ContentIdentity) for item in self.selected_parents
        ):
            fail(
                "RepositoryLineage.selected_parents",
                "must be a tuple of ContentIdentity values",
            )
        if self.selection.mode is RepositoryParentMode.ROOT:
            if self.nodes or self.selected_parents:
                fail(
                    "RepositoryLineage",
                    "root selection must have no resolved lineage nodes",
                )
            return
        if len(self.selected_parents) != len(self.selection.parents):
            fail(
                "RepositoryLineage.selected_parents",
                "must resolve every selected direct parent exactly once",
            )
        if tuple(item.uri for item in self.selected_parents) != tuple(
            sorted({item.uri for item in self.selected_parents})
        ):
            fail(
                "RepositoryLineage.selected_parents",
                "must be uniquely sorted",
            )
        by_identity: dict[ContentIdentity, RepositoryLineageNode] = {}
        by_url: dict[str, RepositoryLineageNode] = {}
        by_project: dict[str, RepositoryLineageNode] = {}
        for index, node in enumerate(self.nodes):
            if node.identity in by_identity:
                fail("RepositoryLineage.nodes", "must have unique node identities")
            if node.repository_url in by_url:
                fail(
                    "RepositoryLineage.nodes",
                    "cannot resolve one repository URL more than once",
                )
            if node.project_id in by_project:
                fail("RepositoryLineage.nodes", "must have unique project IDs")
            missing = tuple(
                parent for parent in node.parents if parent not in by_identity
            )
            if missing:
                fail(
                    f"RepositoryLineage.nodes[{index}].parents",
                    "must refer only to earlier ancestor nodes",
                )
            declared = {
                (item.repository_url, item.requested_revision)
                for item in node.parent_selection.parents
            }
            resolved = {
                (by_identity[item].repository_url, by_identity[item].requested_revision)
                for item in node.parents
            }
            if declared != resolved:
                fail(
                    f"RepositoryLineage.nodes[{index}].parents",
                    "must exactly resolve the node's declared parent selection",
                )
            by_identity[node.identity] = node
            by_url[node.repository_url] = node
            by_project[node.project_id] = node
        selected = {
            (item.repository_url, item.requested_revision)
            for item in self.selection.parents
        }
        resolved_selected = {
            (by_identity[item].repository_url, by_identity[item].requested_revision)
            for item in self.selected_parents
            if item in by_identity
        }
        if (
            len(resolved_selected) != len(self.selected_parents)
            or selected != resolved_selected
        ):
            fail(
                "RepositoryLineage.selected_parents",
                "must exactly resolve the selected direct parents",
            )
        reachable = set(self.selected_parents)
        pending = list(self.selected_parents)
        while pending:
            current = by_identity[pending.pop()]
            for parent in current.parents:
                if parent not in reachable:
                    reachable.add(parent)
                    pending.append(parent)
        if reachable != set(by_identity):
            fail("RepositoryLineage.nodes", "must not contain unreachable nodes")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "selection": self.selection.to_dict(),
            "nodes": [item.to_dict() for item in self.nodes],
            "selected_parents": [item.to_dict() for item in self.selected_parents],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "RepositoryLineage"
    ) -> RepositoryLineage:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"selection", "nodes", "selected_parents"}),
        )
        return cls(
            RepositoryParentSelection.from_dict(
                data["selection"], path=f"{path}.selection"
            ),
            tuple(
                RepositoryLineageNode.from_dict(item, path=f"{path}.nodes[{index}]")
                for index, item in enumerate(list_value(data["nodes"], f"{path}.nodes"))
            ),
            tuple(
                _identity(item, f"{path}.selected_parents[{index}]")
                for index, item in enumerate(
                    list_value(data["selected_parents"], f"{path}.selected_parents")
                )
            ),
        )


__all__ = [
    "MAX_REPOSITORY_FETCH_CONNECT_SECONDS",
    "MAX_REPOSITORY_FETCH_NO_PROGRESS_SECONDS",
    "MAX_REPOSITORY_FETCH_TOTAL_SECONDS",
    "MAX_REPOSITORY_LINEAGE_NODES",
    "MAX_REPOSITORY_PARENTS",
    "MIN_REPOSITORY_FETCH_CONNECT_SECONDS",
    "MIN_REPOSITORY_FETCH_NO_PROGRESS_SECONDS",
    "MIN_REPOSITORY_FETCH_TOTAL_SECONDS",
    "REPOSITORY_FETCH_DEADLINE_POLICY_SCHEMA",
    "REPOSITORY_LINEAGE_NODE_SCHEMA",
    "REPOSITORY_LINEAGE_SCHEMA",
    "REPOSITORY_PARENT_REFERENCE_SCHEMA",
    "REPOSITORY_PARENT_SELECTION_SCHEMA",
    "RepositoryFetchDeadlinePolicy",
    "RepositoryLineage",
    "RepositoryLineageNode",
    "RepositoryParentMode",
    "RepositoryParentReference",
    "RepositoryParentSelection",
]

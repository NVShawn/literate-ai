"""Pure resolution of complete repository-parent DAGs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphError,
    AuthorityGraphNode,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectDefinition,
    RepositoryLineage,
    RepositoryLineageNode,
    RepositoryParentMode,
    RepositoryParentReference,
    RepositoryParentSelection,
)


class RepositoryLineageResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ResolvedRepositorySnapshot:
    """Non-executed authority read from one exact repository revision."""

    reference: RepositoryParentReference
    resolved_revision: str
    project_id: str
    project_identity: ContentIdentity
    parent_selection: RepositoryParentSelection


@dataclass(frozen=True, slots=True)
class RepositoryCatalogFile:
    """One regular, tracked catalog file read without checking repository code out."""

    path: str
    content: bytes
    executable: bool = False

    def __post_init__(self) -> None:
        parsed = PurePosixPath(self.path)
        if (
            not self.path
            or parsed.is_absolute()
            or parsed.as_posix() != self.path
            or ".." in parsed.parts
            or "\\" in self.path
        ):
            raise ValueError("repository catalog path must be normalized and relative")
        if not isinstance(self.content, bytes):
            raise TypeError("repository catalog content must be bytes")
        if not isinstance(self.executable, bool):
            raise TypeError("repository catalog executable flag must be boolean")


@dataclass(frozen=True, slots=True)
class ResolvedRepositoryCatalog:
    """The authored catalog files of one exact repository-lineage node."""

    node: RepositoryLineageNode
    definition: ProjectDefinition
    files: tuple[RepositoryCatalogFile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.node, RepositoryLineageNode):
            raise TypeError("resolved repository catalog requires a typed node")
        if not isinstance(self.definition, ProjectDefinition):
            raise TypeError("resolved repository catalog requires a project definition")
        if self.definition.project_id != self.node.project_id:
            raise ValueError(
                "repository catalog project does not match its lineage node"
            )
        if not isinstance(self.files, tuple) or any(
            not isinstance(item, RepositoryCatalogFile) for item in self.files
        ):
            raise TypeError("resolved repository catalog files must be typed")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(set(paths))):
            raise ValueError(
                "resolved repository catalog files must be uniquely sorted"
            )


class RepositorySnapshotProvider(Protocol):
    def resolve(
        self, reference: RepositoryParentReference
    ) -> ResolvedRepositorySnapshot: ...


def repository_lineage_authority_graph(lineage: RepositoryLineage) -> AuthorityGraph:
    """Project exact persisted lineage into the canonical repository DAG solver."""

    if not isinstance(lineage, RepositoryLineage):
        raise TypeError("repository graph projection requires typed lineage")
    by_identity = {node.identity: node for node in lineage.nodes}
    try:
        return AuthorityGraph.create(
            "repository-lineage",
            (
                AuthorityGraphNode(
                    f"repository:{node.project_id}",
                    "repository",
                    node.project_id,
                    node.project_id,
                    node.repository_url,
                    inherited=True,
                    inheritable=True,
                    properties=(
                        ("project_identity", node.project_identity.uri),
                        ("requested_revision", node.requested_revision),
                        ("resolved_revision", node.resolved_revision),
                    ),
                )
                for node in lineage.nodes
            ),
            (
                AuthorityGraphEdge(
                    f"repository:{by_identity[parent].project_id}",
                    f"repository:{node.project_id}",
                    "repository-parent",
                )
                for node in lineage.nodes
                for parent in node.parents
            ),
        )
    except (AuthorityGraphError, KeyError) as exc:
        code = (
            exc.code
            if isinstance(exc, AuthorityGraphError)
            else "graph.edge_unresolved"
        )
        message = (
            exc.message
            if isinstance(exc, AuthorityGraphError)
            else "repository lineage names an unknown parent identity"
        )
        raise RepositoryLineageResolutionError(code, message) from exc


def resolve_repository_lineage(
    selection: RepositoryParentSelection,
    provider: RepositorySnapshotProvider,
) -> RepositoryLineage:
    """Resolve every declared ancestor and return canonical ancestor-first evidence."""

    if selection.mode is RepositoryParentMode.ROOT:
        return RepositoryLineage(selection, (), ())

    snapshots: dict[str, ResolvedRepositorySnapshot] = {}
    requested_revisions: dict[str, str] = {}
    project_urls: dict[str, str] = {}
    pending = list(selection.parents)
    cursor = 0
    while cursor < len(pending):
        reference = pending[cursor]
        cursor += 1
        previous_revision = requested_revisions.get(reference.repository_url)
        if previous_revision is not None:
            if previous_revision != reference.requested_revision:
                raise RepositoryLineageResolutionError(
                    "repository_lineage.conflicting_revision",
                    "one repository URL is requested at conflicting revisions",
                )
            continue
        requested_revisions[reference.repository_url] = reference.requested_revision
        snapshot = provider.resolve(reference)
        if not isinstance(snapshot, ResolvedRepositorySnapshot):
            raise RepositoryLineageResolutionError(
                "repository_lineage.snapshot_invalid",
                "repository snapshot provider returned an invalid result",
            )
        if snapshot.reference != reference:
            raise RepositoryLineageResolutionError(
                "repository_lineage.snapshot_mismatch",
                "repository snapshot does not bind the requested repository reference",
            )
        previous_url = project_urls.get(snapshot.project_id)
        if previous_url is not None and previous_url != reference.repository_url:
            raise RepositoryLineageResolutionError(
                "repository_lineage.conflicting_project",
                f"project ID {snapshot.project_id!r} is supplied by multiple "
                "repositories",
            )
        project_urls[snapshot.project_id] = reference.repository_url
        snapshots[reference.repository_url] = snapshot
        pending.extend(snapshot.parent_selection.parents)

    graph_nodes = tuple(
        AuthorityGraphNode(
            f"repository:{snapshot.project_id}",
            "repository",
            snapshot.project_id,
            snapshot.project_id,
            snapshot.reference.repository_url,
            inherited=True,
            inheritable=True,
            properties=(
                ("requested_revision", snapshot.reference.requested_revision),
                ("resolved_revision", snapshot.resolved_revision),
            ),
        )
        for snapshot in snapshots.values()
    )
    graph_edges = tuple(
        AuthorityGraphEdge(
            f"repository:{snapshots[parent.repository_url].project_id}",
            f"repository:{snapshot.project_id}",
            "repository-parent",
        )
        for snapshot in snapshots.values()
        for parent in snapshot.parent_selection.parents
    )
    try:
        graph = AuthorityGraph.create("repository-lineage", graph_nodes, graph_edges)
    except AuthorityGraphError as exc:
        if exc.code == "graph.cycle":
            raise RepositoryLineageResolutionError(
                "repository_lineage.cycle",
                "repository parent declarations contain a cycle: "
                + exc.message.removeprefix("directed cycle detected: "),
            ) from exc
        raise RepositoryLineageResolutionError(exc.code, exc.message) from exc

    nodes: list[RepositoryLineageNode] = []
    by_project: dict[str, RepositoryLineageNode] = {}
    by_project_snapshot = {item.project_id: item for item in snapshots.values()}
    for node_id in graph.topological_order():
        project_id = node_id.removeprefix("repository:")
        snapshot = by_project_snapshot[project_id]
        node = RepositoryLineageNode(
            project_id=snapshot.project_id,
            project_identity=snapshot.project_identity,
            repository_url=snapshot.reference.repository_url,
            requested_revision=snapshot.reference.requested_revision,
            resolved_revision=snapshot.resolved_revision,
            parent_selection=snapshot.parent_selection,
            parents=tuple(
                sorted(
                    (
                        by_project[snapshots[parent.repository_url].project_id].identity
                        for parent in snapshot.parent_selection.parents
                    ),
                    key=lambda item: item.uri,
                )
            ),
        )
        nodes.append(node)
        by_project[project_id] = node

    selected = tuple(
        by_project[snapshots[reference.repository_url].project_id]
        for reference in selection.parents
    )
    return RepositoryLineage(
        selection,
        tuple(nodes),
        tuple(sorted((item.identity for item in selected), key=lambda item: item.uri)),
    )


__all__ = [
    "RepositoryCatalogFile",
    "RepositoryLineageResolutionError",
    "RepositorySnapshotProvider",
    "ResolvedRepositoryCatalog",
    "ResolvedRepositorySnapshot",
    "repository_lineage_authority_graph",
    "resolve_repository_lineage",
]

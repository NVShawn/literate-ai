"""Plan and materialize inherited catalogs from exact repository lineage."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.application.repository_lineage import (
    ResolvedRepositoryCatalog,
    repository_lineage_authority_graph,
)
from literate_ai.authority_graph import AuthorityGraph
from literate_ai.contracts import (
    CatalogImport,
    CatalogImportFile,
    CatalogImportsFile,
    CatalogImportSource,
    CatalogInheritanceDecision,
    ContentIdentity,
    HashAlgorithm,
    RepositoryLineage,
    RepositoryLineageNode,
    TransitiveAncestor,
)
from literate_ai.contracts.authoring_markdown import (
    AuthoringMarkdownError,
    parse_authoring_markdown,
)

_CATALOG_LAYOUT = (
    ("component", "components", "component_roots", "component.md"),
    ("flavor", "flavors", "flavor_roots", "flavor.md"),
    ("skill", "skills", "skill_roots", "SKILL.md"),
    ("mcp", "mcps", "mcp_roots", "mcp.md"),
    ("workflow", "workflows", "workflow_roots", None),
    ("routing", "routing", "routing_roots", None),
)
_DERIVED_COMPONENT_FILE_NAMES = frozenset(
    {"component.lock.json", ".component.lock.write.lock"}
)
_DERIVED_COMPONENT_FILE_PREFIXES = ("component.resolution-audit.",)


def _component_file_is_inheritable(relative: tuple[str, ...]) -> bool:
    """Keep target/host qualification evidence under descendant ownership."""

    name = relative[-1]
    return name not in _DERIVED_COMPONENT_FILE_NAMES and not any(
        name.startswith(prefix) for prefix in _DERIVED_COMPONENT_FILE_PREFIXES
    )


def _is_component_derived_evidence(path: tuple[str, ...]) -> bool:
    """Keep repository-local lock evidence out of inherited Component authority."""

    name = path[-1] if path else ""
    return name == "component.lock.json" or (
        name.startswith("component.resolution-audit.") and name.endswith(".json")
    )


class RepositoryCatalogError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class RepositoryCatalogProvider(Protocol):
    def catalog(self, node: RepositoryLineageNode) -> ResolvedRepositoryCatalog: ...


@dataclass(frozen=True, slots=True)
class InheritedCatalogFile:
    destination: str
    content: bytes
    executable: bool

    @property
    def identity(self) -> ContentIdentity:
        return ContentIdentity(
            HashAlgorithm.SHA256, hashlib.sha256(self.content).hexdigest()
        )


@dataclass(frozen=True, slots=True)
class InheritedCatalogItem:
    kind: str
    name: str
    source: RepositoryLineageNode
    files: tuple[InheritedCatalogFile, ...]
    inheritable: bool = True


@dataclass(frozen=True, slots=True)
class InheritedCatalogDecision:
    item: InheritedCatalogItem
    disposition: str
    selected_source_project_id: str | None = None


@dataclass(frozen=True, slots=True)
class InheritedCatalogPlan:
    lineage: RepositoryLineage
    items: tuple[InheritedCatalogItem, ...]
    decisions: tuple[InheritedCatalogDecision, ...] = ()
    authority_graph: AuthorityGraph | None = None

    def __post_init__(self) -> None:
        graph = self.authority_graph
        if graph is None:
            graph = repository_lineage_authority_graph(self.lineage)
            object.__setattr__(self, "authority_graph", graph)
        if not isinstance(graph, AuthorityGraph):
            raise TypeError("inherited catalog plan requires a solved authority graph")
        expected_nodes = {
            f"repository:{node.project_id}" for node in self.lineage.nodes
        }
        actual_nodes = {node.node_id for node in graph.nodes}
        by_identity = {node.identity: node for node in self.lineage.nodes}
        expected_edges = {
            (
                f"repository:{by_identity[parent].project_id}",
                f"repository:{node.project_id}",
                "repository-parent",
            )
            for node in self.lineage.nodes
            for parent in node.parents
        }
        actual_edges = {(edge.source, edge.target, edge.kind) for edge in graph.edges}
        if (
            graph.project_id != "repository-lineage"
            or actual_nodes != expected_nodes
            or actual_edges != expected_edges
            or any(node.kind != "repository" for node in graph.nodes)
        ):
            raise ValueError(
                "inherited catalog graph differs from its exact repository lineage"
            )


def _root_parts(root: str) -> tuple[str, ...]:
    parsed = PurePosixPath(root)
    return parsed.parts


def _is_beneath(path: tuple[str, ...], root: tuple[str, ...]) -> bool:
    return len(path) >= len(root) and path[: len(root)] == root


def _component_is_inheritable(
    source: bytes, *, repository_id: str, path: tuple[str, ...]
) -> bool:
    """Read the explicit opt-out; ordinary Components inherit by default."""

    if not source.startswith(b"---\n"):
        # Synthetic providers used by embedders may omit human authoring metadata.
        # Such Components retain the documented default.
        return True
    source_path = PurePosixPath(*path).as_posix()
    try:
        frontmatter, _body = parse_authoring_markdown(source, source=source_path)
    except AuthoringMarkdownError as exc:
        raise RepositoryCatalogError(
            "repository_catalog.component_authority_invalid",
            f"repository {repository_id!r} has invalid Component authority at "
            f"{source_path}: {exc.message}",
        ) from exc
    inheritable = frontmatter.get("inheritable", True)
    if not isinstance(inheritable, bool):
        raise RepositoryCatalogError(
            "repository_catalog.component_authority_invalid",
            f"repository {repository_id!r} Component {source_path} must declare "
            "inheritable as true or false",
        )
    return inheritable


def _catalog_items(
    catalog: ResolvedRepositoryCatalog,
) -> tuple[InheritedCatalogItem, ...]:
    files = {PurePosixPath(item.path).parts: item for item in catalog.files}
    declared_roots: list[tuple[str, tuple[str, ...]]] = []
    for kind, _destination, field, _sentinel in _CATALOG_LAYOUT:
        for root in getattr(catalog.definition, field):
            parts = _root_parts(root)
            for other_kind, other in declared_roots:
                if _is_beneath(parts, other) or _is_beneath(other, parts):
                    raise RepositoryCatalogError(
                        "repository_catalog.root_overlap",
                        f"repository {catalog.node.project_id!r} declares overlapping "
                        f"{other_kind} and {kind} catalog roots",
                    )
            declared_roots.append((kind, parts))

    result: list[InheritedCatalogItem] = []
    for kind, destination_root, field, sentinel in _CATALOG_LAYOUT:
        for root in getattr(catalog.definition, field):
            root_parts = _root_parts(root)
            # Samples share the typed Component contract, but their repository role is
            # part of the human taxonomy. Preserve that role across inheritance instead
            # of flattening an inherited demo into the reusable components/ catalog.
            effective_destination_root = (
                "samples"
                if kind == "component" and root_parts[-1] == "samples"
                else destination_root
            )
            if sentinel is None:
                for path, source_file in sorted(files.items()):
                    if len(path) <= len(root_parts) or not _is_beneath(
                        path, root_parts
                    ):
                        continue
                    relative = path[len(root_parts) :]
                    name = PurePosixPath(*relative).as_posix()
                    result.append(
                        InheritedCatalogItem(
                            kind,
                            name,
                            catalog.node,
                            (
                                InheritedCatalogFile(
                                    PurePosixPath(
                                        effective_destination_root, *relative
                                    ).as_posix(),
                                    source_file.content,
                                    source_file.executable,
                                ),
                            ),
                        )
                    )
                continue
            item_roots = tuple(
                sorted(
                    {
                        path[:-1]
                        for path in files
                        if path[-1] == sentinel
                        and len(path) > len(root_parts) + 1
                        and _is_beneath(path, root_parts)
                    },
                    key=lambda value: (len(value), value),
                )
            )
            for item_root in item_roots:
                name_parts = item_root[len(root_parts) :]
                name = PurePosixPath(*name_parts).as_posix()
                sentinel_path = (*item_root, sentinel)
                inheritable = not kind == "component" or _component_is_inheritable(
                    files[sentinel_path].content,
                    repository_id=catalog.node.project_id,
                    path=sentinel_path,
                )
                owned: list[InheritedCatalogFile] = []
                for path, source_file in files.items():
                    if not _is_beneath(path, item_root):
                        continue
                    if kind == "component" and _is_component_derived_evidence(path):
                        continue
                    deeper = tuple(
                        candidate
                        for candidate in item_roots
                        if len(candidate) > len(item_root)
                        and _is_beneath(path, candidate)
                        and _is_beneath(candidate, item_root)
                    )
                    if deeper:
                        continue
                    relative = path[len(item_root) :]
                    if kind == "component" and not _component_file_is_inheritable(
                        relative
                    ):
                        continue
                    destination = PurePosixPath(
                        effective_destination_root, *name_parts, *relative
                    ).as_posix()
                    owned.append(
                        InheritedCatalogFile(
                            destination,
                            source_file.content,
                            source_file.executable,
                        )
                    )
                result.append(
                    InheritedCatalogItem(
                        kind,
                        name,
                        catalog.node,
                        tuple(sorted(owned, key=lambda item: item.destination)),
                        inheritable,
                    )
                )
    return tuple(sorted(result, key=lambda item: (item.kind, item.name)))


def plan_inherited_catalogs(
    lineage: RepositoryLineage,
    provider: RepositoryCatalogProvider,
) -> InheritedCatalogPlan:
    """Compose catalogs ancestor-first; descendants override, siblings conflict."""

    if not isinstance(lineage, RepositoryLineage):
        raise TypeError("catalog inheritance requires typed repository lineage")
    graph = repository_lineage_authority_graph(lineage)
    by_project = {item.project_id: item for item in lineage.nodes}
    selected: dict[tuple[str, str], InheritedCatalogItem] = {}
    selected_source: dict[tuple[str, str], str] = {}
    decisions: list[InheritedCatalogDecision] = []
    for node_id in graph.topological_order():
        node = by_project[node_id.removeprefix("repository:")]
        try:
            catalog = provider.catalog(node)
        except RepositoryCatalogError:
            raise
        except Exception as exc:
            raise RepositoryCatalogError(
                str(getattr(exc, "code", "repository_catalog.read_failed")),
                str(
                    getattr(
                        exc,
                        "message",
                        f"repository catalog could not be read for {node.project_id!r}",
                    )
                ),
            ) from exc
        if not isinstance(catalog, ResolvedRepositoryCatalog) or catalog.node != node:
            raise RepositoryCatalogError(
                "repository_catalog.result_invalid",
                "repository catalog provider returned inconsistent evidence",
            )
        ancestors = frozenset(graph.ancestors(node_id))
        for item in _catalog_items(catalog):
            key = (item.kind, item.name)
            previous = selected_source.get(key)
            if item.inheritable and previous is not None and previous not in ancestors:
                raise RepositoryCatalogError(
                    "repository_catalog.conflicting_origin",
                    f"incomparable repositories provide {item.kind}:{item.name}",
                )
            if not item.inheritable:
                decisions.append(InheritedCatalogDecision(item, "withheld"))
                if previous is not None and previous in ancestors:
                    prior = selected.pop(key)
                    selected_source.pop(key)
                    decisions.append(
                        InheritedCatalogDecision(
                            prior, "shadowed", item.source.project_id
                        )
                    )
                continue
            if previous is not None:
                prior = selected[key]
                decisions.append(
                    InheritedCatalogDecision(prior, "shadowed", item.source.project_id)
                )
            selected[key] = item
            selected_source[key] = node_id
    decisions.extend(
        InheritedCatalogDecision(selected[key], "effective") for key in sorted(selected)
    )
    return InheritedCatalogPlan(
        lineage,
        tuple(selected[key] for key in sorted(selected)),
        tuple(
            sorted(
                decisions,
                key=lambda item: (
                    item.item.kind,
                    item.item.name,
                    item.item.source.project_id,
                    item.disposition,
                ),
            )
        ),
        graph,
    )


def _safe_destination(root: Path, relative: str) -> Path:
    path = root.resolve()
    for part in PurePosixPath(relative).parts:
        path /= part
        if path_is_link_or_reparse(path):
            raise RepositoryCatalogError(
                "repository_catalog.path_unsafe",
                f"inherited catalog path crosses a link or reparse point: {relative}",
            )
    return path


def materialize_inherited_catalogs(
    target: Path,
    plan: InheritedCatalogPlan,
) -> tuple[str, ...]:
    """Write a prevalidated catalog plan and its existing provenance contract."""

    root = Path(target).resolve()
    if not root.is_dir():
        raise RepositoryCatalogError(
            "repository_catalog.target_invalid",
            "inherited catalog target must be an initialized directory",
        )
    for item in plan.items:
        for inherited in item.files:
            destination = _safe_destination(root, inherited.destination)
            if destination.exists() and (
                destination.is_symlink() or not destination.is_file()
            ):
                raise RepositoryCatalogError(
                    "repository_catalog.path_conflict",
                    "inherited catalog conflicts with a non-file: "
                    f"{inherited.destination}",
                )

    created: list[str] = []
    for item in plan.items:
        for inherited in item.files:
            destination = _safe_destination(root, inherited.destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(inherited.content)
            if os.name != "nt":
                os.chmod(destination, 0o755 if inherited.executable else 0o644)
            created.append(inherited.destination)
    catalog_imports_for_plan(plan).save(root)
    created.append(CatalogImportsFile.PATH)
    return tuple(sorted(set(created)))


def catalog_imports_for_plan(
    plan: InheritedCatalogPlan,
    *,
    materialized_at: str | None = None,
) -> CatalogImportsFile:
    """Project exact inherited files into the existing catalog provenance model."""

    timestamp = materialized_at or datetime.now(UTC).isoformat()
    imports: list[CatalogImport] = []
    graph = plan.authority_graph
    assert graph is not None
    by_project = {item.project_id: item for item in plan.lineage.nodes}

    def source(item: InheritedCatalogItem) -> CatalogImportSource:
        ancestor_projects = {
            node_id.removeprefix("repository:")
            for node_id in graph.ancestors(f"repository:{item.source.project_id}")
        }
        ancestors = tuple(
            TransitiveAncestor(
                project_id=node.project_id,
                project_identity=node.project_identity.uri,
                ref=f"git:{node.repository_url}@{node.resolved_revision}",
            )
            for node_id in graph.topological_order()
            for node in (by_project[node_id.removeprefix("repository:")],)
            if node.project_id in ancestor_projects
        )
        return CatalogImportSource(
            project_id=item.source.project_id,
            project_identity=item.source.project_identity.uri,
            ref=f"git:{item.source.repository_url}@{item.source.resolved_revision}",
            transitive_ancestors=ancestors,
        )

    for item in plan.items:
        imports.append(
            CatalogImport(
                kind=item.kind,
                name=item.name,
                source=source(item),
                files=tuple(
                    CatalogImportFile(
                        path=inherited.destination,
                        identity=inherited.identity.uri,
                    )
                    for inherited in item.files
                ),
                copied_at=timestamp,
            )
        )
    inheritance_decisions = tuple(
        CatalogInheritanceDecision(
            kind=decision.item.kind,
            name=decision.item.name,
            source=source(decision.item),
            files=tuple(
                CatalogImportFile(path=item.destination, identity=item.identity.uri)
                for item in decision.item.files
            ),
            disposition=decision.disposition,
            selected_source_project_id=decision.selected_source_project_id,
        )
        for decision in plan.decisions
    )
    return CatalogImportsFile(tuple(imports), inheritance_decisions)


__all__ = [
    "InheritedCatalogFile",
    "InheritedCatalogDecision",
    "InheritedCatalogItem",
    "InheritedCatalogPlan",
    "RepositoryCatalogError",
    "RepositoryCatalogProvider",
    "catalog_imports_for_plan",
    "materialize_inherited_catalogs",
    "plan_inherited_catalogs",
]

"""Canonical, provenance-aware DAG solving and deterministic graph export."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

AUTHORITY_GRAPH_SCHEMA = "literate-ai/authority-graph@2"
_IDENTIFIER = re.compile(r"[^A-Za-z0-9_]")


class AuthorityGraphError(ValueError):
    """Stable graph-integrity failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, order=True, slots=True)
class AuthorityGraphNode:
    node_id: str
    kind: str
    label: str
    project_id: str
    provenance: str
    inherited: bool = False
    inheritable: bool | None = None
    properties: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "project_id": self.project_id,
            "provenance": self.provenance,
            "inherited": self.inherited,
            "inheritable": self.inheritable,
            "properties": dict(self.properties),
        }


@dataclass(frozen=True, order=True, slots=True)
class AuthorityGraphEdge:
    source: str
    target: str
    kind: str
    label: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class AuthorityGraph:
    project_id: str
    nodes: tuple[AuthorityGraphNode, ...]
    edges: tuple[AuthorityGraphEdge, ...]

    def __post_init__(self) -> None:
        if tuple(sorted(self.nodes)) != self.nodes or len(set(self.nodes)) != len(
            self.nodes
        ):
            raise AuthorityGraphError(
                "graph.nodes_noncanonical", "graph nodes must be unique and sorted"
            )
        if tuple(sorted(self.edges)) != self.edges or len(set(self.edges)) != len(
            self.edges
        ):
            raise AuthorityGraphError(
                "graph.edges_noncanonical", "graph edges must be unique and sorted"
            )
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(set(node_ids)) != len(node_ids):
            raise AuthorityGraphError(
                "graph.node_id_conflict", "graph node IDs must be unique"
            )
        known = set(node_ids)
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise AuthorityGraphError(
                    "graph.edge_unresolved",
                    f"{edge.kind} edge names an unknown endpoint: "
                    f"{edge.source} -> {edge.target}",
                )
            if edge.source == edge.target:
                raise AuthorityGraphError(
                    "graph.cycle", f"self-cycle detected at {edge.source}"
                )
        self.topological_order()

    @classmethod
    def create(
        cls,
        project_id: str,
        nodes: Iterable[AuthorityGraphNode],
        edges: Iterable[AuthorityGraphEdge],
    ) -> AuthorityGraph:
        return cls(project_id, tuple(sorted(nodes)), tuple(sorted(set(edges))))

    def topological_order(self) -> tuple[str, ...]:
        """Return one stable topological order or report the exact remaining cycle."""

        outgoing = {node.node_id: [] for node in self.nodes}
        indegree = {node.node_id: 0 for node in self.nodes}
        for edge in self.edges:
            outgoing[edge.source].append(edge.target)
            indegree[edge.target] += 1
        ready = sorted(node for node, degree in indegree.items() if degree == 0)
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for target in sorted(outgoing[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
                    ready.sort()
        if len(ordered) != len(self.nodes):
            remaining = {node for node, degree in indegree.items() if degree}
            cycle = self._cycle_path(remaining, outgoing)
            raise AuthorityGraphError(
                "graph.cycle",
                "directed cycle detected: " + " -> ".join(cycle),
            )
        return tuple(ordered)

    def topological_layers(self) -> tuple[tuple[str, ...], ...]:
        """Return stable dependency-first parallel layers from the solved DAG."""

        dependencies = {node.node_id: set() for node in self.nodes}
        for edge in self.edges:
            dependencies[edge.target].add(edge.source)
        remaining = set(dependencies)
        layers: list[tuple[str, ...]] = []
        while remaining:
            ready = tuple(
                sorted(
                    node_id
                    for node_id in remaining
                    if not (dependencies[node_id] & remaining)
                )
            )
            if not ready:
                # Construction already proves acyclicity; this guards mutation through
                # hostile object-level test fixtures at a later trust boundary.
                outgoing = {node.node_id: [] for node in self.nodes}
                for edge in self.edges:
                    outgoing[edge.source].append(edge.target)
                cycle = self._cycle_path(remaining, outgoing)
                raise AuthorityGraphError(
                    "graph.cycle",
                    "directed cycle detected: " + " -> ".join(cycle),
                )
            layers.append(ready)
            remaining.difference_update(ready)
        return tuple(layers)

    @staticmethod
    def _cycle_path(
        remaining: set[str], outgoing: dict[str, list[str]]
    ) -> tuple[str, ...]:
        """Return the shortest deterministic closed cycle without recursion."""

        candidates: list[tuple[str, ...]] = []
        for start in sorted(remaining):
            pending = [start]
            parent: dict[str, str | None] = {start: None}
            cursor = 0
            while cursor < len(pending):
                current = pending[cursor]
                cursor += 1
                for target in sorted(set(outgoing[current]) & remaining):
                    if target == start:
                        path = [current]
                        while parent[path[-1]] is not None:
                            predecessor = parent[path[-1]]
                            assert predecessor is not None
                            path.append(predecessor)
                        candidates.append(tuple((*reversed(path), start)))
                        pending.clear()
                        break
                    if target not in parent:
                        parent[target] = current
                        pending.append(target)
        if not candidates:
            raise AssertionError("cyclic graph had no recoverable cycle path")
        return min(candidates, key=lambda item: (len(item), item))

    def ancestors(self, node_id: str) -> tuple[str, ...]:
        return self._closure(node_id, reverse=True)

    def descendants(self, node_id: str) -> tuple[str, ...]:
        return self._closure(node_id, reverse=False)

    def _closure(self, node_id: str, *, reverse: bool) -> tuple[str, ...]:
        if node_id not in {node.node_id for node in self.nodes}:
            raise AuthorityGraphError("graph.node_unknown", f"unknown node: {node_id}")
        adjacency = {node.node_id: [] for node in self.nodes}
        for edge in self.edges:
            source, target = (
                (edge.target, edge.source) if reverse else (edge.source, edge.target)
            )
            adjacency[source].append(target)
        seen: set[str] = set()
        pending = [node_id]
        while pending:
            current = pending.pop()
            for target in adjacency[current]:
                if target not in seen:
                    seen.add(target)
                    pending.append(target)
        return tuple(sorted(seen))

    def filtered(
        self,
        *,
        kinds: Iterable[str] = (),
        provenance: Iterable[str] = (),
        ownership: str | None = None,
        inheritance: str | None = None,
        edge_kinds: Iterable[str] = (),
    ) -> AuthorityGraph:
        """Return a deterministic view without changing the solved source graph."""

        selected_kinds = frozenset(kinds)
        selected_provenance = frozenset(provenance)
        selected_edge_kinds = frozenset(edge_kinds)
        nodes = tuple(
            node
            for node in self.nodes
            if (not selected_kinds or node.kind in selected_kinds)
            and (not selected_provenance or node.provenance in selected_provenance)
            and (ownership is None or node.inherited == (ownership == "inherited"))
            and (
                inheritance is None
                or node.inheritable == (inheritance == "inheritable")
            )
        )
        node_ids = {node.node_id for node in nodes}
        edges = tuple(
            edge
            for edge in self.edges
            if edge.source in node_ids
            and edge.target in node_ids
            and (not selected_edge_kinds or edge.kind in selected_edge_kinds)
        )
        return AuthorityGraph.create(self.project_id, nodes, edges)

    def to_dict(self) -> dict[str, object]:
        return {**self._identity_material(), "identity": self.identity}

    def _identity_material(self) -> dict[str, object]:
        return {
            "schema": AUTHORITY_GRAPH_SCHEMA,
            "project_id": self.project_id,
            "acyclic": True,
            "topological_order": list(self.topological_order()),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @property
    def identity(self) -> str:
        """Bind every view to the exact solved graph without recursive material."""

        content = json.dumps(
            self._identity_material(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def rebalance_recommendations(self) -> tuple[dict[str, object], ...]:
        """Suggest exact-identity upward moves; never infer semantic equivalence."""

        repository_ids = {
            node.node_id for node in self.nodes if node.kind == "repository"
        }
        entities: dict[str, list[AuthorityGraphNode]] = {}
        for node in self.nodes:
            if node.kind not in {"component", "flavor", "skill"}:
                continue
            identity = dict(node.properties).get("identity")
            if identity is not None:
                entities.setdefault(identity, []).append(node)
        recommendations: list[dict[str, object]] = []
        for identity, duplicates in sorted(entities.items()):
            if len(duplicates) < 2:
                continue
            owners = {
                f"repository:{node.provenance}"
                for node in duplicates
                if f"repository:{node.provenance}" in repository_ids
            }
            if len(owners) < 2:
                continue
            common: set[str] | None = None
            for owner in owners:
                ancestry = {owner, *self.ancestors(owner)} & repository_ids
                common = ancestry if common is None else common & ancestry
            if not common:
                continue
            destination = max(
                common,
                key=lambda item: (
                    len(set(self.ancestors(item)) & repository_ids),
                    item,
                ),
            )
            recommendations.append(
                {
                    "schema": "literate-ai/authority-rebalance-recommendation@2",
                    "basis": "exact-content-identity",
                    "identity": identity,
                    "entities": [node.node_id for node in sorted(duplicates)],
                    "current_repositories": sorted(owners),
                    "lowest_common_ancestor": destination,
                    "affected_repository_descendants": sorted(
                        {
                            repository
                            for repository in self.descendants(destination)
                            if repository in repository_ids
                        }
                    ),
                    "context_cost": {
                        "duplicate_authority_nodes": len(duplicates),
                        "repository_copies": len(owners),
                        "estimated_copies_avoided": len(duplicates) - 1,
                    },
                    "risks": [
                        "authored ownership changes",
                        "descendant imports and locks require review",
                    ],
                    "automatic_action": False,
                }
            )
        return tuple(recommendations)

    def render(self, format_name: str) -> str:
        renderers = {
            "json": self._json,
            "text": self._text,
            "mermaid": self._mermaid,
            "dot": self._dot,
            "svg": self._svg,
        }
        try:
            return renderers[format_name]()
        except KeyError as exc:
            raise AuthorityGraphError(
                "graph.format_unknown", f"unsupported graph format: {format_name}"
            ) from exc

    def _json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"

    def _text(self) -> str:
        by_id = {node.node_id: node for node in self.nodes}
        lines = [f"project {self.project_id}"]
        for node_id in self.topological_order():
            node = by_id[node_id]
            flags = ["inherited" if node.inherited else "local"]
            if node.inheritable is not None:
                flags.append("inheritable" if node.inheritable else "private")
            lines.append(
                f"  {node.kind} {node.label} [{', '.join(flags)}] <- {node.provenance}"
            )
        if self.edges:
            lines.append("edges")
            lines.extend(
                f"  {edge.source} -{edge.kind}-> {edge.target}"
                + (f" ({edge.label})" if edge.label else "")
                for edge in self.edges
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _graph_id(node_id: str) -> str:
        return "n_" + _IDENTIFIER.sub("_", node_id)

    def _mermaid(self) -> str:
        lines = ["flowchart LR"]
        for node in self.nodes:
            label = node.label.replace('"', "'")
            lines.append(f'    {self._graph_id(node.node_id)}["{label}"]')
        for edge in self.edges:
            label = edge.kind if not edge.label else f"{edge.kind}: {edge.label}"
            lines.append(
                f'    {self._graph_id(edge.source)} -->|"{label}"| '
                f"{self._graph_id(edge.target)}"
            )
        return "\n".join(lines) + "\n"

    def _dot(self) -> str:
        lines = ["digraph literate_ai {", "  rankdir=LR;"]
        for node in self.nodes:
            label = node.label.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'  "{node.node_id}" [label="{label}"];')
        for edge in self.edges:
            label = edge.kind if not edge.label else f"{edge.kind}: {edge.label}"
            label = label.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'  "{edge.source}" -> "{edge.target}" [label="{label}"];')
        lines.append("}")
        return "\n".join(lines) + "\n"

    def _svg(self) -> str:
        """Render a dependency-free SVG without discarding graph semantics."""

        layers = self.topological_layers()
        positions = {
            node_id: (40 + layer_index * 230, 40 + row_index * 90)
            for layer_index, layer in enumerate(layers)
            for row_index, node_id in enumerate(layer)
        }
        width = max(320, 100 + len(layers) * 230)
        height = max(150, 80 + max((len(layer) for layer in layers), default=1) * 90)
        edge_lines = []
        for edge in self.edges:
            x1, y1 = positions[edge.source]
            x2, y2 = positions[edge.target]
            attributes = (
                f'data-source="{html.escape(edge.source, quote=True)}" '
                f'data-target="{html.escape(edge.target, quote=True)}" '
                f'data-kind="{html.escape(edge.kind, quote=True)}" '
                f'data-label="{html.escape(edge.label, quote=True)}"'
            )
            edge_lines.append(
                f'<g class="edge" {attributes}><title>'
                f"{html.escape(edge.kind + (': ' + edge.label if edge.label else ''))}"
                f'</title><line x1="{x1 + 170}" y1="{y1 + 25}" x2="{x2}" '
                f'y2="{y2 + 25}" stroke="#52606d" stroke-width="2" '
                'marker-end="url(#arrow)"/></g>'
            )
        by_id = {node.node_id: node for node in self.nodes}
        node_lines = []
        for node_id in self.topological_order():
            node = by_id[node_id]
            x, y = positions[node_id]
            fill = "#d9eaf7" if node.inherited else "#e7f6e7"
            attributes = (
                f'data-id="{html.escape(node.node_id, quote=True)}" '
                f'data-kind="{html.escape(node.kind, quote=True)}" '
                f'data-project="{html.escape(node.project_id, quote=True)}" '
                f'data-provenance="{html.escape(node.provenance, quote=True)}"'
            )
            node_lines.extend(
                (
                    f'<g class="node" {attributes}><title>'
                    f"{html.escape(node.node_id)}</title>"
                    f'<rect x="{x}" y="{y}" width="170" height="50" rx="8" '
                    f'fill="{fill}" stroke="#334e68"/>',
                    f'<text x="{x + 85}" y="{y + 30}" text-anchor="middle" '
                    f'font-family="sans-serif" font-size="12">'
                    f"{html.escape(node.label)}</text></g>",
                )
            )
        body = "".join((*edge_lines, *node_lines))
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Literate AI authority graph"><defs>'
            f'<marker id="arrow" markerWidth="8" '
            'markerHeight="8" refX="7" refY="4" orient="auto">'
            '<path d="M0,0 L8,4 L0,8 z" '
            f'fill="#52606d"/></marker></defs>{body}</svg>\n'
        )


__all__ = [
    "AUTHORITY_GRAPH_SCHEMA",
    "AuthorityGraph",
    "AuthorityGraphEdge",
    "AuthorityGraphError",
    "AuthorityGraphNode",
]

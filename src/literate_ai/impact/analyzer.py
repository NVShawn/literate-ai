"""Explain which immutable outputs are stale after an input identity changes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProvenanceNode:
    node_id: str
    kind: str
    digest: str

    def __post_init__(self) -> None:
        if not self.node_id or not self.kind:
            raise ValueError("provenance node identity cannot be empty")
        if not self.digest.startswith("sha256:") or len(self.digest) != 71:
            raise ValueError("provenance node digest must be sha256")


@dataclass(frozen=True, slots=True)
class ProvenanceEdge:
    input_node_id: str
    output_node_id: str
    relationship: str

    def __post_init__(self) -> None:
        if not self.input_node_id or not self.output_node_id or not self.relationship:
            raise ValueError("provenance edge fields cannot be empty")
        if self.input_node_id == self.output_node_id:
            raise ValueError("provenance edge cannot reference itself")


@dataclass(frozen=True, slots=True)
class ImpactRecord:
    changed_node_ids: tuple[str, ...]
    affected_node_ids: tuple[str, ...]
    reasons: tuple[tuple[str, tuple[str, ...]], ...]


class ImpactAnalyzer:
    def __init__(
        self,
        nodes: tuple[ProvenanceNode, ...],
        edges: tuple[ProvenanceEdge, ...],
    ) -> None:
        self.nodes = {node.node_id: node for node in nodes}
        if len(self.nodes) != len(nodes):
            raise ValueError("provenance node IDs must be unique")
        self.dependents: dict[str, list[ProvenanceEdge]] = {}
        for edge in edges:
            if (
                edge.input_node_id not in self.nodes
                or edge.output_node_id not in self.nodes
            ):
                raise ValueError("provenance edge references an unknown node")
            self.dependents.setdefault(edge.input_node_id, []).append(edge)

    def analyze(self, changed_node_ids: tuple[str, ...]) -> ImpactRecord:
        unknown = set(changed_node_ids) - self.nodes.keys()
        if unknown:
            raise ValueError(f"unknown changed provenance nodes: {sorted(unknown)}")
        queue = deque(changed_node_ids)
        affected: set[str] = set()
        reasons: dict[str, set[str]] = {}
        while queue:
            current = queue.popleft()
            for edge in sorted(
                self.dependents.get(current, ()),
                key=lambda item: (item.output_node_id, item.relationship),
            ):
                reasons.setdefault(edge.output_node_id, set()).add(
                    f"{current}:{edge.relationship}"
                )
                if edge.output_node_id not in affected:
                    affected.add(edge.output_node_id)
                    queue.append(edge.output_node_id)
        return ImpactRecord(
            changed_node_ids=tuple(sorted(set(changed_node_ids))),
            affected_node_ids=tuple(sorted(affected)),
            reasons=tuple(
                (node_id, tuple(sorted(values)))
                for node_id, values in sorted(reasons.items())
            ),
        )

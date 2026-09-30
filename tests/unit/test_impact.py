"""Impact analysis is explainable and never mutates accepted records."""

from __future__ import annotations

import unittest

from literate_ai.impact import (
    ImpactAnalyzer,
    ProvenanceEdge,
    ProvenanceNode,
)


def node(name: str, character: str) -> ProvenanceNode:
    return ProvenanceNode(name, name.split(":")[0], "sha256:" + character * 64)


class ImpactAnalyzerTests(unittest.TestCase):
    def test_change_propagates_through_evidence_plan_source_and_package(self) -> None:
        nodes = (
            node("source:dependency", "a"),
            node("evidence:api", "b"),
            node("plan:files", "c"),
            node("source:generated", "d"),
            node("package:artifact", "e"),
            node("source:unrelated", "f"),
        )
        edges = (
            ProvenanceEdge("source:dependency", "evidence:api", "indexed-as"),
            ProvenanceEdge("evidence:api", "plan:files", "supports"),
            ProvenanceEdge("plan:files", "source:generated", "generates"),
            ProvenanceEdge("source:generated", "package:artifact", "builds"),
        )
        result = ImpactAnalyzer(nodes, edges).analyze(("source:dependency",))
        self.assertEqual(
            result.affected_node_ids,
            (
                "evidence:api",
                "package:artifact",
                "plan:files",
                "source:generated",
            ),
        )
        self.assertNotIn("source:unrelated", result.affected_node_ids)


if __name__ == "__main__":
    unittest.main()

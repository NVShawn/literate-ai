from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_html_surfaces``."""











from literate_ai.authority_graph import (
    AuthorityGraph,
    AuthorityGraphEdge,
    AuthorityGraphNode,
)




def _graph() -> AuthorityGraph:
    return AuthorityGraph.create(
        "example",
        (
            AuthorityGraphNode(
                "repository:example", "repository", "Example", "example", "local"
            ),
            AuthorityGraphNode(
                "component:one",
                "component",
                "Ω <one>",
                "example",
                "components/one",
                properties=(("path", "components/one/component.md"),),
            ),
        ),
        (AuthorityGraphEdge("repository:example", "component:one", "defines"),),
    )


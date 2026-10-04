from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_html_source_excerpts``."""

import hashlib







from literate_ai.adapters.html_surfaces import bind_authority_graph

from literate_ai.authority_graph import AuthorityGraph, AuthorityGraphNode

from literate_ai.contracts.html_observability import HtmlView


def source_for(paths: dict[str, bytes]):
    graph = AuthorityGraph.create(
        "example",
        tuple(
            AuthorityGraphNode(
                str(index),
                "component",
                path,
                "example",
                "local",
                properties=(
                    ("path", path),
                    ("identity", "sha256:" + hashlib.sha256(content).hexdigest()),
                ),
            )
            for index, (path, content) in enumerate(paths.items())
        ),
        (),
    )
    return bind_authority_graph(graph, HtmlView("dag", "1.0.0", "project", "example"))


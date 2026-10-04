from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_html_emitter``."""


from pathlib import Path

from literate_ai.contracts.html_observability import (
    HtmlRenderRequest,
    HtmlView,
)
from literate_ai.contracts.identity import canonical_identity

ROOT = Path(__file__).resolve().parents[2]

STAMP = "2026-09-11T00:00:00Z"

DISTRIBUTION = canonical_identity({"test_fixture": "html-emitter"})


def request(
    project: str = "example", *, policy: str = "inline-only"
) -> HtmlRenderRequest:
    return HtmlRenderRequest(
        "authority-graph",
        HtmlView("dag", "1.0.0", "project", project),
        "graph.html",
        "off",
        policy,
    )

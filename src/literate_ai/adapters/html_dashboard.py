"""Offline multi-project shell over exact, already-rendered HTML artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from literate_ai.adapters.html_publication import (
    derived_output_provenance,
    html_output_path,
    publish_html,
    read_output,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.projects import discover_project

DASHBOARD_SCHEMA = "urn:literate-ai:schema:v1:html-observability-dashboard"

_STYLE = """
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #182b35;
  background: #eef2f1; }
* { box-sizing: border-box; }
body { margin: 0; }
header { padding: 24px 28px; background: #123d3a; color: white; }
h1 { margin: 0 0 8px; }
main { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 18px; padding: 18px; }
section { min-width: 0; background: white; border: 1px solid #b9cbc6;
  border-radius: 8px; overflow: hidden; }
h2 { font-size: 1rem; margin: 0; padding: 12px 16px; background: #dfeae7; }
iframe { display: block; width: 100%; height: 70vh; border: 0; }
footer { padding: 12px 28px 24px; color: #536762; }
@media (max-width: 520px) {
  main { grid-template-columns: minmax(0, 1fr); padding: 8px; }
}
""".strip()
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")


def _json_script(value: Any) -> str:
    text = canonical_json_bytes(value).decode("utf-8")
    for character, replacement in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(character, replacement)
    return text


@dataclass(frozen=True, slots=True)
class DashboardInput:
    path: str
    project_id: str
    surface_id: str
    view_id: str
    artifact_identity: str

    def __post_init__(self) -> None:
        path = Path(self.path)
        if (
            path.is_absolute()
            or path.suffix != ".html"
            or "\\" in self.path
            or ":" in self.path
            or not self.path
        ):
            raise ValueError("dashboard input path is not a relative HTML reference")
        for value in (self.project_id, self.surface_id, self.view_id):
            if _NAME.fullmatch(value) is None:
                raise ValueError("dashboard input identity is not portable")
        if _IDENTITY.fullmatch(self.artifact_identity) is None:
            raise ValueError("dashboard input artifact identity is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "project_id": self.project_id,
            "surface_id": self.surface_id,
            "view_id": self.view_id,
            "artifact_identity": self.artifact_identity,
        }


def _document(inputs: tuple[DashboardInput, ...]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": DASHBOARD_SCHEMA,
        "artifacts": [item.to_dict() for item in inputs],
    }
    return {**body, "dashboard_identity": canonical_identity(body).uri}


def _markup(document: dict[str, Any]) -> bytes:
    panes = []
    for index, item in enumerate(document["artifacts"]):
        title = f"{item['project_id']} · {item['surface_id']} / {item['view_id']}"
        panes.append(
            f'<section id="pane-{index}"><h2>{escape(title)}</h2>'
            f'<iframe src="{escape(item["path"])}" title="{escape(title)}" '
            'loading="lazy"></iframe></section>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Literate AI project dashboard</title><style>"
        + _STYLE
        + "</style></head><body><header><h1>Project observability dashboard</h1><p>"
        + str(len(panes))
        + " exact generated artifacts across "
        + str(len({item["project_id"] for item in document["artifacts"]}))
        + " projects</p></header><main>"
        + "".join(panes)
        + "</main><footer>Offline composition shell. Each pane retains its own "
        "source and renderer provenance.</footer>"
        '<script type="application/json" id="litai-dashboard">'
        + _json_script(document)
        + "</script></body></html>\n"
    ).encode("utf-8")


class _DashboardInspection(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active = False
        self.payload = ""
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("id") == "litai-dashboard":
            if values != {"type": "application/json", "id": "litai-dashboard"}:
                raise ValueError("dashboard metadata script is malformed")
            self.count += 1
            self.active = True

    def handle_data(self, data: str) -> None:
        if self.active:
            self.payload += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.active:
            self.active = False


def _recognized(
    content: bytes, *, root: Path | None = None, output: Path | None = None
) -> dict[str, Any]:
    parser = _DashboardInspection()
    parser.feed(content.decode("utf-8"))
    parser.close()
    if parser.count != 1 or parser.active:
        raise ValueError("dashboard metadata is missing or ambiguous")
    document = json.loads(parser.payload)
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "artifacts",
        "dashboard_identity",
    }:
        raise ValueError("dashboard metadata has an unsupported shape")
    expected = _document(
        tuple(
            DashboardInput(
                item["path"],
                item["project_id"],
                item["surface_id"],
                item["view_id"],
                item["artifact_identity"],
            )
            for item in document["artifacts"]
        )
    )
    if document != expected or _markup(document) != content:
        raise ValueError("dashboard output is foreign or edited")
    if (root is None) != (output is None):
        raise ValueError("dashboard recognition requires both path boundaries")
    if root is not None and output is not None:
        for item in expected["artifacts"]:
            if not (output.parent / item["path"]).resolve().is_relative_to(root):
                raise ValueError("dashboard pane escapes the selected root")
    return document


def _observe(root: Path, projects: tuple[Path, ...]) -> tuple[DashboardInput, ...]:
    inputs: list[DashboardInput] = []
    for project in projects:
        loaded = discover_project(project)
        if loaded is None:
            raise ValueError("dashboard project is unavailable")
        selected_root = loaded.root.resolve(strict=True)
        if not selected_root.is_relative_to(root):
            raise ValueError("dashboard projects must be inside the selected root")
        for request in loaded.definition.html_render_requests:
            target = html_output_path(selected_root, request.output_path)
            snapshot = read_output(target)
            if snapshot is None:
                raise ValueError("a declared dashboard artifact is missing")
            provenance = derived_output_provenance(snapshot.content, request.view)
            inputs.append(
                DashboardInput(
                    target.relative_to(root).as_posix(),
                    request.view.scope_identifier,
                    provenance.source_bindings[0].source_label,
                    request.view.view_id,
                    "sha256:" + hashlib.sha256(snapshot.content).hexdigest(),
                )
            )
    if not inputs:
        raise ValueError("dashboard projects declare no rendered HTML artifacts")
    ordered = tuple(
        sorted(inputs, key=lambda item: (item.project_id, item.surface_id, item.path))
    )
    if len(ordered) > 256 or len({item.path for item in ordered}) != len(ordered):
        raise ValueError("dashboard artifact selection is duplicate or oversized")
    return ordered


def render_dashboard(
    root: Path, projects: tuple[Path, ...], output: str
) -> dict[str, Any]:
    """Publish a deterministic shell after exact-input revalidation."""
    selected_root = root.resolve(strict=True)
    if not selected_root.is_dir():
        raise ValueError("dashboard root is unavailable")
    target = html_output_path(selected_root, output)
    observed = _observe(selected_root, projects)
    inputs = tuple(
        DashboardInput(
            Path(
                os.path.relpath(selected_root / item.path, start=target.parent)
            ).as_posix(),
            item.project_id,
            item.surface_id,
            item.view_id,
            item.artifact_identity,
        )
        for item in observed
    )
    document = _document(inputs)
    content = _markup(document)
    previous = read_output(target)
    if previous is not None:
        _recognized(previous.content, root=selected_root, output=target)

    def revalidate() -> None:
        if _observe(selected_root, projects) != observed:
            raise ValueError("dashboard inputs changed before publication")

    publish_html(target, content, previous, revalidate=revalidate)
    return {
        "status": "rendered",
        "output": target.relative_to(selected_root).as_posix(),
        "project_count": len({item.project_id for item in inputs}),
        "artifact_count": len(inputs),
        "dashboard_identity": document["dashboard_identity"],
    }


__all__ = ["DASHBOARD_SCHEMA", "render_dashboard"]

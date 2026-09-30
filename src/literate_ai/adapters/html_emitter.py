"""Single-file HTML bytes over a registered, already-solved JSON surface.

This is the in-memory emitter, not a publication or cache API. Its caller must
provide an observed framework distribution identity. Installed-distribution
resolution, filesystem custody and cache policy belong to the rendering service.
No asset is fetched here; opening a pinned-CDN artifact is a separate browser act.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from literate_ai.adapters import html_dag_view, html_source_excerpts
from literate_ai.adapters.html_source_excerpts import HtmlSourceExcerpt
from literate_ai.adapters.html_surfaces import HtmlSurfaceSource, load_html_surface
from literate_ai.contracts.html_observability import (
    HtmlArtifact,
    HtmlEmbedding,
    HtmlExternalAsset,
    HtmlProvenance,
    HtmlRendererBinding,
    HtmlRenderRefusal,
    HtmlRenderRequest,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.diagnostics import debug_stage
from literate_ai.projects import ProjectError, discover_project

_STYLE = """
:root { color-scheme: light; font-family: system-ui, sans-serif; color: #182b35;
  background: #f4f6f5; line-height: 1.55; }
* { box-sizing: border-box; }
body { margin: 0; }
main { max-width: 1100px; margin: auto; padding: 36px 28px 64px; }
header { border-top: 5px solid #17756e; padding-top: 24px; }
.eyebrow { color: #276860; font-size: .76rem; font-weight: 750; letter-spacing: .13em; }
h1 { font-size: clamp(2rem, 5vw, 3.5rem); line-height: 1.12; margin: 14px 0; }
h2 { font-size: 1.2rem; margin: 0 0 12px; }
p { margin: 10px 0; }
.muted { color: #51636d; }
.stats { display: flex; flex-wrap: wrap; gap: 12px; margin: 26px 0; }
.stat { flex: 1 1 130px; padding: 16px 20px; border: 1px solid #cbd8d4;
  border-radius: 8px; background: white; }
.stat strong { display: block; font-size: 1.75rem; line-height: 1.2; }
nav { display: flex; flex-wrap: wrap; gap: 20px; padding: 0 0 22px; }
a { color: #146d67; text-underline-offset: .18em; overflow-wrap: anywhere; }
section { margin: 24px 0; }
details { background: white; border: 1px solid #cbd8d4; border-radius: 8px;
  margin: 10px 0; min-width: 0; }
summary { cursor: pointer; padding: 14px 18px; overflow-wrap: anywhere; }
summary:hover { background: #edf4f1; }
summary:focus-visible, a:focus-visible { outline: 3px solid #9c4b08;
  outline-offset: 3px; }
details[open] > summary { border-bottom: 1px solid #dce5e1; }
.detail-body { padding: 6px 18px 18px; min-width: 0; overflow-wrap: anywhere; }
.nodes { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px; padding: 12px; }
.node { margin: 0; background: #fafcfb; }
.badge { font-size: .8rem; color: #51636d; margin-left: 8px; }
dl { margin: 12px 0; }
dt { font-size: .8rem; font-weight: 700; margin-top: 12px; color: #51636d; }
dd { margin: 2px 0; overflow-wrap: anywhere; }
code { font-size: .84em; overflow-wrap: anywhere; white-space: pre-wrap; }
ul { padding-left: 22px; }
li { margin: 8px 0; overflow-wrap: anywhere; }
footer { border-top: 1px solid #cbd8d4; padding-top: 18px; font-size: .85rem; }
@media (max-width: 480px) {
  main { padding: 20px 16px 40px; }
  .nodes { grid-template-columns: minmax(0, 1fr); padding: 8px; }
  .stat { padding: 12px; }
}
""".strip()


def _json_script(value: Any) -> str:
    """JSON escaping and HTML escaping are different contexts."""
    result = canonical_json_bytes(value).decode("utf-8")
    for character, replacement in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        result = result.replace(character, replacement)
    return result


def html_renderer_binding(
    framework_distribution_identity: ContentIdentity,
    schema_catalog_release: str,
    external_assets: tuple[HtmlExternalAsset, ...] = (),
    *,
    include_dag: bool = True,
    surface_id: str = "authority-graph",
) -> HtmlRendererBinding:
    """Bind this emitter and its exact asset declarations, not ambient metadata."""
    if (
        not isinstance(external_assets, tuple)
        or len(external_assets) > 32
        or any(not isinstance(asset, HtmlExternalAsset) for asset in external_assets)
        or len({asset.asset_id for asset in external_assets}) != len(external_assets)
    ):
        raise ValueError(
            "external assets must be a bounded, uniquely named typed tuple"
        )
    return HtmlRendererBinding(
        surface_id + "-html",
        "1.0.0",
        framework_distribution_identity,
        schema_catalog_release,
        canonical_identity(
            {
                "presentation": "interactive-dag" if include_dag else "static-assembly",
                "emitter_source": Path(__file__).read_text(encoding="utf-8"),
                "dag_source": Path(html_dag_view.__file__).read_text(encoding="utf-8"),
                "excerpt_source": Path(html_source_excerpts.__file__).read_text(
                    encoding="utf-8"
                ),
                "external_assets": [asset.to_dict() for asset in external_assets],
            }
        ),
    )


@dataclass(frozen=True, slots=True)
class HtmlEmission:
    content: bytes
    artifact: HtmlArtifact

    def __post_init__(self) -> None:
        if not self.artifact.matches_bytes(self.content):
            raise ValueError("emission bytes do not match their artifact")


def _definition(label: str, value: str) -> str:
    return f"<dt>{escape(label)}</dt><dd><code>{escape(value)}</code></dd>"


def _graph_markup(
    document: dict[str, Any], excerpts: tuple[HtmlSourceExcerpt, ...] | None = None
) -> str:
    nodes = document["nodes"]
    anchors = {node["id"]: f"node-{index}" for index, node in enumerate(nodes)}
    labels = {node["id"]: node["label"] for node in nodes}
    previews = {item.node_id: item for item in excerpts or ()}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        groups[node["kind"]].append(node)
    sections = []
    for kind in sorted(groups, key=lambda value: (value != "repository", value)):
        cards = []
        for node in groups[kind]:
            definitions = [
                ("Identifier", node["id"]),
                ("Project", node["project_id"]),
                ("Provenance", node["provenance"]),
                ("Inherited", "yes" if node["inherited"] else "no"),
                (
                    "Inheritable",
                    "unspecified"
                    if node["inheritable"] is None
                    else "yes"
                    if node["inheritable"]
                    else "no",
                ),
                *sorted(node["properties"].items()),
            ]
            cards.append(
                f'<details class="node" id="{anchors[node["id"]]}">'
                f"<summary><strong>{escape(node['label'])}</strong></summary>"
                '<div class="detail-body"><dl>'
                + "".join(_definition(key, value) for key, value in definitions)
                + "</dl>"
                + (
                    _excerpt_markup(previews[node["id"]])
                    if excerpts is not None
                    else ""
                )
                + "</div></details>"
            )
        sections.append(
            '<details class="catalog-group"><summary><strong>'
            + escape(kind)
            + f'</strong><span class="badge">{len(cards)} '
            + ("node" if len(cards) == 1 else "nodes")
            + "</span></summary>"
            + '<div class="nodes">'
            + "".join(cards)
            + "</div></details>"
        )
    catalog = (
        "".join(sections) or '<p class="muted">No authority nodes are present.</p>'
    )
    edges = []
    for edge in document["edges"]:
        source, target = edge["source"], edge["target"]
        edges.append(
            f'<li><a href="#{anchors[source]}">{escape(labels[source])}</a>'
            f' → <a href="#{anchors[target]}">{escape(labels[target])}</a>'
            f' <span class="badge">{escape(edge["kind"])}</span>'
            + (f" — {escape(edge['label'])}" if edge["label"] else "")
            + "</li>"
        )
    relationships = (
        "<ul>" + "".join(edges) + "</ul>"
        if edges
        else '<p class="muted">No authority relationships are present.</p>'
    )
    return (
        '<section id="catalog"><h2>Catalog</h2>'
        '<p class="muted">Expand a type, then a node to inspect its authority.</p>'
        + catalog
        + '</section><section id="relationships"><h2>Relationships</h2>'
        '<p class="muted">Each arrow points from source to target. '
        "Relationship kinds name the connection; color carries no extra meaning.</p>"
        f"<details><summary>Explore {len(edges)} relationships</summary>"
        '<div class="detail-body">' + relationships + "</div></details></section>"
    )


def _excerpt_markup(excerpt: HtmlSourceExcerpt) -> str:
    if excerpt.text is None:
        return '<p class="muted">No identity-bound local source is declared.</p>'
    return (
        '<details class="source-preview">'
        "<summary>Read verified source preview</summary>"
        '<div class="detail-body">'
        + f"<p>{escape(excerpt.path)}</p>"
        + (
            "<p>Bounded preview; complete file hash verified.</p>"
            if excerpt.truncated
            else "<p>Complete source; hash verified.</p>"
        )
        + f"<pre><code>{escape(excerpt.text)}</code></pre></div></details>"
    )


def _version_health_markup(document: dict[str, Any]) -> str:
    status = "Pass" if document["ok"] else "Fail"
    cards = []
    for key, label in (
        ("distribution", "Distribution"),
        ("tag", "Release tag"),
        ("project", "Project"),
        ("schema_catalogs", "Schema catalogs"),
    ):
        report = document[key]
        # Catalog diagnostics have no independent overall boolean. Do not infer one.
        state = (
            ("Pass" if report["ok"] else "Fail") if "ok" in report else "Diagnostics"
        )
        detail = str(report.get("state", report.get("install_kind", "")))
        cards.append(
            f"<details><summary>{label}: {state}"
            + (f" · {escape(detail)}" if detail else "")
            + '</summary><div class="detail-body"><pre><code>'
            + escape(json.dumps(report, indent=2, ensure_ascii=False))
            + "</code></pre></div></details>"
        )
    return (
        '<div class="stats"><div class="stat"><strong>'
        + status
        + "</strong>Version agreement</div></div>"
        '<p class="muted">Observed version agreement at the generation time below. '
        "This report does not run project builds or tests. Unavailable and unreleased "
        "states retain the version-check command’s policy and diagnostics.</p>"
        '<nav aria-label="Artifact sections"><a href="#health">Checks</a>'
        '<a href="#provenance">Provenance</a></nav>'
        '<section id="health"><h2>Version checks</h2>' + "".join(cards) + "</section>"
    )


def _lock_health_markup(document: dict[str, Any]) -> str:
    gate = document["gate"]
    cards = []
    for row in document["components"]:
        report = row["report"]
        detail = row["error"] if report is None else report
        artifacts = ""
        if report is not None:
            for key, label in (("lock", "Lock"), ("catalog_audit", "Resolution audit")):
                if key in report:
                    artifacts += (
                        f"<p>{label}: <strong>"
                        + escape(str(report[key]["state"]))
                        + "</strong></p>"
                    )
        cards.append(
            "<details><summary>"
            + escape(row["component"])
            + " · "
            + escape(row["state"])
            + '</summary><div class="detail-body">'
            + artifacts
            + "<pre><code>"
            + escape(json.dumps(detail, indent=2, ensure_ascii=False))
            + "</code></pre></div></details>"
        )
    repository = document["repository"]
    return (
        '<div class="stats"><div class="stat"><strong>'
        + escape(gate["state"])
        + "</strong>Lock verification</div></div><p>"
        + escape(gate["detail"])
        + "</p>"
        '<p class="muted">Current committed-lock checks at the observation time below. '
        "Components without committed locks are outside this verifier gate. "
        "A repository "
        "lock failure stops Component checks. "
        "This view does not build or execute Components.</p>"
        '<nav aria-label="Artifact sections"><a href="#health">Component checks</a>'
        '<a href="#repository">Repository check</a>'
        '<a href="#provenance">Provenance</a></nav>'
        '<section id="health"><h2>Component locks and resolution audits</h2>'
        + ("".join(cards) if cards else "<p>No Component checks were performed.</p>")
        + '</section><section id="repository"><h2>Repository lock</h2>'
        + (
            "<pre><code>"
            + escape(json.dumps(repository, indent=2, ensure_ascii=False))
            + "</code></pre>"
            if repository is not None
            else "<p>No repository check result.</p>"
        )
        + "</section>"
    )


def _verification_health_markup(document: dict[str, Any]) -> str:
    cards = [
        "<details><summary>"
        + escape(row["gate"])
        + ": "
        + escape(row["state"])
        + '</summary><div class="detail-body"><p>'
        + escape(row["detail"])
        + "</p></div></details>"
        for row in document["gates"]
    ]
    return (
        '<div class="stats">'
        + "".join(
            '<div class="stat"><strong>'
            + str(document["counts"][state])
            + "</strong>"
            + state.capitalize()
            + "</div>"
            for state in ("pass", "fail", "skipped")
        )
        + "</div><p><strong>Selected gates: "
        + ("Pass" if document["ok"] else "Fail")
        + "</strong></p>"
        '<p class="muted">Observed authority, locks, source intelligence and test '
        "receipt checks. The HTML-artifact gate is excluded to avoid depending on "
        "this dashboard’s own freshness. Run <code>litai verify</code> for all "
        "declared gates, including HTML freshness. These observations do not "
        "build or run project tests; skipped checks are not passes.</p>"
        '<nav aria-label="Artifact sections"><a href="#health">Checks</a>'
        '<a href="#provenance">Provenance</a></nav>'
        '<section id="health"><h2>Verification checks</h2>'
        + "".join(cards)
        + "</section>"
    )


def _performance_history_markup(document: dict[str, Any]) -> str:
    spans = document["spans"]
    run_ids = sorted({row["run_id"] for row in spans})
    failures = sum(not row["ok"] for row in spans)
    maximum = max((row["duration_ms"] for row in spans), default=0)
    cards = []
    for row in sorted(spans, key=lambda item: (item["started_at"], item["stage"])):
        width = 0 if maximum == 0 else max(1, round(row["duration_ms"] * 20 / maximum))
        bar = "█" * width
        cards.append(
            "<details><summary>"
            + escape(row["stage"])
            + " · "
            + str(row["duration_ms"])
            + " ms · "
            + ("pass" if row["ok"] else "fail")
            + '</summary><div class="detail-body"><div aria-label="Relative duration">'
            + bar
            + "</div><dl>"
            + _definition("Run", row["run_id"])
            + _definition("Target", f"{row['target_kind']}:{row['target_id']}")
            + _definition("Started", row["started_at"])
            + _definition("Ended", row["ended_at"])
            + _definition("Error", row["error_code"] or "none")
            + "</dl></div></details>"
        )
    return (
        '<div class="stats"><div class="stat"><strong>'
        + str(len(run_ids))
        + "</strong>Runs</div>"
        + '<div class="stat"><strong>'
        + str(len(spans))
        + "</strong>Timed stages</div>"
        + '<div class="stat"><strong>'
        + str(failures)
        + "</strong>Failures</div></div>"
        '<p class="muted">Historical diagnostic spans from the selected project’s '
        "disposable build root. Bars are scaled to the longest retained span. "
        + (
            "The oldest records were omitted at the bounded read limit. "
            if document["truncated"]
            else ""
        )
        + "Empty history makes no claim that builds or tests ran.</p>"
        '<nav aria-label="Artifact sections"><a href="#history">History</a>'
        '<a href="#provenance">Provenance</a></nav>'
        '<section id="history"><h2>Build, test and command history</h2>'
        + ("".join(cards) if cards else "<p>No performance spans are available.</p>")
        + "</section>"
    )


def _workflow_routing_markup(document: dict[str, Any]) -> str:
    workflow_cards = []
    for entry in document["workflows"]:
        workflow = entry["workflow"]
        stages = []
        for stage in workflow["stages"]:
            dependencies = ", ".join(stage["dependencies"]) or "none"
            stages.append(
                "<li><strong>"
                + escape(stage["stage_id"])
                + "</strong> · "
                + escape(stage["kind"])
                + " · depends on "
                + escape(dependencies)
                + "</li>"
            )
        workflow_cards.append(
            "<details><summary>"
            + escape(workflow["workflow_id"])
            + " · "
            + escape(entry["path"])
            + '</summary><div class="detail-body"><ul>'
            + "".join(stages)
            + "</ul><dl>"
            + _definition("Version", workflow["version"])
            + _definition("Content identity", entry["content_identity"])
            + "</dl></div></details>"
        )
    route_cards = []
    for entry in document["routes"]:
        route = entry["routing"]
        route_cards.append(
            "<details><summary>"
            + escape(str(route.get("routing_id", entry["path"])))
            + " · "
            + escape(entry["path"])
            + '</summary><div class="detail-body"><pre><code>'
            + escape(json.dumps(route, indent=2, ensure_ascii=False))
            + "</code></pre><dl>"
            + _definition("Content identity", entry["content_identity"])
            + "</dl></div></details>"
        )
    return (
        '<div class="stats"><div class="stat"><strong>'
        + str(len(workflow_cards))
        + "</strong>Workflows</div>"
        + '<div class="stat"><strong>'
        + str(len(route_cards))
        + "</strong>Routing policies</div></div>"
        '<p class="muted">Exact project-declared workflow and routing catalog '
        "entries. Stage dependencies expose execution nesting; routing records "
        "retain their canonical policy fields.</p>"
        '<nav aria-label="Artifact sections"><a href="#workflows">Workflows</a>'
        '<a href="#routing">Routing</a><a href="#provenance">Provenance</a></nav>'
        '<section id="workflows"><h2>Workflow definitions</h2>'
        + (
            "".join(workflow_cards)
            if workflow_cards
            else "<p>No workflows are declared.</p>"
        )
        + '</section><section id="routing"><h2>Routing policies</h2>'
        + (
            "".join(route_cards)
            if route_cards
            else "<p>No routing policies are declared.</p>"
        )
        + "</section>"
    )


def _markup(
    source: HtmlSurfaceSource,
    provenance: HtmlProvenance,
    excerpts: tuple[HtmlSourceExcerpt, ...] | None = None,
) -> bytes:
    document = source.document()
    health = source.surface.surface_id == "version-check"
    locks = source.surface.surface_id == "lock-health"
    verification = source.surface.surface_id == "verification-health"
    performance = source.surface.surface_id == "performance-history"
    workflow_routing = source.surface.surface_id == "workflow-routing"
    title = (
        "Workflow and routing"
        if workflow_routing
        else "Performance history"
        if performance
        else "Verification health"
        if verification
        else "Lock health"
        if locks
        else "Version check"
        if health
        else "Authority graph"
    )
    project_id = source.view.scope_identifier
    assets = []
    for asset in provenance.external_assets:
        common = (
            f'integrity="{escape(asset.integrity)}" crossorigin="anonymous" '
            f'data-litai-asset="{escape(asset.asset_id)}"'
        )
        if asset.asset_kind == "script":
            assets.append(f'<script src="{escape(asset.url)}" {common} defer></script>')
        else:
            assets.append(
                f'<link rel="stylesheet" href="{escape(asset.url)}" {common}>'
            )
    details = [
        ("Source identity", source.binding.source_identity.uri),
        ("Render inputs", provenance.render_inputs_identity.uri),
        ("Provenance identity", provenance.provenance_identity.uri),
        ("Renderer", provenance.renderer.renderer_id),
        ("Renderer version", provenance.renderer.renderer_version),
        (
            "Framework distribution",
            provenance.renderer.framework_distribution_identity.uri,
        ),
        ("Template identity", provenance.renderer.template_identity.uri),
        ("Generated at (UTC)", provenance.generated_at),
    ]
    style = _STYLE + (html_dag_view.DAG_STYLE if excerpts is not None else "")
    text = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title} · {escape(project_id)}</title>"
        f"<style>{style}</style>"
        + "".join(assets)
        + '</head><body><main><header><p class="eyebrow">LITERATE AI / OBSERVATORY</p>'
        f"<h1>{title}</h1>"
        f"<p>Project <strong>{escape(project_id)}</strong></p>"
        '<p class="muted">Derived from canonical JSON. '
        "No server or companion files required.</p></header>"
        + (
            _workflow_routing_markup(document)
            if workflow_routing
            else _performance_history_markup(document)
            if performance
            else _verification_health_markup(document)
            if verification
            else _lock_health_markup(document)
            if locks
            else _version_health_markup(document)
            if health
            else (
                '<div class="stats">'
                f'<div class="stat"><strong>{len(document["nodes"])}</strong>'
                "Authority nodes</div>"
                f'<div class="stat"><strong>{len(document["edges"])}</strong>'
                "Relationships</div>"
                '<div class="stat"><strong>Acyclic</strong>Validated graph</div></div>'
                '<nav aria-label="Artifact sections"><a href="#catalog">Catalog</a>'
                '<a href="#relationships">Relationships</a>'
                '<a href="#provenance">Provenance</a></nav>'
                + (html_dag_view.DAG_MARKUP if excerpts is not None else "")
                + _graph_markup(document, excerpts)
            )
        )
        + '<section id="provenance"><h2>Provenance</h2><details>'
        "<summary>Inspect exact input and renderer identities</summary>"
        '<div class="detail-body"><dl>'
        + "".join(_definition(key, value) for key, value in details)
        + "</dl></div></details></section><footer>Regenerable view, "
        "not source authority. Canonical JSON and exact source bindings "
        "remain authoritative."
        '</footer></main><script type="application/json" id="litai-source">'
        + _json_script(document)
        + '</script><script type="application/json" id="litai-provenance">'
        + _json_script(provenance.to_dict())
        + "</script>"
        + (
            '<script type="application/json" id="litai-excerpts">'
            + _json_script([item.to_dict() for item in excerpts])
            + '</script><script type="text/javascript" id="litai-dag">'
            + html_dag_view.DAG_SCRIPT
            + "</script>"
            if excerpts is not None
            else ""
        )
        + "</body></html>\n"
    )
    return text.encode("utf-8")


class _MarkupInspection(HTMLParser):
    """Attest this emitter's closed vocabulary, not sanitize arbitrary HTML/CSS."""

    _TAGS = frozenset(
        "html head meta title style script link body main header footer p h1 h2 "
        "strong div nav a section details summary span dl dt dd code ul li "
        "pre aside label select option button".split()
    )
    _ATTRS = {
        "html": {"lang"},
        "meta": {"charset", "name", "content"},
        "nav": {"aria-label"},
        "a": {"href"},
        "details": {"open"},
        "div": {"aria-label", "role", "tabindex"},
        "p": {"role"},
        "aside": {"aria-label"},
        "label": {"for"},
        "select": {"disabled"},
        "option": {"value"},
        "button": {"type", "disabled"},
        "script": {
            "type",
            "src",
            "integrity",
            "crossorigin",
            "data-litai-asset",
            "defer",
        },
        "link": {"rel", "href", "integrity", "crossorigin", "data-litai-asset"},
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.ids: set[str] = set()
        self.anchors: set[str] = set()
        self.assets: list[HtmlExternalAsset] = []
        self.inline: dict[str, str] = {}
        self.styles: list[str] = []
        self.active: str | None = None
        self.doctypes = 0

    def handle_decl(self, decl: str) -> None:
        if decl.lower() != "doctype html":
            raise ValueError("unsupported HTML declaration")
        self.doctypes += 1

    def handle_comment(self, data: str) -> None:
        raise ValueError("unexpected HTML comment")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise ValueError("unexpected self-closing element")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag not in self._TAGS or len(attributes) != len(attrs):
            raise ValueError("unsupported element or duplicate attributes")
        if set(attributes) - {"id", "class", "hidden"} - self._ATTRS.get(tag, set()):
            raise ValueError("unsupported attribute or companion resource")
        if "id" in attributes:
            identifier = attributes["id"]
            if not identifier or identifier in self.ids:
                raise ValueError("ambiguous HTML identifier")
            self.ids.add(identifier)
        if tag == "meta" and not (
            attributes == {"charset": "utf-8"}
            or attributes
            == {"name": "viewport", "content": "width=device-width, initial-scale=1"}
        ):
            raise ValueError("unsupported metadata or redirect")
        if tag == "a":
            href = attributes.get("href", "")
            if not href or not href.startswith("#"):
                raise ValueError("only artifact-local navigation is permitted")
            self.anchors.add(href[1:])
        if tag == "link" or (tag == "script" and "src" in attributes):
            expected = {"integrity", "crossorigin", "data-litai-asset"}
            expected |= {"rel", "href"} if tag == "link" else {"src", "defer"}
            if set(attributes) != expected:
                raise ValueError(
                    "external reference differs from the emitted declaration"
                )
            if tag == "link" and attributes.get("rel") != "stylesheet":
                raise ValueError("only pinned external stylesheets are permitted")
            asset = HtmlExternalAsset(
                attributes.get("data-litai-asset"),
                attributes.get("href" if tag == "link" else "src"),
                attributes.get("integrity"),
                attributes.get("crossorigin"),
                "stylesheet" if tag == "link" else "script",
            )
            self.assets.append(asset)
            if tag == "script":
                self.active = "external"
        elif tag == "script":
            identifier = attributes.get("id")
            if set(attributes) != {"id", "type"}:
                raise ValueError("unexpected inline script attributes")
            kinds = {
                "litai-source": "application/json",
                "litai-provenance": "application/json",
                "litai-excerpts": "application/json",
                "litai-dag": "text/javascript",
            }
            if identifier not in kinds or attributes.get("type") != kinds[identifier]:
                raise ValueError("undeclared inline executable script")
            self.active = identifier
            self.inline[identifier] = ""
        elif tag == "style":
            self.active = "style"
            self.styles.append("")
        if tag not in ("meta", "link"):
            self.stack.append(tag)

    def handle_data(self, data: str) -> None:
        if self.active == "style":
            self.styles[-1] += data
        elif self.active == "external":
            if data.strip():
                raise ValueError("external script contains undeclared inline content")
        elif self.active is not None:
            self.inline[self.active] += data

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("unbalanced emitted HTML")
        if tag in ("style", "script"):
            self.active = None


def _inspect_markup(
    content: bytes,
    provenance: HtmlProvenance,
    source: HtmlSurfaceSource,
    excerpts: tuple[HtmlSourceExcerpt, ...] | None = None,
) -> HtmlEmbedding:
    parser = _MarkupInspection()
    parser.feed(content.decode("utf-8"))
    parser.close()
    if parser.doctypes != 1 or parser.stack or parser.anchors - parser.ids:
        raise ValueError("incomplete HTML document or broken local navigation")
    style = _STYLE + (html_dag_view.DAG_STYLE if excerpts is not None else "")
    if parser.styles != [style] or tuple(parser.assets) != provenance.external_assets:
        raise ValueError("emitted assets do not match the declared template")
    expected = {
        "litai-source": _json_script(source.document()),
        "litai-provenance": _json_script(provenance.to_dict()),
    }
    if excerpts is not None:
        expected["litai-excerpts"] = _json_script([item.to_dict() for item in excerpts])
        expected["litai-dag"] = html_dag_view.DAG_SCRIPT
    if parser.inline != expected:
        raise ValueError("embedded source/provenance does not match the bound inputs")
    # Re-open the actual block through the typed identity verifier.
    HtmlProvenance.from_dict(json.loads(parser.inline["litai-provenance"]))
    return HtmlEmbedding(
        True, 0, len(parser.inline), len(parser.styles), len(parser.assets)
    )


@dataclass(frozen=True, slots=True)
class HtmlRenderInputs:
    """One source and presentation observation shared by rendering and verification."""

    source: HtmlSurfaceSource
    renderer: HtmlRendererBinding
    excerpts: tuple[HtmlSourceExcerpt, ...] | None
    external_assets: tuple[HtmlExternalAsset, ...]


def _load_html_inputs(
    project: Path,
    request: HtmlRenderRequest,
    *,
    framework_distribution_identity: ContentIdentity,
    schema_catalog_release: str,
    external_assets: tuple[HtmlExternalAsset, ...],
    include_dag: bool,
) -> HtmlRenderInputs | HtmlRenderRefusal:
    with debug_stage("html.source", surface=request.surface_id):
        source = load_html_surface(project, request)
    if isinstance(source, HtmlRenderRefusal):
        return source
    if external_assets and request.external_asset_policy == "inline-only":
        return HtmlRenderRefusal(
            "render.external_asset_forbidden",
            "The selected assets require pinned-CDN policy.",
        )
    try:
        renderer = html_renderer_binding(
            framework_distribution_identity,
            schema_catalog_release,
            external_assets,
            include_dag=include_dag,
            surface_id=source.surface.surface_id,
        )
    except (TypeError, ValueError):
        return HtmlRenderRefusal(
            "render.external_asset_unpinned",
            "The renderer or external assets cannot be pinned.",
        )
    except OSError:
        return HtmlRenderRefusal(
            "render.surface_unavailable", "The renderer template is unavailable."
        )
    try:
        excerpts = None
        if include_dag:
            loaded = discover_project(project)
            if loaded is None:
                raise ValueError("The source project is no longer available")
            excerpts = html_source_excerpts.load_source_excerpts(loaded.root, source)
            if isinstance(excerpts, HtmlRenderRefusal):
                return excerpts
        return HtmlRenderInputs(source, renderer, excerpts, external_assets)
    except (OSError, UnicodeError, TypeError, ValueError, ProjectError):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "The selected source could not produce complete pinned inputs.",
        )


def load_html_inputs(
    project: Path,
    request: HtmlRenderRequest,
    *,
    framework_distribution_identity: ContentIdentity,
    schema_catalog_release: str,
) -> HtmlRenderInputs | HtmlRenderRefusal:
    """Select the supported public view identically for emission and currency."""
    return _load_html_inputs(
        project,
        request,
        framework_distribution_identity=framework_distribution_identity,
        schema_catalog_release=schema_catalog_release,
        external_assets=(html_dag_view.CYTOSCAPE,)
        if request.surface_id == "authority-graph"
        else (),
        include_dag=request.surface_id == "authority-graph",
    )


def _emit_inputs(
    inputs: HtmlRenderInputs,
    request: HtmlRenderRequest,
    generated_at: str,
) -> HtmlEmission | HtmlRenderRefusal:
    source, renderer, excerpts = inputs.source, inputs.renderer, inputs.excerpts
    try:
        provenance = HtmlProvenance.create(
            view=request.view,
            source_bindings=(source.binding,),
            renderer=renderer,
            external_assets=inputs.external_assets,
            generated_at=generated_at,
        )
        content = (
            _markup(source, provenance, excerpts)
            if excerpts is not None
            else _markup(source, provenance)
        )
        embedding = _inspect_markup(content, provenance, source, excerpts)
        artifact = HtmlArtifact.from_bytes(
            artifact_path=request.output_path,
            content=content,
            provenance=provenance,
            embedding=embedding,
        )
        return HtmlEmission(content, artifact)
    except (OSError, UnicodeError, TypeError, ValueError, ProjectError):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "The selected source could not produce a complete pinned artifact.",
        )


def _emit_html(
    project: Path,
    request: HtmlRenderRequest,
    *,
    framework_distribution_identity: ContentIdentity,
    schema_catalog_release: str,
    generated_at: str,
    external_assets: tuple[HtmlExternalAsset, ...] = (),
    include_dag: bool = False,
) -> HtmlEmission | HtmlRenderRefusal:
    """Emit and inspect completed bytes; never write the requested output path."""
    inputs = _load_html_inputs(
        project,
        request,
        framework_distribution_identity=framework_distribution_identity,
        schema_catalog_release=schema_catalog_release,
        external_assets=external_assets,
        include_dag=include_dag,
    )
    if isinstance(inputs, HtmlRenderRefusal):
        return inputs
    return _emit_inputs(inputs, request, generated_at)


def emit_html(
    project: Path,
    request: HtmlRenderRequest,
    *,
    framework_distribution_identity: ContentIdentity,
    schema_catalog_release: str,
    generated_at: str,
) -> HtmlEmission | HtmlRenderRefusal:
    """Emit a registered view with its selected assets and native readable content."""
    inputs = load_html_inputs(
        project,
        request,
        framework_distribution_identity=framework_distribution_identity,
        schema_catalog_release=schema_catalog_release,
    )
    if isinstance(inputs, HtmlRenderRefusal):
        return inputs
    return _emit_inputs(inputs, request, generated_at)

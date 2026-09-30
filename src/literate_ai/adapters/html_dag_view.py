"""Fixed inline DAG presentation; no planner, source fetches or product asset tree."""

from __future__ import annotations

from literate_ai.contracts.html_observability import HtmlExternalAsset

# MIT, Cytoscape Consortium. CDN bytes independently matched upstream commit
# 716a1cb6c6015d57b674abe626deaeb5e817ee30; see the Phase 1 implementation plan.
CYTOSCAPE = HtmlExternalAsset(
    "cytoscape-3.34.3",
    "https://cdn.jsdelivr.net/npm/cytoscape@3.34.3/dist/cytoscape.min.js",
    "sha384-qPKQxl9uMXOw7vSTUDAnpUilhLuulovw6P5Z4db4bqxW5VhumS7przEmHX0iM0Oc",
    "anonymous",
    "script",
)

DAG_STYLE = """
[hidden] { display: none !important; }
.graph-controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; }
.graph-controls label { flex: 1 1 220px; min-width: 0; font-size: .85rem;
  font-weight: 650; }
select, button { font: inherit; color: inherit; background: white;
  border: 1px solid #aabfb8; border-radius: 5px; padding: 9px 12px; min-height: 44px; }
select { display: block; width: 100%; min-width: 0; margin-top: 6px; }
button { cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
button:focus-visible, select:focus-visible, #graph-canvas:focus-visible {
  outline: 3px solid #9c4b08; outline-offset: 3px; }
.graph-navigation { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
.graph-workspace { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  border: 1px solid #cbd8d4; border-radius: 8px; overflow: hidden; background: white; }
#graph-canvas { height: 470px; min-width: 0; position: relative; background: #fafcfb; }
#node-inspector { padding: 20px; min-width: 0; border-left: 1px solid #cbd8d4; }
.graph-workspace:has(#graph-canvas[hidden]) { grid-template-columns: minmax(0, 1fr); }
.graph-workspace:has(#graph-canvas[hidden]) #node-inspector { border-left: 0; }
#node-title { overflow-wrap: anywhere; }
#node-source-status, #node-meta { overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 12px 0;
  padding: 12px; border: 1px solid #dce5e1; background: #f6f8f7; max-height: 360px;
  overflow-y: auto; font-size: .8rem; }
.source-preview { grid-column: 1 / -1; }
#graph-status, #graph-summary { font-size: .9rem; }
@media (max-width: 700px) {
  .graph-workspace { grid-template-columns: minmax(0, 1fr); }
  #graph-canvas { height: 350px; }
  #node-inspector { border-left: 0; border-top: 1px solid #cbd8d4; }
}
""".strip()

DAG_MARKUP = """
<section id="graph"><h2>Explore the authority graph</h2>
<p class="muted">Filter a catalog type to inspect its structure. Choose a node or
tap it in the graph to read its source. Arrows point from source to target;
relationship names remain available in the catalog below.</p>
<p id="graph-status" role="status">Interactive layout requires JavaScript and the
pinned library. The native catalog, relationships and source previews below remain
readable without scripts.</p>
<div class="graph-controls">
<label for="graph-filter">Catalog type<select id="graph-filter" disabled>
<option value="">All types</option></select></label>
<label for="graph-node">Inspect a node<select id="graph-node" disabled>
<option value="">Choose a node</option></select></label></div>
<div class="graph-navigation" aria-label="Graph navigation">
<button type="button" id="graph-zoom-in" disabled>Zoom in</button>
<button type="button" id="graph-zoom-out" disabled>Zoom out</button>
<button type="button" id="graph-fit" disabled>Fit graph</button>
<button type="button" id="graph-focus" disabled>Focus selected</button>
<button type="button" id="graph-left" disabled>Pan left</button>
<button type="button" id="graph-right" disabled>Pan right</button>
<button type="button" id="graph-up" disabled>Pan up</button>
<button type="button" id="graph-down" disabled>Pan down</button></div>
<p id="graph-summary" role="status">The full canonical graph is listed below.</p>
<div class="graph-workspace">
<div id="graph-canvas" role="img" tabindex="0" hidden
aria-label="Authority graph. Node selector provides accessible details.
Arrow keys pan; plus and minus zoom; Home fits."></div>
<aside id="node-inspector" aria-label="Selected authority node">
<h2 id="node-title">Choose an authority node</h2>
<p id="node-meta" class="muted">Inspect its kind, relationships and
exact source binding.</p>
<p><a id="node-catalog-link" href="#catalog">Read the native catalog</a></p>
<p id="node-source-status" class="muted">Source previews are verified
before rendering.</p>
<pre id="node-source" hidden><code id="node-source-text"></code></pre>
</aside></div></section>
""".strip()

# All untrusted text enters through textContent or native option text. Cytoscape
# receives generated node/edge IDs, not user-shaped selectors. No fetch/storage API.
DAG_SCRIPT = r"""
(() => {
  "use strict";
  document.addEventListener("DOMContentLoaded", () => {
    const byId = (id) => document.getElementById(id);
    const graph = JSON.parse(byId("litai-source").textContent);
    const excerpts = JSON.parse(byId("litai-excerpts").textContent);
    const filter = byId("graph-filter");
    const picker = byId("graph-node");
    const status = byId("graph-status");
    const canvas = byId("graph-canvas");
    const nodes = graph.nodes.map((node, index) => (
      {...node, uiId: `n${index}`, index}));
    const lookup = new Map(nodes.map((node) => [node.id, node]));
    const previews = new Map(excerpts.map((item) => [item.node_id, item]));
    let cy = null;
    let visible = nodes;

    function option(value, text) {
      const item = document.createElement("option");
      item.value = value;
      item.textContent = text;
      return item;
    }
    function caption(text, limit) {
      const points = Array.from(text);
      return points.slice(0, limit).join("") + (points.length > limit ? "…" : "");
    }
    function choose(id) {
      const node = nodes.find((item) => item.uiId === id);
      picker.value = node ? node.uiId : "";
      if (cy) {
        cy.elements().removeClass("focused");
        cy.nodes().unselect();
        if (node) {
          const chosen = cy.getElementById(node.uiId);
          chosen.select();
          chosen.connectedEdges().addClass("focused");
        }
      }
      byId("graph-focus").disabled = !cy || !node;
      const preview = node && previews.get(node.id);
      byId("node-title").textContent = node ? node.label : "Choose an authority node";
      const degree = node ? graph.edges.filter(
        (edge) => edge.source === node.id || edge.target === node.id).length : 0;
      byId("node-meta").textContent = node
        ? `${node.kind} · ${node.project_id} · ${degree} canonical relationships`
        : "Inspect its kind, relationships and exact source binding.";
      byId("node-catalog-link").setAttribute(
        "href", node ? `#node-${node.index}` : "#catalog");
      byId("node-source").hidden = !preview || preview.text === null;
      byId("node-source-text").textContent = preview && preview.text !== null
        ? preview.text : "";
      byId("node-source-status").textContent = !preview
        ? "Source previews are verified before rendering."
        : preview.text === null ? preview.unavailable_reason
        : `${preview.path} · sha256:${preview.source_identity.digest} · `
          + (preview.truncated ? "Bounded preview; complete file hash verified."
            : "Complete source; hash verified.");
    }
    function fit() {
      if (cy && cy.nodes().length) cy.fit(cy.elements(), 35);
    }
    function redraw() {
      visible = nodes.filter((node) => !filter.value || node.kind === filter.value);
      const ids = new Set(visible.map((node) => node.id));
      const edges = graph.edges.filter(
        (edge) => ids.has(edge.source) && ids.has(edge.target));
      picker.replaceChildren(option("", "Choose a node"));
      for (const node of visible) {
        picker.append(option(node.uiId, `${node.kind} · ${node.label}`));
      }
      for (const node of nodes) byId(`node-${node.index}`).hidden = !ids.has(node.id);
      for (const group of document.querySelectorAll(".catalog-group")) {
        group.hidden = !Array.from(group.querySelectorAll(".node")).some(
          (node) => !node.hidden);
      }
      byId("graph-summary").textContent = `${visible.length} of ${nodes.length} nodes`
        + " · "
        + `${edges.length} of ${graph.edges.length} relationships in this view`;
      if (cy) {
        cy.elements().remove();
        cy.add([
          ...visible.map((node) => ({data: {id: node.uiId,
            label: `${caption(node.kind, 24)}\n${caption(node.label, 48)}`}})),
          ...edges.map((edge, index) => ({data: {id: `e${index}`,
            source: lookup.get(edge.source).uiId, target: lookup.get(edge.target).uiId,
            kind: edge.kind.slice(0, 64)}}))
        ]);
        if (visible.length) {
          cy.layout({name: "grid", spacingFactor: 1.2, avoidOverlap: true,
            padding: 35, animate: false}).run();
          if (edges.length) {
            cy.layout({name: "cose", randomize: false, animate: false,
              numIter: 500, nodeDimensionsIncludeLabels: true,
              nodeRepulsion: () => 8000, idealEdgeLength: () => 100,
              componentSpacing: 80, padding: 35}).run();
          }
          fit();
        }
      }
      choose("");
    }
    for (const kind of [...new Set(nodes.map((node) => node.kind))].sort()) {
      filter.append(option(kind, kind));
    }
    filter.disabled = false;
    picker.disabled = false;
    filter.addEventListener("change", redraw);
    picker.addEventListener("change", () => choose(picker.value));
    byId("node-catalog-link").addEventListener("click", () => {
      const node = nodes.find((item) => item.uiId === picker.value);
      if (!node) return;
      const card = byId(`node-${node.index}`);
      card.open = true;
      card.closest(".catalog-group").open = true;
    });
    // Native relationship links must open their targets even after local filtering.
    for (const link of document.querySelectorAll('#relationships a[href^="#node-"]')) {
      link.addEventListener("click", () => {
        filter.value = "";
        redraw();
        const card = byId(link.getAttribute("href").slice(1));
        card.open = true;
        card.closest(".catalog-group").open = true;
      });
    }
    redraw();
    if (!nodes.length) {
      filter.disabled = true;
      picker.disabled = true;
      status.textContent = "The graph is empty. No authority nodes are present.";
      return;
    }
    if (typeof window.cytoscape !== "function") {
      status.textContent = "Interactive layout unavailable: "
        + "the pinned library did not load. Local filters, node selection "
        + "and the native source catalog remain available.";
      canvas.hidden = true;
      return;
    }
    try {
      canvas.hidden = false;
      cy = window.cytoscape({container: canvas, elements: [],
        minZoom: 0.001, maxZoom: 4,
        wheelSensitivity: 0.2, boxSelectionEnabled: false, autounselectify: false,
        style: [
          {selector: "node", style: {label: "data(label)", shape: "round-rectangle",
            width: 145, height: 90, "background-color": "#edf4f1",
            "border-color": "#17756e",
            "border-width": 1, color: "#182b35", "font-size": 11, "text-wrap": "wrap",
            "text-max-width": 132, "text-overflow-wrap": "anywhere",
            "text-valign": "center", "text-halign": "center"}},
          {selector: "node:selected", style: {"border-width": 4,
            "border-color": "#9c4b08"}},
          {selector: "edge", style: {width: 1.3, "line-color": "#97ada5",
            "target-arrow-color": "#617d72", "target-arrow-shape": "triangle",
            "curve-style": "bezier"}},
          {selector: "edge.focused", style: {label: "data(kind)", "font-size": 10,
            "text-background-opacity": 1, "text-background-color": "#ffffff",
            width: 2.5}}
        ]});
      cy.on("tap", "node", (event) => choose(event.target.id()));
      redraw();
      status.textContent = nodes.length
        ? "Interactive graph ready. All controls transform local data only."
        : "The graph is empty. No authority nodes are present.";
      function zoom(factor) {
        const level = Math.max(
          cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor));
        cy.zoom({level,
          renderedPosition: {x: canvas.clientWidth / 2, y: canvas.clientHeight / 2}});
      }
      const controls = {
        "graph-zoom-in": () => zoom(1.35), "graph-zoom-out": () => zoom(1 / 1.35),
        "graph-fit": fit, "graph-left": () => cy.panBy({x: -70, y: 0}),
        "graph-right": () => cy.panBy({x: 70, y: 0}),
        "graph-up": () => cy.panBy({x: 0, y: -70}),
        "graph-down": () => cy.panBy({x: 0, y: 70})
      };
      for (const [id, callback] of Object.entries(controls)) {
        byId(id).disabled = !nodes.length;
        byId(id).addEventListener("click", callback);
      }
      byId("graph-focus").addEventListener("click", () => {
        const node = cy.getElementById(picker.value);
        if (!node.length) return;
        cy.fit(node.closedNeighborhood(), 35);
        if (cy.zoom() > 1.4) { cy.zoom(1.4); cy.center(node); }
      });
      const zoomState = () => {
        byId("graph-zoom-in").disabled = !nodes.length || cy.zoom() >= cy.maxZoom();
        byId("graph-zoom-out").disabled = !nodes.length || cy.zoom() <= cy.minZoom();
      };
      cy.on("zoom", zoomState);
      zoomState();
      canvas.addEventListener("keydown", (event) => {
        const key = {ArrowLeft: "graph-left", ArrowRight: "graph-right",
          ArrowUp: "graph-up",
          ArrowDown: "graph-down", "+": "graph-zoom-in", "=": "graph-zoom-in",
          "-": "graph-zoom-out", Home: "graph-fit"}[event.key];
        if (key && nodes.length) { event.preventDefault(); controls[key](); }
      });
      new ResizeObserver(() => { cy.resize(); fit(); }).observe(canvas);
    } catch (_error) {
      if (cy) cy.destroy();
      cy = null;
      canvas.hidden = true;
      status.textContent = "Interactive layout unavailable: "
        + "the graph library could not initialize. Local filters, node selection "
        + "and the native source catalog remain available.";
    }
  }, {once: true});
})();
""".strip()

#!/usr/bin/env python3
"""Bundle authored lab sources, with exact offline source inspection."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>{title}</title><style>{css}</style></head><body>
<div class="shell"><aside class="lesson"><div class="eyebrow" data-level></div>
<h1 data-title></h1><p data-subtitle></p><p class="learning" data-learning></p>
<h2 class="flow-title">Lesson flow (illustrative)</h2><p class="inventory" data-inventory></p>
<div data-graph></div><div class="authority" data-authority>Select a role to inspect its responsibility.</div>
<details data-spec-details><summary>Readable lab specification — component.md</summary><pre class="spec" data-spec></pre></details>
<p class="footer">Authored JavaScript and CSS bundled into one offline file. This diagram is not a resolved Component DAG; this artifact has no lifecycle acceptance receipt.</p>
</aside><main class="stage"><header class="stage-head"><h2>{stage}</h2><span class="badge">offline artifact</span></header>
<section class="viewport"><canvas aria-label="Lab visual output"></canvas><div class="flash"></div></section>
<div class="controls" data-controls></div><div class="readout" data-readout>ready</div>
<section class="harness-panel" aria-label="Surrounding inputs and harness"><h2>Surrounding inputs &amp; harness</h2>
<p>The lesson flow above describes lab responsibilities. These are the external authoring inputs and the harness boundaries for this actual bundle.</p>
<div class="harness-grid" data-harness></div></section></main></div>
<section class="source-inspector"><h2>Exact bundled source</h2>
<p>Complete authored inputs, including shared support, the HTML builder and project/catalog context. The specification is not the entire implementation. SHA-256 identifies bytes; it does not certify generation or acceptance.</p>
<div data-sources></div></section>
<script>{runtime}</script><script>{main}</script>
<script id="lab-config" type="application/json">{config}</script>
<script>LitLab.mount(JSON.parse(document.getElementById('lab-config').textContent),window.startLab);</script>
</body></html>
"""


def source_paths(lab: Path) -> list[Path]:
    if (lab / "project/literate.project.json").exists():
        project = lab / "project"
        manifest = json.loads((project / "literate.project.json").read_text())
        component = project / manifest["repository_policy"]["default_component"]
        lock = json.loads((component / "component.lock.json").read_text())
        inputs = [
            component / "component.md",
            lab / "lab.json",
            lab / "main.js",
            ROOT / "shared/lab.css",
            ROOT / "shared/lab-runtime.js",
            ROOT / "scripts/build_labs.py",
            project / "literate.project.json",
            component / "component.lock.json",
        ]
        for node in lock["nodes"]:
            revision = node["revision"]
            for ref in revision["authoring_inputs"] + [
                revision["workflow_definition"],
                revision["routing_policy"],
            ]:
                inputs.append(project / ref["uri"])
        # These are exactly the explicitly selected catalog Flavors, not every
        # catalog entry. The lock and rendered selection are verified separately.
        for selector in manifest["default_flavor_selectors"]:
            inputs.append(
                project / "flavors" / selector.rsplit("/", 1)[-1] / "flavor.md"
            )
        return list(dict.fromkeys(inputs))
    return [
        lab / "component.md",
        lab / "lab.json",
        lab / "main.js",
        ROOT / "shared/lab.css",
        ROOT / "shared/lab-runtime.js",
        ROOT / "scripts/build_labs.py",
        ROOT.parent / "literate.project.json",
        ROOT.parent / "flavors/lang-javascript/flavor.md",
    ]


def build_page(lab: Path) -> str:
    config = json.loads((lab / "lab.json").read_text())
    canonical = (lab / "project/literate.project.json").exists()
    if canonical:
        return build_canonical_page(lab, config)
    config["spec"] = (lab / "component.md").read_bytes().decode("utf-8")
    project = json.loads((ROOT.parent / "literate.project.json").read_text())
    lab_path = lab.relative_to(ROOT.parent).as_posix()

    def registered(roots: list[str]) -> bool:
        return any(
            lab_path == root or lab_path.startswith(root.rstrip("/") + "/")
            for root in roots
        )

    config["harness"] = {
        "project": project["project_id"],
        "version": project["version"],
        "documentation_root": registered(project["documentation_roots"]),
        "component_root": registered(project["component_roots"]),
        "selected_flavors": [],
        "language_selection": "Authored JavaScript; chosen manually, not resolved from a Flavor.",
        "available_flavor": "flavors/lang-javascript/flavor.md",
    }
    config["sources"] = [
        {
            "path": path.relative_to(ROOT.parent).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "content": path.read_bytes().decode("utf-8"),
        }
        for path in source_paths(lab)
    ]
    # JSON is data, never executable code; escape script end tags in source text.
    serialized = json.dumps(config, separators=(",", ":")).replace("<", "\\u003c")
    return TEMPLATE.format(
        title=html.escape(config["title"]),
        stage=html.escape(config["stage"]),
        css=(ROOT / "shared/lab.css").read_text(),
        runtime=(ROOT / "shared/lab-runtime.js").read_text(),
        main=(lab / "main.js").read_text(),
        config=serialized,
    )


def build_canonical_page(lab: Path, config: dict) -> str:
    project = lab / "project"
    manifest = json.loads((project / "literate.project.json").read_text())
    component = project / manifest["repository_policy"]["default_component"]
    lock = json.loads((component / "component.lock.json").read_text())
    config["spec"] = (component / "component.md").read_text()
    config["canonical"] = True
    nodes, edges, known = [], [], set()

    def add(identity: str, label: str, detail: str) -> None:
        if identity not in known:
            nodes.append({"id": identity, "label": label, "detail": detail})
            known.add(identity)

    def key(identity: dict) -> str:
        return identity["digest"]

    for node in lock["nodes"]:
        revision = node["revision"]
        # Canonical JSON hashing is not needed: root/edge identity fields in the
        # lock already bind the Component revision.
        identity = key(node["target_flavor_selection"]["component_revision"])
        coordinate = revision["coordinate"]
        add(
            identity,
            coordinate["name"],
            "Component "
            + coordinate["namespace"]
            + "/"
            + coordinate["name"]
            + "; specification "
            + revision["specification_set_identity"]["digest"],
        )
        for slot in node["target_flavor_selection"]["slots"]:
            for selection in slot["selected"]:
                selected = key(selection["flavor_revision"])
                add(
                    selected,
                    "Flavor: " + selection["value"],
                    "Selected Flavor for "
                    + slot["slot"]["axis"]
                    + "; revision "
                    + selected,
                )
                edges.append([selected, identity])
        for ref in revision["authoring_inputs"] + [
            revision["workflow_definition"],
            revision["routing_policy"],
        ]:
            ref_id = key(ref["identity"])
            label = (
                ref["uri"].split("/")[-2]
                if ref["uri"].endswith("SKILL.md")
                else Path(ref["uri"]).stem
            )
            add(ref_id, label, ref["kind"] + ": " + ref["uri"] + "; SHA-256 " + ref_id)
            edges.append([ref_id, identity])
    for edge in lock["edges"]:
        edges.append([key(edge["source_revision"]), key(edge["target_revision"])])
    config["nodes"], config["edges"] = nodes, edges
    selected = sorted(
        {
            selection["value"]
            for node in lock["nodes"]
            for slot in node["target_flavor_selection"]["slots"]
            for selection in slot["selected"]
        }
    )
    config["harness"] = {
        "project": manifest["project_id"],
        "version": manifest["version"],
        "component_count": len(lock["nodes"]),
        "selected_flavors": selected,
        "component_root": True,
        "documentation_root": True,
        "framework_version": "1.3.1",
        "lock_sha256": hashlib.sha256(
            (component / "component.lock.json").read_bytes()
        ).hexdigest(),
    }
    config["sources"] = [
        {
            "path": path.relative_to(ROOT.parent).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "content": path.read_bytes().decode("utf-8"),
        }
        for path in source_paths(lab)
    ]
    serialized = json.dumps(config, separators=(",", ":")).replace("<", "\\u003c")
    template = (
        TEMPLATE.replace("Lesson flow (illustrative)", "Resolved authority graph")
        .replace(
            "Select a role to inspect its responsibility.",
            "Select a resolved input to inspect its authority.",
        )
        .replace(
            "Authored JavaScript and CSS bundled into one offline file. This diagram is not a resolved Component DAG; this artifact has no lifecycle acceptance receipt.",
            "Canonical Literate AI 1.3.1 project. This preview is authored retained input; inspect the separate project receipt for actual lifecycle qualification.",
        )
        .replace(
            "The lesson flow above describes lab responsibilities. These are the external authoring inputs and the harness boundaries for this actual bundle.",
            "The graph comes from the Component lock. Components, selected Flavors and pinned authoring inputs have distinct responsibilities.",
        )
    )
    return template.format(
        title=html.escape(config["title"]),
        stage=html.escape(config["stage"]),
        css=(ROOT / "shared/lab.css").read_text(),
        runtime=(ROOT / "shared/lab-runtime.js").read_text(),
        main=(lab / "main.js").read_text(),
        config=serialized,
    )


def main() -> None:
    labs = sorted(ROOT.glob("[0-9][0-9]-*/lab.json"))
    for manifest in labs:
        (manifest.parent / "index.html").write_text(build_page(manifest.parent))
    print(f"built {len(labs)} standalone labs with exact source inspection")


if __name__ == "__main__":
    main()

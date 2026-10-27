#!/usr/bin/env python3
"""Bundle shared lab support into one offline HTML file per lab."""
from __future__ import annotations
import html, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "shared/lab.css").read_text()
RUNTIME = (ROOT / "shared/lab-runtime.js").read_text()
TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{css}</style></head><body><div class="shell"><aside class="lesson"><div class="eyebrow" data-level></div><h1 data-title></h1><p data-subtitle></p><p class="learning" data-learning></p><div data-graph></div><div class="authority" data-authority>Select a graph node to inspect its authority.</div><details><summary>Readable specification</summary><pre class="spec" data-spec></pre></details><p class="footer">One file. No network. Generated from shared lab support plus this lab's Component.</p></aside><main class="stage"><header class="stage-head"><h2>{stage}</h2><span class="badge">offline artifact</span></header><section class="viewport"><canvas aria-label="Lab visual output"></canvas><div class="flash"></div></section><div class="controls" data-controls></div><div class="readout" data-readout>ready</div></main></div><script>{runtime}</script><script>{main}</script><script>LitLab.mount({config},window.startLab);</script></body></html>"""

def main() -> None:
    count = 0
    for manifest in sorted(ROOT.glob("[0-9][0-9]-*/lab.json")):
        lab = manifest.parent
        config = json.loads(manifest.read_text())
        config["spec"] = (lab / "component.md").read_text()
        output = TEMPLATE.format(title=html.escape(config["title"]), stage=html.escape(config["stage"]), css=CSS,
                                 runtime=RUNTIME, main=(lab / "main.js").read_text(),
                                 config=json.dumps(config, separators=(",", ":")).replace("</", "<\\/"))
        (lab / "index.html").write_text(output)
        count += 1
    print(f"built {count} standalone labs")
if __name__ == "__main__": main()


#!/usr/bin/env python3
"""Bundle authored lab sources, with exact offline source inspection."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
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
'''


def source_paths(lab: Path) -> list[Path]:
    return [
        lab / 'component.md', lab / 'lab.json', lab / 'main.js',
        ROOT / 'shared/lab.css', ROOT / 'shared/lab-runtime.js',
        ROOT / 'scripts/build_labs.py', ROOT.parent / 'literate.project.json',
        ROOT.parent / 'flavors/lang-javascript/flavor.md',
    ]


def build_page(lab: Path) -> str:
    config = json.loads((lab / 'lab.json').read_text())
    config['spec'] = (lab / 'component.md').read_bytes().decode('utf-8')
    project = json.loads((ROOT.parent / 'literate.project.json').read_text())
    lab_path = lab.relative_to(ROOT.parent).as_posix()
    def registered(roots: list[str]) -> bool:
        return any(lab_path == root or lab_path.startswith(root.rstrip('/') + '/') for root in roots)
    config['harness'] = {
        'project': project['project_id'],
        'version': project['version'],
        'documentation_root': registered(project['documentation_roots']),
        'component_root': registered(project['component_roots']),
        'selected_flavors': [],
        'language_selection': 'Authored JavaScript; chosen manually, not resolved from a Flavor.',
        'available_flavor': 'flavors/lang-javascript/flavor.md',
    }
    config['sources'] = [
        {'path': path.relative_to(ROOT.parent).as_posix(),
         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
         'content': path.read_bytes().decode('utf-8')}
        for path in source_paths(lab)
    ]
    # JSON is data, never executable code; escape script end tags in source text.
    serialized = json.dumps(config, separators=(',', ':')).replace('<', '\\u003c')
    return TEMPLATE.format(
        title=html.escape(config['title']), stage=html.escape(config['stage']),
        css=(ROOT / 'shared/lab.css').read_text(),
        runtime=(ROOT / 'shared/lab-runtime.js').read_text(),
        main=(lab / 'main.js').read_text(), config=serialized,
    )


def main() -> None:
    labs = sorted(ROOT.glob('[0-9][0-9]-*/lab.json'))
    for manifest in labs:
        (manifest.parent / 'index.html').write_text(build_page(manifest.parent))
    print(f'built {len(labs)} standalone labs with exact source inspection')


if __name__ == '__main__':
    main()

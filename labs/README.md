# Literate AI labs

These labs teach Literate AI by making increasingly unreasonable little machines.
Each current lab has an authored Markdown specification, a small source module, an
illustrative lesson flow, and a bundled `index.html` that runs offline with no server or
external dependencies.

The authoring support is shared under `shared/`; `scripts/build_labs.py` inlines it
into every lab artifact. That keeps the source DRY while leaving each lab directory
free-standing after generation.

| Lab | New idea | Media |
| --- | --- | --- |
| [00 — Grumpy counter](00-grumpy-counter/README.md) | Specification, component and acceptance | Math, canvas |
| [01 — Orbital teapot](01-orbital-teapot/README.md) | Composition and state | Physics, canvas |
| [02 — Unlicensed birdsong](02-unlicensed-birdsong/README.md) | Capability selection | Music, Web Audio |
| [03 — Impossible furniture](03-impossible-furniture/README.md) | Replaceable realization | 3D projection |
| [04 — Glitch opera](04-glitch-opera/README.md) | Evidence capture | Video, audio |
| [05 — Cathedral of tiny disasters](05-component-cathedral/README.md) | Component DAG composition | Math, physics, 3D, music, video |

Build and verify everything:

```bash
python3 labs/scripts/build_labs.py
python3 labs/scripts/verify_labs.py
```

Then open any lab's `index.html` directly. Select a lesson role to inspect its
responsibility. These roles are not resolved canonical Components. The separate
surrounding-harness panel records manual language choice, shared runtime/style,
HTML assembly, project registration, and absent Flavor/lifecycle selection.
The exact-source inspector includes complete authored bytes and project/catalog
context; hashes identify bytes, not lifecycle admission.

LAB-AUTHORITY-001 tracks the user-requested replacement with independent canonical
Literate AI labs: progressively richer real Component graphs, explicit Flavors,
locks, skills/workflows and genuine lifecycle evidence. The present authored
browser examples are not completion of that curriculum.

The shared authoring inputs are [the visual system](shared/lab.css) and
[the shared lab runtime](shared/lab-runtime.js). The
[builder](scripts/build_labs.py) creates the independent artifacts, and the
[verifier](scripts/verify_labs.py) checks their structure, media coverage, and
offline boundary.


Regression checks: `python3 labs/scripts/test_lab_sources.py`. Real-browser checks:
`node labs/scripts/test_labs_browser.cjs` with an existing Playwright installation
and Chromium (optionally `LAB_PLAYWRIGHT_MODULE` and
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`). Captures stay under ignored `_build/`.

Validation sources: [source-integrity regressions](scripts/test_lab_sources.py) and
[real-browser interaction checks](scripts/test_labs_browser.cjs).

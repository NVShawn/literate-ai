# Literate AI labs

Welcome, Unprompted AI attendees—welcome aboard! These labs accompany Jordan Hubbard’s session. Literate AI connects readable specifications, Components and Flavors with actual builds, tests and receipts. Start with a browser preview, then use the documented project lifecycle for the canonical exercises.

From zero, have a Mac, Linux computer or Windows 11/WSL 2 machine; a current browser and terminal; Python 3.11+, Git/Git LFS, Node.js 20+ and GNU Make; Literate AI 1.3.1; and the OpenRouter key in your session email. Choose OpenCode or Claude Code for the documented emailed-key path. Codex CLI and Cursor Agent are also framework adapters with separate authentication/configuration requirements. Follow the [attendee setup guide](../docs/conference/attendee-setup.md) for exact installation and assistant steps.

These labs teach Literate AI by making increasingly unreasonable little machines.
Each numbered directory contains a self-contained canonical `project`, with an
application Component, selected JavaScript/Make/host Flavors, a lock and pinned
skills/workflow/routing. Its complete UI specification is under that project’s
`components/frontend-application` directory.

The offline `index.html` is the authored initial preview. Its graph is derived
from the actual Component lock and distinguishes Components from selected Flavors
and other authority. The initial source is explicitly retained authoring input;
only an actual Standard rebuild can issue lifecycle acceptance and a receipt.
The [qualification record](../verification/unprompted-ai-labs-1.3.1.json) records
actual 1.3.1 rebuilds, accepted-source builds and HTTP runs for all six projects.

| Lab | New idea | Media |
| --- | --- | --- |
| [00 — Grumpy counter](00-grumpy-counter/README.md) | Specification, component and acceptance | Math, canvas |
| [01 — Orbital teapot](01-orbital-teapot/README.md) | Composition and state | Physics, canvas |
| [02 — Unlicensed birdsong](02-unlicensed-birdsong/README.md) | Capability selection | Music, Web Audio |
| [03 — Impossible furniture](03-impossible-furniture/README.md) | Replaceable realization | 3D projection |
| [04 — Glitch opera](04-glitch-opera/README.md) | Evidence capture | Video, audio |
| [05 — Cathedral of tiny disasters](05-component-cathedral/README.md) | Composed media; Component decomposition exercise | Math, physics, 3D, music, video |

Build and run through Literate AI (starting at the repository root):

```bash
cd labs/00-grumpy-counter/project
litai doctor
litai status
litai build --update-receipt
litai run components/frontend-application/grumpy-counter -- --litai-serve 8877
```

Open `http://127.0.0.1:8877/` in your browser. Keep the terminal running while
using the lab; press Ctrl+C when finished. `litai build` runs the Standard
lifecycle: it resolves the locked inputs, builds, tests and independently accepts
the application before exporting what `litai run` executes. A fresh build can
invoke your configured coding assistant and consume model credits. If a current
accepted source entry is already available, use `litai build --from-accepted-source`
to require its revalidation and prevent a model call. Missing or stale evidence is
an error, rather than permission to bypass the harness.

The verified initial-source exercise uses a fresh build/cache directory. Version
1.3.1 has known limitations when discovering optional committed caches or multiple
historical retained-source provider bindings. Keep the default cache target and
follow the project's reviewed retained-source instructions; the qualification
record links those limitations. Normal assistant-backed regeneration is a
separate exercise and was not used to manufacture the initial lab receipts.

The `--litai-serve` arguments belong to the lab application; `litai run` passes
them to the accepted executable. They are not a separate Python web server.
Each numbered lab has its own `project` directory: change into that directory to
use its default Component. The checked-in host selection is macOS; the project
README describes selecting another host Flavor and rebuilding.

For a quick preview, open a lab’s `index.html` directly. The source inspector
shows complete authored and selected authority bytes with SHA-256. These hashes
identify content; the separate project receipt records what the lifecycle tested.

LAB-AUTHORITY-001 also requires progressively richer multi-Component composition.
The initial conversion has one application Component per project; its internal
physics/audio/drawing roles are not mislabeled as additional Components. That
composition requirement remains open until it is implemented and qualified.

The shared authoring inputs are [the visual system](shared/lab.css) and
[the shared lab runtime](shared/lab-runtime.js). The
[builder](scripts/build_labs.py) creates the independent artifacts, and the
[verifier](scripts/verify_labs.py) checks their structure, media coverage, and
offline boundary.


Maintainer-only preview assembly and regression checks are described in
[the authoring guide](scripts/README.md). Attendees should use `litai build` and
`litai run` above.

Validation sources: [source-integrity regressions](scripts/test_lab_sources.py) and
[real-browser interaction checks](scripts/test_labs_browser.cjs).

# Literate AI labs

These labs teach Literate AI by making increasingly unreasonable little machines.
Each lab has a readable Component specification, a small source module, a visible
Component DAG, and a generated `index.html` that runs offline with no server or
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

Then open any lab's `index.html` directly. The graph on the left is part of the
lesson: select a node to see which visible behavior that authority owns.

The shared authoring inputs are [the visual system](shared/lab.css) and
[the component-DAG runtime](shared/lab-runtime.js). The
[builder](scripts/build_labs.py) creates the independent artifacts, and the
[verifier](scripts/verify_labs.py) checks their structure, media coverage, and
offline boundary.


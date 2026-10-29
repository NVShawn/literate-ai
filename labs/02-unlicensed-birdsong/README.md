# Lab 02: Bureau of Unlicensed Birdsong

A pentatonic permitting office staffed entirely by oscillators.

Open [the offline preview](index.html), or build and run the real Component
through Literate AI:

```sh
cd project
litai doctor
litai build --update-receipt
litai run components/frontend-application/unlicensed-birdsong -- --litai-serve 8877
```

Open `http://127.0.0.1:8877/`; Ctrl+C stops the server. A fresh build may invoke
your configured coding assistant. [Project instructions](project/README.md) cover
exact accepted-source reuse, explicit retained-source admission and host Flavor
selection.

Read [the complete specification](project/components/frontend-application/unlicensed-birdsong/component.md)
and [the actual lock](project/components/frontend-application/unlicensed-birdsong/component.lock.json).
The graph distinguishes the real application Component, selected Flavors and
pinned skills/workflow/routing. Its drawing/audio roles are not separate Components.

Try one behavior change in the specification, relock and inspect the plan before
rebuilding. Compare the resulting page, tests and independent browser evidence.
The initial authored source is retained input; it does not claim a model generation
that did not occur. Progressive multi-Component composition remains open under
LAB-AUTHORITY-001.

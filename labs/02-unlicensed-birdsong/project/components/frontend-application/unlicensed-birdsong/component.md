---
namespace: unprompted
version: 1.0.0
display_name: Bureau of Unlicensed Birdsong
profiles:
  - application
  - frontend
sample: false
inheritable: false
provides:
  - name: literate-ai.frontend-application
    version: 1.0.0
    interface: null
  - name: application.web-frontend
    version: 1.0.0
    interface: null
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/frontend-application/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-specification-planning/SKILL.md
workflow_definition:
  uri: workflows/sample-host.md
routing_policy:
  uri: routing/sample-host.json
flavor_slots:
  - slot_id: language
    axis: implementation.language-ecosystem
    cardinality: exactly-one
    capability_contract: application.web-frontend
  - slot_id: ui
    axis: implementation.ui-framework
    cardinality: zero-or-one
    capability_contract: application.web-frontend
  - slot_id: packages
    axis: packaging
    cardinality: zero-or-one
    capability_contract: application.web-frontend
  - slot_id: os
    axis: platform.os
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: build-system
    axis: build.system
    cardinality: zero-or-one
    capability_contract: sample.portable-app
  - slot_id: toolchain
    axis: toolchain
    cardinality: zero-or-one
    capability_contract: sample.portable-app
entrypoints:
  - name: page
    kind: web-application
    path: page
acceptance_contracts: []
source_dependencies: []
---
# Bureau of Unlicensed Birdsong

## Canonical authority and interface

This lesson is a self-contained Literate AI 1.3.1 project. Its browser application
is a real Component. JavaScript, Make and the selected host OS are real Flavors;
workflow, routing and authoring skills are pinned by its Component lock. The
resolved Component graph and selected inputs come from that lock. Lesson roles
from the earlier demonstration are not additional Components.

Use a dark two-column desktop layout with a 280–360 CSS pixel lesson sidebar and
a flexible canvas stage. At widths up to 820 pixels, stack the sidebar and stage
without horizontal page overflow. Show the lab number, title, subtitle, learning
statement, exact readable specification, resolved authority graph and a separate
panel explaining the selected inputs and retained-source boundary. Graph nodes
support pointer, Enter and Space selection with visible focus and descriptions.
The graph distinguishes Components, Flavors and workflow/skill/routing inputs.

Canvas output scales with device pixel ratio without changing its CSS size. The
stage has a descriptive heading, a labeled canvas, controls and a text readout.
The canvas is at least 480 CSS pixels high on desktop and 420 on narrow screens.
Provide full, selectable source inspection with paths and SHA-256 for authored
inputs and all selected authority. Long source text scrolls inside its panel.
No scripts, styles, images, fonts or model calls are fetched by the preview.
Audio begins only after a user gesture. Finished previews work without networking.

The initial implementation is explicitly authored retained input, not a claimed
model-generated implementation. The public Standard rebuild must validate, build,
test, execute and independently accept it before issuing admission or a receipt.
The preview does not embed its own future receipt: read the separately published
project receipt. A byte hash alone is not acceptance. Generation after changing
the specification uses the selected coding assistant and a newly reviewed plan.

## Audio, state and visuals

Start silent. A visual-only feather is added every 700 ms while audio is stopped.
Start the paperwork creates/resumes Web Audio only after the gesture and starts
one 240-ms timer; repeated start does not create duplicate timers. Stop issuing
permits clears that timer, resumes visual-only additions and permits later restart.
For step k choose frequency from [261.6,293.7,329.6,392,440] at
(3*k+floor(k/4)) modulo five. A sounding note is a sine oscillator whose gain
rises from 0.0001 to 0.12 in 20 ms and falls to 0.0001 at 280 ms; stop at 300 ms.
Add each feather at ((47*k) mod 700,80+(83*k) mod 280), lifetime 1. Each animation
frame multiply life by 0.985, remove below 0.02, and draw a rotating teal outlined
feather with lifetime opacity. Wrap positions within the current canvas size.
The readout distinguishes “audio licensed” and “visual permits only”, with a
monotonically increasing docket count. No microphone or external media is used.

## Acceptance

Verify initial silence, an actual user start gesture, running/resumed audio state,
notes scheduled by the single timer, stop and restart. Independently observe the
audio context/oscillator and visual readout; a text label alone is insufficient.
Desktop/mobile layouts and full source inspection remain usable offline.

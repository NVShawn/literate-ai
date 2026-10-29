---
namespace: unprompted
version: 1.0.0
display_name: Glitch Opera for One Nervous Rectangle
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
# Glitch Opera for One Nervous Rectangle

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

## Animation, audio and capture

Animate eighteen translucent alternating teal/orange rectangles around the canvas
center using frame-based sine/cosine positions, changing size and rotation. Use a
slowly changing dark HSL background. Start in silent rehearsal. Authorize ominous
drone creates one 55-Hz sawtooth oscillator at gain 0.025 after a user gesture;
repeated presses must not duplicate it. No microphone is requested.
Capture 4 second aria records only the canvas at 30 FPS for four seconds. If
MediaRecorder is unavailable, show an explicit unsupported message and retain the
animation. Otherwise request a browser-supported video format and download a
nonempty file with the matching extension. Release the object URL afterward.
Prevent concurrent captures. Distinguish recording/completed state and allow a
second capture. The video contains the canvas; browser audio is not claimed to
be muxed into it. Readout reports silent/drone state and frame outside capture.

## Acceptance

Verify eighteen rectangles per frame, initial silence, actual oscillator creation
on gesture, and a real nonempty four-second video download in a supporting browser.
Verify the unsupported recorder branch separately. Desktop/mobile/offline checks
must reject console errors, failed requests and horizontal page overflow.

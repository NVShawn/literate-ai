---
namespace: unprompted
version: 1.0.0
display_name: Cathedral of Tiny Disasters
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
# Cathedral of Tiny Disasters

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

## Composed visual and media behavior

Animate ninety particles plus ten rotating projected spokes. Particle i begins
with angle 0.7*i, radius 30+(29*i mod 210), and angular speed 0.002+(i mod 9)*0.0004.
At frame t advance the angle by speed*(1+0.3*sin(0.01*t+i)); modulate radius by
0.7+0.3*sin(0.008*t+i), use x=cos(angle)*radius and y=sin(1.7*angle)*radius*0.72,
center the system and draw radius 2+(i mod 5) in alternating orange/teal/blue with
additive blending. Reset the blend mode before drawing spokes. Ten spokes rotate
with angle 0.006*t+i*pi/5, depth sin(angle)*95+260 and scale 420/depth.

Wake the choir starts/resumes audio after a user gesture, one note every 360 ms,
cycling [110,146.8,164.8,220,293.7]. Use triangle oscillators with gain rising from
0.0001 to 0.05 in 30 ms, falling at 350 ms and stopping at 400 ms. Repeated start
must not duplicate timers. Silence the choir stops scheduling notes and permits
restart. Initial state is silent. No microphone or model connection is used.
Bottle 5 seconds records canvas video at 30 FPS for five seconds with a supported
format, prevents overlapping captures, downloads a nonempty file, then releases
its object URL. Unsupported capture is explicit and does not stop animation.
The readout reports choir state and “90 tiny disasters”. The video is canvas-only;
it does not claim the synthesized audio is muxed into the exported video.

## Acceptance

Independently observe ninety particle arcs and ten spokes per frame, start/stop/
restart of the real audio timer, and a real nonempty five-second recording. Verify
unsupported capture, keyboard controls, source parity, desktop/mobile and offline
behavior. A composed drawing inside one application is not proof of a multi-
Component graph; progressive Component decomposition remains a separate task.

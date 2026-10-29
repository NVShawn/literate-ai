---
namespace: unprompted
version: 1.0.0
display_name: Orbital Teapot (Teapot Pending)
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
# Orbital Teapot (Teapot Pending)

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

## Gravity and state

Start with three bodies: position (-80,0), velocity (0,-0.72), mass 8, orange;
position (80,0), velocity (0,0.72), mass 8, teal; position (0,115), velocity
(-0.62,0), mass 5, blue. The labeled Gravity slider ranges from 8 to 90, initially
42; divide by 100 to obtain G. Each animation frame, process each unordered pair
once, using displacement (dx,dy), distance max(24,hypot(dx,dy)) and f=G/distance².
Add (dx*f*otherMass,dy*f*otherMass) to the first velocity and subtract the symmetric
term from the other. Then advance each position once by its velocity. This is a
frame-based integrator, not a claimed wall-clock simulation or energy-conserving solver.
Draw circles of radius equal to mass, centered relative to the canvas midpoint.
Use a translucent dark fill to retain trails. Reset universe restores the exact
initial body state and clears trails; preserve the chosen gravity. The readout is
“G N.NN · teapots 0”. The absent teapot remains an explicit joke, not missing data.

## Acceptance

Verify three bodies, default G 0.42, keyboard slider bounds 0.08/0.90, updated
readout after a frame, and deterministic reset at a fixed frame. Changing gravity
must affect later positions. All controls and the source inspector remain usable
at desktop and 390-pixel widths without network access or runtime errors.

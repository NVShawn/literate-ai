---
namespace: unprompted
version: 1.0.0
display_name: The Grumpy Counter
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
# The Grumpy Counter

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

## Arithmetic and animation

Draw integers 1 through 239, in increasing order, about the canvas center.
For integer n, frame t (starting at zero), modulus m and smaller canvas dimension s:
angle = n × 0.31 + t × 0.002 radians; radius = 22 + (n mod m) × s × 0.006.
Position each dot using that radius and the cosine/sine of its angle. Advance t
once per animation frame, clear the canvas every frame, and continue without
network access, audio, randomness or permissions. The sequence is frame-based,
not a fixed real-time rotation speed. Resizing preserves the current modulus.

A prime is an integer at least 2 with no integer divisor from 2 through its square
root. Primes use teal #38d6c2, radius 3.4 CSS pixels and opacity 0.9; composites
use orange #ff8a3d, radius 2 and opacity 0.35. The integer 1 is neither prime nor
composite and uses gray #9db0c4, radius 2 and opacity 0.55. Color classification
never changes with the modulus. The canvas background is #050a10.

## Modulus interaction and acceptance

Show a labeled Modulus range, min 7, max 97, step 1, initial 37. Explain that
remainders change radii but primality does not change. The visible legend is
“Prime · teal; Composite · orange; 1 · gray”. Associate this and the explanation
with the slider. On input update the numeric output and “modulus N · primes remain
smug” synchronously. Verify Home/End/arrows and pointer input, all 239 dots with
independent primality/position calculations, and resizing without resetting m.

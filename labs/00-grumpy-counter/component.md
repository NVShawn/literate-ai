# Grumpy counter

## Scope and source boundary

This is one authored lab specification, stored as documentation in `labs/`.
It is not a canonical, locked Literate AI Component. The diagram is an illustrative
flow between specification, arithmetic, rendering and acceptance criteria; its
four roles are not four Components. `lab.json` supplies lesson copy and that flow.
`main.js` and the shared runtime/style are authored implementation inputs.
The builder bundles those inputs into HTML; it does not generate an implementation
from this Markdown or provide a lifecycle acceptance receipt.

## User interface

- Use a dark two-column desktop page: a lesson sidebar 280–360 CSS pixels wide
  and a flexible visual stage. At widths up to 820 CSS pixels, stack the lesson
  above the stage without horizontal overflow.
- The sidebar shows “Lab 00”, “The Grumpy Counter”, its subtitle and learning
  statement; a “Lesson flow (illustrative)” diagram; an explicit inventory of
  one authored lab specification and four lesson roles; and the complete Markdown
  below “Readable lab specification — component.md”.
- The four diagram roles are “Lab spec”, “Number goblin”, “Canvas” and “Criteria”.
  Clicking a role or activating it with Enter/Space highlights it and displays
  its description. This selection does not change the arithmetic or certify it.
- The stage heading is “Arithmetic having a mood”, with an “offline artifact”
  badge. Its rounded, near-black canvas is at least 480 CSS pixels high on desktop
  and 420 on narrow screens. The page background is navy with light text, teal
  and orange accents. The canvas scales to its CSS size and device pixel ratio.
- Below the canvas, show a labeled “Modulus” range slider and its numeric value.
  It accepts integers from 7 through 97 inclusive, step 1, initially 37. The slider
  supports pointer and native keyboard input (arrows, Home and End).
- Explain the control in visible text: the modulus changes how integer remainders
  become ring radii; it does not change which numbers are prime. Associate the
  explanation and a visible color legend with the slider for assistive technology.
- Label the legend “Prime · teal; Composite · orange; 1 · gray”. Show the current
  modulus in the range output and the readout “modulus N · primes remain smug”.
  Update both immediately on input, before waiting for the next animation frame.
- Provide an “Exact bundled source” inspector for all authored inputs, including
  this Markdown, lesson JSON, lab JavaScript, shared runtime/style and builder.
  Include project registration and the available-but-unselected language Flavor
  as context sources. Show each file's repository path, SHA-256 and full text, without abridgement.
  Long source is scrollable and selectable, including at narrow widths. These
  hashes identify bundled bytes; they are not signed lifecycle evidence.

## Surrounding inputs and harness

Show a separate, labeled panel beside/below the stage with six color-keyed cards:
manual JavaScript language choice (teal); shared runtime/style and HTML bundler
(blue); actual project registration (purple); and absent Flavor selection and
lifecycle evidence (orange). Derive project/version and documentation/Component
root membership from the bundled project manifest. Explain that the existing
lang-javascript Flavor targets Node.js 20+ and was not selected or qualified for
this browser example. Its source is visible as context, not an imported dependency.
Do not imply that a slider is a Flavor, roles are Components, or omitted lifecycle
settings exist elsewhere. The source inspector includes exact project and catalog
context in addition to the six implementation/authoring inputs.

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

## Acceptance

1. Open the standalone HTML with networking disabled: the canvas animates and the
   default labeled control and readout both show 37. No sound starts.
2. Pointer changes and keyboard Home/End/arrow input produce the expected integer
   values, including 7 and 97, and update both text displays synchronously. At a
   fixed frame and canvas size, changing m changes positions but not prime colors.
3. Independently check representative classification: 1 neutral, 2/3/239 prime,
   4/9/49 composite; verify positions and dot counts against the formulas above.
4. Each of the four lesson roles is keyboard and pointer operable. Its description
   is visible; the inventory never labels the four roles as resolved Components.
5. At desktop and 390-pixel widths, controls, diagram, specification and full source
   remain available without horizontal page overflow. Resizing retains selection.
6. The displayed Markdown and every inspector file match the exact authored bytes
   and hashes bundled by the builder. A changed or omitted input fails the verifier.
7. These checks qualify this documentation example only. A genuine Component DAG,
   selected Flavors, locks, generation and independent lifecycle admission require
   separate canonical project work; this lab does not claim those results.

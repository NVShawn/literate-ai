# Literate AI course creative brief

This is this repository's story, not inherited framework policy. Derived projects
choose their own presenters, tone, format, visuals and subject. The generic
`author-instructional-videos` skill teaches
the production craft; this document owns LitAI and Sam.

## Creative direction

### Current revision (1.2): settle the argument, before CI

The ten-minute "earn a small trial" cut spent too long persuading Sam that narrow
wins justify adoption. The 1.2 film inverts that. LitAI opens with a compelling,
concrete picture of agentic software development done properly: reviewed promises,
independent acceptance, three-platform builds, an installed package that ran and a
commit-bound receipt, all before CI. Sam reacts badly to "the usual AI slop sales
pitch". He is the ringleader of the holdouts in an argument raging at his company
(and across the industry): the CTO wants agent-first everything, and Thursday's
architecture review decides.

LitAI then takes Sam's beliefs one at a time and settles each with evidence. Every
debunk doubles as the tutorial step that proves it, so the audience learns
installation, planning, generation, independent acceptance, bounded repair,
pre-CI release gates, HTML views, brownfield adoption, learning and releases as a
side effect of the argument. The seven beliefs are: agent code is unreviewable;
the agent grades its own homework; CI is the safety net; an agent is a black box;
adoption means a rewrite; agents never learn; and releases are where it breaks.
The ending settles the argument both ways: agents do not ship on vibes, and
hand-reading every generated line was never the safety net. Evidence is.

Sam is not a straw man. He keeps real objections and the last laugh; LitAI still
concedes real limits at the moment they matter (a lenient oracle passes, run-history
controls are not shipped, remote steps are walkthroughs, later adoption stages are
not shown complete). "Superhuman" means stamina, not judgment: the harness runs the
whole gate every time. The CI chapter names conventional CI/CD pitfalls (push and
pray, queue latency, discovering platform failures last, untested pipeline files
holding production keys) and answers them with `make release-check` and
`litai release qualify` (this host, then workers, then CI only where needed).

Speech text and display text are separate. Pronounce AI as individual letters,
likewise CLI and SSH; say C plus plus. Preserve literal commands in captions and
visuals. New expressive local voices require a fresh audition: acceptance of the
previous Kokoro opening does not approve either these voices or this script.

LitAI is a capable, warm female instructor with dry wit. Sam is a competent male
C++/Make engineer whose habits leave packaging and release proof under-specified.
Neither is a lecture delivery device. Sam makes choices, tries commands, notices
results, objects, and occasionally gets the last laugh. LitAI responds to those
actions instead of continuing a prewritten monologue. Make is not the villain:
unrecorded assumptions and unproved shipping decisions are the problem.

Use short exchanges and earned callbacks, not alternating paragraphs. The running
joke is Sam's `final-final-really-final.zip`, not his intelligence. Humor should
make a boundary memorable, then get out of the way. No generic corporate music
bed is required; clean voices and intentional pauses matter more.

## Visual language

Use the two original character portraits at readable scale with shot/reaction
changes; avoid postage-stamp mascots over a bullet deck. Show a binary moving
from a developer's laptop toward another machine, the specification-to-product
path, a real terminal, and the evidence or package produced. Reveal only the
relevant boundary at the moment it is discussed. Commands remain legible and
copyable from companion documentation. Do not put the entire spoken explanation
on the screen. Captions occupy a reserved lower band.

## Greenfield narrative

1. Cold open: Sam has a compiled program and calls it shipped. LitAI asks what
   actually reached the user. Show the gap, rather than listing lifecycle stages.
2. Acquisition: show `github.com/jordanhubbard/literate-ai`, the README and
   installation documentation. Distinguish current development checkout from a
   published release; do not invent a PyPI package or nonexistent release asset.
3. Setup: demonstrate one complete macOS/Linux checkout route: Python 3.11+,
   Git and Make; `git clone`, `cd`, and the documented `make install` path. Explain
   prompts for prerequisites, the reported installation prefix/PATH, and verify
   `litai version` and `litai doctor`. Link the documented Windows alternative.
4. Agent readiness: choose and authenticate a supported coding CLI separately;
   never display credentials or claim installation alone grants model access.
5. Create: Sam reviews the read-only onboarding plan, then acknowledges apply.
   Show the actual files and explain the hello specification by changing one
   observable requirement, not by describing the entire taxonomy.
6. Code and proof: capture a real coding session and actual tests/acceptance;
   keep meaningful failures and fixes, trim waiting with an on-screen time-edit
   label. No invented terminal output or animation labeled as screen capture.
7. Packaging and shipping: show the actual wheel and verification. A local wheel
   is not a published release. End with an actionable next step and the ZIP-name
   callback, not another lifecycle summary slide.

## Production gate

The project owner accepted the neural audition's direction and requested one
continuous video play. Begin with character introductions and their perspectives;
make the real coding/adoption work the middle, and close with Sam reluctantly
convinced but asking about updates and linked repositories. Use chapter navigation,
not disconnected snippets. Humor targets unverified shortcuts and industrial-speed
AI slop, never the audience's intelligence. Public feedback publication is authorized.
The [continuous production package](play/README.md) owns the script and fresh evidence.

The original three slide-based cuts are rejected creative drafts, not the target
quality. First produce a short replacement opening with locally generated neural
voices, back-and-forth, visual action, and readable subtitles. Keep originals for
comparison. Obtain reviewer feedback on that cut before spending on a full-series
rerender. Record which voice/model and visuals were actually reviewed; technical
media verification alone does not constitute creative acceptance.

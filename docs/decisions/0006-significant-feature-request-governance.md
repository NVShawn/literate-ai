# ADR 0006: Significant Feature Requests Require an ADR, a Planning Cycle, and a Roadmap Item — in That Order

- Status: Accepted
- Date: 2026-08-19
- Decision owners: literate-ai maintainers
- Roadmap: n/a — this ADR establishes the process that governs future roadmap entries

## Context

During the 0.5.1 patch-release cycle, a sample-portfolio audit (requested mid-release,
outside the four bug fixes the release branch actually needed) surfaced real findings:
12 of 17 `specification-to-source` skills are exercised by zero samples or components,
Flavor coverage is concentrated and mostly dormant under default gates, and two new
samples ship without any conformance-harness wiring. Those findings are real, but acting
on them — pinning skill selections, authoring roughly a dozen new samples, re-pinning
Flavor axes across existing samples — is a substantial body of new work with real
design decisions embedded in it (which orphaned skills are worth keeping vs. retiring,
what a "thin" sample must still prove, how newly-pinned languages interact with the
existing conformance matrix). That work was about to start directly from a chat
directive, with no separate record of the decision itself, no review point before
authoring a dozen new specifications, and no durable link between "why we did this" and
"what shipped."

This is not the first time a mid-cycle idea has expanded scope without a written
decision preceding the work. The framework already has two separate mechanisms that
individually do part of this job — `docs/decisions/*.md` (ADRs, for architecture and
process decisions) and `skills/agent/record-user-directed-work/SKILL.md` (turning
direction into a roadmap queue item with evidence checkboxes) — but nothing connects
them in a required order, and nothing distinguishes "significant" scope-expanding work
from an ordinary bug fix or small follow-up that can go straight to the roadmap queue.

## Decision

A **significant feature request** — new capability, a scope expansion beyond what a
release or task was originally cut for, or a change that embeds a real design decision
(not just an implementation detail) — follows this order, and does not skip a step:

1. **Author an ADR in `docs/decisions/`** before any implementation lands. The ADR states
   the context, the decision, and its consequences, using the existing numbered format
   (see `docs/decisions/0001` through `0005` for the shape: Status/Date/Decision
   owners/Roadmap header fields, then Context, Decision, and a closing consequences
   section). The ADR is where the design decisions live — what's in scope, what's
   deliberately deferred or rejected, and why — not the roadmap queue, which only tracks
   execution state.
2. **Run a planning cycle on the ADR before implementing it.** Whoever directs the work
   reviews the ADR's proposed decision and explicitly accepts, amends, or rejects it
   before any subtask begins. A plan is not a formality to produce and immediately act
   on in the same turn; the review point is real. An ADR's `Status` field records the
   outcome (`Proposed` while under review, `Accepted` once approved, `Rejected` or
   `Superseded` otherwise) and must reflect the actual state, not be defaulted to
   `Accepted` on creation.
3. **Add the accepted ADR's concrete subtasks to the roadmap for the release currently
   in progress**, via `skills/agent/record-user-directed-work/SKILL.md`'s existing
   allocation and evidence-checklist mechanism. The roadmap item links back to its
   owning ADR instead of restating the design; the ADR links forward to its roadmap
   item once one exists, so either document leads a reader to the other.

**What counts as "significant"** is a judgment call, not a bright line, but the
sample-portfolio case is the calibrating example: work that (a) wasn't part of the
release's original committed scope, (b) involves a real design choice with more than
one defensible answer, and (c) will take meaningfully longer than the task that
surfaced it. An ordinary bug fix, a formatting correction, a one-file documentation
edit, or work already fully specified by an existing ADR does not need a new one —
route it straight to `record-user-directed-work`'s roadmap queue as today. When in
doubt, the cost of writing a short ADR is small relative to the cost of a dozen new
specifications landing without one.

**This applies to every Literate AI project**, not only this framework repository —
it is process guidance, not a framework-specific mechanic, so it belongs in the
project-generic release-engineering skill rather than a framework-only document.

## Consequences

`skills/agent/release-project/SKILL.md` gains a step requiring this ADR-first sequence
before a release absorbs new scope beyond its already-planned fixes. This adds real
friction — a written decision and an explicit review point before work that used to
start immediately from a chat directive — which is the intended effect: it is meant to
slow down exactly the moment where scope quietly expands mid-release, not to slow down
the release's already-scoped work.

The cost is one more artifact to keep current: an ADR whose `Status` is stale (left at
`Proposed` after acceptance, or `Accepted` without ever having been reviewed) is worse
than no ADR, so the discipline only pays off if the status field is honestly maintained
across the planning cycle. `record-user-directed-work` is unchanged in mechanics; it
gains one required upstream input for the significant-request case and remains the
sole roadmap-recording path for everything else.

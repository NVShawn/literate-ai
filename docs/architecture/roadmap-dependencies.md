# Roadmap completion dependencies

Work completion and release execution are separate obligations. Every included
goal must complete before release readiness. A goal must not require that same
release to pass readiness, publication or post-publication verification first.
Final qualification and publication remain mandatory after deliverables complete.

## The 1.3.1 incident

[79903e680](https://github.com/jordanhubbard/literate-ai/commit/79903e680e907c2fa297cffa9886323f82abc91f)
introduced the goal manifest, included RELEASE-019, and required checked parent
goals for readiness. The same commit gave RELEASE-019 an evidence checkbox
requiring the actual release plan, qualification, publication, verification and
post-release synchronization. Thus its completion required publication, which
could not start until that goal completed.

[61da15eee](https://github.com/jordanhubbard/literate-ai/commit/61da15eee63a5590da71bc8574b28ce1a98fde35)
assigned COURSE-004 to 1.3.1 and added a checkbox requiring the declared release-goal
gate and exact qualification to pass. COURSE-004 completion required readiness,
while readiness required COURSE-004 completion. That film branch did not yet
contain the enforcement manifest. Merge
[8cae3cf86](https://github.com/jordanhubbard/literate-ai/commit/8cae3cf86)
combined the branches and recorded both goals in the manifest; PR #136 landed it
on main at [8d5c83c11](https://github.com/jordanhubbard/literate-ai/commit/8d5c83c11).
The later 18-goal reconciliation retained the existing cycle rather than creating it.

The recorded rationale was to prevent another silently deferred film and require
all promised goals complete before release. The implementation mixed acceptance
of a deliverable/enforcement mechanism with execution of its consuming release.
These are roadmap/gate ordering errors, not Python package dependencies. Git's
author identity alone does not establish who formulated or reviewed a sentence.
The user's requirement to complete goals did not require circular ordering.

The original `_roadmap_release_goals` projected headings to completion Booleans
and release targets. It checked membership, inclusion, approved deferral and
completion, without prerequisite edges or checkbox semantics. Its regression
cases covered missing, incomplete, omitted and deferred goals, not a goal
requiring its own release. Documentation lifecycle checks covered ownership,
status, links and archival evidence, not completion graphs. These are observable
coverage gaps; the history does not establish any individual's review rationale.

## Correction and retained obligations

The user approved correcting the circular checks. COURSE-004 means exact approved
film delivery, reviews, provenance and published links. RELEASE-019 means tested
enforcement, scope controls and the newly requested cycle prevention; it stays
open until that work passes. All 18 goals and their 1.3.1 targets remain, with none
removed or deferred. The [same-release execution checklist](../roadmap/1.3.1-release-execution.md)
retains plan, preparation, every qualification, check, publication, published
verification, synchronization and milestone closure as mandatory unfinished steps.
A document's ownership link does not itself make every step a completion prerequisite.

The audit also exposed a reversed legacy field on GENERATION-011: RELEASE-009
was labeled `Depends on`, immediately followed by “needs this before local fan-out
can close.” That describes `Required by`. Correcting the label preserves the
release's prerequisite and every unfinished evidence box on both items.

## Explicit contract

```markdown
- **Release target:** 1.3.1
- **Completion prerequisites:** INPUT-WALK-001, RETAINED-ACL-PREFLIGHT-001
```

Use one line per field. `Completion prerequisites` accepts comma-separated
existing work IDs, `release:VERSION:PHASE`, or exactly `none`. Unknown references
and misspelled phases fail closed. `litai work record` accepts repeated
`--completion-prerequisite` arguments. These fields do not change checkboxes or
authorize scope moves.

Release execution phases are `ready`, `prepared`, `checked`, `published`,
`verified` and `synchronized`. Each later phase requires the preceding one;
`checked` means the prepared candidate's release check. The separate `qualified`
phase represents standalone clean-source `litai release qualify`, which can run
before preparation and does not implicitly depend on readiness. Readiness requires
every included manifest goal and every item with that exact release target.
Deferral approval remains the existing release-goal gate's responsibility.
The graph spans all declared releases, including checked and deferred work.
Errors give an ordered path, e.g. `COURSE-004 -> release:1.3.1:ready -> COURSE-004`.

Legacy `Depends on` fields contribute existing IDs before a semicolon or
parenthesized explanation; fields beginning with `none` have no edges. Repeated
historical sections contribute the union of their edges. Fenced examples and HTML
comments do not contribute graph nodes or edges.

Arbitrary prose, checkbox sentences, external documents and unmodeled external
systems cannot be reliably interpreted. Put relevant ordering in the typed field;
review must still check that it faithfully describes acceptance criteria. The
incident fixture preserves the historical sentences and an explicit semantic
transcription rather than claiming natural-language inference.

## Write and validation coverage

- Work record/close validate before returning proposals; the CLI additionally
  checks the selected manifest before atomic replacement.
- Update-derived imports check existing and proposed content, including no-ops.
  Project update application checks its entire prospective overlay, including
  reviewed merges, before file or baseline writes, even without a custom validator.
  Inherited catalog application also checks its selected overlay before writes.
  Model-assisted documentation reconciliation validates the entire proposed batch
  before replacing any document, including unrelated documents in that batch.
- Initialization/adoption checks selected templates before scaffold writes.
  Native rewrite planning checks the appended ADOPT-002 proposal before writing
  its queue, companion decision, detailed program or documentation-index links.
  Onboard delegates there. Reparent changes lineage rather than roadmap bytes;
  normal authority validation checks the roadmap and composite update imports use
  the prospective-overlay check.
- Documentation graph validation checks conventional queue dependencies. Project
  validation additionally loads policy-selected/custom queues and manifests.
  Release readiness checks the complete ledger graph.
- `make documentation-check` runs the read-only
  `scripts/check_roadmap_dependencies.py` gate. Documentation-only CI and Python
  test cells include deterministic cycle regressions.

There is no separate roadmap dependency-edit or deferral-import mutator. Manual
edits and arbitrary Git merges cannot be intercepted before filesystem writes;
validation/CI and release readiness reject cyclic results. Rejected supported
proposals do not rewrite existing roadmap data or remove goals.

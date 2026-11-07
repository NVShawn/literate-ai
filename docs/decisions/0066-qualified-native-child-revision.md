# ADR 0066: Revise a Qualified Native Child Without Reusing Its Evidence

- Status: Accepted
- Date: 2026-11-06
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0, per the included disposition on issue #350
- Roadmap: QUALIFIED-CHILD-REVISION-001 in docs/roadmap/active-work.md
- GitHub issue: [#350](https://github.com/jordanhubbard/literate-ai/issues/350)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)
- Approval: directing user explicitly accepted this design on 2026-11-07 ("Accept both designs", ADRs 0062 and 0066)

## Context

An adopted parent can hold a passing retained-harness receipt and a qualified native
library. An upstream behavior change can require a new accepted specification and a
new independently qualified implementation of that same logical library. The
project identifier and Component coordinates must remain stable for consumers.

The existing child-addition path refuses an already-registered identity. The
existing source-replacement path refuses a qualified parent or child. Qualified
recertification requires the same accepted specification identity, so it cannot
authorize changed intent. Issue #350 records these public refusals on a passing
parent. A private registry edit would discard the evidence boundary that makes the
parent's current qualification meaningful.

Accepted ADR 0062 ([PR #319](https://github.com/jordanhubbard/literate-ai/pull/319)),
also accepted by the directing user on 2026-11-07, adds a different qualified child. It does not change the registered
child's identity or intent.
The existing Standard rebind and unqualified replacement operations also remain
separate. This decision proposes a revision of one already-qualified child.

## Decision

### Qualify a new physical revision independently

Add a public native-child revision plan, acknowledged apply, status and recovery
path. The candidate has the same logical project identifier and Component
coordinates as the active child, but a distinct direct physical revision path and
a different accepted specification identity. It completes ordinary signed source
acceptance and independent two-build qualification before a parent switch can be
planned. A project identifier, directory, registry entry or saved success flag
does not stand in for those current qualifications.

The old active tree and new candidate tree both remain intact. The registry names
exactly one active physical revision for the logical child. Historical physical
revisions remain inspectable but are not active parent dependencies or alternate
providers selected by path search. Duplicate active identifiers or Component
coordinates remain errors; the revision path is never an escape from those guards.
No revision command edits the child's accepted specification, source, locks or
qualification evidence to make a candidate appear eligible.

### Bind a complete read-only switch plan

Planning reads, but does not mutate or execute, the current parent definition and
authority, retained implementation and receipt, complete child registry, old
child definition and qualification, new child definition and qualification, and
both physical trees. It binds the exact active registry entry, old and new
accepted specification identities, qualified source identities, target and
Component coordinates. It also binds every unaffected registered child and the
parent evidence that would be renewed. The plan reports the proposed active
registry and exact evidence consequences under the parent's current policy.

The normal public qualification readers reopen both child records; source
identity, current locks, signed acceptance and independent build evidence must
agree. Refuse missing evidence, an unqualified candidate, changed intent without
new acceptance, stale or mixed target evidence, redirected or aliased paths,
cross-project custody, an active duplicate and any drift since planning. The
candidate must remain separate from the retained parent's original source.

An acknowledged apply revalidates the entire plan under the canonical parent
lifecycle lock. If parent policy requires renewed execution or application
acceptance after switching a child, apply performs those operations only under
their existing explicit authorizations, or stops before publication and reports
the required work. A metadata-only projection cannot manufacture a current
receipt or consumer parity. The unaffected second child and its evidence remain
bound to the complete proposed parent state.

### Publish a recoverable active-revision transition

Stage the new complete registry and affected parent qualification evidence in
bounded attempt-owned storage on the same filesystem. Preserve the exact old
registry, parent evidence and both child trees. Before the first replacement,
write a durable transaction record binding the reviewed plan, old and new bytes,
the two child qualifications, expected paths and publication phase. Reserve
affected files and directories against concurrent owners and recheck their
identities immediately before each publication step.

The registry and evidence are a logical transaction across several filesystem
operations, not one atomic rename. Public readers refuse an in-progress or
mixed revision. On interruption, the public recovery operation verifies the
record and current nodes, then either finishes the exact staged state or restores
the exact old state. It never overwrites a foreign change, deletes either child
tree or treats recovery metadata as proof of qualification. Ambiguous or corrupt
state remains blocked for explicit operator recovery. Apply reports success only
after a fresh process can reopen the complete new parent state.

Keep the old child tree and its independent qualification as history. Its former
parent registration and old parent receipt cannot qualify the new active child.
The switch may publish only with policy-supported renewed parent evidence; actual
consumer acceptance remains a separate gate. Unaffected children's independent
qualification does not need to be rebuilt solely because one sibling changed.
Removal of a historical tree is a separate reviewed operation, outside this ADR.

### Keep consumer and release claims separate

The transaction changes active child authority. It does not prove that the
parent's actual consumer uses v2 correctly, transfer retained-source authority,
authorize application release, or publish a package. Those claims require fresh
application acceptance, real consumer v2 behavior and the ordinary release gates.
The child-addition, Standard rebind and unqualified replacement paths retain
their own contracts.

## Validation required before completion

1. Use the noneditable installed public CLI to qualify a retained parent with a
   v1 dependency-free library and an unchanged second child. Independently accept
   a changed v2 specification and qualify v2 through two genuine builds. Plan and
   apply a switch to v2, reopen from a fresh process and prove the same logical
   identifier and Component coordinates now select the distinct v2 physical tree.
2. Run an actual consumer against v2 and require its new behavior plus fresh
   application acceptance. Keep the original retained-harness receipt and both
   child trees inspectable; show that stale v1 parent evidence cannot qualify v2.
3. Reject unqualified, unsigned, wrong-target, renamed, duplicated, aliased,
   redirected, changed-source and stale-plan inputs. Mutate each bound parent,
   registry, child and receipt input after planning and prove no companion write.
4. Inject failures before and between each publication step and during recovery.
   Reopen through the public API and prove a complete old or new state, exact
   rollback bytes and preservation of foreign files and both physical trees.
5. Complete independent source review, native supported-platform and installed
   journeys, full regression and exact-head CI. Verify that consumer acceptance
   and release authorization remain separate from the registry transaction.

These are acceptance criteria for a future implementation, not evidence that
this proposal has met them. The directing user accepted this design under ADR 0006; implementation and every
acceptance criterion above remain required.

# ADR 0062: Add an Independently Qualified Native Child to a Qualified Retained Parent

- Status: Accepted
- Date: 2026-11-05
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0
- Roadmap: [QUALIFIED-CHILD-ADD-001](../roadmap/active-work.md#qualified-child-add-001-add-a-qualified-native-child-to-a-qualified-retained-parent-atomically)
- GitHub issue: [#315](https://github.com/jordanhubbard/literate-ai/issues/315)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)
- Approval: directing user explicitly accepted this design on 2026-11-07 ("Accept both designs", ADRs 0062 and 0066)

## Context

A retained adopted parent can have a current harness receipt and a registered,
qualified native child. Another native library can be accepted and qualified
independently through its normal two-build path. The integrated source acceptance
path refuses after the parent is qualified, while registering the second child
alone changes the registry without renewing the parent's qualification evidence.
There is no public operation that commits both facts as one reviewed addition.

The existing native-project registry binds each child path, project identifier
and project-definition identity. Its low-level registration operation publishes
that registry atomically as one file, but does not bind parent qualification.
The registered-child update path in [issue #48](https://github.com/jordanhubbard/literate-ai/issues/48)
rebinds an existing child. This request adds a distinct child without changing
the existing children or transferring the parent's retained source authority.

## Decision

### Expose one public plan and apply operation

Add a versioned public plan/apply transaction for adding an already-qualified
native child to an already-qualified retained parent. Planning is read-only: it
does not register a child, renew a receipt, build, test, install tools or fetch
artifacts. Applying requires an explicit acknowledgement of the exact plan and
uses the normal public qualification and authority readers. Consumers do not
call private registry or receipt writers.

The plan binds the exact current parent project definition, source authority,
retained harness receipt, qualification policy and evidence; the complete
existing child registry and each registered child's current definition; and the
new child's project definition, coordinate, target and completed independent
qualification. It includes the proposed complete registry and renewed parent
qualification projection. A location or digest alone is insufficient proof that
the child is currently qualified. Reopen the actual evidence and policy under
their existing custody rules before accepting the plan.

Renewal must use a policy-supported composition of the actual parent receipt
and independently qualified child evidence. The registry writer cannot mint a
new qualification by hashing metadata. If the current parent policy requires
fresh execution after child membership changes, the plan must state that work
and apply may run it only under its normal explicit lifecycle authorization;
otherwise it refuses before publishing. No success is inferred from the old
parent receipt alone.

The new child must be a direct, safe child of the parent's designated native
project directory. Reject traversal, links or reparse points at every ancestor,
duplicate paths or project identifiers, case-folding collisions on applicable
platforms, mismatched coordinates, unsupported targets and a child whose
definition or qualification changes after planning. The existing children and
their qualification evidence remain in the complete renewed projection.

### Publish one recoverable state transition

Apply takes the parent transaction lock and reopens every plan binding before
mutation. Stage the complete new registry and parent qualification evidence in
bounded, attempt-owned files. Preserve exact previous registry and parent
evidence bytes for recovery. Validate the staged state as a complete parent
state before publication. Use a durable transaction record with an explicit
prepared/committed state, same-filesystem atomic file replacements, and a
directory sync where supported. Readers must refuse an incomplete transition;
they cannot treat the new registry with old evidence, or the reverse, as a
qualified parent. On retry or recovery, verify the transaction record and
either finish the exact staged commit or restore the exact previous state.
Corrupt, foreign or ambiguous recovery data fails closed for operator review.

This is logical atomicity across the registry and evidence, not a claim that
two filesystem replacements are one syscall. The public operation reports
success only after the complete new state can be reopened and verified. A
failed or interrupted attempt must leave the prior qualified parent usable
after successful recovery, without deleting the already-qualified child.

The operation does not promote the retained parent's implementation source,
rebuild its original harness, or silently widen its consumer dependency graph.
A later consumer transfer retains its separate qualification and acceptance
requirements. Existing registered-child update and no-addition paths remain
valid under their own versioned contracts.

## Validation required before completion

1. Use public APIs to qualify a retained parent with native child A, then
   independently accept and qualify dependency-free library B through two
   builds. Plan and apply B's addition; reopen the parent and both children
   from a fresh process and verify the renewed receipt and complete registry.
2. Reject an unqualified B, changed parent receipt or authority, changed
   registry, stale plan, changed B definition or qualification, unsafe paths,
   duplicate coordinates, project identifiers and case-colliding names.
3. Inject failures before and between publication steps. Reopen through the
   public recovery path and prove either the complete old or complete new
   state, never a mixed qualified state. Preserve foreign files and exact old
   metadata on rollback.
4. Exercise local and supported remote/native targets, noneditable installed
   framework behavior, independent source review, full regression gates and
   exact-head CI. Keep source-transfer evidence separate.

These are acceptance criteria for a future implementation, not evidence this
proposal has met them. The directing user accepted this design under ADR 0006; implementation and every
acceptance criterion above remain required.

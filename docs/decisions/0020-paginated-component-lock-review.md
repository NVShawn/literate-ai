# ADR 0020: Paginate Genuine Large Component-Lock Reviews Before Atomic Replacement

- Status: Accepted
- Date: 2026-08-27
- Decision owners: literate-ai maintainers
- Roadmap: [LOCK-004](../roadmap/active-work.md#lock-004-paginate-genuine-large-component-lock-reviews-before-atomic-replacement)
- Release: pending

## Context

Component-lock replacement is intentionally fail-closed when a semantic diff emits more
than 512 entries. ADR-independent repair `LOCK-003` separated that review limit from the
internal traversal budget, so a large mostly-equal lock no longer fails merely because
the comparison walked many values. A downstream Physics Workbench sample parent transition now proves
the remaining case is real: the first stale `ide-shell` lock has more than 512 actual
semantic differences. Moving through the first intervening parent commit still reaches
the same limit.

Deleting the stale lock would erase the authority being reviewed. Raising the per-output
limit would turn one bounded review into an arbitrarily large response. Writing a
partially updated lock would create authority that does not correspond to any resolved
Component plan. The framework needs an explicit way to review a genuinely large
transition in bounded pieces while retaining one atomic final replacement.

## Decision

### 1. Large review is a distinct explicit transaction

The ordinary `litai lock`, `--check`, and `--diff` behavior remains unchanged. A diff
with more than 512 emitted entries still fails closed and writes nothing.

An explicit large-review command starts a transaction under the ignored project object
directory. The transaction binds:

- the project, Component, target, and ordered Flavor selectors;
- the exact current lock path, bytes, and identity;
- the exact newly resolved lock identity;
- the semantic-diff algorithm and ordering version;
- the total difference count and ordered page identities; and
- the installed framework distribution and policy identities.

Starting a transaction never changes the Component lock.

### 2. Every page remains bounded and explicitly acknowledged

The command emits at most 512 complete semantic-difference entries per page. Pages use
the existing deterministic path ordering and carry the transaction identity, page
number, total page count, previous-page identity, and page identity.

Continuation requires the caller to provide the exact identity of the page just
reviewed. The adapter records only acknowledgements bound to the immutable transaction;
it does not accept a page number, cursor, or blanket `--force` as equivalent evidence.
Missing, skipped, reordered, duplicated, malformed, or mismatched acknowledgement fails
closed.

The full transaction also retains a separate bounded traversal-work ceiling and a
bounded total-difference ceiling. Pagination limits presentation size; it does not
authorize unbounded computation or storage.

### 3. Only the final step replaces authority

After every page is acknowledged, one final apply command re-resolves the Component and
recomputes the complete semantic diff. It requires the current lock bytes, proposed lock
identity, selectors, page identities, framework identities, and source authority to
match the transaction exactly.

The adapter then atomically replaces the lock and writes its normal resolution audit.
No intermediate page may write a partial lock. A changed source tree, lock, catalog,
policy, executable, transaction file, or page acknowledgement invalidates the
transaction and requires a new review.

Transaction state is derived evidence, never committed project authority. Successful
apply removes it; failed or abandoned state remains diagnosable until an explicit
cleanup command removes it.

### 4. The interface is automation-safe

The CLI exposes machine-readable start, continue/acknowledge, status, apply, and cleanup
operations. Every result has stable reason codes and bounded output. Interactive shells
may present the same protocol ergonomically, but no interactive-only prompt is required
for CI, coding agents, or accessibility tooling.

## Rejected alternatives

### Raise or remove the 512-entry limit

Rejected. That limit bounds one review surface and protects callers from oversized
output; a larger ambient default does not establish that the transition was reviewed.

### Delete and regenerate the stale lock

Rejected. Absence of old authority is not evidence that every semantic change was
reviewed.

### Write one partial lock per page

Rejected. Intermediate files would be valid JSON but invalid authority: they would
represent neither the old nor the resolved Component graph.

### Require every downstream project to refactor its Component graph

Rejected as the general solution. Smaller graphs can be desirable product design, but a
framework parent transition may legitimately change more than 512 fields in one
indivisible root lock.

## Consequences

- Genuine large transitions become reviewable without weakening the existing per-page
  bound or atomic lock semantics.
- The framework gains ignored transaction state, stable pagination contracts, CLI
  commands, and recovery/cleanup tests across supported platforms.
- Review takes multiple explicit acknowledgements and a final revalidation; this
  friction is intentional.
- Physics Workbench sample can resume its 0.1.1 lock refresh only after this ADR is accepted,
  implemented, merged, and adopted through normal parent authority.

## Acceptance Criteria

- The directing maintainer explicitly accepts this proposed decision in a planning
  cycle before implementation begins.
- Existing lock commands preserve their current 512-entry failure behavior.
- Every emitted page contains at most 512 complete differences and is chained by exact
  identities.
- Apply rejects skipped, reordered, replayed, tampered, stale, or cross-transaction
  acknowledgements and any current/proposed authority drift.
- No partial lock is ever written; successful completion performs one atomic final
  replacement and normal audit update.
- Transaction state is path-safe, permission-safe, bounded, ignored, and portable on
  Linux, macOS, and Windows.
- Focused regressions cover start, continuation, restart/status, final apply, cleanup,
  all fail-closed cases above, and an end-to-end downstream-sized transition.

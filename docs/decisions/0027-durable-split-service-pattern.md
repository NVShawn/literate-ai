# ADR 0027: Durable Split Web/API/Collector/Cache Application Pattern with Scheduler-Lease Authority

- Status: Accepted
- Date: 2026-09-01 (proposed); 2026-09-01 (accepted after planning-cycle review)
- Decision owners: literate-ai maintainers
- Roadmap: [SPLIT-SERVICE-001](../roadmap/active-work.md) (#216),
  [SCHEDULER-LEASE-001](../roadmap/active-work.md) (#217)
- Depends on: [ADR 0026](0026-multi-entrypoint-deployment-units.md)
  (multi-entrypoint / deployment units), [ADR 0005](0005-executable-component-semantics.md)
  (public interfaces and edge semantics), [ADR 0002](0002-reference-implementation-ecosystem.md)
  (reference ecosystem / portfolio samples)

## Context

Auditing `portfolio-dashboard` and `telemetry-dashboard` surfaced one recurring
provider-neutral architecture that generation does not currently produce by
default. A dashboard-style service that polls expensive upstream systems keeps
being generated as a **single service that recollects upstream data inside
request handlers and loses all state on restart**, coupling frontend availability
to collection latency and to upstream credentials.

The correct shape, which both projects converged on independently, has four
boundaries:

1. a browser frontend that consumes only a same-origin, read-only API;
2. a read-only HTTP API that consumes only a durable local snapshot store;
3. a single-writer collector/worker that alone may call upstream providers;
4. a durable embedded SQL snapshot cache (SQLite first).

Projects can model this today with multiple Components and `provides`/`requires`,
but the reusable contracts are not cataloged, so every project re-derives them and
repeatedly gets the anti-pattern. Both projects also need the same collector
cadence behavior: a once-daily nominal UTC run with bounded jitter, durable
schedule state, and a single-writer lease so restarts and concurrent workers do
not double-collect (#217).

This ADR decides the reusable pattern and the scheduler-lease authority together,
because the scheduler is the collector boundary's cadence contract — separating
them would let the lease store drift from the cache boundary it must share.

## Decision

### Catalog a four-boundary reference composition, not a monolith template

Ship an inheritable reference Component set (and exact specification-to-source
guidance) for the four boundaries, composed through **public capabilities only**
(ADR-0005 `PublicInterfaceContract`), so no boundary sees another's private
specifications:

- **frontend** (browser, ADR-0026 deployment unit) requires only the API's public
  read capability; it holds no upstream credentials and no database handle.
- **read-only API** requires only the cache's read capability; it never calls
  upstream providers and never writes.
- **collector/worker** is the *only* boundary whose generated code may call an
  upstream provider fixture, and the only writer to the cache.
- **snapshot cache** provides a read capability and a single-writer write
  capability over an embedded SQL store: SQLite with WAL, a busy timeout,
  explicit migrations, and read-only connections for readers.

The four are independent deployment units (ADR-0026) so restarting the frontend
or API does not invoke the collector. Capability contracts connect them without
exposing private transitive specifications; the framework template carries no
provider commands, product schemas, credentials, or application vocabulary — those
stay in the deriving project's authority (ADR-0001 boundary, ADR-0002 ecosystem).

**Rejected alternative:** a single "durable-dashboard" Component kind with four
built-in surfaces. That would bake product-shaped structure into the framework,
violate ADR-0001, and prevent projects from substituting, say, a non-SQL cache or
a different frontend. The pattern is a *composition of public capabilities*, not a
new kind.

### Coherent snapshot publication and read isolation

The cache boundary publishes a **current snapshot** only after all required writes
for a collection window validate. Readers (API, therefore frontend) always see the
last complete snapshot: a failed or partial collection leaves readers on the prior
complete snapshot rather than a half-written one. This is the durable-availability
property both audited projects needed and is expressed as a cache capability
postcondition, not left to generated code to reinvent.

### Scheduler-lease authority is generation guidance, not a framework daemon

The collector's cadence is a reusable worker/scheduler generation skill (or
Component template) — **reference authority for an application's own worker**, not
a scheduler process the framework runs. It specifies:

- a configurable nominal UTC run time and symmetric bounded jitter window;
- injectable random and clock sources so tests are deterministic;
- persisted schedule state: schedule window, selected jittered instant, last
  completed window, retry state — stored in the same embedded cache boundary;
- a database-backed **lease** (owner, acquisition time, expiry) so only one worker
  executes a window;
- restart semantics — before target: preserve the selected instant; after a
  completed target: do not rerun; after an expired running lease: recover safely;
- bounded retry/backoff tracked separately from the next daily schedule;
- append-only run and per-source outcome records; progress/failure output that
  never logs secrets; graceful cancellation and next-run status readable by the
  separate API process.

Placing the lease and schedule state inside the cache boundary (rather than a
fifth store) is deliberate: the single-writer lease and the single-writer cache
write are the same concurrency invariant, so they share one durable store and one
migration lineage.

## Consequences

A new portfolio sample composes the four Components and is wired into the
conformance harness (ADR-0002, ADR-0012 sample-portfolio matrix), proving the
boundaries on macOS, Linux, and Windows where SQLite is available. Component locks
and CycloneDX evidence preserve the four-boundary composition (ADR-0026 deployment
units). The scheduler skill joins the `specification-to-source` catalog with its
own SkillEvaluator coverage and deterministic acceptance tests for jitter bounds,
UTC rollover, restart states, concurrent single-winner leasing, expired-lease
recovery, boundary-crossing long runs, retry-without-double-run, and cross-process
next-run readability.

The cost is a non-trivial reference composition plus a new skill, and it hard-
depends on ADR-0026 landing first (the independent deployment units). The benefit
is that the durable, restart-safe, credential-isolated architecture both audited
projects needed becomes the default a project inherits, instead of an anti-pattern
each one re-derives. The frontend of this pattern is exactly what ADR-0028's
browser acceptance validates.

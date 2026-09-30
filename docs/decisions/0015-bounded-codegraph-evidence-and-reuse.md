# ADR 0015: Bound CodeGraph to Consumed Evidence and Reuse Exact Index Custody

- Status: Accepted; mandatory product/host dependency superseded by
  [ADR 0016](0016-codegraph-not-a-product-dependency.md)
- Date: 2026-08-24
- Decision owners: literate-ai maintainers
- Roadmap: `CODEGRAPH-RESILIENCE-001` for 0.6.0

## Context

Literate AI currently requires CodeGraph at project-maintenance and generated-source
boundaries. The integration provides useful source intelligence and tamper-evident
evidence: an exact source tree is bound to the selected executable and runtime version,
the frozen SQLite artifact, declarations, references, call-graph relationships, and
bounded graph counts. Agents also use the graph directly to navigate this repository.

The 0.6.0 release qualification exposed a disproportionate operational cost. Several
otherwise-valid cross-platform runs failed while repeatedly initializing CodeGraph over
content-identical disposable snapshot replicas. Windows path sensitivity produced only
`CodeGraph command failed with exit status 1` because the sanitized runner captured
stderr but discarded it from its result. Release engineering then changed test custody
paths repeatedly without access to the provider's actual diagnostic. Compilation,
tests, acceptance, exact source manifests, SBOMs, and tree identities were healthy; the
derived source-intelligence operation was the failing boundary.

Removing CodeGraph or making every failure advisory would discard evidence that the
framework deliberately treats as part of generated-source admission. Keeping every
current invocation unchanged would retain a high-availability dependency whose repeated
work is not justified when the exact source tree and provider closure are unchanged.

This is significant release scope under ADR 0006: it changes when a mandatory lifecycle
provider must execute and which previously captured evidence may satisfy that boundary.
It therefore requires this decision and explicit review before implementation.

## Decision

### Keep the evidence boundary

CodeGraph remains the built-in source-intelligence provider and remains required at a
stage whose declared policy requires graph-derived evidence. A cache hit never means
"skip source intelligence." It means verify and reuse an immutable evidence artifact
that already binds the exact current inputs.

Project-level indexing remains mandatory for canonical project validation, lifecycle
planning, and release checks. This ADR does not weaken a missing, stale, substituted,
malformed, or incompatible project index into a warning.

### Reuse exact generated-tree index custody

Generated-source indexing becomes content-addressed by all evidence-defining inputs:

- exact generated source-tree identity and source snapshot material;
- provider ID and provider contract version;
- runtime version and executable identity;
- extraction/schema version and canonical artifact path; and
- the capabilities and relationship evidence required by the consuming stage.

When immutable custody for that key exists, the framework verifies its canonical
evidence envelope, artifact digest, exact source manifest, provider/runtime closure, and
required graph properties, then reuses it without invoking CodeGraph again. Missing,
stale, corrupt, foreign-provider, foreign-runtime, or differently keyed custody invokes
CodeGraph normally and atomically publishes new custody only after full verification.

Equal source trees in different disposable workspace paths may share custody. Host paths
are not cache-key material. A reused index must not be copied into source authority; it
remains a derived evidence attachment under framework-owned cache custody.

### Consume graph evidence deliberately

Every mandatory CodeGraph gate must name the graph-derived fact it consumes. For 0.6.0,
the accepted facts remain:

- canonical project index currency and exact database evidence;
- generated-tree declaration/reference/call-graph capability evidence;
- exact relationship counts and unresolved-relationship bounds where currently
  required; and
- frozen index identity retained by generated-source admission and release evidence.

A gate that checks only that CodeGraph ran, while consuming no graph-derived fact, must
be removed or folded into the nearest consuming gate. This is deduplication, not a
policy downgrade. Broader replacement of CodeGraph or redesign of source-intelligence
contracts is outside 0.6.0.

### Preserve actionable bounded diagnostics

The sanitized CodeGraph runner must retain bounded stdout and stderr separately. A
failed operation reports:

- the non-secret operation (`preflight`, `init`, `sync`, `status`, or query class),
- exit status or timeout,
- a bounded, UTF-8-safe diagnostic excerpt retaining the beginning and end, and
- whether output was truncated.

Diagnostics must not expose ambient credentials, environment values, arbitrary command
arguments, or unbounded provider output. Successful machine envelopes remain unchanged
unless a versioned evidence field is explicitly required.

### Keep the 0.6.0 implementation bounded

The 0.6.0 scope is limited to:

1. Preserve bounded CodeGraph stderr and operation context on failure.
2. Reuse verified generated-tree index custody for an exact content/provider key.
3. Remove demonstrably duplicate generated-tree invocations that consume no additional
   graph fact.
4. Add cross-platform regressions proving equal-tree reuse, invalidation, tamper
   rejection, and actionable diagnostics, including the two-pass Windows self-host
   snapshot replication that motivated this decision.

The release does not add another source-intelligence provider, make required indexing
optional, change authored project policy, or migrate persisted evidence wire formats
unless implementation proves a versioned field is unavoidable.

## Rejected Alternatives

### Make CodeGraph advisory everywhere

Rejected because exact graph evidence is already part of generated-source admission and
project release policy. Silently proceeding would make receipts claim a weaker boundary
without an explicit migration.

### Remove CodeGraph from 0.6.0

Rejected because repository navigation and exact source-intelligence evidence provide
real value, and removal is a larger provider-contract migration than the observed
fragility requires.

### Keep re-indexing and only shorten paths

Rejected because path shortening treats one symptom. Content-identical immutable trees
should not repeatedly depend on a fallible external process when exact verified custody
already exists.

### Reuse by source-tree digest alone

Rejected because the same bytes indexed by a different executable, runtime, extraction
schema, or capability contract are not equivalent evidence.

## Consequences

- CodeGraph remains a real trust dependency, but repeated equal-tree operations stop
  turning it into an unnecessary availability dependency.
- A provider failure becomes diagnosable from CI evidence without reproducing it on the
  same host.
- Cache custody and invalidation become more important and require fail-closed tests.
- Release qualification should become faster and less path-sensitive while preserving
  the same graph-derived claims.
- Any broader narrowing of mandatory source intelligence requires a later ADR backed by
  measurements of which graph relationships lifecycle decisions actually consume.

## Acceptance Criteria

- The directing maintainer explicitly accepts or amends this ADR before implementation.
- An accepted roadmap item enumerates the exact 0.6.0 subtasks and evidence.
- Two content-identical generated trees at different paths invoke CodeGraph once and
  independently verify the same immutable custody.
- Changed source, provider executable/runtime, extraction version, capability contract,
  artifact bytes, or evidence envelope cannot reuse custody.
- A failed real or simulated CodeGraph operation reports bounded actionable diagnostics
  without leaking sanitized environment content.
- Project-level required indexing and generated-source graph evidence remain fail-closed.
- The complete Linux, macOS, and Windows CI matrix passes on the final release revision.

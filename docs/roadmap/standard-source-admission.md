# Standard generated-source admission

- **Status:** active
- **Owning queue item:** [CACHE-007](active-work.md#cache-007-admit-verified-generated-source-before-target-builds)
- **Completion / archival evidence:** pending while CACHE-007 remains open

## Problem and boundary

Standard can generate source on a coordinator and can fan out an already accepted
source-cache member, but its accepted derivation currently proves a completed local
build, tests, and acceptance. A coordinator intentionally stopping before
target-specific object, binary, and package work therefore cannot publish its
test-passing source. Remote workers then miss accepted membership and invoke their own
coding CLI. This is a lifecycle evidence gap, not permission to weaken the full
artifact lifecycle.

The new transition is:

```text
generated source candidate
  -> independent generated-source verification
  -> accepted-source admission evidence and cache membership
  -> explicit publication
  -> compatible remote object/binary/package lifecycle
  -> final Standard receipt
```

Source admission proves only source identity, provenance, and source-level verification.
It grants neither build authorization nor downstream acceptance. Every worker must still
index, classify, authorize, build, reconcile dependencies, execute required artifact
tests and oracles, and produce downstream evidence before finalization.

## Canonical evidence

Define one versioned, closed-schema source-admission record owned by the verifier. Its
semantic identity includes:

- exact Component, Component lock, generation recipe, execution plan, selected Flavor
  set, transitive skill closure, and application/framework authority identities;
- accepted source tree and source manifest identities, generated test manifest/suite,
  and source SBOM where the current Standard contract requires it;
- coding-CLI executable/tool binding, model selector, route, generation request,
  transcript/journal evidence digest, and source-generation provenance identity;
- ordered verifier/test/oracle declarations and complete case results, with every
  required result passing and no missing or skipped case;
- one explicit source portability scope: target-independent, or target-specific with a
  canonical selector set covering every source-affecting OS, architecture, ABI,
  toolchain, and target-profile input;
- exact installed framework distribution identity and admission policy/verifier
  implementation identity; and
- an admission timestamp and bounded operational metadata excluded from semantic
  authority except where replay policy explicitly consumes them.

The record is constructed only by the verifier service from retained typed generation
and test evidence. The generator cannot submit a boolean or self-authored admission
claim. Missing coding-CLI/transcript evidence, incomplete tests, identity drift, an
unknown selector scope, or source-manifest mismatch fails closed.

The generation request is represented by two non-interchangeable identities. The
component-orchestration request binds the prepared node, context, complexity decision,
and workspace custody. The per-node planned coding-CLI request binds the exact prompt
that entered the coding CLI and remains the transaction identity in
`SourceDerivationCacheKey`. Candidate and provenance bind both; admission verifies both.
Cache membership is published under a separate
`AcceptedSourceLookupKey`: recipe identity, execution-plan identity, portable
coding-CLI tool binding, provider/model binding, and sanitized source-request semantics.
The latter retains the Component generation plan/key, context manifest, direct public
interfaces, entrypoints, and bounded-prompt identity. The recipe closes over the exact
Component definition/lock, selected Flavor and skill authority, managed source graph,
and source-relevant inputs; the execution plan closes over workflow and routing policy.
Only the orchestration request and workspace allocation fields, plus their derived
stage-input digest, are removed. Session, channel, nonce, timestamp, and other handoff
envelope details never enter that source request. They remain auditable custody but
cannot perturb lookup. Multi-node orchestration may therefore share one orchestration
identity, fresh continuation sessions may allocate another, and every admitted node
still retains its exact originating coding transaction.

## Source-test workspace custody

The CLI captures the original candidate before source tests and checks its canonical
source-tree identity. Capture is bounded to 4,096 files, 16,384 entries, 32 MiB per
file and 256 MiB total, matching the cache's source limits. Indirect paths, reparse
points, hardlinks and special files are refused. Commands run from a fresh copy
with the same relative layout, source bytes and executable permissions; empty
directories are preserved. Commands within one admission share test outputs.

Before and after each command, the verifier rechecks the original tree's membership,
bytes and permissions, plus all original file bytes, permissions and directory nodes
in the copy. Test-created files in the copy are not accepted source. Editing or
removing original source, substituting a directory, or changing the original candidate
fails admission. Publication still verifies the unchanged candidate through the existing
filesystem cache. Cleanup removes only the still-owned disposable root; a substituted
root is preserved and refused. These checks provide cooperative custody under the
existing host-execution authorization, not production containment.

## Publication and worker consumption

Extend the existing Standard source-cache membership rather than creating a parallel
store. A source-only admitted member carries the source-admission identity and omits
build/resolved-SBOM/artifact acceptance fields by schema. Existing full-lifecycle
members remain readable and can project the same source-admission boundary only when
their retained evidence satisfies the new verifier.

`litai cache publish` may copy a source-only member only after independently loading and
validating the exact admission record and rehashing its source closure. The publication
adapter compares every key, lock, plan, Flavor, skill, framework, provenance, manifest,
and selector identity. Runtime absence remains a stable error only when no admitted
runtime member exists; generated-but-unadmitted source never becomes publishable.
Legacy request-keyed membership is not reindexed automatically: it misses under the
new membership schema and must be readmitted from reverified source or regenerated.

A remote `build --from-accepted-source` request names exactly one membership and
admission identity. The receiver verifies archive bytes, manifest, membership, framework
distribution, target compatibility, and current authority before lifecycle allocation.
It has no generator port and starts at source indexing/object build. Target-independent
source may fan out to compatible targets; target-specific source requires selector
equality or an explicitly defined compatible relation and never falls back to
regeneration.

## Receipt composition

Each completed node result binds both:

1. the source-admission evidence consumed by that node; and
2. current worker index, build authorization, object/binary/package, resolved-SBOM,
   generated-artifact test, execution, and independent acceptance evidence.

Project membership and the aggregate receipt require the exact planned node set and both
evidence layers for every node. Source admission alone cannot construct a lifecycle
result, project admission, finalized candidate, or release. A downstream failure records
diagnostics but cannot finalize or overwrite the current passing receipt.

## Proof plan

Unit and integration proof must cover successful admission plus failed test, source
tamper, plan/lock/Flavor/framework drift, selector mismatch, and missing coding-CLI
provenance. Worker tests must prove the generator is unreachable after accepted-source
selection. Receipt tests must prove both evidence layers are present and downstream
failure cannot finalize.

The release-shaped E2E builds the real wheel and target-specific Linux/Windows worker
closures from the same revision. One installed-wheel process admits twelve nodes under
session-A request identities; a second process derives fresh session-B identities and
restores those twelve admissions across the canonical 27-cell Linux matrix plus the
Windows portability probe. The restore process has no generator port. It first asserts
the old generated-only state still yields `source_cache.runtime_absent`, then proves
publication/fanout succeeds with zero worker generator invocations.

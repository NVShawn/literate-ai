# Change: Bootstrap the literate-ai Lifecycle Framework

## Why

OVA has proven that specifications, exact dependency source, source intelligence, model
routing, generation, caches, packages, publication, settings, and generated applications
belong to one Component lifecycle. Those general capabilities are currently inseparable
from OVA and Omniverse policy. A software-neutral framework is required before OVA or
another product can safely reuse and evolve them.

## What Changes

- Define Component definitions and immutable revisions as the universal unit of
  composition, including generated applications and the framework itself.
- Adopt OpenSpec through a provider boundary and bind generation to exact specification
  artifacts and immutable intent events.
- Resolve authoritative signed source into canonical snapshots and query revision-bound
  source intelligence for every selected dependency.
- Replace fixed generation stages with durable, typed workflow DAGs and first-class model
  endpoints, groups, and stage routing policies.
- Define immutable source/build/artifact bundles, empty-cache fault-in, explicit
  publication, scoped typed settings, and complete CycloneDX 1.7 dependency evidence.
- Require security classification and short-lived build authorization after origin
  verification, while supporting an explicit, expiring, fully warned `yolo` policy.
- Make software-neutral samples a release-blocking conformance suite, including a
  generated framework-readiness application and a separately pinned deterministic
  two-generation snapshot-replay proof with explicitly narrow claims.
- Provide a compatibility and shadow-execution boundary for the two-phase OVA rebase.
- Add a literate-ai-only Phase 8 bounded context that uses content-pinned skills and
  exact source evidence to produce reviewable, coverage-bearing OpenSpec drafts from
  existing source code.
- Establish Python 3.11+ as the initial reference package, admit an exactly pinned
  CycloneDX JSON validator behind the dependency adapter, and isolate the npm-based
  OpenSpec CLI beneath `tools/openspec/` as contributor-only tooling.
- Add first-class composable Flavors so platform, accelerator, language ecosystem,
  toolchain, packaging, and deployment specifications remain outside target-neutral
  Components.

## Capabilities

### New Capabilities

- `component-lifecycle`: Define, resolve, revise, compose, and discover first-class
  Components.
- `spec-led-development`: Preserve exact specifications and intent across the lifecycle.
- `source-intelligence`: Ground generation in exact authoritative source and structured
  source evidence.
- `ai-workflow`: Execute durable typed workflows using model groups and stage policies.
- `artifact-lifecycle`: Fault, validate, build, package, link, and publish immutable
  artifacts.
- `settings-publication`: Resolve typed scoped settings and configure publication
  independently from local cache use.
- `security-policy`: Verify origin, classify exact source closures, authorize builds,
  and expose explicit `yolo` operation.
- `living-samples`: Use executable Components as complete living conformance documents.
- `ova-compatibility`: Preserve and shadow OVA behavior through a two-phase rebase.
- `source-to-specification`: Derive reviewable specifications from exact existing source
  with skill-directed analysis and explicit evidence, coverage, uncertainty, and approval.
- `implementation-ecosystem`: Keep the reference runtime small and maintained while
  preserving language-neutral contracts and isolating contributor/UI/native tooling.
- `component-flavors`: Resolve typed specification mix-ins into exact effective Component
  revisions without mutating or contaminating base specifications.

### Modified Capabilities

None. This is the first framework change.

## Impact

The change establishes public schemas, domain/application boundaries, ports and reference
adapters, neutral samples, security gates, and OVA compatibility fixtures. OVA remains
authoritative until every Phase 1 exit gate passes. It introduces no production-security
claim and performs no destructive conversion of existing OVA caches.

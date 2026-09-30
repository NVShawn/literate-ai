## Context

OVA's source-grounded Component implementation is a successful vertical slice with a
strong provenance chain. It is also coupled to OVA manifests, Omniverse foundation
policy, CodeGraph shell output, four LLM stages, Python compilation, filesystem caches,
and Litestar Settings. Some immutable package records are mutated by reverse-dependency
updates, acceptance is atomic only per file, workflow state is not durable, and security
is currently a plan.

The framework must preserve source-level understanding, empty-cache operation,
first-class generated Components, model groups, explicit publication, and living samples
without treating any dependency as a binary SDK or baking one product into the core.

## Goals / Non-Goals

**Goals:**

- specify software-neutral immutable contracts and lifecycle transitions;
- make every source/evidence/model/package decision reproducible and auditable;
- separate descriptor discovery from lazy exact-source materialization;
- support arbitrary typed workflow DAGs and multiple model providers;
- enforce signed-source verification, classification, and build authorization;
- provide typed settings and publication services without requiring a UI;
- prove the design through neutral executable samples plus a deterministic,
  verifier-only two-generation framework snapshot replay; and
- rebase OVA with fixtures, shadow comparison, rollback, and no cache destruction.

**Non-Goals:**

- shipping a production-ready framework in this architecture-only change;
- implementing an OVA, Omniverse, Isaac, ROS, or Kit runtime in core;
- asserting that signed source is safe;
- choosing one model provider, intelligence engine, language, builder, registry, or UI;
- preserving flawed legacy digests without an explicit mapping; or
- regenerating all OVA source in the current OVA descriptor self-host sample.

## Decisions

### Use a hexagonal core with versioned wire contracts

Domain objects are immutable pure values. Application services coordinate explicit
ports; adapters own I/O. Every schema has a URI/version, rejects unknown core fields,
allows only namespaced extensions, and hashes canonical semantic content. Timestamps,
host paths, aliases, and status are events/projections.

### Separate Component coordinate, revision, and local state

A logical coordinate survives changes. A Component revision binds exact specs,
authoring inputs, workflow policy, and source. Cache entries, `latest` aliases, reverse
edges, publication state, and run status are machine-local/rebuildable projections.
Generated applications and generated libraries use the same objects as their inputs.

### Use provider-neutral specs and source intelligence

OpenSpec is the first `SpecificationProvider`; CodeGraph is the first
`IntelligenceProvider`. Exact artifacts, source snapshots, indexes, queries, and evidence
cross every generation boundary. Adapters return structured results, never regex-parsed
human output. Descriptor vocabulary is global; exact grounded evidence is lazy for the
selected closure and comparison candidates.

### Make workflow and model routing data

A versioned typed DAG replaces four fixed stages. Model endpoint/group/routing policy
objects attach to stage capability requirements. A run separates deterministic input
identity from append-only execution events and records request/response digests,
parameters, tools, cost, fallbacks, privacy, and policy. Mutable routing decisions are
scoped to one run.

### Commit output trees transactionally

Generation stages write immutable workspace trees. Acceptance updates one revision
reference after validators and spec/source drift checks pass. A durable event store and
idempotency keys permit restart without accepting a partial file set.

### Keep packages immutable and publication separate

Source, build, and artifact bundles are CAS records with exact forward dependencies.
Reverse dependencies and mutable channels are separate projections. Local readiness
never requires publication; publication is an explicit append-only operation with
policy, remote identity, attestation, retry, failure, revocation, and promotion records.

### Treat authenticity and compile safety as distinct decisions

Signed authoritative source is canonicalized and verified in quarantine. Findings and
dependency propagation produce a classification. A builder requires a short-lived
authorization bound to exact source, closure, policy, builder, and toolchain. `yolo` is a
maximum-privilege policy requiring exact signed identity, actor, reason, enumerated
privileges, expiry, acknowledgement, warning, audit, and downstream provenance. It does
not disable signature, identity, audit, or attestation.

### Render one typed settings registry

Framework defaults, repository, workspace, user, machine, and run scopes have explicit
precedence and per-value provenance. Secret references are not secret values. CLI, JSON
API, optional generic UI, and the downstream OVA Settings UI consume the same schemas
and actions. Publication remains its own settings/action section.

### Migrate OVA in exactly two phases

Phase 1 freezes fixtures, establishes the framework, runs neutral conformance and
side-effect-free OVA shadow comparisons while OVA stays authoritative. Phase 2 pins OVA
to a release, cuts over reversible seams, proves real OVA-on-framework self-hosting, and
retires duplication only after compatibility releases and rollback drills.

### Add a separate source-to-specification bounded context in Phase 8

The inverse workflow lives under `src/literate_ai/source_to_specification/`, with shipped
skill packs under `skills/source-to-specification/` and conformance samples under
`tests/fixtures/source_to_specification/`. It consumes exact source snapshots and intelligence
artifacts, invokes ordered content-pinned skills, and produces typed behavior
observations, a provider-valid draft specification set, evidence coverage, uncertainty,
and a proposed patch.

Observed current behavior, inferred intent, suspected defects, compatibility quirks,
conflicts, and unknowns remain distinct. Generated specs are drafts until a decision
authorized by a versioned `SpecificationPromotionPolicy` promotes them to the
authoritative `SpecificationSet`; human approval is the default. Dynamic observation
requires a separate exact `ObservationExecutionAuthorization` and sandbox runner.
Standalone local source requires an accepted signed local attestation before promotion.
This prevents reverse engineering from canonizing bugs or laundering model guesses into
requirements. Phase 8 changes only literate-ai and is not a third OVA migration phase.

### Use Python for the reference kernel and isolate ecosystem-specific tooling

Python 3.11+ is the initial reference implementation because the OVA extraction baseline
and its strongest contracts/tests are Python. The distribution admits the exactly pinned
CycloneDX library with strict JSON validation behind its dependency adapter. New runtime
packages require explicit maintenance, security, license, transitive-size, ownership,
isolation, and removal review.

The npm-based OpenSpec CLI lives only under `tools/openspec/`; Node.js is a contributor
validation prerequisite, not a framework install/runtime prerequisite. Optional
TypeScript presentation and future Rust/native security helpers stay behind adapters and
ports. Canonical JSON/JSON Schema contracts remain language-neutral.

### Compose target-specific behavior through first-class Flavors

Base Component definitions and specifications remain target-neutral. Versioned Flavor
objects contribute one typed variation axis such as OS, architecture, accelerator,
language ecosystem, toolchain, packaging, or deployment. A target profile and resolver
produce an exact `FlavorSetLock`, `EffectiveSpecificationSet`, and
`EffectiveComponentRevision` without mutating the base revision.

Flavors use declared typed contribution points; raw document patches and silent
last-writer-wins precedence are forbidden. Base requirements and security policy are
monotonic. Flavor conflicts fail with an explained resolution, and every target-sensitive
cache, run, bundle, and publication includes the effective revision identity. Concrete
Windows, CUDA, language, and NVIDIA policies remain in catalogs/adapters rather than core.

## Risks / Trade-offs

- Contract-first extraction is slower initially, but prevents application coupling from
  becoming a public API.
- Compatibility and shadow execution temporarily duplicate work, but give evidence and
  rollback for cache/provenance migrations.
- Canonical identity rules may change legacy digests; explicit maps add complexity but
  prevent false identity claims.
- Arbitrary workflows and provider ports enlarge the model; profiles and reference
  adapters keep common paths approachable.
- Maximum-privilege `yolo` cannot make execution safe. Persistent provenance and policy
  allow informed use and downstream rejection.
- A readiness application or deterministic package replay is useful lifecycle evidence,
  but neither is model-authored framework self-hosting; documentation and release gates
  must preserve that distinction.

## Migration Plan

The normative sequence and gates are in
[`docs/migration/ova-two-phase-rebase.md`](../../../docs/migration/ova-two-phase-rebase.md).
Phase 1 releases the framework without changing OVA's authority. Phase 2 introduces
feature-flagged OVA seams, imports legacy caches read-only, canaries and drills rollback,
then removes duplication after two compatibility releases.

## Open Questions

- Which canonical JSON implementation and schema dialect will be normative across
  languages?
- Which signature and transparency ecosystems are required for the first Git/local
  source adapters?
- What minimum sandbox guarantees are supportable on macOS and Linux before builders
  are enabled?
- What additional authority and acceptance policy would be required before claiming
  model-authored framework self-hosting?
- Which registry protocol is the first non-filesystem publication adapter?
- Which language/framework skill packs meet the Phase 8 coverage threshold for the first
  stable source-to-specification release?

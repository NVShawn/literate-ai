# ADR 0001: Extract a Software-Neutral Lifecycle Kernel

- Status: Accepted for bootstrap
- Date: 2026-08-02
- Decision owners: literate-ai maintainers
- Supersedes: none

## Context

OVA implemented Components, source-grounded generation, model groups/selectors, local
caches, packaging/linking, publication, Settings integration, living samples, and a
source-security plan while building an Omniverse application generator. Most of those
concepts describe a general spec-led software lifecycle. Their current Python types and
services also encode OVA policy, CodeGraph CLI behavior, Python build behavior,
filesystem storage, and fixed model stages.

Copying or renaming the implementation would make literate-ai an OVA utility library.
Rewriting without compatibility fixtures would discard the strongest invariants and
make an eventual OVA rebase unsafe.

## Decision

Build literate-ai as a software-neutral, hexagonal lifecycle kernel with immutable,
versioned wire contracts. Domain and application packages depend only on ports. OpenSpec,
Git/local sources, CodeGraph, model providers, builders, storage, and publishers are
adapters. OVA becomes a downstream adapter and conformance consumer.

The public object model distinguishes logical Components, immutable revisions, exact
source snapshots, knowledge/evidence, durable workflow runs, security decisions,
immutable bundles, publication records, and scoped settings. Generated outputs return
to the system as ordinary Component revisions.

Migration uses the two phases in
[`ova-two-phase-rebase.md`](../migration/ova-two-phase-rebase.md): framework extraction
and shadow proof first; OVA cutover and duplicate retirement second.

## Boundary rule

Core code and schemas must not contain:

- `ova` names, IDs, paths, environment variables, or manifest assumptions;
- Omniverse, Isaac, ROS, Kit, physics, or foundation-component policy;
- CodeGraph wire formats or shell-output parsing;
- a particular model provider, programming language, builder, filesystem, UI, or
  publication transport; or
- four globally fixed LLM stages.

OVA-specific behavior belongs in the OVA repository or `compatibility/ova` during the
migration window. Generic adapters may live here if they satisfy neutral port contracts.

## Consequences

Positive consequences:

- Components created by any product receive the same first-class lifecycle treatment.
- Exact-source knowledge and model decisions remain auditable as dependencies change.
- OVA can rebase incrementally with observable parity and rollback.
- Other applications can adopt the framework without inheriting graphics/robotics policy.
- caches, packages, publications, and settings gain stable cross-product semantics.

Costs and risks:

- Phase 1 must define and test wire contracts before rapidly porting code.
- OVA requires a temporary compatibility layer and dual execution.
- some OVA identities cannot remain identical after canonicalization and need explicit
  mappings.
- true security enforcement, durable workflows, and self-hosting require more than the
  current prototype provides.
- two repositories and compatibility releases increase short-term operating cost.

## Rejected alternatives

### Rename and move OVA modules

Rejected because it preserves OVA foundation injection, eager indexing, fixed stages,
mutable package projections, Python build semantics, and storage/UI coupling.

### Keep the framework inside OVA

Rejected because dependency direction would remain wrong and other products would have
to import application policy to use general lifecycle contracts.

### Rewrite with no parity layer

Rejected because existing manifests, locks, caches, provenance, samples, and failure
semantics are part of the behavior to preserve or explicitly migrate.

### Use signatures as build authorization

Rejected because a signature proves source origin and integrity, not behavioral safety.
Classification and short-lived build authorization remain separate mandatory decisions.

## Validation

This decision is enforced by import/dependency tests, schema fixtures, neutral samples,
OVA shadow comparison, exact identity mapping, security adversarial tests, true
self-hosting, and the Phase 1/Phase 2 exit gates.

The reference implementation ecosystem and tooling isolation are decided separately in
[ADR 0002](0002-reference-implementation-ecosystem.md).

## Purpose

Defines target-qualified release, publication, import, and deployment-readiness evidence without confusing a package format or local build with release authority.

> **Implementation status:** these release-level requirements remain open. Existing source/build/SBOM and receipt primitives are prerequisites, not evidence that package closure, release variants, publication/import, deployment readiness, or standalone status are implemented.

## ADDED Requirements

### Requirement: Release manifest binds the complete closure
A release manifest SHALL bind the root Component and effective revision, target and Flavor
lock, accepted Component workspaces, artifact and resource graph, package plan and result,
entrypoints, runtime closure, SBOMs, tests, independent acceptance, toolchains, policies,
and exact receipt identities.

#### Scenario: Resource blob changes after packaging
- **WHEN** a release resource no longer matches its manifest identity
- **THEN** verification fails before publication, import, or deployment

### Requirement: Release variants remain exact and independent
One accepted specification revision MAY have multiple target-qualified release variants.
Each variant SHALL retain its own lock, artifact graph, package kind, runtime closure,
acceptance, and release identity without weakening the specification.

#### Scenario: Portable and RTX variants are built
- **WHEN** one root application is released for portable mock and Linux RTX targets
- **THEN** both releases reference the same accepted specification revision and distinct exact target evidence

### Requirement: Publication is separate from local readiness
A locally qualified release SHALL remain usable without a publication record. Publication
and import SHALL be explicit authorized operations that bind immutable release content,
publisher or repository identity, policy, and revocation state and that reconstruct and
verify the complete closure after restart.

#### Scenario: Unpublished release is launched locally
- **WHEN** local policy accepts its current qualified receipt
- **THEN** deployment may proceed without fabricating a publication identity

### Requirement: Deployment readiness is receipt-governed
The framework SHALL report whether a release is deployable for a requested target and
policy. Required package, artifact-publication, deployment, signature, SBOM, test, and
acceptance results SHALL be present exactly when configured; missing, stale, revoked,
wrong-target, or caller-asserted evidence SHALL fail closed.

#### Scenario: Deployment receipt is stale
- **WHEN** deployment policy requires a current receipt whose lock differs from the release
- **THEN** readiness is denied with the exact receipt mismatch

### Requirement: Standalone status is truthful
A release SHALL claim standalone execution only when its package and runtime closure prove
that no undeclared external runtime is required. Other package kinds SHALL enumerate their
runtime requirements explicitly.

#### Scenario: Packaged application requires an interpreter
- **WHEN** the runtime closure includes an external interpreter
- **THEN** the release is labeled as a runtime-dependent bundle rather than a standalone binary

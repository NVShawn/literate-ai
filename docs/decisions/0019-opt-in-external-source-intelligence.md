# ADR 0019: Projects May Opt In to External Source Intelligence

- Status: Accepted
- Date: 2026-08-26
- Accepted: 2026-08-26
- Decision owners: literate-ai maintainers
- Roadmap: [SOURCE-INTELLIGENCE-001](../roadmap/active-work.md#x-source-intelligence-001-restore-opt-in-external-source-intelligence)
- Partially supersedes: [ADR 0016](0016-codegraph-not-a-product-dependency.md)
- Release: `0.7.0` and the next `0.6.x` patch

## Context

ADR 0016 removed a source-graph indexer completely: no adapter, optional CLI,
worker capability, documentation, tests, or accepted provider other than `none`.
That made the framework independent of the tool, but also made an existing
canonical derived project impossible to update. Physics Workbench sample explicitly requires
`provider_id: codegraph-cli` at project-maintenance, generation, admission, and
structural-review boundaries. Both the 0.6.x and 0.7.0 lines reject that policy
before the derived project can review an otherwise bounded Component-lock update.

The directing maintainer selected a partial reversal: restore an opt-in external
provider on `main` and `release/0.6.x`, while keeping the framework itself free of
an ambient or installed CodeGraph dependency.

## Decision

### 1. `none` remains the default

New projects continue to select `provider_id: none` with every stage off unless a
caller explicitly requests `codegraph-cli`. A project using `none` never discovers,
invokes, installs, or reports a source-graph executable.

### 2. An explicit `codegraph-cli` policy is supported

The existing `ProjectSourceIntelligencePolicy` fields remain the authority:
provider ID, command, minimum version, artifact path, stage modes, and publication
mode. `litai project source-intelligence sync|check` executes the configured
command only for a project that selected `codegraph-cli`.

Required stages fail closed when the executable is absent or too old, the database
is absent or stale, status is malformed, the source identity changed, or the
artifact changed during observation. Optional stages report bounded unavailable
status. Off stages perform no provider work.

This adapter observes the configured project index. It does not claim that the same
database describes an independently quarantined repository-source tree. New opt-in
policies therefore keep `repository-source-admission` off; a project that explicitly
requires per-tree admission intelligence still fails closed until it supplies a
separate generated-source provider.

### 3. Literate AI does not provision the provider

The framework does not add an npm dependency, bootstrap the executable, export a
worker companion capability, or list CodeGraph as a universal prerequisite. The
operator or derived project owns installation and worker provisioning. Literate AI
only verifies and invokes an explicitly configured external command.

### 4. Lifecycle and verification honor explicit policy

Project validation, `litai verify`, and lifecycle boundaries consume the same
bounded project-index observation for the selected stage. A required opt-in policy
cannot be silently skipped or rewritten to `none`; a default-none project keeps the
dependency-free behavior established by ADR 0016.

## Rejected alternatives

### Fully restore CodeGraph as a framework prerequisite

Rejected. Projects that do not select source intelligence must remain independent
of the executable and sidecar.

### Preserve ADR 0016 and fork the derived project

Rejected. Changing a required derived-project policy to `none` weakens its authored
evidence boundary, while carrying a private framework fork defeats repository
lineage.

### Restore only the CLI parser

Rejected. A command that can write an index while validation, verification, and
lifecycle ignore or reject it creates false evidence rather than compatibility.

## Consequences

- ADR 0016 remains authoritative for default behavior, automatic provisioning,
  worker capabilities, and framework prerequisites.
- Downstream projects may require an externally managed source-intelligence provider
  and receive fail-closed evidence from normal framework commands.
- The 0.6.x backport is additive and opt-in; existing `none` projects keep identical
  behavior.

## Acceptance Criteria

- The directing maintainer explicitly selected and accepted partial reversal on
  2026-08-26.
- Projects using `none` pass without a source-graph executable and invoke none.
- Explicit `codegraph-cli` projects can sync and check a bounded project-local
  artifact on Linux, macOS, and Windows.
- Required stages reject missing, stale, malformed, tampered, or version-incompatible
  evidence; off stages invoke nothing.
- No package, bootstrap, worker-capability, or global prerequisite is added.
- Main and 0.6.x pull requests carry equivalent behavior and focused regressions.

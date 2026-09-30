## ADDED Requirements

### Requirement: Every sample is a complete executable Component

Each sample SHALL contain a canonical manifest, standalone-valid OpenSpec, pinned
authoring inputs, workflow/routing policy, expected lifecycle states and failures, a
declared generated entrypoint, and assertions over locks, evidence, provenance, and
packages. Each generated tree SHALL include its current implementation tests and
CycloneDX 1.7 source SBOM; each accepted build SHALL verify a resolved CycloneDX SBOM.
Generated application source SHALL NOT be cached in this repository.

#### Scenario: New user runs a sample from an empty cache

- **WHEN** the sample's documented command runs with no pre-populated Component cache
- **THEN** it faults exact requirements, generates source from the loaded specifications,
  validates and authorizes the generated tree, compiles it, executes the compiled
  entrypoint on the host, and verifies its expected output

### Requirement: Samples execute useful portable applications

Each primary sample SHALL perform deterministic application work beyond returning a
fixture marker. Its portable base behavior SHALL avoid platform-only dependencies, and
Linux, macOS, and Windows requirements SHALL be independently content-pinned Flavors.

#### Scenario: A supported host runs the sample ladder

- **WHEN** the ladder runs on Linux, macOS, or Windows
- **THEN** it selects the matching OS Flavor and every generated application completes
  its OpenSpec-bound acceptance scenario using the selected language toolchain or
  runtime

### Requirement: Samples cover the lifecycle from simple to complex

The conformance ladder SHALL cover minimal generation, generated reuse, stacks,
multi-repository source, resolution conflict, model routing/fallback, cache modes,
validation/repair, refresh, publication roundtrip, security profiles, framework
readiness, and deterministic snapshot replay.

#### Scenario: Framework behavior changes

- **WHEN** a framework change affects a covered lifecycle contract
- **THEN** the corresponding sample specifications and assertions are updated in the same
  change or CI rejects it

### Requirement: Framework readiness and snapshot replay have narrow claims

The historically named self-hosting sample SHALL generate, build, and run a
self-contained compatibility/readiness application from its specifications. A separate
verifier-only fixture MAY replay an already frozen framework source snapshot through
the lifecycle twice and SHALL require stable exact-tree identities without invoking a
coding model.

#### Scenario: A report overstates the proof

- **WHEN** the readiness application or deterministic snapshot replay passes
- **THEN** the report describes exactly which build, execution, isolation, and
  second-generation identities were verified
- **AND** it does not claim that a coding model authored or redesigned the framework

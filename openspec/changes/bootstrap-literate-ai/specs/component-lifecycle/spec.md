## ADDED Requirements

### Requirement: Components are universal first-class objects

The framework SHALL represent libraries, tools, services, applications, generated
projects, samples, and the framework itself through the same versioned Component
contracts.

#### Scenario: Generated application re-enters composition

- **WHEN** a generation run accepts a new application revision
- **THEN** the framework registers that immutable revision as an ordinary Component
- **AND** a later Component can resolve and use its declared capabilities

### Requirement: Logical identity is separate from immutable revision and cache state

The framework SHALL distinguish a stable Component coordinate from content-addressed
revisions, source snapshots, packages, mutable aliases, and machine-local materializations.

#### Scenario: Empty user cache resolves a known Component

- **WHEN** a user with an empty cache selects a discoverable Component revision
- **THEN** the materializer faults the exact required objects into that user's cache
- **AND** no cache path or mutable alias changes the revision identity

### Requirement: Capability resolution is explicit and explainable

The resolver SHALL select providers using versioned policy and record every candidate,
constraint, rejection reason, selected provider, typed edge, and tie-break decision.

#### Scenario: Multiple Components provide one capability

- **WHEN** more than one provider satisfies a requirement
- **THEN** the resolver applies the declared policy rather than identifier ordering
- **AND** the composition lock explains the deterministic selection

### Requirement: Discovery does not materialize the ecosystem

The framework SHALL expose a descriptor vocabulary of all discoverable Components while
faulting exact source and intelligence only for the selected closure and explicit
comparison candidates.

#### Scenario: Catalog entry is unavailable

- **WHEN** an entry cannot be materialized under current settings or policy
- **THEN** it remains visible with an availability reason
- **AND** it is not presented as grounded API evidence

### Requirement: Domain versions and revisions remain independently exact

The framework SHALL use strict Semantic Versioning for Component-domain versions,
retain the content identity of every immutable revision, and allow multiple versions
and revisions of one logical coordinate to coexist without an implicit latest choice.

#### Scenario: Two versions share one Component coordinate

- **WHEN** both versions are registered and a consumer supplies an exact Component ref
- **THEN** the resolver returns the matching coordinate, semantic version, and revision
- **AND** a bare-coordinate compatibility lookup fails as ambiguous

### Requirement: Legacy version migration is explicit

The framework SHALL migrate a legacy versioned document only through a registered,
deterministic compatibility reader with enough caller-supplied identity to produce one
exact result.

#### Scenario: Legacy manifest omits semantic version

- **WHEN** no authoritative version binding is supplied
- **THEN** migration fails with a stable ambiguity diagnostic
- **AND** the framework does not invent a version or select a cached latest revision

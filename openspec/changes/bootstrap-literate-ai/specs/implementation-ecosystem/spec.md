## ADDED Requirements

### Requirement: The initial reference kernel is Python

The framework SHALL provide a Python 3.11+ reference distribution named `literate-ai`
with import package `literate_ai`. Its only initially admitted runtime capability SHALL
be an exact CycloneDX JSON-validation dependency isolated behind an adapter; the neutral
domain SHALL NOT import that package.

#### Scenario: User installs the framework bootstrap

- **WHEN** a user installs and imports the Python distribution without Node.js present
- **THEN** the `literate_ai` package imports successfully
- **AND** the Python package manager installs the exact declared CycloneDX dependency
- **AND** no npm package is required at runtime

### Requirement: Runtime dependency admission is explicit

Every proposed runtime dependency SHALL have a reviewed maintenance, security, license,
transitive-size, ownership, architectural-boundary, versioning, and removal record before
admission.

#### Scenario: Adapter needs a third-party library

- **WHEN** an adapter proposes a new library that is unnecessary for the domain kernel
- **THEN** it is isolated in an optional adapter dependency group
- **AND** the domain package does not import it

### Requirement: Node.js tooling is isolated from the framework runtime

The npm-based OpenSpec CLI SHALL live under `tools/openspec/` with a private manifest and
lockfile and SHALL NOT be required to install, import, or run the Python kernel.

#### Scenario: Contributor validates OpenSpec

- **WHEN** a contributor installs repository validation tooling
- **THEN** npm dependencies are installed only beneath `tools/openspec/`
- **AND** the operation does not add Node dependencies to Python package metadata

### Requirement: Portable contracts remain language-neutral

Public schemas, canonical identities, lifecycle semantics, and provider ports SHALL NOT
depend on Python-, Node-, TypeScript-, or Rust-specific serialization or runtime objects.

#### Scenario: Another language implements a provider

- **WHEN** a conforming implementation consumes published JSON Schemas and canonical
  serialization rules
- **THEN** it can exchange identity-equivalent lifecycle objects without embedding Python

### Requirement: UI and native helpers remain optional adapters

TypeScript presentation code and Rust or platform-native security helpers MAY be added
only behind versioned ports and SHALL NOT become dependencies of the domain contracts.

#### Scenario: Native sandbox helper is introduced

- **WHEN** a supported platform requires a native execution helper
- **THEN** the helper implements the sandbox runner port with a reviewed protocol
- **AND** portable workflow and authorization objects retain their existing wire format

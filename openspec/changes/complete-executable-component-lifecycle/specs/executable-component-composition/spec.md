## Purpose

Defines Components as independently executable lifecycle units connected through exact public contracts while preserving each Literate composition idiom distinctly.

> **Implementation status:** capability/interface, composition, Flavor-slot, exact DAG, direct-interface projection, and invalidation contracts are implemented with diamond and scaling conformance tests. Ordinary project/sample adoption and the later package/release stages remain open.

## ADDED Requirements

### Requirement: Component is the executable lifecycle unit
Every generated or acquired Component revision SHALL be independently planable,
generatable when applicable, buildable, testable, acceptable, cacheable, packageable, and
publishable. A root application SHALL compose these units rather than flattening their
private specifications and source into one synthetic Component.

#### Scenario: Diamond application is planned
- **WHEN** root A depends on B and C and both depend on D
- **THEN** the execution graph contains four exact Component nodes and schedules D once

### Requirement: Cross-Component edges use public capability interfaces
Every dependency edge SHALL bind a required capability to one exact provided capability
and a versioned retrievable public interface contract. Private specifications, source,
tests, acceptance oracles, and implementation resources SHALL NOT become cross-Component
authority unless explicitly exported by that interface.

#### Scenario: Structurally similar undeclared interface is present
- **WHEN** a consumer and provider appear compatible but no exact capability contract binds them
- **THEN** composition fails before generation instead of inferring an edge

### Requirement: Composition semantics remain distinct
The framework SHALL represent Component revision lineage, local specification-node parent
or refinement relationships, typed additive Flavor contributions, and capability
dependency composition as separate contracts. It SHALL NOT introduce a generic
inheritance field that merges those semantics.

#### Scenario: Flavor refines a target
- **WHEN** a Flavor adds target-specific requirements to a Component
- **THEN** the effective revision records the typed Flavor contribution without creating a Component parent or capability edge

### Requirement: Executable graphs are exact and acyclic
An execution graph SHALL bind exact node and edge identities, expose deterministic
topological layers, deduplicate shared dependencies, and reject cycles, ambiguous
providers, incompatible interfaces, and unresolved target conflicts before execution.

#### Scenario: Dependency cycle is introduced
- **WHEN** a selected Component graph contains a cycle
- **THEN** planning fails with the exact cycle and no node is generated or built

### Requirement: Public changes invalidate exact consumers
Changing a Component's private implementation authority SHALL not alter another
Component's generation identity. Changing an exported public interface SHALL invalidate
the provider and exactly the direct and transitive consumer actions whose inputs include
that interface.

#### Scenario: Shared leaf changes privately
- **WHEN** D's private specification changes without changing its exported interface
- **THEN** D is replanned while B and C retain the same generation-context identities

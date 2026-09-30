## Purpose

Separates portable human-authored Component intent from deterministic target resolution so exact builds never turn mutable resolution state into authoring authority.

> **Implementation status:** authored intent, deterministic target locks, check/diff/update behavior, stale/forged-lock rejection, and migration staging are implemented. Binding every downstream cache, package, release, and publication surface to the selected lock and the welded-manifest compatibility exit remain open.

## ADDED Requirements

### Requirement: Authored intent is portable and unresolved
A normal Component SHALL use one canonical `component.md`: its frontmatter describes
portable metadata and its Markdown body is the default behavioral specification root.
The document SHALL describe provided and required capabilities, Flavor slots,
entrypoints, resources, skills, workflow, routing, and acceptance intent without embedding
selected provider revisions, concrete Flavor selections, mutable cache locations, or
host-specific tool paths. Additional specification or interface documents SHALL be
admitted only for named domain, module, protocol, or independently consumed public
Component boundaries and SHALL NOT duplicate the default body.

#### Scenario: Portable Component is planned for two targets
- **WHEN** the same authored Component is resolved for two valid targets
- **THEN** its authored identity remains unchanged and each target receives a distinct exact lock

#### Scenario: Simple Component is authored
- **WHEN** a Component has no independently named domain or public interface boundary
- **THEN** its complete authored metadata and behavior live in `component.md` without peer `component.json`, `openspec/`, or acceptance-authority files

### Requirement: Exact resolution is recorded in a deterministic lock
The framework SHALL produce a versioned lock that binds the root Component revision,
complete dependency closure, public interface revisions, selected Flavors and role slots,
resources, toolchains, skills, workflow, routing policy, target, and every content identity
needed to plan execution. Repeating resolution with identical authority SHALL produce
byte-identical lock content regardless of catalog traversal order.

#### Scenario: Resolution is repeated
- **WHEN** unchanged authored intent and catalogs are locked twice for the same target
- **THEN** both lock documents and lock identities are byte-identical

### Requirement: Lock drift fails before execution
Generation, build, test, linking, packaging, and release SHALL require a current lock and
SHALL reject missing, forged, stale, ambiguous, or partially resolved locks before model
egress or host execution.

#### Scenario: Selected dependency content changes
- **WHEN** a selected dependency no longer matches the identity recorded in the lock
- **THEN** planning fails with an exact stale-lock diagnostic before any model or builder runs

### Requirement: Lock changes are reviewable
The framework SHALL expose check and semantic-diff operations that distinguish authored
intent changes, public interface changes, target/Flavor changes, and incidental catalog
changes that do not affect the selected closure.

#### Scenario: Unselected catalog entry changes
- **WHEN** an unselected Component revision is added or modified
- **THEN** lock checking reports no selected-resolution change

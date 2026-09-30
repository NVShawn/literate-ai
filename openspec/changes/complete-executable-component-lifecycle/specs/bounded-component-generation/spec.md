## Purpose

Defines deterministic per-Component generation inputs, bounded direct-interface context, exact invalidation, and evidence for independently generated nodes.

> **Implementation status:** exact per-node plans, bounded context/budgets, complete preparation, source-only scheduling, tree/bundle candidates, provenance, invalidation, and accepted-source reuse are implemented. Durable aggregate receipt integration and ordinary sample/qualification adoption remain open.

## ADDED Requirements

### Requirement: Every generatable node has an exact action plan
The project execution plan SHALL contain one exact generation action for each generatable
Component node. The action SHALL bind its effective specification, lock, Flavors,
resources, skills, workflow, stage routing policy, direct public dependency interfaces,
model capability requirements, tests, acceptance policy, and derivation key.

#### Scenario: Catalog discovery order changes
- **WHEN** the same locked project is planned after catalog enumeration order changes
- **THEN** every action plan, topological layer, and derivation identity remains byte-identical

### Requirement: Model context is local and direct
A Component generation request SHALL contain only that Component's effective local
authority and the public interfaces of its direct dependencies, plus shared framework or
skill material explicitly selected by the action plan. It SHALL exclude private
transitive specifications, source, tests, locks, resources, and acceptance oracles.

#### Scenario: Unrelated private descendant is added
- **WHEN** a private descendant outside a Component's direct public interface closure is added
- **THEN** the Component's prompt bytes, context manifest, and generation identity do not change

### Requirement: Context is manifest-bound and budgeted
Before model invocation the framework SHALL emit a versioned context manifest enumerating
every segment, authority kind, source Component, inclusion reason, identity, visibility,
byte count, token estimate, and configured complexity budget. Exceeding any hard budget
SHALL fail before egress.

#### Scenario: Direct-interface fan-in exceeds policy
- **WHEN** a Component's admitted direct interfaces exceed its byte or token budget
- **THEN** generation is rejected before endpoint selection or model invocation

### Requirement: Per-node tests and acceptance stay independent
Each generated node SHALL run recipe-bound generated tests and separately authored
independent acceptance appropriate to that Component before its output is admitted for a
dependent build or cache publication.

#### Scenario: Dependency generated tests fail
- **WHEN** a dependency node builds but its generated tests fail
- **THEN** its dependents do not build and no accepted cache membership is published for the failed node

### Requirement: Generation evidence is node-specific
Every attempt SHALL retain the exact context manifest, endpoint decision, prompt and
response identities, attempt count, elapsed time, token and cost evidence when available,
output tree identity, test results, acceptance results, and parent-run lineage without
making provider assertions acceptance authority.

#### Scenario: Two nodes use different endpoints
- **WHEN** policy routes two Component actions to different eligible endpoints
- **THEN** each route decision is retained on its own node action and the project receipt orders both

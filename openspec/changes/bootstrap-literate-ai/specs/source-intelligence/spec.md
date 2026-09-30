## ADDED Requirements

### Requirement: Cached source has canonical content identity and verified origin

The source layer SHALL canonicalize each exact tree or aggregate source collection and
record independent origin/signature verification before the source can become usable.

#### Scenario: Authoritative signed source populates an empty cache

- **WHEN** a provider supplies authoritative signed source for a selected revision
- **THEN** the framework materializes it in quarantine, verifies its attestation, and
  stores a canonical snapshot identity
- **AND** repository URL plus mutable revision name is not used as the content digest

### Requirement: Intelligence is bound to exact source

Every knowledge index and evidence artifact SHALL identify the provider/version/config,
exact source snapshot, structured query, returned symbols/relations/paths, bounded
content, diagnostics, and immutable artifact identity.

#### Scenario: Index and source identities disagree

- **WHEN** an intelligence result was built from a different source snapshot
- **THEN** the framework rejects it before contract, plan, or generation use

### Requirement: Evidence is durable and reconstructable

The framework SHALL store structured and bounded raw evidence in immutable artifact
storage and reference those artifact IDs from contracts, plans, files, and provenance.

#### Scenario: Original checkout has been removed

- **WHEN** an auditor reconstructs an accepted generation after source checkout cleanup
- **THEN** all evidence presented to the workflow remains retrievable and digest-verifiable

### Requirement: Selected evidence has protected budget

Prompt construction SHALL prioritize and reserve stage-aware budget for detailed selected
closure evidence before optional global descriptor summaries.

#### Scenario: Catalog vocabulary exceeds the model context

- **WHEN** global Component summaries exceed the remaining context budget
- **THEN** the framework truncates or omits optional summaries
- **AND** required selected-component evidence remains present or the stage fails closed

### Requirement: Generated API use is reconciled with evidence

Validators SHALL derive applicable API-use relationships from proposed source and
reconcile them with evidence-backed source contracts rather than accepting cited IDs as
semantic proof by themselves.

#### Scenario: Generated code calls an unsupported symbol

- **WHEN** proposed source uses a dependency API absent from its evidence-backed contract
- **THEN** validation rejects the proposal or performs bounded evidence-assisted repair

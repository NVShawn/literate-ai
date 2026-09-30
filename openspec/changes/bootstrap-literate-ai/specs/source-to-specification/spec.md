## ADDED Requirements

### Requirement: Source-to-specification is a separate first-class bounded context

The framework SHALL implement inverse specification authoring under
`src/literate_ai/source_to_specification/` and SHALL represent requests, observations,
drafts, coverage, uncertainty, review decisions, and runs as versioned portable objects.

#### Scenario: Existing source has no specifications

- **WHEN** a user starts a bootstrap source-to-specification run for an exact source
  snapshot
- **THEN** the bounded context emits a validated `SpecificationDraftSet`, coverage map,
  uncertainty ledger, and complete provenance
- **AND** it does not mutate the analyzed source tree

#### Scenario: Standalone source has no Component manifest

- **WHEN** a user derives specifications from an exact local, Git, or aggregate source
  descriptor that is not registered as a Component
- **THEN** the run also emits a reviewable `ComponentDefinitionDraft`
- **AND** no pre-existing manifest is required

#### Scenario: Standalone local source lacks accepted origin

- **WHEN** a canonical local snapshot has no signed local actor/machine attestation
  accepted by current trust policy
- **THEN** analysis remains quarantined and its outputs are marked unverified
- **AND** no resulting draft can be promoted to an authoritative specification

### Requirement: Spec-authoring skills are content-pinned composable inputs

The framework SHALL resolve named/versioned `SpecAuthoringSkillSet` objects into ordered
`SpecAuthoringSkill` objects by capability and record their versions, digests,
trust/classification, dependencies, prompts/tools/validators, model requirements,
limitations, and execution provenance.

#### Scenario: Architecture and API skills analyze one Component

- **WHEN** the request selects multiple compatible skills
- **THEN** each skill emits typed evidence-backed observations for its declared facets
- **AND** the run records deterministic skill selection and ordering decisions

#### Scenario: Skills disagree

- **WHEN** two skills produce incompatible observations
- **THEN** the conflict remains in the uncertainty ledger for review
- **AND** synthesis does not silently choose one claim

### Requirement: Observations distinguish behavior from intent

Each observation SHALL classify its claim as observed current behavior, inferred intent,
suspected defect, compatibility quirk, conflict, or unknown and SHALL cite durable exact
source evidence.

#### Scenario: Tests preserve a likely bug

- **WHEN** code and tests consistently exhibit behavior that a skill identifies as a
  suspected defect
- **THEN** the draft does not automatically make that behavior normative
- **AND** a reviewer must explicitly accept compatibility preservation to do so

### Requirement: Draft specifications require explicit acceptance

A generated `SpecificationDraftSet` SHALL NOT become an authoritative
`SpecificationSet` or production generation input until a validated atomic review
decision authorized by a versioned `SpecificationPromotionPolicy` accepts its statements
and complete spec-tree patch. Human approval SHALL be the default, source content SHALL
NOT grant promotion authority, and stricter profiles MAY require named human approvals.

#### Scenario: Draft passes strict OpenSpec validation

- **WHEN** a generated draft is syntactically and structurally valid but has not been
  reviewed
- **THEN** it remains a draft and cannot authorize forward generation as behavioral truth

#### Scenario: Automation lacks a promotion grant

- **WHEN** an automated reviewer has no matching signed grant under promotion policy
- **THEN** it cannot promote the draft regardless of validation or model confidence

### Requirement: Coverage exposes uncertainty instead of hiding it

The framework SHALL map in-scope behavior surfaces to covered, intentionally excluded,
implementation detail, unresolved, conflicting, or unsupported states and SHALL trace
every normative draft statement to evidence.

#### Scenario: Source path lacks sufficient evidence

- **WHEN** the selected skills cannot support a behavioral claim for an in-scope surface
- **THEN** the coverage map marks it unresolved or unsupported
- **AND** the model does not invent a requirement to increase a coverage score

### Requirement: Existing specifications support audit and incremental refresh

The inverse lifecycle SHALL compare accepted specifications with new source snapshot,
skill-set, intelligence/evidence, model-routing/redaction, specification-provider, and
accepted-base identities; invalidate or re-evaluate affected observations; preserve
unaffected reviewed statements; and propose explicit conflicts or changes without
automatic mutation.

#### Scenario: One dependency API behavior changes

- **WHEN** a later source snapshot changes evidence for one accepted scenario
- **THEN** refresh proposes a narrowly attributable diff and impact record
- **AND** unrelated accepted requirements retain their identities

#### Scenario: Skill set changes without source changes

- **WHEN** a skill digest, ordering, or capability selection differs from the prior run
- **THEN** observations produced or depended on by that skill are re-evaluated
- **AND** the old draft is not presented as current merely because source is unchanged

### Requirement: Source and skills remain untrusted inputs

The inverse workflow SHALL enforce prompt-injection isolation, source/model-egress policy,
secret redaction, independent skill trust, and separate authorization before executing
analyzed source.

#### Scenario: Source comment instructs the model to ignore policy

- **WHEN** analyzed source contains instruction-like text
- **THEN** the framework treats it only as attributed source evidence
- **AND** it cannot change skill selection, system policy, review rules, or security gates

#### Scenario: Remote model would receive disallowed source

- **WHEN** model egress or redaction policy does not authorize the proposed evidence
- **THEN** the call is rejected before any source content or secret is transmitted

#### Scenario: Source supplies an untrusted skill

- **WHEN** analyzed source proposes a skill that lacks independent trust/classification
- **THEN** the workflow rejects it unless an explicit policy authorizes that relationship

#### Scenario: Dynamic observation has no exact execution authorization

- **WHEN** a skill requests tests, instrumentation, or other analyzed-source execution
  without a matching `ObservationExecutionAuthorization`
- **THEN** the sandbox runner does not start the source process

### Requirement: Phase 8 is literate-ai-only

Implementation Phase 8 SHALL modify only literate-ai contracts, services, skills,
samples, CLI/API, and documentation and SHALL NOT be treated as a third OVA migration
phase.

#### Scenario: Phase 8 is complete before OVA adopts it

- **WHEN** all source-to-specification gates pass in literate-ai
- **THEN** the feature can release independently
- **AND** OVA behavior and repositories remain unchanged until a separate downstream
  decision is approved

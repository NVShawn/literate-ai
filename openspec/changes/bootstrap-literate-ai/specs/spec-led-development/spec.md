## ADDED Requirements

### Requirement: OpenSpec is the initial specification provider

The framework SHALL load and strictly validate every declared OpenSpec artifact, retain
its complete content and digest, and bind the resulting `SpecificationSet` to generation.

#### Scenario: Specification changes during generation

- **WHEN** any declared specification content differs before acceptance
- **THEN** the run fails with a stable specification-drift error
- **AND** no proposed source becomes the accepted revision

### Requirement: Specification behavior and prompt provenance are distinct

The framework SHALL record original requests as immutable `IntentEvent` objects and
record resulting provider-specific specification changes without embedding raw prompts
as hidden document markers.

#### Scenario: Same prompt receives a revised interpretation

- **WHEN** an accepted interpretation changes while original prompt text is identical
- **THEN** the framework records a new intent decision and specification change
- **AND** the immutable prior event remains auditable

### Requirement: Fast-moving APIs do not leak into durable behavior schemas

Behavioral specifications SHALL express required outcomes while exact symbols and API
surfaces are captured in revision-bound evidence and source contracts.

#### Scenario: Dependency API changes upstream

- **WHEN** a new signed dependency revision changes its API surface
- **THEN** impact analysis refreshes source contracts and affected plans against the new
  source intelligence
- **AND** the behavioral requirement is not silently rewritten to match the dependency

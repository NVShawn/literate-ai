## ADDED Requirements

### Requirement: Authenticity does not imply compile safety

The framework SHALL treat origin/signature verification and behavioral security
classification as independent mandatory decisions over exact source identities.

#### Scenario: Validly signed source contains a prohibited behavior

- **WHEN** authoritative source passes signature verification but violates classification
  policy
- **THEN** the exact revision remains authentic but is classified blocked
- **AND** no normal build authorization is issued

### Requirement: Compilation requires exact short-lived authorization

Every builder SHALL reject execution unless supplied a current `BuildAuthorization`
binding the exact source, dependency closure, findings, policy, builder, toolchain,
sandbox, actor, and allowed outputs.

#### Scenario: Source changes after classification

- **WHEN** any bound source or dependency identity changes
- **THEN** the authorization no longer matches and compilation is denied

### Requirement: Dependency risk propagates monotonically

The effective classification SHALL incorporate dependency findings and SHALL NOT permit a
consumer to erase or lower a dependency restriction without an explicit auditable policy
exception.

#### Scenario: Dependency is blocked

- **WHEN** a required dependency is classified blocked
- **THEN** the consumer's normal build path is blocked with the causal dependency recorded

### Requirement: Maximum-privilege yolo mode is explicit and auditable

The framework SHALL support a non-default `yolo` profile scoped to one exact signed
revision and closure, with authenticated actor, reason, enumerated privileges, expiration,
explicit acknowledgement, revocation, persistent warnings, and downstream provenance.

#### Scenario: User explicitly requests full yolo

- **WHEN** an authorized user acknowledges maximum risk for an exact signed revision and
  enumerated network, filesystem, device, process, secret, compiler, and sandbox privileges
- **THEN** policy may issue an expiring yolo build authorization
- **AND** CLI, API, UI, run, bundle, and publication records display an unmissable warning

#### Scenario: Missing policy attempts to fall back to yolo

- **WHEN** normal classification or configuration is absent, invalid, or incompatible
- **THEN** the framework fails closed
- **AND** yolo is not selected implicitly

### Requirement: Yolo never disables identity or audit

The `yolo` policy SHALL NOT bypass signature verification, exact identity matching,
provenance, audit events, output attestation, expiration, or revocation checks.

#### Scenario: Yolo source signature is invalid

- **WHEN** the requested revision lacks a valid accepted source attestation
- **THEN** no yolo authorization is issued

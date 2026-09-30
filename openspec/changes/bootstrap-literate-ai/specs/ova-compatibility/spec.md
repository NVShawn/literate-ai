## ADDED Requirements

### Requirement: OVA migration has exactly two gated phases

The migration SHALL first establish literate-ai with side-effect-free OVA shadow
comparison while OVA is authoritative, then rebase OVA and retire duplication only after
parity, self-hosting, compatibility, and rollback gates pass.

#### Scenario: Framework contract is not yet proven

- **WHEN** any Phase 1 exit gate is incomplete
- **THEN** OVA remains authoritative and does not delete its lifecycle implementation

### Requirement: Legacy OVA state is translated without destructive conversion

Compatibility readers SHALL preserve `ova.yaml` v2, legacy IDs/digests, settings, caches,
packages, provenance, publication records, and stable errors through explicit mappings
while leaving legacy stores unchanged.

#### Scenario: User has a populated legacy cache

- **WHEN** the framework materializes an object already present in the OVA cache
- **THEN** it verifies and imports or references that object through a recorded identity map
- **AND** it does not modify or delete the legacy cache

### Requirement: OVA policy remains downstream

Omniverse/Isaac/ROS catalogs, foundation and physics rules, platform validators/builders,
NVIDIA model defaults, runtime linking, launch, Litestar UI, and OVA samples SHALL remain
in an OVA adapter or OVA application code.

#### Scenario: OVA selects foundation Components

- **WHEN** OVA policy adds its required foundation or physics providers
- **THEN** the neutral resolver records the explicit policy decision
- **AND** no OVA-specific component name exists in framework core

### Requirement: Cutover is comparable and reversible

Each OVA lifecycle seam SHALL run against frozen fixtures in legacy and framework modes,
compare normalized outputs without dual-writing accepted state, and retain a documented
feature-flag rollback through the compatibility window.

#### Scenario: Framework generation differs unexpectedly

- **WHEN** shadow comparison finds an unexplained closure, evidence, source tree,
  validation, package, provenance, settings, or error difference
- **THEN** the seam remains on the legacy authority until the difference is fixed or an
  explicit migration record is approved

### Requirement: OVA self-hosting is a real successor build

Before duplicate removal, OVA SHALL generate, classify, build, test, launch, package, and
use a successor OVA through the released literate-ai framework from an empty new cache.

#### Scenario: OVA descriptor sample passes but successor does not build

- **WHEN** only the existing composition descriptor behavior succeeds
- **THEN** the Phase 2 self-hosting gate remains incomplete

### Requirement: Final cutover evidence distinguishes proof from authority

The framework SHALL provide a content-addressed final-cutover manifest covering every
lifecycle seam, exact compatibility releases, dependency audit, duplicate retirement,
retained compatibility reads, rollback rehearsal, and observation disposition.

#### Scenario: Owner directs cutover before an elapsed observation window

- **WHEN** the authorized owner directs final cutover without completed observation
- **THEN** the manifest records `user-directed-without-elapsed-window`
- **AND** it emits `production-observation-window-not-completed`
- **AND** it does not contain an observation evidence identity

#### Scenario: Downstream duplicate writer remains

- **WHEN** any seam retains a downstream provider-neutral write implementation
- **THEN** the final-cutover manifest is not ready
- **AND** the retained legacy compatibility reader remains read-only

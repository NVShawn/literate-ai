## ADDED Requirements

### Requirement: Settings are typed, scoped, and provenance-bearing

The framework SHALL resolve settings from framework default, repository, workspace, user,
machine, and run scopes and report the source of every effective value.

#### Scenario: Run override changes a user default

- **WHEN** a permitted one-run override supplies a value also present in user settings
- **THEN** the effective settings show the override value and both provenance records

### Requirement: Secrets are referenced but not stored as settings

Settings schemas SHALL accept secret references and credential availability status but
SHALL NOT persist or render secret values.

#### Scenario: User inspects a model endpoint

- **WHEN** the endpoint requires an API credential
- **THEN** CLI, API, and UI expose only its reference and availability state

### Requirement: All presentations use one settings and actions contract

CLI, headless API, optional generic UI, and downstream product UIs SHALL consume the same
versioned settings registry, validation, migrations, status, and action contracts.

#### Scenario: Settings schema is upgraded

- **WHEN** an older user document is loaded
- **THEN** a pure versioned migration is applied or a stable actionable error is returned

### Requirement: Publication has a dedicated configuration surface

Publication targets, credentials references, target policy, promotion/retention settings,
records, and explicit actions SHALL be separate from local source/cache configuration.

#### Scenario: User opens publication settings

- **WHEN** publication is available in a product UI
- **THEN** it appears as its own section with target policy and lifecycle status
- **AND** changing cache readiness does not implicitly publish content

## ADDED Requirements

### Requirement: Workflows are versioned typed DAGs

The framework SHALL execute arbitrary versioned workflow DAGs with typed immutable stage
inputs/outputs instead of requiring four global model stages.

#### Scenario: Component requires a non-code workflow

- **WHEN** a Component selects a workflow that omits code generation and repair
- **THEN** the engine executes only the stages declared by that workflow
- **AND** lifecycle/provenance semantics remain identical

### Requirement: Runs are durable and transactionally accepted

The framework SHALL separate deterministic run input identity from append-only events,
support idempotent retry/reconciliation, and accept a complete workspace tree through one
atomic revision-reference transition.

#### Scenario: Process fails after validation but before acceptance

- **WHEN** the workflow process restarts with identical input identities
- **THEN** it resumes or safely repeats the interrupted transition
- **AND** no partial file set is visible as an accepted revision

### Requirement: Model endpoints, groups, and routing policies are first class

The framework SHALL version model endpoints and groups and attach capability, privacy,
locality, context, cost, provider, and fallback routing constraints to workflow stages.

#### Scenario: Preferred model is unavailable

- **WHEN** a stage's preferred endpoint is unavailable and fallback is allowed
- **THEN** selection follows the declared group/policy order
- **AND** the selected endpoint and fallback reason are recorded

### Requirement: Model decisions are reproducible run events

Every model call SHALL record run-scoped endpoint/model/revision/group/provider identity,
request/response and template digests, decoding/tool parameters, token/cost accounting,
data-egress policy, retries, failures, and fallback decisions.

#### Scenario: Concurrent Components use one portfolio

- **WHEN** two workflow runs select models concurrently
- **THEN** each run records an isolated decision stream
- **AND** mutable shared selection history cannot change either run's provenance

#### Scenario: Two model group versions share one logical ID

- **WHEN** a stage policy supplies an exact group reference
- **THEN** routing evaluates the endpoints pinned by that group revision
- **AND** a bare-ID policy fails as ambiguous rather than selecting the newest group

### Requirement: Endpoint locality and source egress are enforced

The model adapter SHALL validate transport locality and require explicit policy
authorization before source or evidence crosses a network boundary.

#### Scenario: Endpoint claims local but resolves remote

- **WHEN** a local-only stage selects an endpoint whose validated transport is remote
- **THEN** the model call is rejected before prompt data is sent

### Requirement: Coding-CLI providers use exact bounded transports

The coding-CLI adapter SHALL support `codex`, `claude`, `cursor-agent`, and `opencode`
as explicit model-transport providers. Explicit `CODING_CLI` selection SHALL fail
without fallback when its executable is unavailable; otherwise deterministic `PATH`
discovery SHALL use that provider order. Every provider binding SHALL preserve the
exact executable and content identity, provider-specific model option, bounded process
and output custody, provider-scoped credentials, stable authentication diagnostics,
and an honest non-hermetic isolation profile.

#### Scenario: OpenCode performs a bounded non-interactive generation

- **WHEN** `opencode` is selected for a generation or model-backed JSON task
- **THEN** the adapter first probes the exact pinned executable through its bounded,
  non-model `--pure run --help` surface and requires the pure-plugin, detached-workspace,
  agent, and output-format options used by the generation command
- **AND** the adapter invokes its `run` command in pure-plugin mode against the exact
  LitAI workspace with the built-in build agent and any explicit model selector
- **AND** a LitAI-owned clean configuration allows only workspace read, edit, list,
  glob, and grep tools while denying shell, subagent, external-directory, network,
  language-server, skill, question, and task-list tools
- **AND** project configuration, Claude compatibility inputs, automatic sharing,
  automatic updates, default/external plugins, and automatic LSP downloads cannot alter
  the invocation

#### Scenario: OpenCode lacks the required command capabilities

- **WHEN** the selected OpenCode executable cannot complete the bounded compatibility
  probe or its help surface omits any command option required by LitAI
- **THEN** the adapter fails with `coding_cli.incompatible` before sending a prompt or
  invoking a model provider
- **AND** the diagnostic identifies the required pure non-interactive interface and
  recommends upgrading OpenCode without including raw output or credential values
- **AND** explicit selection never falls through to another provider or weakens the
  pure-plugin isolation profile

#### Scenario: OpenCode relies on its provider permission policy

- **WHEN** the OpenCode transport binding is recorded
- **THEN** its permission configuration is identified as a provider tool policy rather
  than an operating-system filesystem sandbox
- **AND** temporary-workspace custody, output admission, executable drift checks, and
  bounded process limits remain the framework enforcement boundary

#### Scenario: Explicit OpenCode selection is unavailable or unauthenticated

- **WHEN** `CODING_CLI=opencode` names no executable or its selected model provider has
  no usable stored login or admitted environment credential
- **THEN** selection or authentication fails with the corresponding stable coding-CLI
  error and never falls through to another provider
- **AND** no credential value or raw provider output enters framework evidence

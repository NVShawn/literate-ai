## Purpose

Defines one reusable provider-neutral lifecycle for resolving, generating, building, accepting, packaging, and releasing an exact Component project graph.

> **Implementation status:** the Standard core now runs complete prepared nodes through source-only generation/reuse, current indexing/authorization, authorization-bound plan finalization, dependency-safe build, node test, execution, acceptance, project admission, and receipt issuance. `CACHE-250`, link/package/release stages, durable stage interruption/retry, public CLI/default-driver wiring, and ordinary sample/qualification adoption remain open.

## ADDED Requirements

### Requirement: Worker routing preserves one exact Component graph
Worker assignments SHALL bind every and only planned Component revision to an exact
worker and private catalog identity, target profile and locked Flavor selection.
Changing the execution plan, lock or catalog invalidates those assignments before
dispatch. Public routing records SHALL omit private endpoints, workspaces and
credentials. Target eligibility and native ABI compatibility SHALL be checked without
substituting a different target or implementation.

#### Scenario: A worker catalog changes after assignment
- **WHEN** the coordinator revalidates an assignment against a changed catalog
- **THEN** dispatch fails before any node runs or any dependency is imported

### Requirement: Artifact handoff is scoped and verified before consumer execution
The existing lifecycle DAG SHALL determine a consumer's artifact and packaging
predecessors. A handoff SHALL include their exact accepted product identities and
exports, bound to the consumer assignment and routing identity. It SHALL exclude
unrelated siblings and generation-only private implementation products. The importer
SHALL verify each blob's size and digest, retain its unchanged export/ABI/target
metadata, and verify destination bytes before issuing a bound import receipt.
An import receipt alone SHALL NOT assert native compatibility or lifecycle acceptance.

#### Scenario: A predecessor blob is corrupted in transfer
- **WHEN** source or destination bytes differ from the exact declared blob
- **THEN** the importer returns no successful receipt and the consumer cannot execute

#### Scenario: Independent workers feed a consumer
- **WHEN** accepted predecessors on distinct workers satisfy one locked consumer
- **THEN** their verified products become that consumer's inputs through the canonical
  scheduler, preserving result correlation, cancellation and current-plan recovery

### Requirement: Independent acceptance preserves finite numeric product data
Portable and library acceptance SHALL preserve finite fractional arguments and results
through verifier declaration, execution, comparison and evidence identity. Canonical
contract JSON v1 SHALL remain integer-only. Existing integer-only acceptance identities
SHALL remain unchanged. Non-finite product values SHALL fail before actuation or before
accepting an observed result; serializing a number as an application string is not a
substitute for accepting the numeric value.
Comparison SHALL accept exactly equal integer and floating-point JSON values without
rewriting invocation or evidence bytes. It SHALL distinguish booleans from numbers,
preserve the sign of negative zero and introduce no numeric tolerance or rounding.

#### Scenario: Fractional native application result
- **WHEN** a verifier supplies time 1.5 and independently expects value 1.5
- **THEN** the application receives numeric input and acceptance compares numeric output
  with stable evidence that changes if the fractional value changes

#### Scenario: Non-finite oracle input
- **WHEN** a nested argument or expected result contains NaN or infinity
- **THEN** declaration fails before invoking the application or library harness

#### Scenario: Integral result from fractional library inputs
- **WHEN** a real library receives 0.75 and 0.25, returns JSON 1 and the oracle expects 1.0
- **THEN** acceptance succeeds and retains the actual JSON 1 bytes and identity
- **AND** reopening validates those bytes and their exact numeric equality to the oracle
- **AND** a boolean result or a neighboring floating-point value is rejected

#### Scenario: Reopen retained fractional library acceptance
- **WHEN** a library result of 1.5 and its exact verifier records are retained
- **THEN** offline reopening validates the same numeric case and result bytes without
  invoking the library or relaxing canonical contract JSON v1

### Requirement: Standard lifecycle executes the locked graph
The standard project lifecycle SHALL validate the project and lock, create the exact
action DAG, acquire or generate each node, validate and classify source, authorize fixed
build actions, build dependency-first, execute generated tests and independent acceptance,
admit accepted workspaces, link and package roots, run integration acceptance, and emit
release evidence in that order.

#### Scenario: Ordinary project is rebuilt
- **WHEN** a valid locked project invokes the standard lifecycle
- **THEN** every configured phase runs through the public lifecycle service without a project-specific orchestration program

### Requirement: Scheduling is dependency-safe and resumable
The lifecycle SHALL schedule deterministic topological layers, never expose an unaccepted
dependency artifact to a consumer, persist identity-bound stage state, and resume safely
after interruption without repeating an accepted action unnecessarily.

#### Scenario: Process stops after one layer
- **WHEN** the lifecycle restarts with unchanged exact inputs
- **THEN** it verifies and reuses accepted completed nodes before continuing the next layer

### Requirement: Failure preserves independent accepted work
A failed node SHALL cancel or block its dependents while leaving unrelated accepted nodes,
the previous current project receipt, and previous qualified releases unchanged. Retry
SHALL create a new attempt or run lineage rather than rewriting failed evidence.

#### Scenario: One parallel branch fails
- **WHEN** one node in a topological layer fails acceptance and another independent node succeeds
- **THEN** the successful node remains reusable and no dependent of the failed node executes

### Requirement: Cache membership is exact per Component action
Generation, source, build, and artifact caches SHALL use predictable requested keys and
complete candidate identities scoped to exact Component actions. Only final accepted
outputs SHALL become reusable membership, and every hit SHALL be revalidated against the
current lock, policy, target, and acceptance requirements.

#### Scenario: Mixed cache hit and miss occurs
- **WHEN** a shared dependency is a valid hit and the root action is a miss
- **THEN** the dependency is verified and reused while only the root is generated and built

### Requirement: Public services own lifecycle behavior
Project validation, locking, planning, recipe construction, lifecycle execution, status,
events, cancellation, artifact retrieval, and release inspection SHALL be available
through provider-neutral public application services. Command-line tools SHALL delegate
to those services. Content-pinned external lifecycle drivers MAY remain an explicitly
trusted advanced option but SHALL NOT be required for a normally initialized project.

#### Scenario: Graphical client rebuilds a project
- **WHEN** a client invokes the public service with the same locked authority as the CLI
- **THEN** both receive identity-equivalent plans, events, results, and receipts

### Requirement: Aggregate receipts match lifecycle membership
The current project receipt SHALL bind the exact lock, action DAG, node results, cache
membership, generated and independent tests, build/link/package results, artifact graph,
release identity, policy, and tested source state. Failed, skipped, stale, or differently
scoped evidence SHALL NOT replace a qualifying current receipt.

#### Scenario: Later release attempt fails
- **WHEN** a current qualifying receipt exists and a later run fails packaging
- **THEN** the prior receipt remains current and the failed run remains separately inspectable

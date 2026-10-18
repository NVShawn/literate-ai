## Purpose

Defines one reusable provider-neutral lifecycle for resolving, generating, building, accepting, packaging, and releasing an exact Component project graph.

> **Implementation status:** the Standard core now runs complete prepared nodes through source-only generation/reuse, current indexing/authorization, authorization-bound plan finalization, dependency-safe build, node test, execution, acceptance, project admission, and receipt issuance. `CACHE-250`, link/package/release stages, durable stage interruption/retry, public CLI/default-driver wiring, and ordinary sample/qualification adoption remain open.

## ADDED Requirements

### Requirement: Retained SBOM roots preserve original-source provenance
A retained managed CycloneDX graph SHALL bind its distinct retained-project root
to the reviewed plan and complete current Component-lock inventory. Its root SHALL
NOT claim a Component revision. Shared dependencies SHALL retain identities, edge
authority and merged scopes. Conflicts, omitted locks and forged managed claims
SHALL refuse. Graph construction SHALL NOT establish observed native closure or
lifecycle acceptance.

#### Scenario: An ordinary Component receipt attempts to consume retained provenance
- **WHEN** a retained-project BOM is verified against an ordinary Component graph
- **THEN** verification refuses the different managed root and binding authority

#### Scenario: A retained graph traverses the source and resolved BOM transition
- **WHEN** the existing verifier admits the exact source and resolved BOM pair
- **THEN** the retained root binding and locked dependency authority remain unchanged

### Requirement: Retained planning binds actual release authority
The retained release-policy input SHALL be the canonical configured policy used
by the existing release owner. Planning SHALL parse its exact reviewed bytes,
retain the profile's byte identity and validate current repository policy.
An unrelated hashed policy, duplicate-key document, changed policy or conflicting
release branch SHALL refuse. Planning SHALL NOT authorize publication.

#### Scenario: An alternate retained policy would bypass the configured release gate
- **WHEN** a retained manifest declares a different file as its release policy
- **THEN** planning refuses before executing commands or issuing evidence

### Requirement: Retained project tests cover the original reviewed inventory
Retained-project planning SHALL bind the configured project receipt policy,
complete declared test cases, permitted skips, action owners and sealed report
paths. Runtime SHALL derive outcomes from those exact reports. Missing, extra,
duplicate or failed cases and unauthorized skips SHALL prevent acceptance.

#### Scenario: A successful command omits an original test
- **WHEN** a retained test action exits zero but its report omits a reviewed case
- **THEN** the lifecycle rejects the report before independent acceptance

#### Scenario: Original tests report only skips
- **WHEN** permitted skips leave fewer actual passes than the configured minimum
- **THEN** the lifecycle refuses rather than counting skips as passes

### Requirement: Python discovery supports versioned native commands
Without an explicit command pin, Python discovery SHALL inspect each PATH
directory in order, preferring `python3`, then `python`, then numerically descending
`python3.MINOR` commands within that directory. Actual runtime version and identity
checks SHALL remain mandatory. An explicit command SHALL never fall back.

#### Scenario: Native worker has only a versioned Python command
- **WHEN** an aligned worker provides `python3.12` without unversioned aliases
- **THEN** Python discovery probes and admits that actual runtime if it qualifies
- **AND** unrelated command names and invalid explicit pins cannot supply fallback

### Requirement: Retained source has no generation provider
Exact retained-source execution SHALL bind the reviewed input as a distinct,
non-executable source selection. It SHALL NOT discover, authenticate or invoke a
coding agent, select a model, or represent retained bytes as a model response or
previously accepted cache entry. Ordinary generation SHALL retain its provider
requirements.

#### Scenario: Native build worker has no coding agent
- **WHEN** a worker receives exact retained source and its current authorization
- **THEN** provider discovery is unnecessary and the normal post-source gates run
- **AND** stale input, failed tests or missing acceptance prevent a passing receipt

#### Scenario: Retained input attempts to select a model
- **WHEN** retained-source execution supplies a pipeline model override
- **THEN** the incompatible selection is rejected rather than invoking a provider

### Requirement: Retained admission preserves post-source gates
Source classification, immutable custody, native dependency/SBOM checks,
compilation, complete current tests, execution and independent acceptance SHALL
remain mandatory after retained-source admission.

#### Scenario: Retained behavior violates its specification
- **WHEN** retained code passes input review but fails a current test or acceptance
- **THEN** it cannot produce a passing receipt or replace a previous passing receipt

### Requirement: Retained rejection never regenerates unchanged input
Retained-origin rejection SHALL be terminal for that exact input. Automatic
candidate replacement SHALL NOT regenerate or recopy unchanged retained bytes.
Changed retained input requires a new review and authorization. The original
failure evidence SHALL remain the reported rejection.

#### Scenario: Native tests reject exact retained bytes
- **WHEN** a retained candidate fails native behavior tests
- **THEN** the lifecycle reports that test rejection without a replacement attempt
- **AND** the existing source-custody checks remain enforced

### Requirement: Python wheel libraries retain import authority and runtime custody

An explicit Python wheel command profile SHALL admit a library without inventing an
application entrypoint. Its locked commands SHALL preserve the declared module and
symbol surface. Offline dependency resolution, exact retained payload verification,
source and resolved SBOM validation, and isolated Python startup SHALL precede
generated tests and import execution. Ambient site-packages SHALL NOT supply custody.

#### Scenario: Library depends on a pinned Python wheel

- **WHEN** a reviewed Python library selects an explicit wheel profile
- **THEN** generated tests and declared module imports use its exact retained dependencies
- **AND** competing build profiles and multiple application entrypoints remain refused

### Requirement: Independent Python library acceptance reopens packaged dependencies

Independent acceptance of a wheel-backed Python library SHALL execute its exact
verifier-owned harness against the sealed package and retained dependency payload
using isolated Python startup. Relocation SHALL NOT require the original generated
tree, artifact or wheelhouse. Missing or modified output, dependency evidence or
SBOMs SHALL fail before harness execution.

#### Scenario: Relocated package survives source retirement

- **WHEN** the original source, artifact and wheelhouse are removed after packaging
- **THEN** the relocated package still passes the library's independent verifier
- **AND** deleting retained package files causes refusal before execution

### Requirement: FreeBSD SDK admission preserves native target identity

Native SDK layouts and acquisition SHALL support an explicit FreeBSD target,
normalize versioned native host names, and refuse foreign host/architecture
execution. Runtime observation SHALL use FreeBSD ELF loader closure and retain
the FreeBSD OS identity in dependency evidence. ELF format alone SHALL NOT
establish ABI compatibility, consumer acceptance or execution permission.

#### Scenario: A native FreeBSD SDK is relocated

- **WHEN** an authorized SDK is built, captured and relocated on its selected FreeBSD host
- **THEN** runtime observation binds exact native ELF bytes and dependency closure
- **AND** altered bytes, incompatible architecture or a different OS host are refused
- **AND** independent consumer execution remains required for acceptance

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
unrelated siblings and generation-only private implementation products.

#### Scenario: Consumer-scoped artifact handoff

- **WHEN** a consumer receives its accepted predecessor products
- **THEN** the handoff binds the consumer assignment and route and excludes unrelated siblings

### Requirement: Artifact import verifies exact bytes and metadata

The importer
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

#### Scenario: Finite product data declaration

- **WHEN** an oracle declares finite fractional arguments and expected results
- **THEN** acceptance preserves their numeric values and existing integer-only contract identities

### Requirement: Product numeric comparison preserves exact distinctions

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

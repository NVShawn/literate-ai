## ADDED Requirements

### Requirement: Flavors are first-class versioned lifecycle objects

The framework SHALL represent target-specific mix-ins as immutable `FlavorDefinition`
and `FlavorRevision` objects that participate in catalogs, vocabulary, caches, packages,
publication, provenance, and refresh independently from base Components.

#### Scenario: CUDA Flavor is published independently

- **WHEN** a compatible CUDA Flavor revision is published after a base Component
- **THEN** the catalog can resolve it without revising or republishing the base Component
- **AND** its source, specification, skill, security, and publication identities remain
  independently auditable

### Requirement: Base Components remain target-neutral

A base Component SHALL declare only abstract Flavor-slot contracts/cardinality when
needed and SHALL NOT embed concrete OS, accelerator, language ecosystem, toolchain,
packaging, or deployment instructions in its base behavioral specification.

#### Scenario: Component supports Windows and Linux

- **WHEN** Windows/MSVC and Linux/clang realizations implement the same base behavior
- **THEN** their platform requirements and acceptance scenarios live in separate Flavors
- **AND** the unchanged base specification applies to both

### Requirement: Target profiles resolve exact Flavor sets

The resolver SHALL transform explicit target constraints and applicable Flavor slots into
a `FlavorResolutionDecision` and canonical `FlavorSetLock` containing every candidate,
rejection/conflict/co-requisite, exact selected revision, policy identity, and ordering.

#### Scenario: Host platform is requested

- **WHEN** a user requests “this machine” as the target
- **THEN** a target-profile provider records the resolved OS/architecture values
- **AND** the host is not an unrecorded implicit input

#### Scenario: Optional CUDA is unavailable

- **WHEN** CUDA is optional and no compatible signed Flavor/toolchain satisfies policy
- **THEN** resolution records that CUDA was not selected and may choose an allowed
  no-accelerator realization
- **AND** the effective revision does not advertise CUDA capability

### Requirement: Flavor contributions are typed and conflict-safe

Flavors SHALL contribute only through typed capability, requirement, specification,
workflow, skill, validator, builder, toolchain, packaging, runtime, and namespaced
extension points with declared merge operators. Raw document patches and silent
last-writer-wins behavior SHALL be rejected.

#### Scenario: Two Flavors select different singleton toolchains

- **WHEN** both contribute non-identical values to one exactly-one toolchain slot
- **THEN** resolution fails with both Flavor identities and the conflicting constraint

### Requirement: Effective revisions preserve base and Flavor identities

Composition SHALL produce an immutable `EffectiveComponentRevision` and
`EffectiveSpecificationSet` referencing the unchanged base revision/specs, exact target
profile, Flavor set, contributions, resolver policy, and canonical digest.

#### Scenario: One base is built for two targets

- **WHEN** Windows/Rust and Linux/Python Flavor sets are selected from the same base
- **THEN** they produce distinct effective revision, run, build, artifact, and cache keys
- **AND** shared base CAS objects are not duplicated or mutated

### Requirement: Base behavior and security are monotonic

A Flavor SHALL NOT remove or weaken base requirements, acceptance contracts, trust,
classification, authorization, audit, or publication policy. A Flavor MAY request
privileges but SHALL NOT grant them or select `yolo`.

#### Scenario: Accelerator Flavor attempts to bypass a correctness test

- **WHEN** the Flavor contradicts or removes a base acceptance contract
- **THEN** effective specification construction fails
- **AND** a new reviewed base revision or explicit compatibility process is required

### Requirement: Language ecosystems are interchangeable realization Flavors

Language ecosystem Flavors SHALL bind implementation-specific workflows, skills,
validators, dependency providers, builders, and packaging while implementing the same
base behavioral contracts and obeying axis cardinality.

#### Scenario: User selects Rust instead of Python

- **WHEN** both Flavors satisfy an exactly-one implementation-language slot
- **THEN** only the selected ecosystem's exact workflow/toolchain/dependencies enter the
  effective revision
- **AND** the base Component identity and behavioral requirements remain unchanged

### Requirement: Source-to-specification separates target observations

Source-to-specification analysis SHALL propose observed target-specific behavior as
evidence-backed `FlavorDraftSet` content rather than silently inserting it into the base
`SpecificationDraftSet`.

#### Scenario: Existing code has Windows-only build logic

- **WHEN** source evidence identifies MSVC commands and Windows service behavior
- **THEN** synthesis proposes a Windows Flavor draft linked to that evidence
- **AND** a reviewer can independently accept, edit, or reject base and Flavor drafts

### Requirement: Flavor preferences have a dedicated settings surface

Target-profile providers, Flavor preferences, availability, conflicts, and resolution
explanations SHALL use typed settings/status contracts separate from portable Component
definitions and machine-local discovery.

#### Scenario: CUDA is present only on one machine

- **WHEN** machine discovery reports a compatible GPU/toolchain
- **THEN** the UI may offer the CUDA Flavor for an explicit target selection
- **AND** it does not edit the Component manifest or silently make CUDA mandatory

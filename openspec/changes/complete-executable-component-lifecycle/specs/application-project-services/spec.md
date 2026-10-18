## Purpose

Provides software-neutral project and application services that higher-level tools can use to author, inspect, build, and release root Components and shared graphs.

> **Implementation status:** root-Component and shared-graph contracts/planning are implemented. The complete structured application service, initialized-project golden path, CLI/default-driver delegation, and source-promotion adoption remain open.

## ADDED Requirements

### Requirement: Applications are root Components
The framework SHALL represent an application as a selected root Component and target lock
over an exact dependency graph. Multiple application roots MAY share Components inside one
project without copying specifications, source authority, resources, or accepted artifacts.

#### Scenario: Two applications share a library
- **WHEN** two root applications select the same exact library action for one target
- **THEN** the lifecycle builds and caches that library once and records both root dependency paths

### Requirement: Release-significant suites are explicit Components
A group that must version, test, package, or release atomically SHALL be represented as a
root distribution Component. Cosmetic folders, tags, navigation order, and other UI-only
organization SHALL remain outside framework authority.

#### Scenario: Application collection is only a UI folder
- **WHEN** applications share no atomic release behavior
- **THEN** the framework does not mint a suite Component or shared release identity for the folder

### Requirement: Project application service is complete and provider-neutral
The framework SHALL expose structured operations to create or open projects, derive and
review specifications, validate, lock, plan, rebuild, release, inspect status and events,
cancel runs, and retrieve specifications, interfaces, resources, artifacts, receipts, and
releases without importing CLI-private helpers or application-specific code.

#### Scenario: Provider-neutral project service access

- **WHEN** an IDE requests project operations
- **THEN** the service exposes the structured operations without CLI-private helpers

### Requirement: Project services preserve canonical Component authoring

Project creation, specification editing, and source-promotion operations SHALL preserve
the canonical `component.md`-first authoring form. They SHALL NOT manufacture parallel
JSON/OpenSpec/acceptance authoring files for a simple Component, and SHALL add another
specification or interface document only when the caller names the independently meaningful
boundary it represents.

#### Scenario: IDE opens a project
- **WHEN** a client requests the project graph through the public service
- **THEN** it receives exact root/shared Component, specification, interface, Flavor, lock, resource, run, and release identities

#### Scenario: IDE creates a simple application Component
- **WHEN** a client supplies portable metadata and one reviewed behavioral specification
- **THEN** the service commits one canonical `component.md` and returns its exact authored identity

### Requirement: Source-derived projects preserve admitted semantics
Source-to-specification promotion SHALL create canonical multi-Component projects with
reviewed node kinds, entrypoints, public interfaces, resource ownership, dependency edges,
target intent, and acceptance boundaries. A library SHALL NOT be promoted as an
application, and a cross-Component edge SHALL NOT be admitted without a complete contract.

#### Scenario: Source-derived library promotion

- **WHEN** reviewed source identifies an importable library
- **THEN** promotion preserves its kind and complete public dependency contracts

### Requirement: Promoted Components retain canonical document boundaries

Each promoted Component SHALL use one `component.md` by default; recovered named public or
domain boundaries MAY be retained as explicit additional documents without recreating the
retired peer-file layout.

#### Scenario: Recovered library lacks an application entrypoint
- **WHEN** reviewed source evidence identifies a node as a library
- **THEN** promotion preserves the library kind and does not fabricate a runnable application entrypoint

### Requirement: Initialized projects use the standard lifecycle
A normally initialized project SHALL include valid standard lifecycle and receipt policy
bindings and SHALL be lockable, planable, and rebuildable without sample-only code or a
hand-authored external driver.

#### Scenario: Fresh project follows the golden path
- **WHEN** a user initializes a project, authors a minimal Component, locks it, and rebuilds it
- **THEN** the standard lifecycle produces a tested accepted result using installed framework services


### Requirement: Parent repository README workflow
The framework SHALL route project initialization, adoption, update and reparenting agent workflows through a reusable parent-README maintenance skill. The helper SHALL read validated direct-parent authority, produce a read-only plan by default, and reconcile descriptive clickable browser links only with apply authorization.

#### Scenario: Authorized workflow follow-up
- **WHEN** a parent-changing workflow succeeds and its README follow-up is authorized
- **THEN** the helper reconciles links from current direct-parent authority

### Requirement: Preserve authored README content and parent link currency
The helper SHALL preserve authored README content, avoid duplicate links, represent every direct parent, and remove its managed attribution when parent authority becomes empty.

#### Scenario: Arbitrary parent changes
- **WHEN** a project's declared parent changes from one repository to another
- **THEN** authorized reconciliation replaces its managed link with the current parent's browser URL and preserves authored content

#### Scenario: Root project and repeated reconciliation
- **WHEN** a project has no declared parent or reconciliation is repeated without changes
- **THEN** the helper invents no parent and performs no unnecessary write

### Requirement: Explicit browser identity for local-only parents
The helper SHALL require an explicit browser URL when the declared repository identity has no derivable network browser location.

#### Scenario: Local parent lacks browser evidence
- **WHEN** the parent identity is a local file URL and no browser override is supplied
- **THEN** the helper refuses without writing a fabricated parent link


### Requirement: Refresh revalidation preserves exact retained custody
Repository refresh MAY reuse previously discovered metadata locations only while rechecking their exact locator bytes, nodes and current configuration. It SHALL revalidate indexes, reference chains, Gitlink authority, registered worktrees, reflogs, source cleanliness and project authority before each custody boundary without repeated discovery of unchanged facts.

#### Scenario: Revalidation inputs drift
- **WHEN** index, reference, worktree registration, Gitlink, repository locator or project authority inputs change after preparation
- **THEN** refresh refuses before mutation and does not treat cached discovery as current evidence

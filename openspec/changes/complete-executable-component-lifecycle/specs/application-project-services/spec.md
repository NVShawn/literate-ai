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

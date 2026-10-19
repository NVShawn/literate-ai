## Purpose

Provides software-neutral project and application services that higher-level tools can use to author, inspect, build, and release root Components and shared graphs.

> **Implementation status:** root-Component and shared-graph contracts/planning are implemented. The complete structured application service, initialized-project golden path, CLI/default-driver delegation, and source-promotion adoption remain open.

## ADDED Requirements

### Requirement: Registered child lifecycle updates coordinate exact parent authority

The framework SHALL provide an explicit reviewed Standard lifecycle rebind for a
registered native child. Its plan SHALL bind the exact parent manifest, registry,
prior child manifest and proposed binding. Apply SHALL revalidate those inputs
under the registry lock and update the child and its registration together.

#### Scenario: Reviewed child framework rebind preserves parent validation
- **WHEN** an operator applies a current registered-child lifecycle rebind plan
- **THEN** the parent registry binds the exact updated child definition
- **AND** existing receipt and accepted-source evidence remain stale until rebuilt

#### Scenario: Uncoordinated child rebind
- **WHEN** a registered child rebind omits its explicit registered parent
- **THEN** the command refuses before mutation

### Requirement: Registered child update failures preserve prior authority

Failed updates SHALL restore the prior child and registration bytes. Unsafe paths,
stale plans and changed metadata SHALL refuse. Arbitrary edited child files SHALL
NOT refresh a registration. Concurrent changes outside the transaction SHALL
remain untouched and produce an explicit rollback-conflict diagnostic.

#### Scenario: Registry update fails after the child changed
- **WHEN** the registry write or final validation fails without concurrent mutation
- **THEN** both the child manifest and registry retain their exact prior bytes

#### Scenario: Registered metadata changes after review
- **WHEN** the parent, child or registry differs from the reviewed plan
- **THEN** apply refuses without silently updating that changed registration

#### Scenario: Special or indirect metadata
- **WHEN** registered metadata is a symlink, reparse point or special file
- **THEN** the update refuses without following it or waiting for a writer

### Requirement: Coordinated rebinds retain historical metadata custody

A changing rebind SHALL retain the exact before/after child and registry metadata
as identity-bound historical custody. Failure to retain that history SHALL roll
back the update. Historical metadata SHALL NOT make an old receipt or promotion
audit current. An unchanged rebind SHALL preserve metadata without new history.

#### Scenario: Rebind history cannot be retained
- **WHEN** history storage fails or its destination conflicts with prior custody
- **THEN** the child and registration retain their prior bytes and existing history remains untouched

### Requirement: Replacing an unqualified promotion preserves reviewed recovery custody

Source promotion SHALL offer an explicit reviewed replacement path for the exact
unqualified native child registered by an adopted parent. Replacement SHALL bind
the prior registry, child and promotion identities to current signed source-review
evidence. It SHALL retain the prior promotion as recovery evidence, coordinate the
replacement and registration, and roll back a failed update.

#### Scenario: A reviewed import declaration replaces an unqualified child
- **WHEN** a tiny source-derived Python library receives a new current signed review
- **THEN** only its exact reviewed unqualified promotion is replaced and registered
- **AND** the revised child runs its verifier-owned oracle before qualification

### Requirement: Qualified or stale child promotion cannot be replaced

Replacement SHALL refuse qualified children, stale review or source evidence,
changed prior authority, unsafe paths and symlinks. It SHALL NOT overwrite a
qualified child or weaken the independent verifier-owned acceptance oracle.

#### Scenario: Qualified or stale replacement is refused
- **WHEN** the prior child is qualified or replacement custody has drifted
- **THEN** promotion refuses and preserves prior child authority and registration

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


### Requirement: Installed parent README entry point
The parent-README workflow SHALL use an installed `litai project parent-readme` entry point, including isolated framework installations whose modules are unavailable to ambient Python. It SHALL discover the selected canonical project and preserve the helper's read-only default and explicit apply boundary.

#### Scenario: Isolated framework installation
- **WHEN** the installed launcher reconciles a project's parent README
- **THEN** planning and authorized application run in the installed framework runtime without importing the SDK through ambient Python

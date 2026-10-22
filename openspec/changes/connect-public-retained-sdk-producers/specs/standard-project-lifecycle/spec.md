## ADDED Requirements

### Requirement: Public retained SDK product expectations

The public retained lifecycle SHALL support an explicit policy binding the exact
Component lock, selected native SDK recipe, complete SDK snapshot and placement.
It SHALL require fresh live producer custody rather than treating that policy as
admission. Existing run-bound SDK policies SHALL retain their wire identities and
exact producer-run refusal behavior.

#### Scenario: Fresh producer has the same selected product

- **WHEN** an authorized public local retained run rebuilds the exact selected SDK
- **THEN** it SHALL compare the complete selected recipe and snapshot closure
- **AND** it SHALL use fresh producer and command authorization through acceptance
- **AND** a different earlier run identity SHALL confer no authority

#### Scenario: Selection or product changes

- **WHEN** the current lock, selection, snapshot, placement or captured bytes differ
- **THEN** the lifecycle SHALL refuse before admitting consumer execution evidence

### Requirement: Public retained SDK execution authorization

The public retained lifecycle SHALL acquire SDKs only after exact-plan and host
execution authorization. Planning SHALL remain read-only. Current source inspection,
classification, grants, revocation, input custody and independent acceptance SHALL
remain mandatory.

#### Scenario: Authorization is absent

- **WHEN** the exact current plan or host execution is not authorized
- **THEN** no SDK source build SHALL begin

#### Scenario: Authority changes after acquisition

- **WHEN** project authority or a producer's current authorization changes
- **THEN** command or receipt admission SHALL refuse
- **AND** prior source and failure evidence SHALL remain available

### Requirement: Captured retained SDK runtime environments

A retained SDK command MAY use reviewed captured runtime directories. The framework
SHALL bind the exact retained manifest, plan, complete native dependency graph and
command environment. It SHALL preserve SDK-owned image placements, native code
identities and equivalent dependency edges. Caller-supplied loader variables SHALL
confer no authority.

#### Scenario: Reviewed captured runtime dependency is needed

- **WHEN** a retained consumer requires a captured runtime directory and a selected source-built SDK
- **THEN** build, test and independent acceptance SHALL retain the complete captured dependency context in each SDK command grant/evidence
- **AND** current input, native dependency, SDK producer and revocation checks SHALL remain live through cleanup

#### Scenario: Loader context differs from reviewed native inputs

- **WHEN** an ambient override, changed environment, redirected/missing directory, input drift or SDK dependency shadowing is observed
- **THEN** the command or its evidence SHALL refuse
- **AND** no profile contribution SHALL be silently removed to obtain acceptance

#### Scenario: Captured external runtime copy

- **WHEN** an external runtime image is copied to a reviewed captured directory
- **THEN** its native code identity and complete dependency graph SHALL remain equivalent
- **AND** SDK-owned images SHALL retain their reviewed placements
- **AND** the actual captured paths and ownership SHALL remain in command evidence

#### Scenario: Caller mapping changes during authorization

- **WHEN** authorization mutates a caller-owned argv or environment mapping
- **THEN** the process SHALL use the SDK scope's immutable approved values or refuse
- **AND** a changed outer command context SHALL prevent retained admission

### Requirement: Fresh bounded installed-distribution inventory

Installed wheel observation SHALL read a bounded current RECORD inventory and
verify each selected member from its actual bytes. Inventory enumeration SHALL
NOT silently omit a missing recorded member. Avoiding preliminary existence
scans SHALL preserve canonical member identities, non-editable selection, current
source and revocation checks, and existing non-RECORD distribution support.


#### Scenario: Recorded member disappears

- **WHEN** an installed wheel records a selected member that is missing or unavailable
- **THEN** distribution observation SHALL refuse
- **AND** the member SHALL NOT silently disappear from the observed inventory

#### Scenario: Inventory contains malformed or excessive rows

- **WHEN** the current RECORD is malformed or exceeds inventory bounds
- **THEN** observation SHALL refuse before returning a trusted distribution

#### Scenario: Repeated observation after member mutation

- **WHEN** selected member bytes change between observations
- **THEN** the next observation SHALL reflect the changed bytes or refuse
- **AND** an earlier successful observation SHALL confer no cached authority

### Requirement: Stable descriptor-bound member reads

Selected members SHALL remain regular files bound to their opened descriptors
through observation. Member reads SHALL verify descriptor and named-path
stability and SHALL NOT reuse member observations across calls.

#### Scenario: Payload path redirects during open

- **WHEN** a recorded member becomes a symlink at the open boundary
- **THEN** observation SHALL refuse, even if its target preserves the earlier inode
- **AND** refusal SHALL also hold on platforms without a no-follow open flag

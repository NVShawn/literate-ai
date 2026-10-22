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

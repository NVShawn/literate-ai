## ADDED Requirements

### Requirement: Retained secondary identities bind exact current accounts
An explicitly reviewed secondary test identity SHALL bind its account name,
nonroot numeric UID, nonzero primary GID and complete nonzero numeric group set.
The execution owner SHALL require current forward and reverse account mappings
and group membership. Unknown, unsupported or changing observations SHALL refuse.
The observation SHALL NOT create accounts or grant workspace access.

#### Scenario: The account name is reassigned between reviewed and actual execution
- **WHEN** a reviewed secondary identity resolves to a different UID or reverse name
- **THEN** execution refuses before granting permissions

#### Scenario: Membership changes while resolving the identity
- **WHEN** repeated group or account observations differ
- **THEN** currentness refuses rather than accepting the earlier mapping

### Requirement: Secondary access requires live scoped provisioning authority
Secondary retained workspace access SHALL require worker/profile/action-bound
reviewed scopes and a live disposable allocation capability issued by its trusted
provisioning owner. Parsed configuration alone SHALL NOT grant that capability.
Permissions SHALL preserve private defaults, deny unrelated and newly created
resources, retain no-follow custody and restore exact previous permissions on
every exit. Drift or incomplete cleanup SHALL prevent lifecycle acceptance.

#### Scenario: A configured worker claims to be disposable without an allocation
- **WHEN** a secondary access policy has no current provisioner-owned allocation
- **THEN** the workspace refuses access without changing its private permissions

#### Scenario: A privileged test passes while a required unprivileged test skips
- **WHEN** the original reviewed inventory requires both identities
- **THEN** the lifecycle refuses incomplete coverage and issues no passing receipt

### Requirement: Reviewed views are part of the exact profile identity
An optional secondary access policy SHALL bind the exact worker and account,
eligible test actions, disjoint named read-only input/export prefixes and fresh
writable fixture names. Export prefixes SHALL have declared build producers.
The policy SHALL participate in the profile and plan identities. Absent policy
SHALL preserve the existing profile serialization.

#### Scenario: A view prefix or secondary group changes
- **WHEN** reviewed view scope or numeric group membership changes
- **THEN** the profile identity and required plan authorization change

#### Scenario: A profile selects a fixture name that aliases a read-only view
- **WHEN** the delegated namespace contains a duplicate view or fixture name
- **THEN** profile construction refuses before creating or granting resources

### Requirement: View declarations cannot supply execution authority
Secondary access policy SHALL NOT contain provisioning authority or select an
observer endpoint. Explicit malformed policy and execution without an enforcing
provisioning owner SHALL refuse. Manifest binding SHALL require a declared
directory and reject aliases that leave the selected view at any path step.

#### Scenario: A relative alias escapes and returns to the selected directory
- **WHEN** normalization ends inside the view after traversal above its root
- **THEN** manifest binding refuses rather than treating the alias as internal

### Requirement: Provisioning inspection is fresh and separate from allocation
An optional private operator inspection command SHALL inspect the actual live
allocation using a fresh challenge bound to worker, plan and policy. Cached
responses, changed instances or workspace prefixes, insufficient lifetime and
configuration drift SHALL revoke the owner. The framework SHALL NOT send
inspection requests to a legacy allocation command. Parsed policy alone SHALL
NOT issue this non-deserializable live owner.

#### Scenario: A successful response is replayed for another check
- **WHEN** the inspector returns the previous request identity
- **THEN** the live owner refuses the response and remains revoked

#### Scenario: An existing provisioner has no inspection command
- **WHEN** secondary access requests live allocation custody
- **THEN** inspection refuses without invoking the allocation command

### Requirement: ACL projection preserves other principals and held inode custody
Granting a named secondary UID SHALL NOT unmask permissions for another user or
group. The backend SHALL retain exact previous access/default ACLs and restore
them after commands, exceptions and entry-time permission drift. A reused
descriptor number SHALL NOT cause mutation or closure of another inode. This
backend SHALL NOT substitute for path/ancestor or allocation authority.

#### Scenario: A secondary execute grant widens the POSIX ACL mask
- **WHEN** another named user's execute bit was masked before the grant
- **THEN** that user's effective execute permission remains denied

#### Scenario: Another inode replaces the descriptor number before restoration
- **WHEN** descriptor identity differs from the retained resource
- **THEN** cleanup refuses and neither changes nor closes the other inode

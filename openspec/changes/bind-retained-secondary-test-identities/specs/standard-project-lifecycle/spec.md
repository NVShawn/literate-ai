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

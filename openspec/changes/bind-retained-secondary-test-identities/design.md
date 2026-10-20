# Secondary identity custody

The reviewed identity contains the account name, numeric UID, primary GID and
complete sorted group set including the primary group. Root IDs, privileged
group zero, booleans, duplicate groups and malformed names refuse. The POSIX
adapter observes both name-to-ID and ID-to-name mappings and supplementary
groups twice. It never creates or edits accounts. Unsupported platforms refuse.
This observation is a prerequisite and cannot grant resource access.

The remaining integration must bind a reviewed policy to worker, profile and
action plus a live allocation capability issued by the provisioning owner.
Deserialized claims cannot supply that capability. Resource custody must anchor
no-follow descriptors, limit traversal/read/execute and fixture writes, deny
unselected siblings and new children, observe identity and permission drift,
and restore exact pre-existing ACLs on every exit. Native capability probes
show feasibility but are not the framework permission implementation.

Admission must retain grant/currentness/cleanup observations with command and
completion evidence. Incomplete cleanup or required root/unprivileged cases
must prevent acceptance. Existing profiles without this explicit policy retain
their previous serialization and private workspace behavior.

## Native descriptor backend

The FreeBSD adapter uses libc's descriptor ACL APIs on duplicated, non-inheritable
handles. Custody requires existing root-owned regular files with one link or
root-owned directories. POSIX access/default ACLs are captured and restored
exactly; NFSv4 or unsupported filesystems refuse. Permission drift, command
exceptions and failed context entry still trigger restoration. If a descriptor
number was reused for another inode, cleanup refuses without changing or closing
that other inode. The caller must separately own current path/ancestor checks,
resource budgets, reviewed identity and live allocation authority.

A POSIX ACL mask applies to every named user and group entry. Before widening it
for the selected UID, projection clamps all existing affected entries to their
previous effective permissions. Other principals therefore gain no access from
the wider mask. Native execution fixtures verify a previously masked executable
stays denied to the existing user while the selected UID can execute it.

The actual backend also runs all 24 original parser/utility cases in the
source-level disposable guest probe, with no skips, exact pre-existing ACL
restoration, no remaining fixture children, unchanged accounts/payload and all
owned descriptors closed. This is not installed lifecycle integration, SDK
admission, independent acceptance or per-unit package qualification.

## Selected integration direction: declared read-only resource views

The complete captured SDK has 352 directories and 28 aliases. A source-level
native probe now mounts that SDK and the retained products as two read-only,
nosuid nullfs views and runs all 24 exact original cases with no skips. Only the
fresh view parent and fixture directory need permission changes. The original
private directories remain outside the delegated view. This is the selected
integration direction; the per-file closed-view experiment remains retained.

The workspace owner must bind declared input/export prefixes, complete internal
alias closure and the secondary account to the reviewed profile and action.
Reserve the view before launcher preparation, verify effective kernel flags
before sharing, and retain actual root/unprivileged dependency observations.
Source-specific launcher preparation must receive explicit view paths. Keep
mounts available while live admission/package consumers need them, then revoke
traversal and clean up through one lifecycle owner on every exit. Failed cleanup
must prevent acceptance. A prototype launcher or successful source probe does
not establish installed lifecycle, SDK-source or per-unit package admission.

Reserved paths in generated test launchers also need cache/relocation treatment:
the existing probe embeds ephemeral view paths. Such a launcher is not proof of
portable cached build products or an installable package. Bind its preparation
explicitly and preserve the distinction between test fixtures and package payload.

The optional profile policy now binds worker identity, exact account, eligible
test actions, named input/export prefixes and fresh fixture names. Names share
one collision-free namespace; same-origin prefixes cannot overlap or alias.
Export prefixes require build producers. Input manifest binding rejects aliases
that leave the selected directory at any intermediate path step. Absent policy
preserves legacy serialization. Both workspace and local orchestration currently
refuse a selected policy before filesystem mutation because the enforcing live
provisioning/view owner is not integrated yet. Twelve focused policy checks pass;
this is a declarative boundary, not successful delegated execution.

Allocation authority comes from a trusted provisioning owner that inspects the
actual allocated instance. Project data cannot select an observation endpoint
or grant authority by setting a disposable flag. The private observation-channel
prototype checks the live supervised QEMU process and devices with fresh
challenges; the reusable worker/provisioning contract still needs integration.
Use the normal mount frontend and verify actual flags: source issue #5 records
that standalone mount_nullfs currently ignores a comma-separated option list.

## Private provisioning inspection owner

The optional `inspection_command` in operator-local provisioner configuration
is separate from the allocation command. The owner executes it on every check
with a fresh nonce and exact worker/plan/policy identities. It requires a live
disposable instance identity, a canonical allocated workspace prefix and enough
remaining lifetime including inspection elapsed time. It pins the instance and
prefix after the first observation. Private configuration is owner-only,
non-symlink, single-link and bounded; current configuration is checked before
and after invocation. Process errors, replay, drift and invalid observations
permanently revoke the owner. Evidence records observation identities and cannot
serve as acceptance or be parsed back into a live capability.

The configured inspector is trusted provisioning code and must inspect the
actual instance, rather than echoing configuration. Real child-process protocol
fixtures are not proof of a native allocation. Wiring this owner to actual
provisioning, view lifetime, workspace execution and receipts remains open.

## Native read-only view owner

`FreeBsdRetainedViewOwner` reserves a fresh private namespace under the allocated
prefix. Original input/output roots must be canonical, disjoint private
descendants. Bounded no-follow directory handles retain path/ancestor identities;
declared input aliases must close inside their selected directory. Input views
mount before launcher preparation, while export views wait for their manifest.
The namespace requires a POSIX-ACL filesystem; unsupported backends refuse.

Only a reviewed test action with every view bound can activate named-account
traversal and fresh fixture writes. Account/allocation currentness, actual mount
flags, source lineage, held ACLs and fixture identities are rechecked. The
workspace must still verify complete contents/inventories around commands.
The backend does not sandbox privileged test code or issue lifecycle receipts.

Cleanup restores the held private view-root inode before attempting unmounts.
It then restores fixture ACLs and closes anchored handles. Busy mounts, nonempty
fixtures or replaced paths prevent cleanup acceptance; failures remain failures
on subsequent close calls. Diagnostic directories remain private. Observations
bind plan, policy and the live allocation observation used for that event.

The first native run exposed that FreeBSD `statvfs().f_fsid` is zero for both the
backing filesystem and nullfs. The corrected owner uses actual vnode device/inode
identity plus kernel read-only/nosuid flags. All six failed-run mounts were
recovered with unchanged source bytes and private roots; the failed log remains
retained. Native filesystem tests use an explicit allocation protocol peer and
do not qualify the actual provisioner or installed workspace integration.

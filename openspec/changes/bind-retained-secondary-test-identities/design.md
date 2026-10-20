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

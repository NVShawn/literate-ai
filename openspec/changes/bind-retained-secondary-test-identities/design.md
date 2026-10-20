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

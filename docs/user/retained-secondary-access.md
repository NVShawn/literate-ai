# Retained tests with a secondary account

Development status: command scoping, cleanup-bound receipt retention and the local
orchestration bridge are implemented. Eleven source-level FreeBSD checks pass
without skips, including real local orchestration, secondary-user access,
independent acceptance, cleanup-bound admission and payload use after run retirement. Installed CLI qualification, a real
provisioning inspector and the complete original-source suite remain open.

## Select reviewed resources

FreeBSD root execution can expose selected read-only directories to an existing
nonroot account for eligible test actions. The profile's optional
`secondary_access` policy binds the worker identity, account name, numeric UID,
primary GID, complete group set, eligible test action IDs, named input/export
directory views and fresh writable fixture names. Changing any of these changes
the reviewed profile and plan identities.

Use these explicit arguments in the reviewed action:

- `{secondary_view:NAME}` for a declared read-only input or export view.
- `{secondary_fixture:NAME}` for a declared fresh writable fixture.
- `{secondary_account}` for the exact reviewed account name.

The ordinary `{input_root}`, `{object_root}` and `{output_root}` bindings remain
private. Input views reserve their paths before build; export views bind after the
final build seals its exports. The workspace grants secondary access only around
eligible test commands. Test launchers must use the reviewed view paths when they
drop privileges. Original and captured child permissions remain intact; an export
directory intended for traversal must already permit the selected account to
traverse it. Read-only views preserve internal relative aliases and require
effective kernel `ro,nosuid` flags.

Original privileged tests remain trusted code. Use an allocation whose trusted
provisioner verifies that the actual machine is disposable. The view owner controls
delegated paths and cleanup; it does not contain arbitrary privileged code.

On FreeBSD, the filesystem containing the provisioner's allocated retained-workspace
prefix must have POSIX.1e ACLs enabled. An NFSv4 ACL brand or the mere presence of
`getfacl`/`setfacl` does not satisfy this requirement. Before capturing inputs or
creating a retained-run root, Literate AI creates a private probe beneath the
allocation prefix and proves that it can apply, read back and exactly restore the
named-user access and default ACL entries used by secondary access. Unsupported
filesystems fail closed with an instruction to enable POSIX.1e ACLs; the preflight
does not mount, remount or otherwise reconfigure the filesystem.

## Configure the operator's inspector

The CLI resolves `worker-provisioner.json` from the operator's configuration root
shown by `litai config paths`. On POSIX, the configuration parent must be private
and owned by the invoking user, and the configuration must be an owned, private,
single-link regular file. Source policy cannot choose this path or an observer
endpoint. Operator context reaches the inspector through its declared environment
bindings; retained command environments keep their existing capture rules.

The existing `urn:literate-ai:schema:v1:worker-provisioner` configuration accepts a
separate `inspection_command` argv. Its implementation must inspect the actual
allocation on every request. Configure it through
`litai worker provisioner configure --file FILE`, review the configuration, then
explicitly enable it with `litai worker provisioner enable`. Configuration starts
disabled. Inspection never invokes the allocation command.

Each inspection request uses
`urn:literate-ai:schema:v1:retained-allocation-inspection-request` and contains a
fresh `nonce`, `worker_identity`, `plan_identity`, `policy_identity` and
`minimum_remaining_seconds`. The response uses
`urn:literate-ai:schema:v1:retained-allocation-inspection-response` and returns:

| Field | Required observation |
| --- | --- |
| `request_identity` | Canonical SHA-256 identity of the complete current request. |
| `allocation_identity` | Stable content identity of the actual owned instance. |
| `workspace_prefix` | Canonical absolute directory within that allocation. |
| `remaining_seconds` | Observed remaining lifetime, sufficient for the requested budget. |
| `disposable` | `true`, established by the trusted provisioner. |

The allocation owner pins the instance and prefix and permanently refuses reuse
after configuration, lifetime, instance, namespace or process failure. Cached
responses cannot satisfy a new challenge. Local orchestration creates a private
run session under the allocated prefix and refuses overlap with retained input
roots before creating caches or sessions.

## Retain the run and close access

Inputs, exports, command streams and independent acceptance observations enter
retained custody while the views remain current. Admission then closes the views,
checks the actual allocation again and binds a canonical secondary-access record
into the completed-run identity. The record includes exact command identities,
allocation inspections, mounted manifest views and eligible activation/revocation
pairs. An observation-only completed run can precede this final record.

Busy mounts, populated fixtures, changed permissions or incomplete cleanup
prevent admission. The exact account is rechecked after private content and
dependency verification, including after cleanup. Cleanup is also attempted when retention fails. Failed runs
retain available command and allocation diagnostics in their private run
directory. A successful admission uses retained custody after the execution tree
is retired. Receipt parsing grants no execution, SDK, package or release authority.
Runs without selected policy preserve their existing wire identities and receipt
roots.

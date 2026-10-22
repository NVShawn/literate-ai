# Stable expectations and fresh producer custody

`literate-ai/retained-project-sdk-selection-policy@1` is distinct from the existing
run-bound policy. Its bindings pin `component_lock_identity`, `selection_identity`,
`snapshot_identity` and a canonical nonoverlapping `prefix`. A selected recipe may
have only one expected snapshot and placement. Selection binds source, target and
recipe authority; the complete snapshot binds file bytes, license/import metadata
and selected topology. Neither contract accepts an admission flag.

The public local runtime validates plan authorization, host acknowledgment, worker,
independent acceptance and current project authority before acquisition. It reads
the exact persisted lock closure through the locked-authority reader and compares
the complete selected recipe set before any SDK build. Production acquisition uses
its existing source inspection, classification, authorization and revocation owners;
its CAS is shared with retained custody so producer and command evidence remain
available. A new owner is retained for every selected lock, including empty owners.
The actual selected snapshot set must match the reviewed policy before commands.

Command authorization is constrained to native SDK execution in the reviewed graph.
Current plan/binding and producer custody remain live through commands, independent
acceptance and final receipt checks. Command failures or late revocation cannot
become a passing observation. SDK source-build failure retains diagnostic metadata
with `admitted: false`. Existing source and workspace cleanup rules remain intact.


## Captured loader context

Retained profiles already project reviewed SDK/toolchain/dependency directories
through the captured runtime environment and observe all native inputs and tools
under that environment. SDK commands reuse that live dependency guard through a
typed retained runtime context. They do not receive a general loader-override
permission. The context binds the manifest, exact retained plan, complete native
observation, captured directory members and actual command environment. Private
acceptance HOME/TMP values are observed as supplied, not silently substituted.

Before and after each command, and during final command-authority checks, the
context revalidates captured inputs and native dependencies. Each selected SDK's
owned image placements, native content/observed shared-cache identities and
dependency edges must agree with its existing producer observation. Captured
copies of external runtime files may relocate only with equivalent code and
complete native edges; their actual paths and ownership remain in the context. Ordered loader-directory
candidates may not shadow those images; declared inode-equivalent hardlinks remain
valid. Ambient import/preload variables, changed directories/files, altered
contexts and live producer revocation remain refusals.

Captured-context commands use `native-sdk-command-execution@4`, embedding the
complete context and binding its identity in the request's command binding.
Existing v2/v3 records and ordinary commands remain unchanged. The retained
lifecycle owns this context; generated-component qualification does not reinterpret
it as a Component command contract. Library-SDK fixture success does not qualify
an operating-system compiler SDK, an installed product or a release.

The retained runner launches the immutable argv/environment returned by the SDK
scope. Authorization-time mutation of the caller's original mapping cannot change
the process request. Later caller-context drift still prevents retained admission.

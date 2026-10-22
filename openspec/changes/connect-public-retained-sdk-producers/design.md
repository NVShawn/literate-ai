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

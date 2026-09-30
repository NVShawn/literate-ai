# Worker storage observation protocol

This implements the storage-collection portion of accepted [ADR 0043](../decisions/0043-worker-resource-health.md).
[WORKER-HEALTH-001](../roadmap/active-work.md#worker-health-001-detect-worker-resource-pressure-and-investigate-bounded-cleanup-candidates)
also owns pressure, workflow admission, investigation and cleanup. Observations do
not authorize execution, allocation or deletion.

## Private receiver selection

`WorkerStorageBindings` binds one exact execution worker, its OS family, four to
sixteen sorted role/path pairs and optional transport configuration into the capacity
policy's private storage-binding identity. Paths must be absolute on that worker;
collection does not create missing paths or substitute their parent directories.

Local workers run a supervised stdlib probe. SSH workers use their configured SSH
transport and an optional private Python executable. A command worker requires an
explicit `WorkerStorageCommand`; its lifecycle dispatcher is never an implicit health
receiver. The command record contains exact argv and optional
`ExecutionWorkerEnvironment` mappings. Its identity includes mapping names and source
variable names, without credential values. Changed commands or mappings require a
new effective policy identity before dispatch.

The command adapter appends `--receive --timeout-ms <budget>` to the configured argv
and invokes it without a shell. The shipped receiver can be selected with:

```text
python -B -m literate_ai.worker_storage_probe
```

An external provider must implement the same operation, route the request to its
exact worker, and return that worker's response. The shipped module measures the
machine where it is executed. It is not a remote scheduler or an authenticated
attestation service. A missing health command reports `unsupported`; a missing
required credential mapping reports `denied` before launch. Lifecycle credentials
and unrelated ambient credentials are not inherited by the health command.

## Request and response

Local private requests use `LITAI_WORKER_STORAGE_REQUEST`; SSH and command requests
use stdin. The request is UTF-8 JSON, limited to 16 KiB. Duplicate or unknown fields
are rejected. Its exact fields are:

| Field | Meaning |
| --- | --- |
| `schema` | `literate-ai/worker-storage-request@1` |
| `worker_id` | Selected portable worker alias |
| `worker_identity` | Exact worker configuration SHA-256 identity |
| `policy_identity` | Effective capacity policy SHA-256 identity |
| `job_identity` | Job SHA-256 identity, or null for inspection |
| `os_family` | `linux`, `macos` or `windows`; must match the receiver before any path measurement |
| `timeout_ms` | Integer probe budget from 1 through 60,000 milliseconds |
| `paths` | Four to sixteen unique, sorted `[role, absolute_path]` pairs |
| `nonce` | Fresh 32-character lowercase hexadecimal nonce |

The receiver validates the request before measurement. The response contains exactly
`schema`, `request_identity`, `os_family` and `samples`; its schema is
`literate-ai/worker-storage-probe@1`. `request_identity` is the SHA-256 digest of the
exact input bytes, including the nonce and complete private context. Samples use the
existing `StorageCapacitySample` contract: portable role and opaque volume aliases,
byte and inode measurements, quota domains, and explicit metric statuses. The
controller requires the expected request digest, platform and exact ordered roles.
It rejects malformed, substituted, extra, duplicate or oversized response fields.

Response stdout is limited to 64 KiB and diagnostic stderr to 4 KiB. Raw commands,
requests, responses and credential-bearing environments are excluded from subprocess
tracing for this operation. Retained observations contain typed metrics and content
identities, without paths, endpoints, arbitrary stderr or credentials. No request
file or persistent receiver service is created.

## Deadlines and failures

The controller supervises input, output and process lifetime. The receiver starts
its own watchdog before reading stdin and retains it through process exit. Once the
request is validated, it enforces the earlier of the command-line ceiling and the
request's budget, measured from receiver start. Slow input cannot reset that budget.

If measurement stalls after request validation, the watchdog emits a request-bound
response whose metrics are `timed-out`. Timeout meaning is carried in the response,
because some Windows SSH shells collapse nonzero child exit codes. A second finite
stop bounds a blocked timeout-response write. If that stop cannot start because
thread capacity is exhausted, the receiver exits immediately. No successful
measurement can be inferred from an absent or incomplete response. Failed observations remain recordable
after expiry; they are not fresh admission evidence.

The pure capacity classifier separately checks worker, policy, job, sample window and
expiry before deciding whether a job may proceed. Unsupported quota measurement
remains explicit. Native SSH measurement and injected blocked-probe checks cover
Linux, macOS and Windows; quota exhaustion, pressure monitoring, dispatch and cleanup
qualification remain owned by WORKER-HEALTH-001.

## Independent quota domains

Each storage sample carries one to eight sorted, unique `quotas` entries. Each entry
has an opaque `domain`, `available_bytes` and `available_inodes`. An empty list cannot
stand for unlimited capacity. This revises an unreleased observation contract;
earlier private development observations must be collected again.

Admission sums concurrent demand once per bound quota domain, keeps one maximum
reserve per domain, and checks byte and inode limits independently. A shared user
limit can overlap separate group limits; free capacity in one cannot satisfy another.
Failed measurements remain explicit and cannot remove that role's demand from a
shared domain. Conflicting measured and not-applicable results require rechecking.

The Linux adapter admits 64-bit x86-64/AArch64 ext filesystems and uses the existing
open directory descriptor for read-only `quotactl_fd` queries. It queries class
applicability before filesystem user/group identities; `ESRCH` from a per-identity
query is unavailable, not proof of disabled quotas. It conservatively includes both
the process filesystem GID and directory GID when they differ. Soft limits are
conservative ceilings without assuming their grace periods survive the job. Byte
limits use the kernel's 1,024-byte units; inode limits remain independent. These
interfaces are defined by the [Linux quota UAPI](https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/quota.h)
and [quotactl_fd reference](https://www.man7.org/linux/man-pages/man2/quotactl_fd.2.html).

No enable, set, sync or mount operation is issued. Active project quotas, XFS,
macOS/APFS, unknown ABIs and unavailable syscalls remain explicitly unsupported or
unavailable. Existing-worker Linux qualification demonstrates disabled-class
applicability; real active-quota exhaustion and additional filesystem/platform
coverage remain required. Fixtures cover user/group byte and inode exhaustion,
partial kernel records, denied queries and overlapping admission domains. Windows
continues to use caller-available capacity with explicit inapplicable inode quotas.

## Public storage inspection

Run `litai worker health --worker-id ID --health-config /private/health.json` with
an existing private worker catalog (or select one with `--worker-config`). The
versioned `literate-ai/private-worker-health@1` file contains `worker_id`, `os_family`,
sorted absolute `paths` pairs, optional `python_executable` and `health_command`
values (null when unused), and a `capacity` object. Capacity contains `roles`,
`write_heavy`, `maximum_age_ms`, `probe_timeout_ms`, `maximum_retries` and
`warning_headroom_bytes`; role fields match `StorageRoleCapacityPolicy`. The shipped
`worker-health-configuration.schema.json` describes this private file.

Every workspace/temp/cache/output role must appear in both paths and capacity.
Use the worker's paths and OS, not controller paths. The CLI derives the binding
identity from the selected worker, paths and receiver; users do not transcribe that
digest. Both input files are guarded across measurement. Duplicate JSON fields,
unknown fields, role mismatches and changed inputs refuse with redacted errors.
Configuration is limited to 64 KiB and the worker catalog to 1 MiB. This command
uses existing local, SSH or explicit command receivers and never creates role paths.

`--json` emits `literate-ai/worker-storage-health-result@1` with `scope: storage`,
a typed observation, an assessment and actionable alerts. Interactive output names
worker/role aliases, measured deficits and next actions. Exit 0 means this storage
assessment permits the requested footprint; 1 means hold and 2 means retry after a
fresh check. Optional unknown metrics remain visible even when the decision permits
work. `--job-identity sha256:...` binds the result to an exact job without authorizing
it. Each invocation inspects once; dispatch owns its future bounded retry state.

An optional `pressure` policy adds a bounded sustained sample window, CPU threshold,
available-memory floor, paging threshold, GPU-memory floor and required/optional
metric flags. Local and SSH workers use the framework receiver; an explicit command
worker must route `--pressure-receive`. Denied, unsupported, stale, malformed and
unreachable pressure evidence stays unknown. High CPU/GPU utilization with progress
is not overload; stalled progress, queue impact, paging or allocation failure changes
classification. The command performs no dispatch, host update, MCP discovery or
implicit health-state write. File debug output and MCP discovery flags refuse. Without an alert-state selection, repeated inspections report current findings
each time. Pressure sampling, automatic investigation and dispatch/polling
use the same application classifier.

## Explicit alert history

Add `--alert-state /private/state/worker-alerts.json` to retain a bounded local
reporting baseline. Its parent must already exist and be free of symlink/reparse
traversal. On POSIX, the state file must belong to the invoking user and exclude
group/other permissions; newly published files use mode 0600. Hardlinked, malformed,
foreign-worker, oversized and changed files refuse. This explicit option updates
the selected state file and a sibling advisory lock; it does not grant cleanup or
execution permission and does not enable general CLI telemetry or MCP writes.

One file holds one worker/policy/job context, at most 80 role/resource records and
64 KiB. History older than 24 hours or from a different policy/job resets visibly.
Foreign-worker state, future history and out-of-order observations refuse. A finite
same-host lock serializes cooperating writers; publication uses an atomic staged
replacement with input, state and directory checks. This is local reporting state,
not distributed coordination or immutable admission evidence.

With persistence selected, JSON retains the complete current `assessment` and
`alerts` snapshot, and adds an `events` array plus `alert_history` metadata.
Consume `events` for deduplicated notifications. Events bind exact observation,
policy and job identities and report initial incidents, escalation, changed
conditions, improvement and recovery. Numeric jitter within the same classification
and reason does not repeat an event. Storage and sustained pressure findings share
that transition history, so unchanged CPU, memory, paging and GPU incidents are also
suppressed and their recovery is explicit. Interactive output reports new events
with measurements, impact and next action, or says there are no new transitions.

A missing measurement becomes unknown and cannot erase a prior incident. Recovery
requires a current healthy finding, including a positively known not-applicable
quota/inode result. Alert suppression never changes the job decision. State update
failure is a visible CLI failure; it never widens a write or removes unrelated data.
Remote collection still keeps this selected reporting state on the controller.

## Workflow admission and polling

Lifecycle `build`, `test`, `run` and package commands accept
`--worker-health-config`. For an explicitly configured worker they collect a fresh
job-identity-bound observation before substantial execution. Critical capacity or
required unknown evidence holds new work; `retry-after-recheck` is retried only up
to the policy bound. `--worker-health-poll-seconds` controls bounded checks while a
remote dispatch remains active. A new critical finding holds only later task-owned
work: it does not cancel the already-running job, kill unrelated processes or reboot.
Failed dispatches recheck the same policy before an allocation retry.

## Cleanup investigation and exact apply

The optional `cleanup` policy names task-owned roots beneath configured storage
bindings, active and completed-use markers, recovery cost and an exact supported
tool argv containing one `{target}` placeholder. Disk warnings and critical findings
automatically scan top-level candidates under entry, depth and time budgets. An
overloaded worker receives a shallower, shorter scan. Links, junctions and reparse
points are not followed. Reports contain opaque target identities and never raw
paths; active or uncertain targets cannot enter an executable proposal.

Local workers scan locally. SSH workers receive a staged stdlib-only scanner bound
to the request digest and configured worker OS. Explicit command workers must route
the same private request when invoked with `--cleanup-investigate`. Remote response
substitution, malformed output, denial and unreachability remain distinct failed
investigation states; the controller never substitutes its own filesystem paths.

`litai worker cleanup plan` recomputes the read-only proposal and explicitly reports
that deletion is unauthorized, using the bound remote investigation when the
selected worker is remote. `litai worker cleanup apply` is local-only and
requires a current `literate-ai/worker-cleanup-authorization@1` document binding the
exact worker, capacity policy, proposal, creation time, complete target-ID set,
`configured-cleanup-tool` operation and expiry. Apply re-scans every target before
calling the configured tool without a shell, then remeasures actual available bytes.
Remote apply refuses rather than acting on controller paths. Administrative access,
release approval and ADR acceptance never substitute for cleanup authorization.

# ADR 0043: Worker resource health uses bounded observations and explicit cleanup authority

- Status: Accepted for 1.1; implementation and qualification pending
- Acceptance: Explicit maintainer approval on 2026-09-16 UTC
- Decision owners: Literate AI maintainers
- GitHub issue: [#440](https://github.com/NVIDIA-dev/literate-ai/issues/440)
- Roadmap owner: WORKER-HEALTH-001 in docs/roadmap/active-work.md

## Context

Worker capability discovery identifies available platforms and tools, but it does
not establish whether a worker has enough space or sustained resource headroom for
a particular job. A workspace may have adequate space while temporary extraction,
package caches or output live on another exhausted or quota-limited volume. High
CPU or GPU utilization can be useful work rather than overload. A failed probe is
not evidence of capacity.

The framework owns worker selection, execution and polling. Health belongs at those
boundaries, with deterministic Python collection and classification and a thin,
shipped agent skill. The design must support Linux, macOS and Windows without a
persistent daemon, private host details in source control, or implied permission to
delete files.

## Decision

### Private policy and observations

Add a versioned worker-health policy and observation contract. Keep the current
hardware inventory contract unchanged. Private worker/job configuration names
workspace, temporary extraction, package/model cache and output roles, expected
peak additional allocation, reserve bytes, minimum free percentage, inode reserve,
probe deadlines, sampling window and supported recovery commands. Resolve each role
on its actual worker; never infer remote paths from the controller's filesystem.

Bind every observation to worker alias, job/dispatch identity when applicable,
policy identity, measurement time, sample window and expiry. Return resource-role
aliases and measurements rather than hostnames, credentials, process arguments or
raw private paths. Keep path mappings and bounded alert state in private user/job
state. Health observations do not become authored hardware minima or immutable
proof that future execution is safe.

Measure available bytes for the invoking identity, total bytes and percentage,
quota headroom and available inodes where supported. Account for roles sharing one
volume so their concurrent peak demand is combined and free capacity is not counted
twice. Compare effective available capacity against the job's additional footprint
plus reserve. Windows quota-aware availability and Unix user-available blocks must
not be confused with administrator-visible free space. Unknown quota or inode data
is explicit, with applicability determined by the platform and policy.

Collect bounded CPU, available memory, paging, progress/queue and applicable GPU
utilization/VRAM observations using platform adapters. Every probe is deadline- and
output-bounded, including filesystem operations that may block on remote mounts.
Unsupported, denied, stale, unreachable and malformed observations retain distinct
reasons and cannot yield an overall healthy result. Unavailable optional metrics
need not prohibit otherwise justified work; missing policy-required capacity does.

### Classification and dispatch

Expose healthy, warning, critical and unknown resource findings, and a separate
job decision: proceed, hold or retry-after-recheck. Reasons name the affected role,
measured deficit, job impact and next action. A high utilization sample alone is
not overload: classify sustained pressure across the configured window together
with stalled progress, paging, allocation failures or queue impact. Classify GPU
OOM and disk-full failures explicitly even when the next sampled utilization is low.

Use the same classifier before substantial allocation, during existing active-job
polling, and after resource-related failures. Critical capacity deficits hold new
write-heavy dispatches. Missing required measurements also hold them after bounded
probe retries. Do not cancel a healthy active job merely because another job is
held. Reduce only concurrency owned and authorized by the current task, or select an
already-authorized compatible worker whose fresh observations satisfy the same
requirements. Never kill unrelated processes or reboot to repair capacity.

Every warning/critical/unknown transition produces a visible structured CLI event
and a concise human-readable alert with worker alias, measurements, impact and next
action. Deduplicate unchanged findings by worker, resource role and classification;
report escalation and recovery. Persist only bounded private state so repeated CLI
polls do not repeat unchanged alerts. No Slack/email delivery is implied.

### Automatic read-only investigation

Disk incidents automatically invoke a bounded investigator over configured,
task-owned cache and temporary locations. Traversal has entry, depth, byte-accounting
and time budgets. Prefer shallow investigation during overload. Reject symlinks,
junctions and reparse traversal; validate ancestors as well as leaves, and report
partial scans and inaccessible locations explicitly.

A candidate report binds an exact target identity to measured size, ownership
source, active-use evidence, uncertainty and recovery cost. An age or directory
name is insufficient ownership evidence. Active environments, SDKs/drivers, source
trees, datasets, checkpoints and unrelated users' files are protected. Unknown
active-use state remains protected. Shared objects are reclaimable only through a
supported ownership-aware cleanup tool that proves they are unreferenced.

The investigator never deletes. Its reports are proposals rather than permission.
Bounded capacity and candidate reports may be retained for diagnosis; they must not
contain arbitrary file contents, private command lines or credentials.

### Cleanup and retry

Cleanup consumes current explicit authorization for exact proposed targets and the
supported operation. Bind authorization to worker, target identities, policy and
expiry. Revalidate ownership, active use and link/reparse boundaries immediately
before execution; stale, changed or ambiguous targets require a new proposal. Do
not convert administrative credentials, release authorization or acceptance of this
ADR into cleanup approval.

Invoke supported cache/package cleanup tools with bounded execution and retained
results. Remeasure the affected volumes afterward and report actual capacity
recovered, removals and recovery cost; estimated candidate size is not proof of
freed capacity. Retry a failed allocation only after fresh measurements satisfy its
footprint and reserve. Bound retries and stop when the deficit remains unresolved.
Cleanup failures remain visible and never trigger an unapproved wider deletion.

### CLI and shipped skill

Extend the existing `litai worker` command family for health inspection,
investigation and explicit cleanup plan/apply. Preflight, selection and polling call
the same Python implementation rather than shell snippets or agent interpretation.
The framework-owned worker-health skill routes to those commands and explains only
judgment, evidence and authorization boundaries. Ship it through the normal
catalog/wheel paths and reference it from worker configuration and execution flows.
No personal Codex-only installation, daemon, service account or new external
notification channel is required.

## Qualification and rollout

Keep WORKER-HEALTH-001 open until all of these pass:

- Deterministic portable fixtures cover healthy capacity, low bytes/percentage,
  footprint plus reserve, quota/inode exhaustion, shared-volume accounting and a
  full temporary volume despite a healthy workspace.
- Fixtures distinguish sustained CPU/memory/paging pressure, ordinary high GPU
  utilization, GPU OOM and denied, unavailable, malformed or stale probes.
- Real controlled local and remote worker paths exercise bounded collection and
  preflight/polling integration on Linux, macOS and Windows. Unknown results are
  visible and policy-required missing capacity cannot admit a write-heavy job.
- A disk incident automatically investigates and alerts; unchanged alerts dedupe,
  escalation/recovery remain visible, and retained state is bounded and private.
- Scanner tests cover deadlines, partial results, races, links/junctions/reparse
  points, ownership uncertainty, active-use uncertainty and protected data.
- Cleanup tests prove refusal without exact current authorization, refusal after
  target changes, supported tool invocation, actual post-cleanup measurements and
  bounded retry decisions. Use disposable fixtures, not live user cleanup.
- Catalog, workflow, installed-wheel and full hosted qualification prove the same
  public behavior without private identities, credentials or process arguments.

## Consequences

Health is a time-bounded scheduling input, not a security sandbox or a guarantee
against later resource exhaustion. Finite probes and scans may be incomplete; that
uncertainty is part of the result. Shared storage, quotas and changing workloads
require conservative accounting and fresh admission measurements.

The maintainer accepted this cross-platform policy and workflow design for 1.1.
Implementation and qualification may proceed. Acceptance does not relax existing containment,
worker identity, lifecycle evidence or generated-host execution requirements.

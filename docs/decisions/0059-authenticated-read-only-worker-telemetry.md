# ADR 0059: Observe bounded authenticated worker telemetry

- Status: Accepted
- Date: 2026-11-03
- Acceptance: The directing user explicitly accepted ADRs 0056–0059 for 1.4 on 2026-11-03 ("Accept all four ADRs").
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0
- Roadmap: [WORKER-TELEMETRY-001](../roadmap/active-work.md#worker-telemetry-001-expose-bounded-authenticated-read-only-worker-telemetry)
- Issue: [#280](https://github.com/jordanhubbard/literate-ai/issues/280)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

`adapters/worker_pressure.py` already samples bounded local or remote pressure
windows over configured SSH/command routes. `worker_pressure_probe.py` exposes
CPU, available memory, paging and supported GPU observations; progress and queue
depth currently report unavailable. Pressure requests bind a nonce-derived
request identity, OS family, sample count and interval. These are host-capacity
observations, not attributed job network/disk counters or an activity feed.

`action_worker.py`, `adapters/action_transport.py` and
`adapters/action_command_dispatch.py` provide challenged capability descriptions,
bounded request/response framing and authenticated action result attestations.
They are discrete action boundaries, not evidence that ongoing stage/progress or
network I/O is already streamed. This is a source audit; no live worker telemetry
qualification is claimed. Issue #280 requests the missing safe observational feed,
while downstream activity-monitor rendering remains consumer-owned.

## Decision

### Add an observation protocol, not a job control path

Introduce explicit versioned telemetry request/response and observation schemas
over the existing configured authenticated worker transport. Preserve legacy
action, pressure and descriptor wire formats. Negotiate telemetry support
explicitly; an older worker reports unsupported, never fabricated zero activity.
Do not open a listener, discover credentials, bypass a configured route or launch
a job to satisfy an observation request.

An authorized query selects the exact configured worker/registration and logical
action identity, or an explicitly permitted aggregate scope. It does not accept
an arbitrary PID, filesystem path, command, environment or executable selector.
Join action identities only to the worker's actual operation-local process/action
owner, including its process-start/epoch and reservation identity. PID reuse,
stale records, unknown actions or foreign ownership cannot select another job.
Parsing a historical record never creates that live owner.

Bind each response to the requesting controller's authorized observation scope,
current registration/catalog/worker identities, request nonce/identity, expiry,
observation epoch and sequence. Reuse actual receiver authentication/attestation
and replay refusal owners where available; SSH encryption or a saved success
record alone is not an action-owner grant. Connector authentication stays private
and uses the same selected route. Refuse wrong-scope, stale, unattested or mismatched
responses before exposing them as trusted worker observations.

Telemetry permission is observational. Its handlers cannot invoke dispatch,
cancel, renew a lease, send a signal, change priorities/limits or job environment,
alter clocks, publish job results or grant execution. Do not pass an action
capability to a telemetry consumer as observation authorization. Collector timeout
or cancellation stops only the owned observation work, never the selected job.

### Defined fields and honest attribution

Provide worker and logical action identities, sanitized stage, state, progress,
network bytes in/out, disk bytes read/written, CPU and memory measurements. Define
units, monotonic counter epochs, observation time and freshness, and distinguish
instantaneous gauges from cumulative counters. A reset or owner replacement
starts a new epoch; do not produce negative deltas or merge unrelated jobs.

The actual lifecycle owner supplies stage/state transitions and progress only
when it has defined completed/total work units. Opaque build tools and model calls
may have indeterminate progress; elapsed time is not percentage complete. An
observation cannot declare acceptance or successful job completion in place of
the existing authenticated final-result owner. Missing or stale terminal evidence
remains unknown rather than inferred success.

Every metric declares its measurement scope and status: job/process subtree,
container, worker host or unavailable/unsupported/denied. A host NIC counter is
not job network I/O; shared disk/cache traffic is not attributable to an action
without a verified native attribution mechanism. Use null with a typed reason
when a backend cannot provide the requested metric. Keep aggregate observations
explicit and separately authorized; do not enumerate another user's jobs.

Use a closed safe vocabulary and bounded numeric/identity fields. Exclude command
lines, arguments, environment, credentials, stdout/stderr, arbitrary exception
text and undeclared paths. Stage labels are owner-defined protocol values, not
copied log lines or source filenames. Display names are independently declared
safe metadata; a generic redactor cannot make unrestricted process text safe.

### Bounded polling feed and isolation

Start with finite batched polling over the existing request/response transport.
One request selects at most 64 authorized actions, returns at most one current
sample per action and completes within a finite maximum 30-second deadline.
Bound each sample to 16 KiB and the complete response to 1 MiB, including framing;
check the aggregate before retaining or decoding another record. Refuse a batch
that cannot fit rather than silently omit requested actions.

Declare a minimum one-second polling interval per worker/controller scope and
coalesce equivalent simultaneous queries within that scope. Bound concurrent
collectors and retained samples, with no unbounded queue or growing history file.
Rate excess refuses or returns an explicitly identified fresh cached observation;
it never delays the job or extends an execution grant. Consumers own their bounded
display history. If a later version adds windowed streaming, require a separately
reviewed finite window, frame/byte/rate budget and explicit sequence-gap semantics.

Collect from known bounded owner records and native counter APIs, not repeated
unbounded process/tree scans or unrestricted log parsing. Any sampling bookkeeping
belongs to a separate bounded observational namespace. Collection must not take
the job's mutation lock or modify its source, CAS, reservation, result or lifetime.
A slow reader, unavailable native counter, transport failure or stopped consumer
cannot block action execution or final result publication.

### Acceptance and compatibility

Drive real authorized actions through the existing installed worker transport and
observe their actual stage/state transitions and supported network/disk/CPU/memory
metrics. Include both SSH and an explicitly configured command-addressed route,
with controller-side decoding and a downstream-shaped consumer. Keep monitor UI
implementation and its qualification in the downstream consumer.

Qualify native platform backends honestly, recording unavailable metrics and their
scope rather than inventing measurements. Require actual measured job-attribution
positives before labeling counters as job I/O. Prove wrong registration/action,
stale/replayed frames, foreign-process selection, unsupported workers, counter
reset, secret-shaped metadata, output/rate/concurrency limits, slow readers and
collector cancellation. Show that a job's bytes, reservation, deadline, result and
cleanup remain unchanged by observation success or failure.

Retain existing pressure/action positives and refusals, independent review,
non-editable installed journeys, native worker evidence and exact integrated
release gates. An activity display or passing pressure probe alone cannot complete
#280, #235 or the broader release qualification.

## Consequences

Consumers gain a documented bounded feed whose measurements and unsupported
values are interpretable. Receiver-side action attribution, authentication,
rate enforcement and native counters require implementation and actual evidence.
All #280 requirements remain in 1.4. The directing user accepted this design under
ADR 0006 on 2026-11-03; implementation and qualification remain open. It grants no
job-control authority.

# ADR 0013: Refine Abstract Intent into Per-Concern Specification Authority Before Locking

- Status: Proposed
- Date: 2026-08-23
- Decision owners: literate-ai maintainers
- Roadmap: `SPEC-REFINEMENT-001`

## Context

Literate AI currently gives authors a strong boundary after they know what to author.
`component.md` owns observable behavior, public contracts connect independently
generatable Components, Flavors own target choices, assets own literal non-code bytes,
skills own reusable conversion practice, and acceptance contracts provide independent
evidence. `litai spec validate`, `litai lock`, and `litai plan` then reject malformed
graphs and bind exact content identities before generation.

Those gates do not answer an earlier question: how does a user know whether an abstract
request contains enough specification to become those authorities?

Consider this request:

> Write a full-stack application which calculates pi as a service on one or more
> servers with specific CPU/GPU configurations while running a web front end on another
> machine which displays the current computations in progress, their elapsed CPU/GPU
> time, and the results of calculations as they are streamed across.

The request clearly communicates a product direction, but it combines concerns whose
correct specification depths differ materially:

- a straightforward web presentation surface;
- an API, progress stream, reconnect behavior, and distributed job lifecycle;
- a numerical algorithm whose precision, convergence, partitioning, and reduction
  semantics determine correctness;
- CPU and GPU realizations selected for particular hosts;
- resource measurements whose names are meaningless until their clocks, scopes,
  aggregation, availability, and error semantics are defined; and
- deployment placement across several machines.

A coding agent can invent plausible answers to the omitted questions and may generate a
program that compiles and looks convincing. That is precisely the failure mode a
specification-led framework must prevent. Model confidence, generated tests, successful
compilation, and a structurally valid `component.md` do not prove that invented product
decisions match the user's intent.

The existing HIGH/MEDIUM/LOW specification-hierarchy proposal is useful for classifying
representations, but it cannot be a single maturity level assigned to a project. One
request may need high-level scenarios for a UI, mathematical properties for a compute
kernel, content-pinned test vectors, target Flavors for CUDA, and language-native
contracts for local invariants. Asking a user to choose one global level either
underspecifies the hard parts or forces implementation detail into the simple parts.

The framework therefore needs a pre-authoring boundary that decomposes mission intent,
selects specification authority per concern, exposes unresolved decisions, and gives the
user an intelligible stopping rule before any draft becomes generation authority.

## Decision

### Add intent refinement before `lock` and `plan`

Literate AI will treat refinement of an abstract request as a distinct lifecycle phase:

```text
mission request
  -> non-authoritative design draft
  -> explicit human decisions and review
  -> authored Component/interface/asset/Flavor/acceptance authority
  -> spec validation
  -> lock
  -> plan
  -> generation and acceptance
```

Refinement is not generation and does not produce an accepted specification by itself.
It may use deterministic analysis, an agent, or both to propose a decomposition, but its
output remains a draft until an authorized user accepts the decisions that create or
change product intent.

Existing commands retain their meanings:

- `spec validate` proves that an authored specification corpus is structurally valid;
- `lock` resolves exact Component, Flavor, skill, workflow, routing, provider, asset,
  and interface identities without invoking a model;
- `plan` proves that the locked inputs form an exact generation plan; and
- none of these commands retroactively claims that the original mission request was
  semantically complete.

### Select specification depth per concern

The refinement result classifies each material concern and assigns it to the narrowest
authority that can make its acceptance decidable. A whole application is never labeled
with one global specification level.

| Concern shape | Owning authority | Required content |
| --- | --- | --- |
| Product outcome and user workflow | Component behavior | objective, scope, actors, observable states, errors, examples, measurable outcomes |
| Independently deployed or generated surface | Component revision | one entrypoint, lifecycle boundary, provided and required capabilities |
| Cross-Component interaction | Public interface contract | request/response or event shapes, ordering, compatibility, typed failures, version |
| Dense numerical or algorithmic behavior | Component behavior plus properties and acceptance | mathematical result, domains, bounds, precision/error guarantee, invariants, deterministic or explicitly nondeterministic rules, reference cases |
| Literal vectors, constants, tables, or corpora | Content-pinned asset | exact bytes, media type, destination, provenance when required |
| Rule table | A suitable structured provider such as DMN when available | native decision structure and provider-specific structural acceptance |
| State machine or protocol | Behavioral scenarios or a suitable structured provider such as SCXML when available | states, transitions, illegal events, trace and recovery obligations |
| OS, architecture, language, accelerator, toolchain, packaging, or deployment selection | Flavor and target authority | replaceable target constraints and exact selected realization at lock time |
| Reusable implementation technique or host API adaptation | Specification-to-source skill | conversion guidance that cannot weaken product behavior or acceptance |
| Function-local preconditions, postconditions, and invariants | Language-native LOW-layer contract | executable checks co-located with generated implementation and surfaced through the existing test boundary |
| Independent proof of the built result | Acceptance contract and verifier-only evidence | oracle, tolerances, probes, measurement policy, and result comparison hidden from generation where appropriate |

DMN and SCXML must not be selected merely because a concern is complex. A pi algorithm
is neither a decision table nor ordinarily a state machine. Its authority is a
mathematical behavioral contract with properties, bounds, and independent oracles unless
a future concrete case justifies another standard notation. The refinement mechanism
must select by semantic shape, not by perceived difficulty.

### Use independent decidability as the sufficiency rule

A Component is `sufficient` only when an independent verifier can decide whether its
observable behavior is correct from accepted authority and declared target evidence,
without consulting or trusting the generated implementation as an oracle.

This rule has several implications:

- Every public input and output has a bounded, typed, versioned shape or a deliberately
  documented open extension point.
- Normative arithmetic, ordering, state transitions, retry behavior, and errors are not
  left to implementation convention when they affect observable results.
- Terms such as `fast`, `performant`, `real time`, `CPU time`, `GPU time`, `progress`,
  and `stream results` are insufficient until their observable semantics and acceptance
  policy are defined.
- Target-specific APIs may remain implementation choices only when their normalized
  observable behavior is specified and acceptance can account for unavailable features.
- A generated test suite is current-state evidence, not an independent oracle.
- Unknown product decisions do not become implicit model discretion.

Sufficiency is reported per Component and per concern with one of these states:

| State | Meaning |
| --- | --- |
| `sufficient` | Accepted authority makes the concern independently decidable. |
| `needs-decision` | A user decision can materially change observable product behavior or scope. |
| `needs-authority` | The decision is known, but the required Component, interface, asset, Flavor, skill, or acceptance document is absent or incomplete. |
| `target-unresolved` | Portable behavior is adequate, but a required host/deployment selection has not been supplied or cannot be resolved. |
| `unsupported-representation` | The concern requires an authority form the current framework cannot faithfully express. |
| `out-of-scope` | The user explicitly excluded the concern and the exclusion does not contradict accepted requirements. |

Only `sufficient` and deliberate `out-of-scope` findings are non-blocking. An
`out-of-scope` finding records who excluded the concern and why; it is not an automatic
escape from security, validation, or an already accepted requirement.

### Produce a reviewable, non-authoritative design draft

The refinement phase produces a content-identified `DesignDraft` containing at least:

1. the exact mission request and source provenance;
2. a proposed Component graph with the reason each boundary exists;
3. proposed public capabilities, interfaces, entrypoints, and dependency-edge kinds;
4. a concern inventory and selected authority form for every concern;
5. explicit assumptions and proposed defaults;
6. blocking questions and why each answer changes observable behavior;
7. target-bound decisions separated from portable behavior;
8. acceptance obligations and proposed independent oracle classes;
9. per-concern and per-Component sufficiency findings;
10. unsupported concerns and explicit non-goals; and
11. the exact refinement policy, skill, model, and source identities used to produce the
    draft when a model participates.

Draft fields are bounded, strictly validated, canonically ordered where order has no
semantic meaning, and content-identified. Human-readable prose may accompany the typed
record, but the prose cannot hide a question or override its typed status.

The draft must distinguish four kinds of unresolved input:

| Kind | Treatment |
| --- | --- |
| `blocking` | Acceptance cannot proceed until the user or existing authority resolves it. |
| `defaultable` | Literate AI may propose a conventional value with rationale, but the value becomes authority only through explicit acceptance. |
| `target-bound` | Resolve through target/Flavor/deployment authority rather than portable Component behavior. |
| `implementation-local` | A generation skill or implementation may choose it because all observable consequences are already bounded by accepted authority. |

Security and trust-boundary questions default to blocking. The framework must not infer
that a network service is unauthenticated, public, tenant-isolated, encrypted, or trusted
merely because the mission request is silent.

### Make acceptance an atomic authority transition

Accepting a design draft materializes reviewed authority as one atomic change set. It may
create or update Component specifications, public interface contracts, declared assets,
Flavor slots or target selectors, and acceptance contracts. It must not:

- invoke source generation, build, or host execution;
- silently accept unresolved blocking findings;
- flatten independently executable surfaces into one Component;
- copy host-specific API choices into portable behavior;
- overwrite an authored document whose current identity differs from the draft's
  reviewed base; or
- elevate an agent's assumptions solely because they appeared in a draft.

Each accepted decision records the draft identity, selected answer, reviewer authority,
and resulting authored-content identities. Re-running refinement after an authority
change creates a new draft; it never mutates the historical basis of an accepted one.

The intended command surface is:

```console
litai design refine request.md
litai design explain DESIGN_DRAFT [--component COMPONENT] [--concern CONCERN]
litai design accept DESIGN_DRAFT --decisions decisions.json
```

The exact names are not binding if implementation discovers a better provider-neutral
CLI shape, but the three operations are binding: draft, explain, and explicitly accept.
Preview is the default. Acceptance is an explicit write boundary. A noninteractive
accept operation must provide every required decision in an exact input record and fail
closed on omissions, foreign draft identities, stale authored bases, or extra answers.

A representative refinement summary is:

```text
Design status: NEEDS DECISIONS

Proposed Components: 7
Sufficient: 2
Needs authority: 3
Blocking decisions: 5
Defaultable decisions: 6
Target-bound selections: 4

Blocking:
- Required numeric error guarantee is unspecified.
- Meaning of streamed partial results is unspecified.
- CPU and GPU accounting semantics are unspecified.
- Worker-loss and duplicate-event behavior are unspecified.
- Authentication and trust boundary is unspecified.
```

Exit status must distinguish a valid complete draft, a valid draft needing decisions,
and invalid input or framework failure. Machine-readable output is the primary contract;
human rendering is a projection of the same typed findings.

### Preserve existing authority boundaries during decomposition

Refinement applies the existing rule for Component granularity: split a local document
for navigation inside one lifecycle unit; split a Component when a unit needs its own
entrypoint, public contract, generation context, source cache, build, test, SBOM, version,
deployment, or publication lifecycle.

A product with an HTTP API, workers, and a browser frontend is therefore not one
Standard executable Component with several entrypoints. Shared domain logic belongs in
a library Component; executable surfaces consume it through public capabilities. The
draft may initially propose boundaries, but accepted authority must conform to the
single-entrypoint executable semantics and may expose only direct public dependency
contracts during generation.

## Reference Case: Distributed Pi Computation Service

The pi-service request is the reference stress case for this decision because it mixes
simple and complex concerns without requiring obscure product vocabulary. The reference
case is not itself an accepted sample design, and the values below are proposed bounds
for a future sample rather than assumptions the refinement engine may silently apply to
an arbitrary user request.

### Proposed Component graph

| Component | Kind | Responsibility | Key public boundary |
| --- | --- | --- | --- |
| `pi-computation` | library | Mathematical computation, precision/error contract, work-unit partitioning, deterministic combination, progress semantics | compute-work-unit and combine-result capabilities |
| `pi-cpu-worker` | worker application | Execute work units using selected CPU realization and report normalized measurements | worker command/event protocol |
| `pi-gpu-worker` | worker application | Execute supported work units using selected accelerator realization and report normalized measurements | same semantic worker protocol with accelerator capability |
| `pi-job-service` | service application | Admit jobs, schedule work, track lifecycle, cancel, retry, persist result state, expose API and event stream | versioned HTTP/event contracts |
| `pi-progress-protocol` | interface or library Component | Event envelope, sequence, snapshot, reconnect, measurement, and terminal-result schemas | versioned event capability |
| `pi-web-frontend` | web application | Submit and cancel jobs; render active work, elapsed measurements, progress, and terminal results | HTTP/event client contract |
| `pi-deployment` | deployment composition | Place service, workers, and frontend on declared machines and bind target Flavors | deployment evidence, not portable numerical behavior |

Whether `pi-progress-protocol` needs an independently generated Component or only public
interface documents is a review decision. It becomes a Component only if it owns its own
generation/build/version/publication lifecycle. The refinement report must explain that
tradeoff rather than mechanically creating seven Components.

### Blocking product questions

At minimum, the original request leaves these decisions unresolved:

#### Numerical contract

- Is the result a decimal prefix, a correctly rounded decimal value, an approximation
  with an explicit absolute error, or an interval proven to contain pi?
- What precision range is accepted, and which resource limit rejects larger requests?
- Which algorithm families are allowed or required? If several are allowed, must they
  satisfy one common work-unit and progress contract?
- Must CPU and GPU realizations produce byte-identical output, the same rounded digits,
  or merely results within an error bound?
- How are work units partitioned and combined, and is the reduction order deterministic?
- What invariant proves that intermediate or combined results remain valid?

#### Streaming and job lifecycle

- Does a streamed `result` mean provisional digits, a bounded approximation, a verified
  prefix, or only a terminal value?
- What does progress measure: completed work units, terms, verified digits, or an
  estimate? Is total work knowable in advance?
- Are events snapshots or deltas? What orders them, and how does a reconnect resume?
- What are the admitted, queued, running, cancelling, cancelled, failed, and completed
  states, and which transitions are legal?
- What happens after worker loss, duplicate completion, timeout, cancellation races, or
  coordinator restart? Are retries idempotent, and how are stale events rejected?

#### Resource measurement

- Does CPU time mean process CPU time, sum of computation-thread time, allocated-core
  wall time, hardware cycles, or another metric?
- Does GPU time mean kernel execution duration, device-active duration, queue occupancy,
  or host-to-device through result-transfer duration?
- Are measurements per work unit, worker, job, device, or all of these?
- Are counters exact, estimated, or optional? What result is reported when permissions,
  drivers, or hardware cannot supply one?
- Are measurements informational or acceptance-critical, and what monotonicity,
  aggregation, overflow, and unit rules apply?

#### Trust, deployment, and operations

- Which network boundaries are trusted, and are authentication, authorization, transport
  encryption, tenant isolation, quotas, and audit events in scope?
- Are workers fixed by user configuration or selected dynamically by capabilities?
- Which CPU instruction sets, GPU families, driver/toolkit versions, server counts, and
  placements are required versus optional target variants?
- Must jobs survive service restart, and if so, what persistence and recovery guarantees
  are required?

The refinement draft may propose bounded defaults, but these questions remain visible
until accepted or explicitly excluded.

### Normalized measurement contract

Portable Component authority defines metric meaning, while target Flavors and skills
define how a host obtains the observation. A suitable starting vocabulary is:

```text
wall_elapsed_ns
  Monotonic elapsed time from admitted job start to the event observation.

cpu_time_ns
  Sum of operating-system-reported execution time for computation threads that
  contributed accepted work to the job.

gpu_kernel_time_ns
  Sum of completed device-kernel durations attributed to accepted work for the job,
  measured by the selected accelerator API.

measurement_quality
  exact | estimated | unavailable

measurement_scope
  work-unit | worker | job
```

This vocabulary is illustrative until accepted by the sample specification. The final
contract must define whether sums may overlap under concurrency, whether retried or
rejected work contributes, the monotonicity and overflow rules, and the exact relation
between per-worker and aggregate measurements.

Linux process clocks, `perf`, CUDA events, ROCm APIs, device permissions, and counter
availability do not belong in the portable pi algorithm. They belong in OS/accelerator
Flavors and implementation skills whose output conforms to the normalized contract.
An unavailable counter must be represented honestly; substituting wall time and labeling
it CPU or GPU time is forbidden.

### Numerical acceptance obligations

A future sample must not use the generated implementation to establish expected pi
digits. Independent acceptance should combine:

- content-pinned known prefixes or reference vectors withheld from generation when they
  serve as an oracle;
- property checks for requested precision, result shape, bounds, deterministic
  partition/reduction behavior, and CPU/GPU agreement under the accepted contract;
- an independently implemented small-input oracle or interval check;
- invalid and boundary requests;
- stream sequence, reconnect, cancellation, duplicate, and terminal-state checks; and
- measurement schema, unit, monotonicity, aggregation, and unavailability checks.

Performance acceptance is valid only when it names an exact host profile, workload,
warm-up policy, concurrency, measurement method, and tolerance. `Performant` without
those bindings is a blocking ambiguity, not a requirement.

### Bounded sample scope

After the refinement contracts exist, the recommended first sample is deliberately
bounded:

- one coordinator service, one browser frontend, and one or more workers;
- CPU required and one optional CUDA worker target;
- one accepted pi algorithm with a declared precision/error contract;
- bounded precision and job counts;
- Server-Sent Events or another one-way versioned stream with sequence-based reconnect;
- cancellation and worker-loss retry with explicit idempotency semantics;
- normalized wall, CPU, and GPU timing with honest quality/availability; and
- local or user-owned multi-host deployment without claiming internet-scale scheduling,
  arbitrary accelerators, production multi-tenancy, or universal hardware counters.

This sample should prove the abstract-request refinement flow as well as the generated
application. It must be authored through the same provider-neutral contracts available
to users; sample-specific decomposition logic would defeat its purpose.

## Alternatives Considered

### Require users to author complete Components directly

Rejected as the only path. Expert users may continue doing so, but it does not answer
how a new user identifies omitted product decisions or chooses among Component,
interface, Flavor, asset, skill, and acceptance authority.

### Let the coding agent infer missing details during generation

Rejected. This converts model assumptions into unreviewed product behavior, makes output
vary by model, and leaves no authoritative basis for independent acceptance.

### Assign one HIGH, MEDIUM, or LOW level to the whole request

Rejected. Mixed systems need different representations for different concerns. A global
level either over-specifies trivial surfaces or under-specifies numerical, protocol, and
measurement semantics.

### Treat a successful build and generated tests as sufficient

Rejected. They can prove internal consistency of one generated candidate, not alignment
with unstated user intent. Generated tests are also not an independent oracle.

### Add a universal formal specification language

Rejected. Literate AI adopts narrow representations according to semantic shape and uses
existing extension seams. The pi reference case does not justify inventing a new DSL.

### Implement the pi sample before the general refinement contract

Rejected. That would hard-code one decomposition and teach the result without teaching
how the framework derived it. The sample follows the provider-neutral refinement
contract and serves as conformance evidence for it.

## Consequences

The principal benefit is a user-visible answer to “what must I specify next?” that does
not require the user to understand Literate AI's internal taxonomy first. Simple requests
can remain simple: if one Component's observable behavior is independently decidable,
refinement need not manufacture extra documents or questions. Mixed requests receive
depth only where correctness requires it.

The authority model becomes clearer. A model may help discover concerns and formulate
questions, but only reviewed decisions create intent. Structural validation, exact lock,
and generation planning remain deterministic later gates rather than being overloaded
with semantic-completeness claims they cannot honestly make.

The cost is a new versioned contract family, application service, CLI surface, review
workflow, and evidence chain. Sufficiency includes semantic judgment, so the framework
must distinguish deterministic findings from model-proposed findings and retain the
exact provenance of both. It cannot truthfully claim to mechanically prove arbitrary
natural-language completeness.

The refinement system may ask too many questions. Mitigations are concern-level
classification, explicit defaultable and implementation-local categories, one clear
reason per blocking question, and the rule that documents and Components are created
only for real ownership or lifecycle boundaries.

The pi-service sample will be more expensive than the current deterministic JSON
calculators and may require optional accelerator hosts. Its CPU path must remain portable
and independently useful, accelerator scheduling must be capability-gated, and absence
of a GPU must skip only the declared optional target rather than weaken the sample's
portable acceptance contract.

## Implementation Constraints

This ADR does not commit the proposed CLI spelling or the pi-service sample to a release.
It does commit the following constraints on any implementation:

1. Refinement precedes accepted authoring, lock, plan, and generation.
2. Drafts are non-authoritative and content-identified.
3. Specification depth and authority are selected per concern.
4. Independent decidability is the sufficiency criterion.
5. Unresolved blocking decisions prevent acceptance.
6. Agent assumptions require explicit review before becoming product intent.
7. Acceptance is atomic, identity-bound, and non-generating.
8. Portable behavior, target Flavors, skills, assets, interfaces, and verifier evidence
   retain their existing authority boundaries.
9. Executable surfaces retain one entrypoint per Component and compose through public
   capability contracts.
10. The pi-service reference case is implemented only through provider-neutral
    refinement machinery and independently verified acceptance.

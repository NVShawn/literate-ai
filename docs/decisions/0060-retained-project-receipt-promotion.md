# ADR 0060: Promote a verified retained-project run into the project test receipt

- Status: Accepted
- Date: 2026-11-04
- Acceptance: The directing user explicitly accepted this corrected design on
  2026-11-04, recorded in [PR #297 comment6636036548](https://github.com/jordanhubbard/literate-ai/pull/297#issuecomment-6636036548).
  This accepts the design under ADR 0006; draft implementation and qualification remain open.
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0, per the included disposition on issue #295 and PR #297;
  the requesting consumer's release is separate.
- Roadmap: [RETAINED-RECEIPT-PROMOTE-001](../roadmap/active-work.md#retained-receipt-promote-001-promote-a-verified-retained-project-run-into-the-project-test-receipt)
- Issue: [#295](https://github.com/jordanhubbard/literate-ai/issues/295)
- Draft review: [#297](https://github.com/jordanhubbard/literate-ai/pull/297)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

A project using the original-source retained lifecycle can pass its native tests and
independent acceptance cases and retain a receipt, yet remain unable to satisfy its
configured project `test_receipt_policy`. Standard lifecycle finalization and the
converted-harness `run-retained` command have different runner and suite contracts.
Changing that project's custom suite, lowering its evidence requirements, or claiming
conversion it did not perform does not solve this gap.

The draft at `421ce0f70b8da21e2f0b34eb655d3766269ba67d` is not the accepted
implementation of this decision. Its review found three substantive gaps: acceptance
stdout/stderr are not checked, the runner identity omits the semantic test-policy
inventory, and a derived digest is labeled workspace admission despite incomplete
resolved-output/dependency verification. This replacement specifies their required
resolution; source review and real qualification remain open.

## Proposed decision

Add `litai project test-receipt promote-retained CANDIDATE` for an explicitly selected
original-source retained receipt, manifest, profile and custody source. It executes no
build, test, acceptance or SDK command. It reopens the complete retained evidence
against current project authority and emits a finalized candidate only when the
project's unchanged policy can actually be satisfied. The existing separate
`project test-receipt update CANDIDATE` command remains responsible for atomic receipt
replacement and its current authority, policy, suite, runner and count checks.

### Current authority and complete historical verification

The promotion owner must use the canonical retained planning and complete completed-run
verification boundaries. It must not maintain a second weaker receipt verifier. The
existing accepted-run reader and live evidence writer are reusable mechanisms, not
automatic proof that every required promotion semantic has been established.

Before success, the verifier must establish all of the following:

1. The current project revision, exact Component lock set, retained plan, profile,
   complete original-source manifest, test policy and acceptance policy are pinned and
   unchanged before and after the operation. Current source bytes, modes, membership
   and link topology must match the selected retained manifest. Missing or stale inputs
   refuse; a stored manifest or an identifier alone cannot replace this observation.
2. The receipt and every selected record/root have exact canonical bytes, identity,
   size, media role and schema. All observation lists have the exact current count and
   unique identities before reconstruction. Every command has the exact current action,
   tool, stage and context, its retained streams and stage output manifest. Failed,
   incomplete or unexplained observations cannot become passing evidence.
3. Each acceptance case has the exact current case/policy/action/command and output
   context. Its bounded retained stdout and stderr bytes must hash to their named
   records and match `expected_stdout` and `expected_stderr` byte for byte, alongside
   `expected_exit`. No decoding normalization, truncation, missing-stream substitution
   or inference from exit zero is permitted. Declared filesystem expectations, native
   dependency identities and SDK assertions must also pass the canonical checks.
4. Test reports cover exactly the reviewed action/case/report inventory, with no
   duplicate, missing, extra, failed or skipped test. Receipt totals equal the verified
   reports, and actual passing tests satisfy the unchanged project minimum.
5. The source and resolved SBOMs are reconstructed from the complete current plan and
   the exact retained observations, tool/export bytes and dependency records. Export
   and payload membership, modes, topology and content must be verified through the
   existing complete retained-output/dependency boundaries. Hashing only an SBOM or a
   payload manifest does not establish these semantics. An unavailable dependency or
   unsupported historical scope refuses; it is never replaced by an empty assertion.

Pure historical reconstruction returns verified assertions, never
`AdmittedRetainedProject`, a live SDK execution grant, LINK authority or an execution
workspace. No JSON decoder, copied dataclass, receipt identity or caller-supplied
boolean may issue one of those capabilities.

### Runner identity without a policy cycle

Use a distinct versioned runner projection, proposed as
`literate-ai/retained-project-test-runner@2`, rather than silently preserving the
draft's incomplete identity. Its canonical closed body binds:

- every current test action's complete semantic declaration, including action identity,
  argv template, stage, declared outputs, inventory and finite execution limits;
- the exact resolved identities of the tools those actions select;
- every reviewed test-definition input's canonical coordinate, mode/link semantics and
  content identity; and
- the current typed `RetainedProjectTestPolicy.to_dict()` with only the single
  top-level `project_policy_identity` field removed. Its schema, cases, action mappings,
  skip reasons and report coordinates remain bound.

Read and validate the complete current test policy before deriving this projection.
The policy file's entire raw digest must not also enter the noncircular projection:
its `project_policy_identity` refers to the project policy that pins this runner.
Only that circular reference is omitted from the semantic projection; the full policy
bytes and their identity are still required in the actual retained run and promotion.
Other references or fields must not be recursively erased to make an identity fit.

Changing a case, report coordinate, skip authorization, test definition, tool or action
must change the runner identity. Diagnostic/read-only output may disclose the derived
current identity so a project can review and explicitly author its policy. Promotion
never rebinds a policy automatically. Any policy change invalidates the corresponding
old project/run authority; a fresh retained run may be required. That rerun is a
separately authorized lifecycle operation, not an effect of promotion.

### Evidence kinds and admission provenance

The promotion result must state which verified records satisfy each required evidence
kind and refuse the whole promotion if any required kind is unsupported. Extra envelope
bookkeeping is not permission to claim an evidence kind's operational meaning.

| Kind | Required source and meaning |
| --- | --- |
| `acceptance-result` | Complete canonical acceptance observations satisfying the exact exit, streams, filesystem and dependency oracle. |
| `build-result`, `test-report`, `observation-result` | The exact verified command/test/completed-run records; they do not imply package installation or release approval. |
| `test-runner`, `lifecycle-plan` | The noncircular current runner projection and the exact verified retained plan. |
| `source-sbom`, `resolved-sbom` | Fully joined source and resolved observations as specified above, including the required export/dependency closure. |
| `lifecycle-request`, `lifecycle-command` | Explicitly versioned promotion request and command-set assertions bound to the original verified run and current authority. They do not assert that promotion executed the historical commands. |
| `workspace-admission` | A complete historical admission assertion captured by the genuine original live admission owner, and fully reopened against the selected run, current output/dependency closure and current policy. A digest synthesized solely from receipt/plan/input identifiers is insufficient. |

Where a frozen retained receipt omits facts needed for complete historical reopening,
use an additive, versioned evidence sidecar captured while the genuine original
admission and required SDK scopes are current. Reuse the existing live-captured planning
and accepted-run evidence mechanisms where their exact semantics fit. The sidecar must
bind the exact receipt, plan, completed run and required observations; the capture
writer must require the genuine original owner and guard currentness before and after
capture. Neither reader nor writer may reconstruct ownership from serialized data.
The frozen receipt and existing wire families remain unchanged.

An old run without sufficient admission/export/dependency evidence must refuse
`workspace-admission` or another affected requirement. A project requiring that kind
cannot be promoted until the complete evidence is available, potentially from a fresh
retained run using the supported capture path. This is not permission to remove its
requirement, fabricate a sidecar retrospectively, or treat a typed value as a live owner.
Supporting the request's admission-evidence policy is part of implementation and real
qualification; an implementation that only refuses it has not completed issue #295.

The existing finalized envelope requires source-cache control/decision/lifecycle
identities even for original-source authority. Explicit versioned `not-applicable`
bookkeeping may fill that envelope geometry, but is not evidence that a cache was
published, hit, accepted or admitted. If the policy requires operational cache decision,
lifecycle or publication evidence, require actual verified records with those semantics
or refuse. A required cache kind may not pass merely because its name is present beside
a `not-applicable` identity. Existing Standard cache meanings remain unchanged.

### Custody, finite resources and writes

The finalized candidate is the existing local typed TCB boundary, not a cryptographic
attestation. Content identities prove byte custody and joins; they cannot prove that
an arbitrary hostile custody-store writer actually ran a command. Trust in the current
framework producer and invoking operator remains explicit. Imported or remote evidence
must retain the existing authenticated transport/admission boundary; no unsigned
fallback or new receipt-as-authority route is introduced. Deployments requiring signed
execution provenance continue to require their independent policy/trust roots.

All reads must be bounded before loading, decoding or allocating observation lists.
Use explicit per-document, stream, record-count, aggregate byte and aggregate memory
limits, accounting for the receipt, roots, current graphs and verified caches already
retained. Repeated identities may reuse verified bounded data without bypassing fresh
custody checks. Filesystem observations use canonical safe-path and currentness owners;
no ad hoc unsafe reads or global worker/SDK probes are added to historical promotion.

`--dry-run` performs verification and returns its candidate identity or typed refusal
without creating a candidate, changing the project receipt, creating cache/state/lock
files, or invoking build, test, acceptance, SDK, model or worker processes. Trusted
canonical metadata helpers such as bounded read-only Git queries are permitted only
where the current-authority path requires them; they must preserve no-write semantics,
finite resources and currentness. Ad hoc or global probes remain prohibited. Normal
promotion must recheck complete current
authority before exclusive candidate publication; an existing foreign candidate is
preserved. Update remains a separate deliberate command with its unchanged canonical
locking, atomic replacement and final currentness checks. Failures preserve the prior
candidate/receipt and retain bounded, redacted diagnostics.

## Planning and acceptance

The current implementation remains provisional and may not be described as qualified
or accepted. After explicit design acceptance, implementation and independent source
review must repair all three findings before canonical pin records or release claims.
The public journey must use a genuine retained run and prove promotion, unchanged-policy
update, receipt currentness and project verification. It must also cover:

- coherent rehashed zero-exit acceptance with wrong stdout or stderr, missing/truncated
  streams, and changed filesystem/dependency assertions;
- changed cases, report mappings, skip reasons or test tools changing the runner,
  while changing only the circular reference cannot erase the policy/context checks;
- fabricated identifier-only admission, missing live-captured sidecars, coherent but
  incorrect resolved/payload/dependency joins and `not-applicable` cache records under
  a policy requiring actual cache evidence;
- stale plan/current inputs/Component locks/policy, wrong suite or runner, duplicate
  observations, skipped/failed/insufficient tests, custody tampering and all finite
  resource refusals;
- no writes or build/test/acceptance/SDK/model/worker execution in dry-run/refusal
  paths; any necessary canonical read-only metadata helpers remain bounded and
  nonmutating; candidate collision preservation,
  atomic update and stale-candidate refusal; and
- exact full-source gates, a clean committed installed distribution and the real
  requesting consumer's retained-evidence policy. Synthetic or converted-harness-only
  fixtures cannot substitute for that consumer boundary.

## Consequences

Original-source retained projects obtain a supported route to the existing receipt
gate without pretending to use Standard generation or converted-harness semantics.
Some historical runs will require a fresh, fully evidenced run because the missing
facts were never retained. Runner adoption remains an explicit project-policy review.
No minimum, evidence requirement, currentness, trust boundary or existing update check
is weakened. Framework 1.4 inclusion remains distinct from design acceptance and from
implementation, installed/native qualification and issue closure.

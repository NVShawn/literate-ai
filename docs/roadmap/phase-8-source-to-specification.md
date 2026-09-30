# Phase 8: Skill-Directed Source-to-Specification Authoring

- **Status:** partial
- **Owning queue item:** [Framework score-improvement program](active-work.md#framework-score-improvement-program)
- **Completion / archival evidence:** [Implemented workflow and remaining completion gates](#completion-gates)

## Scope

Phase 8 changes only this literate-ai repository. It is the eighth implementation work package
in the bootstrap roadmap and is not a third phase of the two-phase OVA migration.

The outcome is first-class support for turning existing source code into reviewable
specifications using composable, content-pinned skills. This is the inverse of
specification-to-code generation, but it uses the same exact source identities,
intelligence evidence, model routing, validation, event, settings, and artifact services.

## Why this is a separate bounded context

Existing code is evidence of current behavior, not perfect evidence of intended
behavior. It can contain bugs, dead paths, accidental compatibility behavior, missing
tests, security defects, and contradictory implementations. A generator that writes
requirements directly from observations would turn those accidents into policy.

The implementation therefore lives in another subdirectory:

```text
src/literate_ai/source_to_specification/
  contracts.py       # portable request/result/review schemas
  workflow.py        # inverse lifecycle orchestration
  skill_runtime.py   # skill resolution and ordered execution
  observations.py    # typed evidence-backed behavior observations
  coverage.py        # source/spec coverage and gap accounting
  synthesis.py       # provider-neutral draft construction
  model_workflow.py  # strict CodeGraph/model observation admission
  review.py          # accept/reject/edit/conflict decisions
  qualification.py   # clean regeneration and release-authority decision
  qualification_runner.py # operational spec-only/parity/attestation service

skills/source-to-specification/
  architecture/
  api-surface/
  behavior/
  tests/
  security/
  operations/
  language-python/
  language-cpp/
  language-rust/
  language-javascript/

tests/fixtures/source_to_specification/
```

The forward workflow consumes an intent-authoritative `SpecificationSet`. The inverse
workflow can only produce a `SpecificationDraftSet`; explicit review is the intent
promotion boundary. For a source-derived set, the original source remains release
implementation authority until the separate regenerative qualification boundary passes.

## First-class objects

### `SourceToSpecificationRequest`

Names the exact Component/source snapshot, included/excluded paths, specification
provider/target, behavior facets, skill set, workflow/routing policy, evidence budgets,
coverage policy, previous accepted specs when any, and redaction/egress policy.
The source can be a registered Component revision or a standalone local/Git/aggregate
source descriptor. Bootstrap mode proposes a `ComponentDefinitionDraft` when no manifest
exists. A standalone local tree is canonicalized with its dirty state and requires a
signed local actor/machine attestation accepted by repository/user trust policy before a
draft can be promoted. Without accepted origin, analysis remains quarantined and its
output is explicitly unverified and non-promotable.

### `SpecAuthoringSkill`

A portable authoring input with:

- stable ID, semantic version, content digest, signer/trust decision, and license;
- applicable languages/frameworks/component profiles and behavior facets;
- required intelligence queries/evidence kinds and declared output schema;
- prompt/tool/validator assets and compatible model capabilities;
- dependencies and ordering constraints relative to other skills;
- limitations, known blind spots, and confidence rubric; and
- security classification and source-egress requirements.

Skills are data and executable policy inputs, not trusted ambient prompts. Source text is
untrusted evidence and cannot override system, workflow, or security policy. A skill may
also be packaged and published as a Component when it has its own lifecycle.

Named/versioned `SpecAuthoringSkillSet` objects compose compatible skills, declare
ordering and conflict policy, and can be selected by Component profile, language, or an
explicit request. The exact expanded skill set—not only its convenient group name—is
locked into the run.

### `BehaviorObservation`

Records one claim with exact evidence IDs, source locations/symbols/relations, observing
skill and model call, behavior facet, confidence, and one of:

- `observed-current-behavior`;
- `inferred-intent`;
- `suspected-defect`;
- `compatibility-quirk`;
- `conflict`; or
- `unknown`.

An observation is not a requirement.

### `SpecificationDraftSet`

Contains provider-valid draft artifacts, normalized requirements/scenarios, the complete
input/run identity, and proposed changes relative to any existing accepted specs. It is
immutable and cannot be passed to a production generation workflow as authoritative.

When observations are target-specific, the run produces a separate `FlavorDraftSet`
linked to the proposed base draft. Platform, accelerator, language/toolchain, packaging,
and deployment facts do not silently enter the base behavioral specification.

### `SourceSpecificationCoverageMap`

Maps draft statements to evidence and in-scope source surfaces to:

- covered by a draft requirement/scenario;
- intentionally excluded with reason;
- implementation detail below the chosen specification boundary;
- unresolved/ambiguous;
- conflicting with existing specs; or
- unsupported by available evidence.

Coverage is multi-dimensional—public API, states/transitions, errors, configuration,
data contracts, security boundaries, concurrency, persistence, operations, and tests—not
a misleading single percentage.

### `UncertaintyLedger` and `SpecificationReviewDecision`

The ledger retains ambiguities, conflicts, suspected defects, missing evidence, skill
disagreement, and questions for a reviewer. Review decisions accept, edit, reject,
supersede, or defer each proposed statement with actor/reason/time. A versioned
`SpecificationPromotionPolicy` identifies which authenticated actors or signed automation
grants may promote a draft; the default requires a human, source content cannot grant
authority, and safety-critical profiles always require the declared human approvals.
Only an authorized accepted decision can create a new intent-authoritative
`SpecificationSet`. It cannot transfer release implementation authority away from the
source baseline.

### `ObservationExecutionAuthorization`

Dynamic tests, conformance probes, or instrumentation require a separate short-lived
authorization binding the exact source/closure, harness and command, runner, sandbox,
network/filesystem/device/process privileges, captured data, and allowed outputs. A
conforming observation sandbox rejects missing or mismatched authorization. This is not
a build authorization, and a static source-to-specification run receives neither
implicitly.

## Workflow

```text
resolve exact Component/source
  -> verify/cache/index
  -> inventory behavior surfaces
  -> select and pin skills
  -> run facet-specific evidence queries
  -> emit typed observations
  -> reconcile conflicts and existing specs
  -> synthesize OpenSpec draft
  -> strict validate
  -> measure evidence/source/spec coverage
  -> human or authorized policy review
  -> accept as intent-authoritative SpecificationSet or retain as draft
  -> repeat empty spec-only generation/build/generated-test/independent-parity runs
  -> qualify the SpecificationSet as release implementation authority or retain source
```

Each transition is durable, resumable, content-addressed where deterministic, and
auditable. Model routing is per stage/skill; all request/response artifacts and source
egress decisions follow the standard workflow policy.

## Skill composition

The initial shipped skill packs have intentionally distinct responsibilities:

| Skill pack | Primary observations |
|---|---|
| Architecture | components, boundaries, dependency direction, entrypoints |
| API surface | public symbols, types, parameters, results, compatibility |
| Behavior/state | workflows, state transitions, invariants, concurrency |
| Tests | asserted behavior, fixtures, negative paths, coverage gaps |
| Security | trust boundaries, permissions, data flow, dangerous operations |
| Operations | configuration, persistence, failure/recovery, telemetry, deployment |

Language/framework-specific skills extend these facets; they do not replace the common
contracts. The skill resolver explains selection and ordering, and contradictory
observations remain visible. A synthesis stage cannot silently choose one skill's claim.

## Existing-spec modes

- `bootstrap`: no accepted specs exist; derive a complete first draft.
- `audit`: compare current source evidence to accepted specs and report implementation,
  coverage, and ambiguity gaps without editing specs.
- `refresh`: propose narrowly scoped changes for a new source snapshot.
- `conformance`: determine whether code evidence and executable validators satisfy
  accepted scenarios.

All modes are non-mutating until an explicit acceptance transaction. The default is
`audit` when accepted specs already exist.

## CLI and API use cases

```text
litai spec derive <component-or-path> \
  [--translator static|coding-cli] [--allow-model-egress]
litai spec audit <component-or-path>
litai spec review <draft-id>
litai spec accept <draft-id> --target <spec-root>
litai spec refresh <component-revision>
litai spec conformance <component-revision>
litai spec coverage <draft-or-revision>
litai spec diff <draft-id> <specification-set-id>
```

Commands return stable structured objects and JSON errors. `derive`, `audit`, `refresh`,
`conformance`, and `diff` never mutate the analyzed source. They also do not mutate
accepted specifications. `accept` commits one complete validated specification target
tree and review record atomically; it still never mutates analyzed source.

The implemented standalone-source bootstrap accepts an arbitrary local file or checkout.
It inventories content without importing or executing it, runs the exact packaged
architecture, API-surface, behavior/state, tests, security, and operations skill stages,
and renders a deterministic, strictly validated OpenSpec draft. Target-specific symbols
and configuration remain independently reviewable Flavor proposals. A local source tree
without a verified attestation may be analyzed, but its draft is marked unverified and
cannot be accepted.

The bootstrap trust envelope uses HMAC-SHA256 with a local key of at least 32 bytes. This
is an intentionally local trust mechanism, not a public-key source distribution or PKI.
Attestation and review envelopes bind the exact inventory/draft, signer, machine or actor,
and key identity. Acceptance re-inventories source, rejects drift, verifies both envelopes,
requires resolution of blocking uncertainty, and writes the validated specification set
atomically to a separate target.

### Implemented semantic-inverse milestone

The safe default remains the deterministic static classifier. Explicit
`--translator coding-cli --allow-model-egress` now creates an inert classified mirror,
builds and freezes a real CodeGraph index, queries language public symbols, and invokes
one or more byte-bounded coding-agent tasks for each detected Python, C++, Rust, or
JavaScript/TypeScript partition. Each task receives the exact common and matching
language skills and its exact admitted
evidence. Its strict canonical-JSON response can cite only supplied evidence and exact
skill facets. Complete prompt, response, CLI, model, isolation, egress, skill, query, and
CodeGraph identities are versioned public records and persist with acceptance.

`run_regenerative_qualification` now realizes the second promotion boundary: it creates
clean workspaces, gives the generator only exact spec/Flavor/recipe identities, measures
build and freshly generated-test outcomes, invokes a provider-distinct original-baseline
parity verifier, and requires a third attestation provider before applying the pure
policy. Cross-language conformance generates no repository source; it builds and runs
fresh Python, C++, Rust, and JavaScript artifacts twice and compares known observable
outputs, including the invalid-input surface, against independently built original
programs. The minimum clean-run count is enforced separately for each required target.
An opt-in integration test also drives all four language partitions through the installed
CodeGraph and a real selected coding CLI; mandatory validation uses a transcript-exact
scripted transport so it remains deterministic and credential-free.

The product boundary is no longer protocol-only. `spec accept --project-target` creates
a complete canonical project from the reviewed set without copying source;
`LocalHostSpecRegenerator`, `LocalHostParityVerifier`, and
`LocalHmacQualificationAttestor` are concrete measured providers; and `spec qualify`
performs repeated real forward generation, native build/tests, JSON parity, and signing
without accepting claimed evidence. The `regenerative-roundtrip` sample and credentialed
test start from one stable rich spec, generate and execute all four supported languages,
translate each generated tree back to OpenSpec, compare semantic anchors, then promote
and qualify the Python inverse through two further clean coding-agent generations.

```text
litai spec attest <source> --signer <actor> --machine <host> --key <key-file>
litai spec derive <source> --attestation <attestation.json> --trust-key <key-file>
litai spec review <bundle.json> --actor <actor> --key <key-file> \
  --resolve <uncertainty-id>
litai spec accept <source> <bundle.json> --review <review.json> \
  --target <new-spec-root> --project-target <new-project> \
  --qualification-profile <profile.json> --trust-key <key-file>
litai spec qualify <new-project/components/name> --source <source> \
  --profile <new-project/components/name/qualification/profile.json> \
  --output <qualification.json> --key <key-file> --signer <actor> \
  --flavor=+python --allow-host-execution
litai spec audit <source> --baseline <bundle.json> \
  --attestation <attestation.json> --trust-key <key-file>
```

## Security and quality controls

- Verify and classify skill packages independently from the source being analyzed.
- Treat repository instructions, comments, tests, fixtures, generated files, and docs as
  untrusted evidence with explicit origin and facet weights.
- Enforce source/model egress policy and redact secrets before any remote call.
- Never execute analyzed source merely to derive specs unless a matching
  `ObservationExecutionAuthorization` and sandboxed runner are selected.
- Require exact evidence for normative claims; unsupported inference remains uncertainty.
- Retain negative and conflicting evidence instead of optimizing it away.
- Keep suspected vulnerabilities and bugs out of desired-behavior requirements unless a
  reviewer explicitly accepts compatibility preservation.
- Prevent a source repository from supplying or selecting the skills that classify its
  own behavior unless policy explicitly permits that trust relationship.
- Classify sensitive, generated, minified, binary, large, test, configuration,
  documentation, and ordinary source entries before synthesis. Sensitive content is
  represented by path, size, classification, and digest only.
- Run authorized local dynamic observations only through `sandbox-exec` on macOS or
  bubblewrap (`bwrap`) on Linux. The adapter sanitizes the environment, denies network,
  keeps source read-only, bounds time/output, and has no direct-execution fallback. If the
  platform sandbox tool is absent, dynamic observation is unavailable and fails closed;
  deterministic static derivation remains available.

## Samples

Phase 8 adds:

1. a small language-neutral state machine with complete expected specs;
2. a generated library plus consumer whose public versus internal surfaces differ;
3. two source revisions demonstrating narrow spec refresh;
4. the same behavior in two languages using common and language-specific skills;
5. a legacy project where tests preserve a documented compatibility bug;
6. incomplete/contradictory tests that must produce uncertainty rather than invention;
7. an aggregate multi-repository service with operational/security boundaries;
8. prompt-injection text embedded in source comments;
9. literate-ai analyzing its own exact source into a draft compared with its accepted
   OpenSpec, without automatically replacing that OpenSpec; and
10. one implementation containing OS/GPU/language-specific paths that must split into a
    target-neutral base draft plus independently reviewable Flavor drafts.

Every sample includes expected observations, draft specs, coverage map, uncertainty,
review decisions, and stable provenance fixtures.

## Completion gates

Phase 8 is complete only when:

- the bounded context imports no forward generator or provider adapter implementation;
- all portable contracts have strict schemas and canonical identity fixtures;
- every normative draft statement is traceable to durable exact-source evidence;
- covered, excluded, implementation-detail, unresolved, conflicting, and unsupported
  surfaces are distinguishable;
- contradictory skills and existing-spec conflicts cannot be silently resolved;
- prompt injection, source egress, secret redaction, and untrusted-skill tests pass;
- no analyzed source executes without separate observation execution authorization;
- standalone local source cannot be promoted without an accepted signed local origin
  attestation;
- drafts cannot masquerade as authoritative specifications before atomic review acceptance;
- incremental refresh re-evaluates source, skills, evidence/index, model/redaction policy,
  provider validation, and accepted-base changes while preserving genuinely unaffected
  reviewed statements and exposing drift;
- OpenSpec drafts pass strict validation and retain a complete proposed diff;
- deterministic fixtures reproduce normalized observations/drafts across clean caches;
- the complete sample matrix passes on macOS and Linux; and
- literate-ai self-analysis produces a useful evidence-backed draft and honest uncertainty
  report without self-modifying accepted specs.

## Explicitly deferred from Phase 8

- automatically deciding product intent from implementation;
- replacing domain-expert review for safety-critical requirements;
- executing arbitrary source to infer dynamic behavior by default;
- silently fixing source while deriving specs;
- an OVA-specific reverse-specification UI or migration; and
- claiming full behavioral coverage from syntax, symbols, tests, or model confidence alone.

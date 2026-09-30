# Two-Phase OVA Rebase Plan

## Outcome

OVA will become a domain adapter and demanding consumer of literate-ai, rather than the
owner of the general Component lifecycle. The migration has exactly two phases:

1. build, prove, and release the software-neutral framework while OVA remains
   authoritative; and
2. rebase OVA on the released framework, then retire the duplicated implementation.

Milestones below are sequencing within a phase, not additional migration phases.
Neither repository may infer parity from renamed types or passing unit tests alone.

## Governing constraints

- OVA commit `811c71c9472ec07a5bae2dba32f6d8f21b0802bb` is the initial behavioral
  extraction baseline.
- OVA continues to launch and generate applications throughout Phase 1.
- The literate-ai domain and wire formats contain no OVA, Omniverse, Isaac, ROS,
  Litestar, CodeGraph, model-provider, language, or filesystem assumption.
- Base Components remain target-neutral; OS, architecture, accelerator, language,
  toolchain, packaging, and deployment variants compose through exact first-class Flavors.
- Every migration decision has a fixture, an identity mapping, or an explicit
  documented incompatibility. Silent reinterpretation is forbidden.
- Existing OVA caches are never destructively converted. Import is read-only and
  resumable.
- Source signatures authenticate bytes and origin. Classification and build
  authorization separately decide whether those exact bytes may execute.
- `yolo` is supported, but only as an explicit, signed-revision-scoped, expiring,
  persistently warned policy with every privilege enabled and every decision audited.
- Removal in OVA follows two released compatibility versions and a rehearsed rollback.

## Compatibility surface to preserve

| OVA surface | Phase 1 compatibility contract | Phase 2 destination |
|---|---|---|
| `ova.yaml` v2 | Parse into a lossless legacy envelope; emit deterministic diagnostics | OVA adapter projects it to `component.json` domain objects |
| OpenSpec roots and active changes | Preserve full contents, digests, strict validation, and drift checks | `OpenSpecProvider` typed service |
| Component IDs and catalog IDs | Preserve exact legacy strings plus an explicit coordinate map | OVA namespace aliases in `ComponentRegistry` |
| Source and CodeGraph locks | Golden fixtures verify exact binding and error behavior | `SourceSnapshot` and `KnowledgeIndex` records |
| Dependency locks | Map legacy identities; exclude timestamps from new semantic digests | `ResolutionDecision` and composition lock |
| Generated provenance | Preserve legacy record and attach its digest to the new run | `WorkflowRun`, stage outputs, and `LegacyIdentityMap` |
| Source/object packages | Read without mutation; map forward edges and package contents | immutable source/build/artifact bundles in CAS |
| Reverse dependents | Import into a rebuildable projection, never rewrite old packages | `DependencyIndex` |
| Model endpoints/groups/selectors | Preserve routing constraints and exact decisions | workflow-stage routing policy |
| Settings and environment names | Read through a typed compatibility source and show provenance | scoped settings registry rendered by OVA UI |
| Publication state | Import records; never republish merely because a cache was imported | publisher ports and append-only records |
| CLI/Make targets | Forward behind compatibility commands with stable JSON errors | `litai` CLI plus thin OVA commands |
| Sample tiers | Freeze current results as downstream fixtures | neutral conformance ladder plus OVA integration ladder |
| Host/GPU/language target policy | Record current implicit choices as migration evidence | downstream OVA Flavors and exact effective revisions |

## Phase 1 — Extract, stabilize, and shadow

OVA is the production authority in this phase. literate-ai establishes public contracts
and runs without changing OVA's accepted output.

### 1. Freeze the evidence baseline

- Export valid and invalid OVA manifests, OpenSpec sets, source/index/dependency locks,
  component API contracts, artifact plans, model decisions, run provenance, cache
  records, package manifests, publication records, settings documents, and stable JSON
  errors as versioned fixtures.
- Capture simple, intermediate, advanced, composed, and framework-readiness runs from an
  empty cache and a warm cache.
- Record the historically named self-host sample honestly: its primary Component builds
  a self-contained compatibility/readiness application. A separate deterministic
  snapshot-replication fixture tests lifecycle plumbing; neither rebuilds OVA or proves
  coding-CLI self-generation.
- Add adversarial fixtures for source drift, index drift, missing object dependencies,
  stale mutable refs, LFS pointers, partial acceptance, model fallback, and interrupted
  publication.

### 2. Establish immutable public contracts

- Split the domain into Components, specifications, capabilities, source, intelligence,
  workflows, models, validation, security, packages, publication, settings, and events.
- Publish strict versioned JSON Schemas and canonical JSON hashing rules. Operational
  time, paths, aliases, and status projections do not affect semantic identities.
- Distinguish logical Component coordinates, immutable Component revisions, source
  snapshots, generated revisions, and machine-local projections.
- Define Flavor definitions/revisions, axes/slots, target profiles, typed contributions,
  Flavor-set locks, effective specification sets, and effective Component revisions.
- Define pure schema migrations and a `LegacyIdentityMap`; never overload a digest with
  a changed hashing algorithm.
- Define stable machine-readable errors and lifecycle events before exposing a CLI/API.

### 3. Implement services, application ports, and reference adapters

- Implement `OpenSpecProvider` as a typed service while retaining raw intent as an
  immutable event and spec edits as separate projections.
- Implement local and Git source providers, canonical tree snapshots, aggregate source
  collections, signatures/attestations, dirty-source decisions, submodule/LFS identities,
  and a filesystem CAS.
- Implement a typed structured-intelligence service and a CodeGraph adapter. Do not parse
  human-oriented CodeGraph output with regular expressions.
- Implement typed catalog, registry, resolver, reference-index, package, settings, and
  publication services. Add shared ports only for capabilities injected into an
  application use case, including model execution, validation, classification, build
  authorization, building, workspace acceptance, and event persistence.
- Keep capability selection deterministic and explainable. Every candidate and rejection
  reason appears in a `ResolutionDecision`; no lexicographic or invisible foundation
  fallback exists in core.
- Resolve Flavors by typed axes and explicit target profiles. Reject raw document patches,
  silent precedence, conflicting singleton contributions, and unrecorded host inference.

### 4. Build the durable lifecycle

- Execute a versioned workflow DAG with immutable stage inputs/outputs and append-only
  events. Make retries and restart reconciliation idempotent.
- Materialize a complete descriptor vocabulary without cloning the world. Fault exact
  source/index/evidence only for the selected closure and explicit comparison candidates.
- Reserve evidence budget for selected dependencies before optional global summaries.
- Record model request/response identities, templates, decoding and tool parameters,
  token/cost use, routing decisions, and fallback. Scope routing state to one run.
- Accept generated trees through an immutable workspace-tree commit or directory swap;
  per-file atomic writes are insufficient.
- Require application validators in addition to syntax. Treat missing dependency
  artifacts as a hard resolution failure.

### 5. Enforce source trust and build policy

- Verify the authoritative signed source supplied to the cache and retain its trust
  chain, revocation status, tree identity, and verification result.
- Quarantine new source, scan it, propagate dependency findings, classify the exact
  closure, then issue a short-lived build authorization for an exact builder/toolchain.
- Deny builder invocation when authorization is absent, expired, mismatched, or revoked.
- Implement constrained, reviewed, privileged-review, blocked, and `yolo` profiles.
- Make `yolo` require an exact signed revision, actor, reason, privileges, expiration,
  and explicit acknowledgement. It keeps identity/provenance checks enabled and emits
  unmissable warnings in CLI, API, UI, events, packages, and publications.
- Run adversarial tests proving that a valid signature alone cannot authorize a build.

### 6. Make artifacts, publication, and settings real framework services

- Store immutable `SourceBundle`, `BuildBundle`, and `ArtifactBundle` manifests in CAS.
  Put reverse edges and mutable `latest`/promotion aliases in separate projections.
- Never flatten away direct versus transitive dependency meaning, mutate a dependency
  package when a consumer appears, or overwrite a content-addressed package directory.
- Make publication explicit, resumable, signed, auditable, and independent of local
  cache usability. Keep failed/revoked/retained/promoted states as records.
- Implement typed settings sections with default, repository, workspace, user, machine,
  and run scopes; migrations; secret references; and per-value provenance.
- Render the same settings registry through CLI and a headless JSON API. An optional
  generic console may exist, but OVA retains its own product UI.

### 7. Turn samples into the conformance system

Create complete, standalone Components for:

- hello/no-dependency generation;
- generated library consumed by a generated application;
- multi-Component service stack;
- aggregate multi-repository source;
- capability ambiguity and policy-driven resolution;
- model group routing, fallback, locality, and unavailable endpoints;
- empty/warm/offline cache behavior;
- failed/resumed publication;
- constrained, blocked, reviewed, and `yolo` security paths;
- a framework compatibility/readiness application plus a separately classified exact
  snapshot-replication conformance fixture; and
- target-neutral base behavior composed across Windows/Linux, CPU/optional-or-required
  CUDA, and Python/Rust Flavors, including conflict and cache-isolation cases.

Each sample contains one complete `component.md` by default, pinned authoring inputs,
expected lifecycle behavior, positive and negative assertions, and an
executable/verifiable entrypoint. Repository-only vectors and private oracles live under
`samples/_harness/`. Samples are release-blocking living documentation.

### 8. Add OVA compatibility and shadow execution

- Build `compatibility/ova` without importing OVA into the framework core.
- Read `ova.yaml` and legacy settings/cache records; produce new objects plus explicit
  mappings while leaving original files untouched.
- Run representative OVA operations twice: authoritative legacy execution and a
  side-effect-free literate-ai shadow execution.
- Compare selected closures, evidence identity, generated tree, validation outcome,
  package lineage, model decisions, settings provenance, and stable errors. Where exact
  byte parity is intentionally impossible, check a reviewed semantic migration record.
- Exercise the same fixtures on macOS and Linux with clean and warm caches.

### Phase 1 exit gates

Phase 1 completes only when all of the following are true:

- no OVA/Omniverse/Isaac imports or special cases exist in core packages;
- all schemas, canonical identities, migrations, and OVA fixtures pass contract tests;
- descriptor discovery works without eager source/index materialization;
- exact source/index/evidence checks and TOCTOU rejection pass adversarial tests;
- the durable workflow resumes after injected failures without partial acceptance;
- signed-source verification and classification/build authorization are enforced;
- immutable packages survive concurrency, reverse-edge rebuild, and collision tests;
- typed settings and explicit publication pass migration/recovery tests;
- every neutral sample passes from an empty cache on macOS and Linux;
- effective revisions are deterministic, base specs remain unchanged, Flavor conflicts
  fail closed, and target-specific cache/artifact aliases cannot cross-contaminate;
- exact snapshot replication reaches stable semantic output on its second replay without
  being represented as coding-CLI source generation;
- OVA shadow comparisons pass for the agreed fixture matrix; and
- literate-ai releases a pinned compatibility version with rollback documentation.

## Phase 2 — Rebase OVA and retire duplication

literate-ai is authoritative for migrated lifecycle operations in this phase. OVA owns
its product policy, UI, runtime integration, and Omniverse/Isaac-specific adapters.

### 1. Introduce the OVA adapter boundary

- Pin OVA to the released literate-ai version and add an `ova-literate-adapter` package.
- Move `ovstage`, `ovrtx`, `ovstream`, `ovphysx`, Omniverse/Isaac/ROS catalog data,
  capability questions, native validators/builders/linkers, NVIDIA model defaults, and
  OVA workflow prompts into the OVA adapter.
- Express OVA macOS/Linux, RTX/CUDA, ROS distribution, and language/toolchain variants as
  downstream Flavor definitions; no concrete NVIDIA Flavor identity enters framework core.
- Keep `ova.yaml` supported through the compatibility provider while making new OVA
  Components use canonical `component.md` authoring. NVIDIA/OpenUSD policy and resource
  descriptors remain OVA-owned inputs reached through declared selectors rather than
  unknown frontmatter or a second Component schema.
- Put each migrated use case behind a reversible feature flag with a recorded reason.

### 2. Cut over one lifecycle seam at a time

Cut over source identity and cache import, intelligence, composition, generation,
validation, security/build authorization, packaging/linking, publication, and settings
in that order. For each seam:

- run legacy and framework paths against the same exact inputs;
- compare immutable outputs and accepted external behavior;
- canary the framework path;
- retain the old read path for rollback through the compatibility window; and
- remove writes from the old path before moving to the next seam.

The importer reads `~/.ova` caches and publication records on demand, maps identities,
and copies verified blobs into the new store only when needed. It never changes or
deletes the legacy cache. New mutable references live in platform-appropriate
literate-ai directories selected by the settings service.

### 3. Rebase OVA presentation and operations

- Make the OVA Settings UI consume the typed settings/status/action API rather than
  inspecting filesystem details directly.
- Keep distinct UI sections for model groups/selectors, component/source caches,
  security/classification including persistent `yolo` warnings, and publication.
- Add a target/Flavor section showing requested versus resolved OS/GPU/language axes,
  availability, conflicts, effective revision identity, and explicit selection.
- Forward existing CLI and Make targets to application use cases while preserving
  stable structured errors during the deprecation window.
- Keep OVA-specific samples in OVA and run them as downstream literate-ai conformance
  tests. Move only software-neutral samples to literate-ai.

### 4. Prove OVA-on-literate-ai self-hosting

- Starting with empty new caches and a read-only legacy cache, materialize the exact OVA
  dependency closure from signed sources.
- Generate a candidate OVA Component with grounded evidence, classify and authorize it,
  build/package it with OVA adapters, run the complete non-native test and macOS launch
  gates, then use that candidate to generate a second candidate.
- Compare specifications, resolved closure, source/evidence identities, generated tree,
  behavior, packages, and provenance. Record explained nondeterminism rather than hiding
  it behind timestamps.

### 5. Retire duplicated OVA implementation

- Make the framework path default only after canary and rollback drills pass.
- Ship at least two OVA releases that can read legacy records and roll back to the old
  read path without writing incompatible data.
- Remove the duplicate general schemas/services only after dependency analysis proves
  no OVA code imports them. Keep compatibility readers for the documented support term.
- Publish a final identity/cache migration report and deprecation guide. Deleting a
  legacy cache remains a separate, explicit user action.

### Phase 2 exit gates

- OVA behavior, stable errors, and provenance either match or have reviewed migration
  records for every baseline fixture.
- Existing caches work through read-only import; corruption and interrupted migration
  recover safely; rollback was rehearsed on a real migrated cache copy.
- OVA's complete test suite, strict OpenSpec gate, sample gate, and macOS launch pass on
  the framework path.
- Native builds cannot start without signed-source verification and classification-bound
  authorization; `yolo` behavior is visible and audit-complete.
- Source, model, generation, package, publication, and settings UIs show framework-owned
  object identities and lifecycle states.
- A candidate OVA built on literate-ai successfully produces a second semantically
  equivalent candidate.
- No duplicated general lifecycle implementation remains in OVA, and the dependency
  direction is `OVA -> literate-ai`, never the reverse.
- Production default, feature-flag rollback, compatibility-reader retention, and removal
  dates are documented in both repositories.

## Ownership after migration

| literate-ai owns | OVA owns |
|---|---|
| Component/revision/spec/source/evidence/workflow/package/publication/settings/security contracts | OVA product behavior and user experience |
| lifecycle state machines and policy gates | Omniverse, Isaac, ROS, and Kit catalogs/policies |
| ports and neutral reference adapters | OVA-native validators, builders, runtime linking, and launch |
| neutral CLI/API and optional generic console | Litestar OVA UI and OVA CLI compatibility wording |
| CAS, events, identity rules, migrations, conformance | OVA deployment, NVIDIA defaults, and integration samples |
| neutral living samples and lifecycle conformance proofs | OVA-on-framework self-hosting and platform acceptance |

This boundary lets other products use the same spec-led lifecycle without importing OVA
and lets OVA follow fast-moving source components without re-owning the framework.

## Literate-ai implementation Phase 8 (not an OVA migration phase)

The OVA rebase remains exactly two phases. After the seven bootstrap work packages in
the active change, literate-ai has a separate Phase 8 for first-class skill-directed
source-to-specification authoring. It changes only this repository; OVA adoption is a
later downstream decision. The normative Phase 8 scope, contracts, risks, tasks, and
gates are in
[`phase-8-source-to-specification.md`](../roadmap/phase-8-source-to-specification.md).

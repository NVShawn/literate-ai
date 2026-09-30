# Evaluation of OVA's AI-Driven Development and Component Model

## Scope and evidence

This evaluation uses OVA commit `811c71c9472ec07a5bae2dba32f6d8f21b0802bb`
(`feat: add source-grounded component system`) as its extraction baseline. The receiving
repository baseline is literate-ai commit
`1caa902` (`Initial commit`).

The review covers the complete source-grounded implementation, not only its design
documents:

- `src/ova/models/source_grounding.py` and its strict Pydantic schemas;
- source, CodeGraph, composition, cache/linker, model, generation, publication, and
  prompt-journal services under `src/ova/services/`;
- the `ova.source_grounding` CLI and Settings routes;
- root and sample `ova.yaml` manifests;
- the active `adopt-source-grounded-components` OpenSpec change;
- the simple-to-self-hosting sample ladder; and
- the source-security classification plan.

At the baseline, this subsystem comprises roughly 5,943 implementation lines across
eleven central Python modules, plus focused unit and integration tests. OVA's full gate
reported 366 passing non-native tests, strict OpenSpec validation, a clean macOS launch,
and a real deterministic OVA-in-OVA source-grounded generation transaction.
The independent extraction reviewer also re-ran 82 architecture-focused tests with all
passing. Those results validate the current vertical slice; they do not satisfy the
redesign and migration gates below.

## Executive verdict

OVA has invented the outline of a valuable general system, not merely an Omniverse code
generator. Its best idea is the continuity chain:

```text
intent -> specs -> exact dependency closure -> exact source -> knowledge index
       -> evidence -> API contracts -> file plan -> generated source -> validation
       -> source package -> artifact package -> publication -> reusable Component
```

Each arrow is intended to produce an explicit, digest-bound object. Generated output
returns to the beginning as another composable Component. Model selection, skills,
caches, and publications are part of the lifecycle rather than ambient process state.
That is the correct foundation for a modern form of literate programming.

The implementation is also still a first vertical slice. Its reusable domain model is
interleaved with OVA names, Omniverse policy, CodeGraph CLI details, Python compilation,
filesystem layouts, Litestar routes, and four hard-coded LLM stages. Several objects
called immutable are updated through mutable projections, security is plan-only, and
the self-hosting sample proves recursive composition rather than a byte-for-byte OVA
rebuild. Copying the subsystem wholesale would preserve these accidental constraints.

The recommendation is therefore **extract the invariants, redesign the boundaries, and
port behavior through compatibility fixtures**. Do not begin by renaming `ova` to
`literate_ai`.

## What OVA got right

### 1. A generated application is a Component

`OvaComponentSpec`, `CachedOvaComponent`, `ComponentSourceLock`, and the registry make
generated applications discoverable in the same system as framework and catalog
inputs. This enables recursive composition and is the core reason OVA can eventually be
built in OVA.

The general framework must retain this invariant while separating a Component's
logical identity from its immutable revisions and machine-local projections.

### 2. Specifications are generation inputs, not documentation afterthoughts

Every v2 manifest names canonical OpenSpec capability or active-change files.
`OpenSpecArtifactLoader` loads their complete contents, records their digests in the file
plan and generation provenance, and checks them again immediately before acceptance.
`ComponentSpecJournal` preserves user intent in the active proposal.

The TOCTOU check and full-content binding are worth retaining. The journal mechanism
needs redesign: prompt events should be immutable provenance records with an OpenSpec
projection, not comments injected directly into one proposal file.

### 3. Source knowledge is revision-bound evidence

OVA refuses model queries until `ComponentSourceLock` and `CodeGraphIndexLock` agree on
the source revision and digest. `CodeEvidence` carries source, index, query, content,
symbol, and location identities. `EvidencePolicy` checks those identities again at API
contract, plan, and generated-file boundaries.

This is substantially better than SDK-name prompting or documentation-only retrieval.
The exact-source/index/evidence barrier should be a framework invariant, while
CodeGraph becomes the first implementation of a general knowledge-index port.

### 4. Generation is staged and contract-bearing

`SourceGroundedGenerator.generate()` does not request an application in one opaque
prompt. It resolves composition, assembles the full vocabulary, retrieves evidence,
creates component API contracts, creates an evidence-bound file plan, generates files,
validates staged output, performs bounded evidence-assisted repair, checks spec drift,
accepts output, and writes provenance.

The stages and typed transition records are strong. The fixed stage names and one
orchestrator method are not. The neutral system needs a versioned workflow definition
whose default code workflow happens to contain these stages.

### 5. Model policy belongs to the Component

`ModelEndpoint`, `ModelGroup`, `ModelStageSelector`, and `ComponentModelSelector` make
model routing explicit and portable. Groups are versioned and digestible; selectors
express capabilities, locality, context size, cost, providers, and fallback. Exact
selection decisions are written to provenance. Credentials are indirect environment
references and are not rendered or persisted by Settings.

This is a general capability. It should become workflow-stage policy rather than
requiring OVA's four generation stages in every Component schema.

### 6. Empty local caches and explicit publication are first-class

`ComponentFinder`, `ComponentSourceCache`, `CodeGraphIndexManager`, and
`ComponentCacheStore` support local, per-user fault-in. Source packages carry forward
dependencies; artifact packages carry the selected dependency package mapping; the
linker resolves a closure. `SourceCachePublicationService` is deliberately separate
from cache readiness and publication is explicit by default.

That separation is essential for offline, private, and single-user workflows.

### 7. Samples carry executable architectural meaning

The sample ladder covers simple, intermediate, advanced, composed, and self-hosting
tiers. Each sample is itself a Component with specs, a pinned skill, a model selector,
requirements, acceptance criteria, and an entrypoint. Strict OpenSpec validation and a
sample checker turn the playground into conformance input.

The neutral framework should adopt this pattern and replace OVA/Omniverse scenarios
with software-neutral equivalents. OVA-specific samples remain in OVA as downstream
integration tests.

### 8. The security design separates authenticity from safety

The checked-in plan correctly states that authoritative signatures establish origin
and integrity but do not establish safe behavior. It proposes quarantine, scanning,
classification locks, dependency-risk propagation, compiler enforcement, audit, and a
non-default identity-scoped expiring `yolo` policy.

This belongs in the general framework. It is not implemented in the baseline and must
remain described as planned until adversarial tests prove it.

## Structural problems to fix during extraction

### Domain and naming coupling

- `OvaComponentSpec`, `OvaComponentIdentity`, `CachedOvaComponent`, `ova.yaml`,
  `OVA_HOME`, and `ova-generated` make a general concept application-specific.
- `ApplicationIntentSpec = OvaComponentSpec` aliases two lifecycle roles that should be
  distinct: a durable Component definition and a request to produce a revision.
- `kind` is restricted differently in generated manifests, catalog records, and cached
  records. Application, library, framework, sample, and tool are roles or profiles, not
  mutually exclusive identities.
- One 923-line schema module combines every bounded context and encourages cross-layer
  imports.

### Resolver policy is not neutral or sufficiently expressive

- `_FOUNDATION_COMPONENTS = ("ovstage", "ovrtx", "ovstream")` is injected into every
  composition.
- Physics requirements implicitly add `ovphysx`.
- If multiple providers exist, lexicographically first Component ID wins unless a
  preferred ID is supplied.
- Compatibility is currently asserted with the reason "source and CodeGraph index
  identities validated"; platform, version range, ABI, license, policy, feature, and
  toolchain compatibility are not resolved.
- The dependency lock's digest includes timestamps, so logically identical resolution
  runs receive different identities.

Provider selection must become an explicit policy port with a deterministic explanation
object. OVA foundation and physics behavior belongs in an OVA policy adapter.

### Complete vocabulary currently means eager global materialization

`ComponentCompositionService.prepare_all_components()` calls `fault_all()`, so forming
the vocabulary can clone and index every enabled repository. This works for a small
catalog and fails as the ecosystem grows. The live OVA self-hosting verification exposed
this directly through large Git LFS payloads and source-empty meta repositories.

The framework needs two levels:

1. a complete descriptor vocabulary containing every discoverable Component and its
   availability/identity metadata; and
2. exact source evidence faulted only for the selected closure and explicitly requested
   comparison candidates.

Unavailable entries must remain visible without being presented as usable API evidence.

### Source identities are not yet uniformly content identities

- Remote `content_digest` is derived from repository URL and resolved revision rather
  than a canonical tree digest.
- Mutable or retagged remote refs are resolved at fault time. The resulting commit is
  locked, but signature and transparency-log verification are not implemented.
- Local snapshots use a fixed ignore list, synthesize Git commits, and can include file
  mode or platform normalization ambiguity.
- Git LFS now skips non-source payload smudging and fails if known source paths remain
  pointers, but signed LFS object identities are not modeled.
- Source collections such as ROS distribution manifests require a first-class aggregate
  source lock rather than pretending a manifest repository contains the APIs.

The neutral source layer needs canonical tree manifests, source-provider attestations,
aggregate snapshots, and explicit dirty-source policy.

### Evidence retention and prompt allocation are weaker than the lock model

- `CodeGraphRetriever` normalizes human-oriented Markdown with regular expressions. A
  presentation change can therefore become an intelligence-protocol change.
- Vocabulary evidence for every usable Component is appended before detailed evidence
  and then subjected to one sequential 140,000-character limit. A sufficiently large
  catalog can consume the budget before selected-component API/lifecycle evidence.
- Provenance retains evidence references and content digests, but not the evidence bytes
  or a durable artifact-store address. Removing the checkout can make an old decision
  impossible to reconstruct.
- File-level evidence IDs prove that the model declared a binding. They do not prove the
  generated source actually uses only those symbols or that the derived contract is
  semantically faithful to the evidence.

Use structured provider results, CAS-backed evidence artifacts, selected-closure-first
token-aware budgets, and language/provider analyzers that reconcile proposed API-use
graphs with source contracts.

### Cache immutability is partially violated

`ObjectPackageManifest` contains `depending_components`. When a new consumer is built,
`_record_dependent()` mutates the dependency's stored object manifest. This means an
object presented as an immutable package changes because of an external event.

The fix is to keep immutable package manifests content-addressed and store reverse edges
in a separate, rebuildable dependency-index projection. Mutable `latest` aliases must
also live outside package identities.

Object-package construction also removes and rebuilds an existing destination directory.
`latest_object_packages()` silently omits dependency objects that have not been cached,
so a source lock can name a requirement missing from the recorded build closure.
Generated cache records and source packages flatten every transitive locked Component
into `requires`, losing direct/transitive and build/runtime/optional edge meaning. The
new package model must preserve typed edges, fault or fail on every required artifact,
and reject an attempted content-address collision instead of overwriting it.

### Build semantics are a Python-specific placeholder

`build_object_package()` compiles `.py` to checked-hash bytecode and copies every other
file as a resource. This is useful test coverage for package lineage, not a general
builder. It also runs before the planned classification lock exists.

The framework needs builder ports, toolchain locks, hermetic execution requests,
classification authorization, and typed artifacts. No builder—including Python—may be
invoked without the security transition once enforcement is enabled.

This is a present contract contradiction, not only missing hardening:
`SourceGroundedGenerator.generate()` invokes the Python object builder before any
classification lock exists, while the checked-in security specification says
unclassified source must never compile. General build APIs remain disabled until the
compiler guard and adversarial tests are executable.

### Acceptance is not transactionally atomic as a set

Each generated file is written atomically, but `ProjectGenerationTransaction.accept()`
updates files one at a time. A crash can leave a project containing part of a generation
set. Lock files and provenance are written after source acceptance, which can further
separate state.

Use an immutable workspace tree or transactional directory swap, followed by an atomic
revision reference update. A run event store must make restart/reconciliation possible.

### Workflow and provenance need stronger semantics

- Four model stages are required by the Component schema even for non-code workflows.
- Prompt template identities exist, but raw model response identities, decoding
  parameters, seeds, token/cost accounting, and policy decisions are incomplete.
- Repair is bounded but orchestration is not resumable and durable state is implicit in
  files.
- Source excerpts are treated as untrusted in the system prompt, but there is no
  structural taint model or tool-output policy.
- A timestamp-bearing provenance object is useful history but should not be confused
  with the deterministic identity of a run's inputs.

Separate deterministic run input identity from execution events and operational
timestamps. Make workflow definitions, stage contracts, and policy decisions versioned
objects.

Endpoint `locality` is currently asserted metadata, not a verified transport/network
property, and CLI loading does not uniformly apply the URL checks used by Settings.
`ModelPortfolio.decisions` is mutable shared history, which makes concurrent provenance
unsafe. The new model layer must enforce prompt-egress policy and record isolated
request/response artifacts, model revision/fingerprint, parameters, token/cost data,
retry/failure/fallback reasons, and data classification for each call.

### Settings and publication are functional vertical slices, not framework contracts

- Core settings are OVA environment variables and paths.
- The Litestar page mixes filesystem discovery, model configuration, cache status, and
  publication actions.
- There is no setting scope model for repository, workspace, user, machine, and one-run
  overrides or schema migration.
- Publication supports a filesystem target, but not registry capabilities, signed
  manifests, resumable upload, revocation, retention, or promotion channels.
- Current publication copies a whole mutable `cache.json` rather than publishing a signed
  immutable release manifest and has no verified pull/import lifecycle.
- `auto_publish` is modeled but neither exposed nor honored; source-security settings are
  absent.

The neutral core should expose typed settings sections and publication ports. A CLI,
JSON API, or optional UI can render those schemas. OVA keeps its Litestar presentation.

### The current self-hosting proof is intentionally narrow

`samples/ova-self-hosting` proves that OVA can be selected as a Component and that a new
immutable generated Component can be produced from that composition. The deterministic
backend emits a descriptor file; it does not regenerate OVA's complete source tree,
rebuild its distribution, run its full tests, or compare behavior.

True framework self-hosting requires a clean-cache bootstrap that regenerates a
candidate framework revision, builds it with an accepted toolchain and classification,
runs conformance tests, and uses the candidate to repeat the process with stable
semantic results.

Several sample specifications also use normative language for classification, relinking,
and self-hosting behavior that their descriptor/fixture tests do not execute. Until
end-to-end gates exist, those samples are architectural promises rather than evidence of
implementation.

### Versioning, scale, and operational lifecycle remain early

- Component IDs and `kind` use inconsistent validation/taxonomies across manifests,
  catalog entries, generated records, and packages; several version fields are
  unvalidated strings.
- project package metadata and the root Component manifest report different OVA version
  concepts without a declared relationship.
- source/index/query preparation is sequential and currently revisits every usable
  Component on each generation.
- JSON registries have no general schema migration, recovery scan, pin, quota, retention,
  or garbage-collection framework.
- runtime artifact locks exist in the model but are not populated by composition.
- the source-grounded v2 generator remains a separate CLI while the Studio still owns an
  older creator path, so two development models coexist.

These are acceptable prototype limits but must become explicit contracts, projections,
operations, and downstream integration gates before the framework or OVA can claim one
unified lifecycle.

## Extraction decision matrix

| OVA area | Decision | literate-ai destination | What remains in OVA |
|---|---|---|---|
| Strict schemas, path validation, canonical digests | Generalize | `domain/identity`, per-context models | OVA schema compatibility mapper |
| `OvaComponentSpec` and manifests | Redesign | `ComponentDefinition`, `ComponentRevision`, `component.json` | `ova.yaml` reader and OVA extension fields |
| OpenSpec artifact loading and drift check | Extract behind port | `ports/specs`, `adapters/specs/openspec` | OVA capability vocabulary/spec content |
| Prompt journal | Redesign | immutable `IntentEvent` plus OpenSpec projection | OVA prompt capture UI hook |
| Skills with content digests | Generalize | `ToolRecipeRef`/`SkillRef` | OVA-specific authoring skills |
| Catalog and generated registry | Generalize | descriptor registry and revision registry ports | Omniverse/Isaac catalog entries |
| Source cache, leases, Git/local snapshots | Generalize and harden | source domain plus Git/local/CAS adapters | OVA source-provider configuration |
| CodeGraph index and evidence | Generalize behind port | knowledge domain plus CodeGraph adapter | OVA query templates and capability questions |
| Composition and dependency locks | Redesign | policy-driven resolver | foundation/physics provider policy |
| Complete vocabulary | Redesign | descriptor vocabulary plus lazy grounded closure | OVA catalog presentation |
| Model portfolio/groups/selectors | Generalize | model domain and provider adapters | NVIDIA defaults and OVA UI wording |
| Staged generator and evidence policy | Generalize as workflow engine | run/workflow application services | OVA workflow/profile and prompts |
| Python syntax validator | Extract as plugin example | validator adapter | Omniverse/Isaac validators |
| Source/object package cache | Redesign as immutable CAS | package domain/storage ports | OVA legacy-cache importer |
| Reverse dependents | Reject current mutation | separate dependency graph projection | none |
| Python bytecode builder | Keep only as example adapter | optional Python builder | native OVA/Kit/Isaac builders |
| Component linker | Generalize | artifact closure resolver | OVA runtime linking/launch adapter |
| Filesystem publication | Generalize | filesystem publisher adapter | OVA Settings actions |
| Settings service/UI | Split | typed settings core; optional headless API | Litestar templates/routes and runtime settings |
| Sample ladder | Extract pattern and neutral samples | `samples/` and conformance suite | graphics/robotics samples |
| OVA self-host sample | Replace with neutral snapshot-replication proof | framework readiness sample | OVA-on-framework self-host integration |
| Security classification plan | Move and implement centrally | security domain/policy/runner ports | OVA platform-specific isolation profiles |

## Readiness assessment

| Concern | Baseline quality | Extraction implication |
|---|---|---|
| Conceptual Component model | Strong | Preserve, but split logical identity/revision/projections |
| Specification continuity | Strong prototype | Preserve OpenSpec adapter; redesign intent journal |
| Source and evidence grounding | Strong prototype | Preserve invariants; generalize engine and strengthen identities |
| Composition | Early | Replace OVA defaults and simplistic provider selection |
| Model routing | Strong prototype | Extract with arbitrary workflow-stage support |
| Generation orchestration | Strong vertical slice | Recast as durable workflow/event state machine |
| Cache/package lineage | Useful prototype | Move to true CAS and external graph projections |
| Publication | Narrow but well-separated | Retain separation and add registry protocol |
| Settings | Good OVA UI | Define scopes/schema/secrets before extracting presentation |
| Samples | Strong documentation pattern | Rebuild as neutral conformance fixtures |
| Self-hosting | Composition proof only | Add full bootstrap/rebuild proof |
| Security | Thoughtful plan, no enforcement | Phase 1 blocker for any compiler execution |
| Schema/version migration | Missing | Design before first public release |
| Operational restart/recovery | Missing | Add durable runs and reconciliation |

## Conclusion

OVA should not remain the owner of these concepts, and literate-ai should not become an
OVA utility library. The correct boundary is a portable lifecycle kernel with explicit
ports and immutable wire contracts. OVA becomes one application adapter and one demanding
conformance consumer. The migration plan makes OVA authoritative during Phase 1 and
removes its duplicated implementation only in Phase 2 after real parity evidence.

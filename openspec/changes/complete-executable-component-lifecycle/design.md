## Context

See `proposal.md` for motivation. The current working tree is already evolving project,
cache, source-intelligence, and source-translation v2/v3 contracts beyond `08c774c`; it
must be checkpointed before parallel implementation. The current generation adapter forms
one request from root and dependency documents, generated artifacts are text-only, build
results cannot describe multi-Component exports, and ordinary projects still depend on a
project-pinned external lifecycle driver. The existing framework identities, guarded
execution, workspaces, caches, bundles, SBOMs, source-to-specification lifecycle, model
routing, tests, acceptance, and publication records remain the foundation.

## Current Implementation Boundary

The contract/planning foundation and the core node lifecycle are implemented and tested.
`StandardProjectLifecycleService` consumes complete prepared nodes, invokes only the
source-only generation boundary, receives the complete tree/bundle-distinct candidate,
and then performs current indexing, authorization, plan finalization, dependency-artifact
build, node test, execution, acceptance, project admission, and receipt issuance. Reused
source skips the coding runner but never skips current index or authorization checks.
Changed authorization or provider exports rebuild downstream work without forcing source
regeneration.

This is not yet the full design. Cache membership remains `CACHE-250`; link/package and
release closure, publication/import, durable stage interruption and retry lineage,
application-service/CLI/default-driver integration, and ordinary sample and qualification
adoption remain open. The current accepted-node resume boundary is not evidence of
process-restart-safe interruption at every stage.

The current Component authoring boundary is deliberately simpler than the pre-release
sample layout assumed when this change began. `component.md` frontmatter carries portable
Component metadata and its Markdown body is the default `literate-markdown` behavioral
root. An extra specification or interface document is justified only by a named domain,
module, protocol, or independently consumed public Component boundary. Repository harness
vectors and private oracles remain outside Component authority. No service in this change
may reintroduce `component.json`, `openspec/app.json`, `openspec/spec.md`, or an
`acceptance/` directory as peer authoring files for a normal Component.

## Goals / Non-Goals

**Goals:**

- Make Component boundaries executable and observable without application-specific core code.
- Bound every coding-agent request to local authority and direct public interfaces.
- Support arbitrary resources and typed build/link/package edges with exact provenance.
- Provide one public standard lifecycle suitable for CLI, samples, qualification, and IDEs.
- Preserve safe interruption, exact cache invalidation, and truthful target releases.

**Non-Goals:**

- Defining an OVA, Omniverse, UI, provider, language, or build-system-specific core.
- Introducing generic Component inheritance or flattening refinement, Flavor, lineage, and dependency semantics.
- Promising one universal standalone binary format.
- Treating model determinism, cache hits, local builds, or publication records as acceptance by themselves.

## Decisions

### Checkpoint the live v2 foundation before parallel implementation

The current project-v2, cache-v2, source-intelligence, compatibility-schema, and source
translation changes form Wave 0. The integration owner records one clean baseline and its
schema identities before other agents edit shared catalogs, public exports, or CLI
registration. Branching work from `08c774c` would discard real contract evolution.

Alternative considered: let each lane rebase the dirty working tree independently. This
would make schema identity and migration fixtures nondeterministic.

### Use four authority layers

The architecture separates:

1. authored project and Component intent;
2. exact target/dependency lock;
3. per-Component execution/action DAG and accepted workspaces;
4. linked, packaged, qualified release variants.

Each layer points to the prior layer by exact identity and never rewrites it. Mutable UI or
cache indexes may reference these objects but cannot define them.

At the first layer, one canonical `component.md` normally contains both the portable
metadata projection and the default behavioral specification. “Specification set” remains
an identity and composition concept, not a requirement to scatter one Component across
multiple authoring files.

Alternative considered: continue combining authored choices and selected revisions in
`component.json`. That prevents portable reuse and truthful lock diffs.

### Make public interfaces first-class contracts

Capability providers expose retrievable versioned interface content, compatibility, and
artifact expectations. Generation contexts include only directly consumed interfaces.
Revision lineage, local spec-node refinement, typed Flavor contributions, and capability
edges remain separate relations.

Alternative considered: retain interface digests plus dependency prose. A digest cannot
generate a consumer and prose flattening leaks private transitive context.

### Plan and execute one action graph per target lock

The planner emits deterministic topological layers and node/action derivation keys. A
node has distinct generation, build, test, acceptance, link, and package actions where
applicable. Private changes invalidate the producer; exported-interface or artifact
changes invalidate exact consumers at the appropriate later action.

Alternative considered: one root recipe with all dependency documents. That is the
flattening this change removes.

### Assemble resources outside model text

Generated text and authored arbitrary bytes remain distinct until a source manifest
assembles them under collision checks. Typed exports/imports carry role, media type,
target, ABI/compatibility, producer, action, and blob identity. Builders and packagers are
ports; Bazel or any other implementation is an adapter choice.

Alternative considered: base64 resources in prompts or generic file bundles. Both lose
ownership, collision safety, and linking semantics.

### Provide one standard public lifecycle

A `StandardProjectLifecycleService` owns the normal topological lifecycle. Public
application services own validation, lock, planning, recipe construction, status/events,
cancellation, artifacts, and release inspection; CLI modules are presentation adapters.
Content-pinned external drivers remain an advanced trust boundary, not the golden path.

Alternative considered: teach each application adapter to orchestrate framework ports.
That recreates inconsistent lifecycle authorities and is explicitly rejected.

### Cache final accepted Component actions

Requested derivation keys are predictable; complete candidate identities include all
observed outputs and evidence. Membership is per Component action and only final accepted
results are reusable. Hits are always checked against current lock, target, policy, and
receipt requirements.

Alternative considered: cache the aggregate root tree. It prevents mixed reuse and makes
shared dependency invalidation opaque.

### Model applications and suites through root Components

An application is a root Component plus a target lock. Multiple roots can share exact
dependencies. Only collections with atomic build/test/release behavior become distribution
Components; cosmetic organization remains client-owned.

Alternative considered: add a terminal Application class. Components already express the
needed recursion and adding a second terminal type would fork the taxonomy.

### Integrate source-to-specification through public promotion services

Promotion emits reviewed node kinds, interfaces, resources, entrypoints, target intent,
and acceptance boundaries into canonical `component.md` documents, with extra named
documents only where the recovered public/domain boundary warrants them. Source remains
release authority until qualification earns transfer. OVA and other clients consume
structured services, not CLI-private helpers or a second Component schema.

Alternative considered: let clients reinterpret promotion envelopes. That creates a
second Component schema and can fabricate application semantics.

## Risks / Trade-offs

- [Large wire-contract change] -> Freeze schema identities first, retain characterization fixtures, and reject implicit pre-release migration.
- [Public-interface design becomes a new monolith] -> Keep language-neutral compatibility and artifact expectations in core while language bindings remain adapters/Flavors.
- [Context remains indirectly unbounded] -> Emit segment manifests and test canaries proving private graph content has zero identity or byte effect.
- [Cache reuse bypasses current policy] -> Revalidate all hits and publish membership only after independent acceptance.
- [Parallel agents conflict in central modules] -> Use three bounded lanes while one integration owner alone edits schema indexes, public exports, CLI registration, and the lifecycle composition root.
- [Packaging scope delays executable composition] -> Land contracts and the diamond lifecycle first; add package adapters behind the stable artifact graph.

## Migration Plan

1. Checkpoint the current live v2 foundation and pass schema, project, wheel, and compatibility gates.
2. Land executable Component/interface and authored-intent/lock contracts.
3. Land per-Component plans, bounded contexts, and public planning services.
4. Land resources, artifact exports/imports, builders, linking, packaging, and release manifests.
5. Land the standard lifecycle and per-node caches; replace sample-specific orchestration.
6. Land application/source-promotion services and the diamond, multi-root, binary-resource, and initialized-project fixtures.
7. Run compatibility, property, mutation, platform, packaging, documentation, and release gates.
8. Publish one validated upstream checkpoint for OVA to embed; rollback is selecting the prior exact embedded checkpoint, never mixing contracts.

Implementation uses three workers plus one integration owner per wave. Contract/schema,
lifecycle/cache, and adapter/sample lanes have disjoint file ownership; the integration
owner serializes changes to shared exports, schema indexes, CLI registration, and final
quality gates.

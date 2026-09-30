# Framework premise and implementation assessment

- Review date: 2026-08-05
- Repository snapshot: 8e4ebdb
- Status: architectural assessment, not an implementation guarantee

This assessment reviews Literate AI's fundamental premise, its implementation, and the
degree to which the implementation follows the stated design. Three independent review
tracks examined the premise and architecture, source-to-specification maturity, and
operational proof. The comparison cohort was selected by web research before the local
review.

## Verdict

Literate AI attacks a real problem with an unusually strong trust model, but it treats
its boldest hypothesis—source is fungible—as an invariant before the implementation has
earned it.

The blunt assessment is:

- architectural taste: **8/10**;
- scope restraint: **3/10**;
- current usability: **4/10**;
- enterprise readiness: **3/10**; and
- overall today: **6.5/10 — needs improvement**.

The framework is currently an excellent evidence-and-attestation kernel surrounding a
one-shot code generator. It is not yet a generally usable full-SDLC product.

The premise is sound if narrowed to this statement:

> Generated source is non-authoritative, and may become disposable only after a
> Component earns regenerative qualification.

The universal version is not sound yet.

## High-adoption comparison

The research filter was approximately 20,000 or more GitHub stars and 3,000 or more
forks as observed on 2026-08-05. Stars and forks are adoption signals, not evidence of
technical correctness.

Scores are from one through five for durable **S**pec authority, runnable **E**2E,
**T**rust and provenance, **B**rownfield or inverse support, **P**ortability, and
operational **M**aturity. The total measures fit against Literate AI's stated mission,
not general usefulness.

| Framework | Approximate stars / forks | Essence | S/E/T/B/P/M | Total |
| --- | ---: | --- | --- | ---: |
| **Literate AI design target** | — | Regenerable Components with exact provenance, Flavors, skills, qualification, and SBOMs | 5/5/5/5/5/2 | **27/30** |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | 62k / 4.3k | Lightweight, brownfield-first proposal, specification, design, and task deltas | 4/2/2/5/5/4 | **22/30** |
| [GitHub Spec Kit](https://github.com/github/spec-kit) | 121k / 8k+ | Constitution → Spec → Plan → Tasks → Implement across many coding agents | 4/3/2/3/5/5 | **22/30** |
| [OpenAPI Generator](https://github.com/OpenAPITools/openapi-generator) | 26k / 7.5k | Deterministic generation of clients, servers, and documentation from a formal API specification | 5/3/3/1/5/5 | **22/30** |
| [BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD) | 48k / 5.6k | Role-based agents producing PRDs, architecture, stories, and implementations | 3/3/1/4/4/4 | **19/30** |
| **Literate AI current implementation** | private and new | Strong contracts and samples, but incomplete lifecycle and inverse proof | 4/3/4/2/3/1 | **17/30** |
| [MetaGPT](https://github.com/FoundationAgents/MetaGPT) | 69.5k / 8.9k | A multi-agent software company turning one requirement into code and design artifacts | 2/4/1/2/3/3 | **15/30** |
| [GPT Pilot](https://github.com/Pythagora-io/gpt-pilot) | 33.8k / 3.5k | Supervised, iterative app implementation with build and debugging feedback; no longer maintained | 1/4/1/2/3/1 | **12/30** |
| [GPT Engineer](https://github.com/AntonOsika/gpt-engineer) | 55.2k / 7.3k | Minimal natural-language-to-repository generation; archived in April 2026 | 1/3/1/2/3/1 | **11/30** |

Literate AI's differentiation is not specification-driven development; Spec Kit and
OpenSpec already communicate that idea more simply. Its differentiation is
content-addressed derivation, independent acceptance, non-authoritative caches, source
promotion, and supply-chain evidence.

The market lesson is to retain that trust kernel while borrowing Spec Kit and
OpenSpec's onboarding simplicity and GPT Pilot's explicit repair loop. BMAD and MetaGPT
belong on the task and agent-ledger side of the boundary, not inside Literate AI.

## Fundamental premise

The premise is strongest in four places:

- specifications, Flavors, and pinned skills form explicit generation authority;
- generated tests do not grade themselves because verifier-only acceptance remains
  independent;
- cache hits never inherit execution authority and must pass current acceptance; and
- signatures, provenance, build authorization, and behavioral safety have distinct
  meanings.

The weak point is the word **disposable**.

A model alias can change, disappear, or produce another implementation. The
documentation admits that provider weights and defaults are not immutable while also
claiming cache deletion cannot affect reconstruction ability. Those positions are
incompatible: exact prompt hashes prove what was requested, not that the generating
capability remains available.

Knuth's literate-programming transform was deterministic. An LLM synthesis is
underdetermined and probabilistic. The analogy works for human-centered explanation,
but not for reproducible tangling.

The framework should introduce an explicit authority ladder:

1. source-authoritative;
2. spec-assisted;
3. derived-source-retained; and
4. regeneratively-qualified-fungible.

Even in the final state, shipped source should be retained as non-authoritative release
evidence or escrow. Non-authoritative and throwaway are not the same property.

## Critical implementation findings

### 1. Source-free promotion is currently false, and qualification can transfer authority too cheaply

Coding-mode acceptance persists raw source excerpts and complete prompts in
.literate/source-translation.json, including original source text
([source_to_specification.py](../../src/literate_ai/cli/source_to_specification.py),
line 393).
Promotion then copies that entire accepted tree into the root Component
([source_to_specification.py](../../src/literate_ai/cli/source_to_specification.py),
line 1010).

Qualification nevertheless hard-codes source_excluded_from_generation as true
([qualification_runner.py](../../src/literate_ai/source_to_specification/qualification_runner.py),
line 356),
even though every coding-agent isolation profile is explicitly non-hermetic.

Worse, qualification counts successful shell commands rather than parsed test cases
([host_qualification.py](../../src/literate_ai/source_to_specification/host_qualification.py),
line 373).
Its own unit test transfers specification authority with a generated test containing
only assert True
([test_host_regenerative_qualification.py](../../tests/unit/test_host_regenerative_qualification.py),
line 37).

This is the most serious correctness issue. Qualification must reuse the normal forced
rebuild lifecycle, consume typed generated-test and verifier evidence, map probes to
required surfaces, and keep raw translation journals outside the generatable Component
closure.

### 2. Component composition is metadata composition, not executable composition

The system resolves a recursive Component graph, then concatenates every dependency's
documents into one GenerationRecipe and generates one source tree
([generation.py](../../src/literate_ai/cli/generation.py), line 1570).

Therefore:

- deep graphs become giant prompts;
- a leaf change regenerates the root;
- source-cache reuse is not per Component;
- child Components are not independently built or linked; and
- Bazel cannot provide the intended Component-level incremental economics.

Components currently survive in authority and SBOM metadata but disappear at the
derivation and build boundary. The lifecycle needs a real DAG: independently generate,
cache, and build each Component, then link exact artifacts.

### 3. A newly initialized project cannot execute the advertised golden path

The init command creates neither a lifecycle driver nor a test-receipt policy
([project.py](../../src/literate_ai/cli/project.py), line 296). The rebuild command
refuses to run without both
([rebuild.py](../../src/literate_ai/cli/rebuild.py), line 737).

The repository's only complete lifecycle is a 5,408-line sample runner, and that runner
imports private helpers from the CLI layer
([conformance_runner.py](../../tests/conformance/support/sample_runner.py), line 87). That contradicts the
documented domain ← application ← ports ← adapters ← CLI direction.

The actual golden-path application service has not been extracted. Literate AI
currently scaffolds an SDK from which a customer must construct a lifecycle driver.

### 4. Source-to-specification coverage is circular

The model's observations define the complete set of behavioral surfaces
([model_workflow.py](../../src/literate_ai/source_to_specification/model_workflow.py),
line 1645).
Coverage is then calculated only over those model-declared surfaces
([workflow.py](../../src/literate_ai/source_to_specification/workflow.py), line 437).

If the model omits behavior, the framework normally cannot detect the omission.

Additional problems are:

- evidence exceeding the per-language budget is silently dropped while the request
  still declares every inventory path included;
- four-language semantic parity accepts 13 of 16 keyword groups
  ([test_live_bidirectional_roundtrip.py](../../tests/conformance/test_live_bidirectional_roundtrip.py),
  line 81);
- only Python is fully promoted, regenerated, and qualified; and
- recovered graph nodes lack component-kind and entrypoint semantics, yet promotion
  makes every node a portable runnable application
  ([source_to_specification.py](../../src/literate_ai/cli/source_to_specification.py),
  line 1040).

This feature should be described as working alpha source-to-specification authoring,
with Python as the reference path, rather than semantic inverse equivalence.

### 5. Versioned-contract discipline has already been violated

The reviewed snapshot is twelve commits and approximately 99,876 inserted lines beyond
the v0.1.1 tag, while both package versions remain 0.1.1.

More importantly, component_graph_draft was added as a required field to the existing
urn:literate-ai:schema:v1:source-to-specification-result schema
([source-to-specification.schema.json](../../schemas/v1/source-to-specification.schema.json),
line 46).
The Python reader tolerates its absence, but the official v1 wire schema now rejects
previously valid v1 documents.

For a framework built around immutable versioned contracts, that is a release-blocking
defect. Either preserve v1 compatibility or introduce a new schema identity and
migration.

### 6. Operational proof trails the documentation

The repository configures verification/current.json, but that file does not exist
([literate.project.json](../../literate.project.json), line 92). CI does not run authenticated
samples, round trips, remote fan-out, release-check, or current-receipt validation
([ci.yml](../../.github/workflows/ci.yml), line 30).

Repository-source acquisition is still injected ports plus test doubles; ordinary
generation explicitly does not clone, index, build, and cache repository dependencies.
The samples are mostly dependency-free bounded JSON programs.

Coding-agent execution and native builds are non-hermetic and run as the current user
after explicit authorization. That is honest and acceptable for an alpha, but it is not
an enterprise containment boundary.

### 7. Scope has outrun proof

At the reviewed snapshot, the repository contained approximately:

- 101,967 Python lines across source and tests;
- 26 JSON schemas;
- several production modules between 1,500 and 4,500 lines; and
- a 5,408-line conformance runner.

That is too much framework surface for a four-day-old implementation whose reusable
lifecycle is still missing. The lockfile split, bounded repair loop, standard driver,
and one realistic application are more valuable than additional schemas or evidence
record types.

## What is genuinely excellent

The strongest parts should be preserved:

- the separation of invariants, policy, and deferred claims in
  [ADR 0003](../decisions/0003-constraint-classification.md);
- exact identities at authority crossings rather than indiscriminate hashing;
- generated tests versus independent verifier oracles;
- cache materialization remaining current-acceptance-untrusted;
- typed Flavor contributions instead of unordered overlays;
- exact skill pins instead of ambient agent instructions;
- the Component versus repository-only dependency distinction;
- pre-build and post-build CycloneDX evidence preserving the managed graph;
- source-derived specifications remaining drafts until explicit review; and
- the clean boundary between Literate AI as derivation engine and MAC as agent and task
  ledger.

## Hard-constraint assessment

The following should remain framework invariants:

- exact identities at semantic authority and execution boundaries;
- generated tests remaining distinct from independent verification;
- cache contents never becoming authority;
- explicit authorization before compilation or host execution;
- immutable content-addressed objects with separate mutable projections; and
- a valid signature proving integrity or origin, never behavioral safety.

The following are valuable policies rather than universal invariants:

- Bazel as the removable preferred build-system Flavor;
- no source-graph indexer as a product dependency;
- CycloneDX 1.7 as the selected SBOM wire standard;
- the yolo profile name and its exact privilege vocabulary; and
- this repository's complete cross-platform sample matrix.

The following claims should be relaxed or remain explicitly deferred:

- universal source disposability;
- exhaustive provenance for every internal operation;
- absolute completeness of every dynamic runtime dependency closure; and
- enterprise-safe execution before an operating-system containment boundary exists.

For dependency evidence, the invariant should be honest and explicitly bounded
knowledge:

- the managed Literate AI graph can be complete;
- the declared package graph can be complete to a named resolver boundary;
- the observed binary closure can be complete under a named environment and observer;
  and
- a dynamic runtime closure may legitimately be bounded, incomplete, or unknown.

## Recommended order of work

The executable follow-on is the
[framework score-improvement program](../roadmap/framework-score-improvement-program.md).
It assigns task IDs, acceptance evidence, score gates, dependencies, and parallel agent
lanes to these recommendations.

1. Make fungibility an earned Component qualification state.
2. Fix the source-free promotion and qualification defects before another release.
3. Extract and ship one reusable lifecycle application service and standard driver.
4. Execute generation, caching, and building per Component rather than flattening the
   graph.
5. Add a bounded, identity-bearing compiler and test repair loop.
6. Keep human `component.md` intent separate from generated `component.lock.json`.
7. Replace keyword parity with normalized requirement graphs, property tests, mutation
   testing, and invalid-input cases.
8. Add one stateful, networked, dependency-bearing application using both a package and
   a pinned Git dependency.
9. Add authenticated nightly and release workflows producing the compact current
   receipt.
10. Add a production sandbox before making enterprise-security claims.

Bazel should remain a removable project default, and
CycloneDX the selected wire standard. The invariant should be honest, explicitly
bounded dependency knowledge rather than pretending every dynamic runtime closure is
universally knowable.

## Verification performed

Three independent agents audited premise and architecture, source-to-specification, and
operational proof.

The full non-live suite produced:

- 770 tests;
- 764 passed;
- one failed;
- five skipped; and
- 455.486 seconds elapsed.

The historical failure was
ProjectCliTests.test_repository_is_a_self_describing_canonical_project. It exposed a
conflict between an active indexer daemon and a validator that treated live
SQLite sidecars as a frozen artifact. Wave 0 resolved that conflict: operational
validation now evaluates provider status against one disposable, race-checked source
mirror and consistent SQLite backup, while generated-artifact freezing remains a
separate idle-index operation. The active database and its daemon-owned sidecars are
not modified.

A focused inverse suite separately passed 55 tests in 9.57 seconds. Credentialed live
coding-agent tests were not run and remain excluded from normal CI.

No repository files were modified during the assessment itself.

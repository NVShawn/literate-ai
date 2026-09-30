# ADR 0003: Separate Invariants from Policy and Delivery Claims

- Status: Accepted for bootstrap
- Date: 2026-08-03
- Decision owners: literate-ai maintainers
- Supersedes: the undifferentiated “Non-negotiable properties” list in the root README

## Context

The first-day architecture used *non-negotiable* for three different things: semantic
boundaries, choices made by the reference implementation, and end-state guarantees that
still need proof. That wording can freeze replaceable choices and, more seriously, make
an incomplete security or provenance property sound delivered.

## Decision

Every high-level constraint has one of these classes:

| Class | Meaning | Change rule |
| --- | --- | --- |
| **Invariant** | Violating it changes the framework's meaning, loses authority, or creates a security/confused-deputy path. | Supersede this decision, migrate affected contracts, and update conformance proof. |
| **Policy/default** | The reference repository, a project, or a pinned policy chooses it today. Another conforming integration may choose differently and record that choice. | Change the owning policy or decision and its tests; do not present it as universal. |
| **Deferred claim** | It is a desired delivery property without complete end-to-end enforcement evidence. | Describe it as a target or gate. Do not advertise it as a current guarantee. |

“Must” is reserved for an invariant or for a rule inside an explicitly named policy.
Roadmap exit criteria are requirements for that future phase, not claims about the
installed package.

## Disposition of the original hard constraints

| Original property | Classification and current boundary |
| --- | --- |
| The domain core has no OVA, provider, CodeGraph, filesystem, UI, or language dependency. | **Invariant, refined.** Neutral contracts and application decisions cannot depend on product/provider adapters. Pure path and data types are allowed; filesystem I/O belongs in adapters. The dependency-direction test enforces the implemented import boundary, and `compatibility/ova` is an explicit migration adapter. |
| Exact identities and provenance cross every lifecycle transition. | **Split.** Exact identity at specification, policy, generated-tree, authorization, artifact, and publication authority boundaries is an **invariant**. Exhaustive, uniformly serialized provenance for *every* internal transition is a **deferred claim** until one end-to-end audit proves it. |
| All side effects live behind explicit ports and policy gates. | **Over-broad wording retired.** Security-sensitive host effects—model egress, compilation, execution, workspace acceptance, and publication—must cross an explicit application/adapter boundary; compilation and execution also require exact authorization. Requiring a security-policy decision for ordinary reads, formatting, or CLI output is neither necessary nor implemented. |
| Cache contents are immutable; aliases and reverse indexes are separate. | **Invariant** for content-addressed blobs and immutable manifests. Mutable availability, alias, and reverse-dependency views are projections and cannot redefine blob identity. |
| A valid signature proves origin and integrity, not behavioral safety. | **Invariant.** Trust verification cannot itself authorize compilation, execution, or publication. |
| Unclassified source cannot be compiled; `yolo` is exact, expiring, warned exceptional privilege and never fallback. | **Split.** Guarded builders and runners requiring a live authorization for exact inputs is an **invariant**. The profile name `yolo`, its explicit privilege vocabulary, expiry limit, and warning presentation are **current security policy**. Grants are never expanded to a hidden wildcard. Exceptional policy can never be selected implicitly or bypass identity, provenance, or audit; those are invariants. |
| Specifications and samples are living conformance inputs. | **Split.** Accepted specifications are behavioral authority and generated source is disposable—an **invariant**. Maintaining this repository's sample matrix as a release gate is **repository policy**; a consuming project is not required to copy that exact matrix. |
| Bazel is the preferred build system. | **Project policy/default.** “Strong opinion” means pinned text supplied to the code-generation model, not runtime enforcement. Explicit Component specification text and selected Flavors are higher authority; an explicit alternative build Flavor replaces the default, and `-bazel` removes it before prompt assembly. |
| A committed project test receipt proves an external run. | **Deferred claim.** The current compact receipt and runner allowlist are a local, unauthenticated Git assertion. Cryptographic or remote proof requires an evidence resolver and authenticated attestations that are not implemented by the receipt service. |
| Every generated application has a complete dependency graph. | **Invariant plus standard policy, lifecycle-qualified.** Omitting known direct/transitive or Literate-AI-managed Component/repository-source dependencies while claiming completeness is an authority failure. A pre-build BOM may honestly declare only its third-party closure incomplete when a selected authorized resolver is bound to close it; the post-build BOM must be complete before tests. This framework standardizes every SBOM on CycloneDX 1.7 JSON, with separate pre-build and post-build lifecycle evidence; another wire standard would require an explicit superseding decision and migration, not an ambient adapter choice. |
| A previously accepted source-cache hit can skip current acceptance. | **Rejected.** Immutable exact lookup is a non-authoritative optimization. Every materialized candidate is explicitly current-acceptance-untrusted and must pass the present index, validation, classification, authorization, build, dependency, generated-test, and independent-acceptance gates. |

## Flow classification

The high-level lifecycle is a partial order, not a fixed implementation pipeline:

- accepted specification and selected Flavor requirements precede generation;
- validation and classification of the exact generated tree precede build
  authorization;
- authorization precedes compilation or host execution; and
- publication is explicit and never follows from a local build alone.

Those dependency relationships are invariants. Stage names, the number of model stages,
eligible providers, Flavor selections, toolchains, sandbox profiles, and whether an
approved model fallback is allowed come from pinned policy. `litai rebuild` exposes the
complete project lifecycle through a content-pinned project driver; phase-specific
project validation, planning, generation, receipts, and source-to-specification remain
direct CLI surfaces. Native lifecycle services remain injected adapters rather than
hard-coded neutral-CLI policy.

The bootstrap restriction that every Flavor slot use a different axis is retired. It
was a data-model shortcut, not an invariant, and prevented ordinary multi-role systems.
Slot IDs are the selection authority: an axis-wide target remains convenient when the
axis occurs once, while repeated axes require unambiguous slot-scoped constraints.

The following remain delivery claims, not guarantees:

- a hardened operating-system sandbox suitable for arbitrary hostile source;
- exhaustive provenance coverage for every lifecycle transition and projection; and
- authenticated resolution of external evidence named by project test receipts; and
- every lifecycle service being available as an independent first-class public CLI
  command, rather than as a pinned `litai rebuild` driver phase.

## Consequences

Documentation and release notes must identify defaults by owner instead of calling them
framework laws. Conformance tests should protect invariants at authority boundaries.
Repository policy may remain strict—such as the host sample matrix or an exactly pinned
runtime dependency set—without forcing every Literate AI project to adopt it.

The one-page flow remains the stable user story. Detailed architecture documents can
describe stronger future designs, but their status or roadmap heading determines
whether those statements are implemented claims.

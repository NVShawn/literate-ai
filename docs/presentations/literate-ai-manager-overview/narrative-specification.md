# Literate-AI application foundry — narrative specification

## Purpose

Give a reader who needs the fuller technical account — an engineer evaluating adoption,
an architect reviewing the control model, or a manager who wants the deck's claims traced
to their source — a comprehensive, multi-page description of the codebase and its
behavior. This is the `narrative` member of the `literate-ai.document-pair` capability
realized by `component://literate-ai/literate-ai-overview`; its companion `presentation`
member is the succinct deck built from `deck-specification.md`. Both trace to the same
factual ledger in `source-notes.md` and SHALL NOT diverge on a shared claim.

## Relationship to the presentation member

The deck argues; the narrative documents. Where the deck compresses the control model
into five labeled slides, the narrative gives each of those same mechanisms its own
section, in prose, at the depth a technical reader expects: real specification excerpts,
exact identity/invalidation rules, ordering and cost, the failure path, and the
model-egress trust boundary — none abbreviated for time the way a deck must abbreviate.

## Heading hierarchy (H1 → H4, no skipped levels)

1. Literate-AI: the application foundry
   1.1. What problem this solves
   1.2. Who this document is for
2. The control model
   2.1. Readable intent as the durable product
   2.2. Implementation as a renewable candidate
   2.3. The Component specification as durable authority
      2.3.1. A real specification excerpt
      2.3.2. Its public capability contract
3. How generation works
   3.1. Generation keys: what they bind, what they refuse to bind
      3.1.1. Invalidation and its blast radius
   3.2. Layered execution, bounded concurrency, exact budgets
   3.3. The failure path
      3.3.1. What happens when a gate fails
   3.4. The model-egress trust boundary
      3.4.1. What never crosses it
      3.4.2. Current containment state
4. Evidence: what exists today
   4.1. The language, build-system, and operating-system Flavor matrix
   4.2. Sample applications and what they verify
      4.2.1. Verified wheel/Conan output
      4.2.2. OS-pinned applications and cross-platform fan-out
   4.3. Independent acceptance and provenance
      4.3.1. Correctness ships with the artifact
      4.3.2. Pre-build and post-build CycloneDX evidence
   4.4. Process-tree ownership and release-line decisions
      4.4.1. Win32 Job Objects at timeout spawn sites
      4.4.2. Who decides patch content
5. From Components to applications
   5.1. Applications as arbitrary Component DAGs
   5.2. One application intent, many products and runtimes
   5.3. Packaging and documentation ecosystem axes
6. Operating the system
   6.1. `litai`'s separation of concerns
      6.1.1. Package planning
      6.1.2. Native construction
      6.1.3. Independent verification
      6.1.4. Release control
   6.2. One authority connecting roadmap to operations
   6.3. Existing systems as starting knowledge
7. Release engineering: proving the system knows when it's valid
   7.1. Release Policy
      7.1.1. Writable main and exact-main release candidates
      7.1.2. Release-line lockdown and authority
      7.1.3. Pull-request classification and safe branch collection
      7.1.4. Continuous contribution disposition and document-pair evidence
   7.2. Gates that remember where they stopped
      7.2.1. Fail-fast, checkpointed, content-fingerprinted resume
      7.2.2. Why a full clean pass is still required before evidence counts
   7.3. One gate, your choice of target
      7.3.1. A private worker fleet
      7.3.2. GitHub Actions
      7.3.3. Why the same gate runs unmodified either way
   7.4. Timed evidence: `litai perf`
      7.4.1. What gets recorded, and where
      7.4.2. Reading fleet-dispatch cost from real recorded spans
   7.5. Provenance: what a release pin protects
      7.5.1. The lifecycle-driver trust boundary and its exact digest
      7.5.2. The documentation-authority marker
      7.5.3. What happens when either goes stale
8. Where this is going
   8.1. The next hardening horizon
      8.1.1. Operator adoption is now an explicit front door
      8.1.2. Production containment backends
      8.1.3. Multi-entrypoint deployment and rollback
      8.1.4. Native application-package formats
   8.2. The portfolio destination
9. How to engage
   9.1. Starting with one demanding application
   9.2. Building for the portfolio

## Depth requirement

Every section under "How generation works" and "Evidence" SHALL cite the specific file,
Component, or test that backs its claim, per `source-notes.md`. A claim with no traceable
source is out of scope for this narrative, not merely under-cited — remove it rather than
soften it. The two-week horizon and portfolio destination sections SHALL be explicitly
labeled as forward-looking, matching the deck's investment-horizon framing in
`deck-specification.md`.

## Release Policy

The generated narrative SHALL include the exact `Release Policy` heading shown in the
hierarchy. It SHALL explain writable Free/Pre-release `main`, the `major.minor` target,
green RC tags on exact `main`, release-line lockdown, README Release Engineer authority,
strict/loose trunk-first patch authority, the required `Literate-AI-Release` pull-request
line, and marker-based safe collection after worktree/PR inspection. It SHALL distinguish
LitAI command enforcement from forge protection and make no claim that forge settings are
live merely because policy exists. It SHALL also describe the continuous
contribution sweep and authenticated document-pair preflight required for major and
minor cuts, without claiming that `v1.2.0` is already published.

The 1.2 edition SHALL distinguish implemented bounded dependency-DAG scheduling,
provider-neutral shared-cache authority, real local Debian packages, native CLI and
gRPC acceptance, and repository-owned worktree placement from still-open production
command/SSH composition, real LAN cache qualification, remote package custody,
complete native-CLI lifecycle proof, and audited legacy-worktree retirement.

## Style

Continuous prose organized by the heading hierarchy above, not slide-style fragments.
Code and specification excerpts are quoted verbatim in fixed-width blocks. No rasterized
text — this becomes a native Google Doc with real heading styles, not an exported image.

## Edition cadence

Label the local `.docx` and the published Google Doc as the last major or minor
*edition* (this regeneration is the 1.2.0 edition). Realize the `.docx` with the
project-local `python-docx` path (`build_narrative.py`); do not make the Codex
`documents` plugin a hidden requirement. Patch releases keep README citations on that
edition without regenerating.

# Literate-AI application foundry overview

## Purpose

Persuade a mixed product, program, engineering, and executive audience that Literate-AI can become the operating substrate for creating and evolving complete applications—not merely a code generator.

By the end, the audience should believe that a focused application-layer investment is warranted because Literate-AI already proves the control model across real sample applications, multiple languages and build systems, and a cross-platform test harness.

## Narrative stance

This is one cumulative story for every audience. Do not split the deck into product, program, and engineering sections. Let application results and operating consequences carry the argument.

The deck is aspirational and investment-oriented, but it must distinguish:

- current Literate-AI framework evidence;
- current framework, sample-application, CLI, Component-DAG, and fan-out evidence;
- the next two-week investment horizon; and
- the longer-term application-portfolio destination.

## Core message

Make readable intent the durable product. Treat implementation as a renewable candidate. Carry build, test, execution, independent acceptance, provenance, and release evidence with every result. Extend that model from Components into whole applications, then into a living portfolio of shared capabilities.

## Visual direction

- Editorial enterprise campaign rather than a generic presentation template.
- Graphite and warm-white surfaces, orange action/evidence paths, restrained green future-state accents, and cool-blue depth.
- Large claim-led headlines, sparse supporting copy, repository-backed implementation evidence, and cinematic application-scale visuals.
- Titles occupy a reserved two-line band. Section tags, subtitles, body diagrams, and the footer must not share that band. Size boxes for Google Arial substitution, not only Helvetica Neue.
- Use only a few precise diagrams where relationships matter. Do not repeat the same lifecycle claim across multiple slides.
- Workflow pages are picture-led: a diagram or text-free image plus short labels. A paragraph that restates the flow is the defect, not a caption. Keep the real specification excerpt as readable authority.
- Generated imagery must be text-free and preserve usable negative space for slide typography.

## Depth requirement

Claim slides alone read as promotion. The deck carries a labeled five-slide technical
sequence that explains the control model, positioned after the argument is framed and
before its consequences are drawn. That sequence must show the durable authority as a
real artifact, state the identity rules and their blast radius, answer cost and ordering,
cover the failure path, and name the model-egress trust boundary.

Image-led slides carrying only a headline and a paragraph stay a minority of the deck.
Every slide carries speaker notes recording its authority and its limits.

The deck also carries a second, shorter labeled sequence — release engineering —
positioned in Consequences, after the application-DAG and fan-out claims and before the
investment horizon. That sequence must show a real fail-fast/resume checkpoint state
(not a hypothetical), name both dispatch targets (a private worker fleet and GitHub
Actions) as the same gate under one command while stating that GitLab is recognized but
fail-closed as unsupported, show real recorded per-step timing evidence, and state what
a provenance pin protects against and what happens when it fails. It must also make the
release evidence boundary visible: execution leaves a hierarchical, pointer-bearing
ledger, but that ledger indexes evidence rather than authorizing or accepting a release.

## Release Policy

The release-engineering sequence SHALL state that `main` stays writable for ordinary new
work in both Free and Pre-release states. Pre-release names a `major.minor` target and
permits a green RC tag only on exact `main`; cutting the matching release line locks it
down. Only README Release Engineers merge release-line pull requests or create/publish a
major or minor release. Patch authority is per-project: strict is Release-Engineer-only;
loose permits a listed writer to use reason-bearing break-glass only for a trunk-first
commit. Every pull request carries `Literate-AI-Release: major.minor` or
`Literate-AI-Release: none`.

The deck SHALL distinguish LitAI command enforcement from forge branch protection. It
must say forge settings should mirror the policy, not claim that live protection is
configured. It SHALL also name exact merged/dead branch markers and dry-run-first garbage
collection with worktree and pull-request inspection. Keep 26 slides by revising slide
20 and its notes rather than inserting or renumbering slides.

## Slide sequence

1. Software that can rebuild itself from intent.
2. We still scale software by copying its implementation.
3. Make intent the product. Make implementation renewable.
4. The implementation is moving unusually fast across languages, operating systems,
   build systems, and package providers.
5. The samples prove complete application boundaries: verified package bytes,
   multi-entrypoint deployment units, a durable frontend/API/collector/cache split,
   and OS-pinned applications.
6. The goal is not better code generation. It is an application foundry.

### How it works (labeled technical sequence)

7. This is the durable authority. — a real specification excerpt beside its public
   capability contract.
8. What a generation key binds, what it refuses to bind, and the blast radius that
   follows.
9. Layered execution, bounded concurrency, and exact budgets.
10. What happens when a gate fails.
11. What never crosses the model boundary, including the current containment state.

### Consequences

12. Correctness ships with the artifact—and failed work keeps its evidence.
13. Applications are arbitrary Component DAGs—not flattened prompts. Show the real
    durable split-service chain: single-writer collector and snapshot cache feeding a
    read-only API and browser frontend across two independently executable roots.
14. One application intent can serve many products and runtimes, including packaging
    and documentation ecosystem axes.
15. One command fans proof across real operating systems while excluding incompatible
    OS-pinned samples before generation. The private inventory has six worker entries
    spanning macOS ARM64, Windows 11 CPU/GPU, Ubuntu 24.04 CPU/GPU, and Ubuntu 26.04;
    inventory is not a claim that every cell has passed.
16. One authority connects roadmap to operations.
17. Existing systems become starting knowledge.
18. `litai` separates deterministic local/configuration/document operations, package
    planning, native construction, independent verification, and release control
    without becoming the native build or package manager.

### Release engineering (labeled technical sequence)

19. Release checks resume from where they stopped, not from zero.
20. Release Policy: writable `main`, exact-main RCs, and locked release lines. Preserve
    the one-gate target diagram while making the policy explicit: only README Release
    Engineers merge or create/publish major/minor; configured loose writers get only a
    reason-bearing, trunk-first patch break-glass. Show the required PR trailer and state
    that LitAI enforcement should be mirrored by forge settings. Caption or notes must
    also name the continuous contribution sweep and authenticated document-pair
    preflight. Speaker notes carry merged/dead markers, safe GC, worktree/PR inspection,
    and the GitLab boundary.
21. Every generation, build, test, and release step times itself, so the system can
    answer where the minutes went, not just whether it passed.
22. Every release is provenance-pinned and leaves a hierarchical evidence closure: the
    exact source reviewed is the exact source shipped, while evidence remains pointers,
    not release acceptance authority. Show a current, genuine lifecycle-driver pin-check
    excerpt with only paths that exist in the declared implementation closure.

### Consequences (continued)

23. The 1.2 release adds bounded dependency-DAG scheduling, provider-neutral shared
    cache authority, real local Debian packages, native CLI acceptance, native gRPC
    acceptance, and repository-owned worktree placement. Distinguish these implemented
    slices from incomplete production command/SSH composition, real LAN warm-cache
    qualification, remote native-package custody, complete native-CLI lifecycle proof,
    and audited legacy-worktree retirement.
24. The end state is a living portfolio of intent.
25. Start with one demanding application. Build for the portfolio.
26. Build the system that builds the software.

## Investment horizon

The two-week horizon is a target, not a readiness guarantee. It covers four connected outcomes:

1. Exercise installed 1.2 scheduling, cache, package, and native-acceptance slices
   against a demanding downstream application and carry them through explicit evidence.
2. Realize the production-containment contract as supported host backends.
3. Automate multi-entrypoint deployment and durable-service operations without
   weakening their separate authority boundaries.
4. Extend native package construction from the qualified local Debian path to exact
   remotely selected worker custody and the remaining provider formats.

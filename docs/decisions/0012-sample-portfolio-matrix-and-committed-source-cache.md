# ADR 0012: The Sample Portfolio Is a Curated Capability Matrix with a Committed Pre-Filled Source Cache

- Status: Proposed
- Date: 2026-08-22
- Decision owners: literate-ai maintainers
- Roadmap: [MITIGATION-FW-001](../roadmap/active-work.md#mitigation-fw-001-execute-the-framework-defect-mitigation-program), Phase 5 of [framework-defect-mitigation-program](../roadmap/framework-defect-mitigation-program.md)

## Context

Samples are three things at once — the conformance test matrix, the documentation a
newcomer forks, and the proof that the premise survives contact with real problems.
The current portfolio grew opportunistically: 23 samples exist, but coverage is
lopsided. Twenty of them pin the same two `portable-*` skills; the `deployment` axis
had no sample until `containerized-log-tally`; `cmake`, `go`, `windows`, and the new
container-assembly skill are exercised by zero or one samples each; while
`hello-component` variants overlap heavily on the simple end. At the same time, every
fresh clone regenerates all sample source from scratch because the accepted-source
cache was operator-local, even though the committed-cache target
(`generated/committed-source-cache/`) has existed since initialization shipped it.

Three constraints pull in different directions and need one decision:

1. **Coverage** — the matrix must walk simple → complex across skills, flavors, and
   components, including composition axes (packaging, deployment, accelerator).
2. **Budget** — first cold run of the ladder is expensive; redundant samples tax
   every contributor, so overlap is a defect, not safety.
3. **Narrative** — samples are living documents: relatable problems an experienced
   engineer has actually solved (tallying access logs, reconciling invoices,
   scheduling dependencies), rendered in this framework's idiom.

## Decision

1. **One cell, one sample.** Each sample declares which matrix cells it owns
   (language × OS × build × packaging × deployment × skill), and a candidate sample
   that duplicates an owned cell without adding a *new* cell or a new complexity tier
   is rejected at review. Complexity tiers are explicit:
   `starter` (single component, one slot family) →
   `composed` (multi-component graph, packaging) →
   `frontier` (multi-language roles, accelerators, deployment images, multi-repository).

2. **Complexity matches format.** A `composed` or `frontier` sample uses the full
   specification hierarchy: `component.md` holds observable product intent only;
   named wire contracts live in `interfaces/spec.md` documents declared via
   `specification_roots`; target-specific requirements live exclusively in selected
   Flavors and their pinned specification-to-source skills. No flavor-specific code
   requirement may appear in component prose — the container image rules for
   `containerized-log-tally`, for example, live entirely in `deploy-docker` and its
   container-assembly skill.

3. **Committed pre-filled source cache.** After a sample's generation qualifies, its
   accepted source is published into `generated/committed-source-cache/` via
   `litai cache publish --target project-committed` and **tracked in Git**, for this
   repository and every derived repository, so a clone resumes from pre-filled
   candidates instead of paying cold-generation cost. The cache remains exactly what
   it is elsewhere: non-authoritative, identity-keyed, current-acceptance-untrusted.
   Committed entries must satisfy unchanged bytes-for-identity checks like any other
   cache hit.

4. **Exclusions are absolute.** Object code (`_build/` outputs), executables, fetched
   binary assets, toolchains, and package downloads never enter Git. The committed
   target carries accepted *source* trees and their derivation manifests only; a
   review or hook that finds compiled artifacts in the committed cache fails closed.

5. **Portfolio floor.** Every shipped `literate-ai`-namespace flavor axis and every
   packaged specification-to-source skill must be owned by at least one sample or be
   explicitly listed as intentionally dormant in `samples/README.md`. Dormant entries
   are revisit-or-retire decisions with dates, not silent gaps.

### Classification (per ADR 0003)

- **Invariant:** committed cache entries are never authority; binary/object exclusion
  from Git; acceptance remains independent regardless of cache provenance.
- **Policy:** the tier definitions, the cell-ownership rule, and the portfolio floor
  for this repository's matrix.
- **Deferred claim:** "cold-start cost is amortized for every consumer" holds only
  once publication runs cover all samples; until then README marks which samples ship
  pre-filled.

## Consequences

- New-sample proposals state their cells and tier up front; reviewers reject overlaps
  mechanically instead of debating taste.
- Clones get runnable demonstrations immediately from the committed cache while still
  proving regeneration works — a clean-cache run stays the release gate, and the
  pre-filled path is an optimization, never attestation.
- The portfolio can shrink: overlapping starter variants become retirement candidates
  once their cells are owned elsewhere, directly attacking cold-run cost.

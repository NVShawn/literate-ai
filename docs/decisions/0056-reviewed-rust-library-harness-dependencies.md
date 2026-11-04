# ADR 0056: Review Rust library acceptance-harness dependencies explicitly

- Status: Accepted
- Date: 2026-11-03
- Acceptance: The directing user explicitly accepted ADRs 0056–0059 for 1.4 on 2026-11-03 ("Accept all four ADRs").
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0
- Roadmap: [1.4 integration](../roadmap/1.4-integration.md), issue #217 disposition; concrete implementation tasks follow explicit acceptance
- Issue: [#217](https://github.com/jordanhubbard/literate-ai/issues/217)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

The Rust independent library verifier generates a Cargo manifest whose only
dependency is the qualified library. Its harness receives product JSON but cannot
use `serde_json` unless the library happens to expose it. The existing
`library-acceptance-oracle@1` reader requires an exact field set and binds only one
harness file. Adding undeclared crates to every harness would change verifier
inputs without review. ADR 0054 explicitly leaves this issue to a separate decision.

## Decision

### Versioned verifier-owned declarations

Add an opt-in `literate-ai/library-acceptance-oracle@2` for Rust harnesses that need
additional dependencies. Preserve the v1 fields, identities, behavior and refusal
of unknown fields. Do not infer v2 from Rust source imports or reinterpret v1
qualification evidence. Other languages retain their current protocol.

The additional Rust harness declaration binds:

- Exact registry dependency aliases, package names, versions, registry identities,
  default-feature choices and explicitly selected features. Version ranges,
  floating sources, dependency aliases colliding with the library under test,
  undeclared path/Git dependencies and manifest patch overrides refuse.
- One reviewed sibling Cargo lockfile, with its exact digest and byte length.
  The oracle author reviews the complete resolution, including transitive
  dependencies and the qualified library's own dependencies.
- Explicit provisioned dependency-source and configuration custody, the selected
  native Cargo/Rust identities and finite input budgets. Reuse canonical source,
  alias and streaming custody owners; do not introduce another cache inventory or
  an implicit larger allowance.

The source declaration is verifier-owned authority alongside the harness and
cases. A candidate library, generated source, copied lockfile or parsed declaration
cannot grant independent verification permission. Reopen the exact current oracle,
lockfile and dependency sources at the existing independent-acceptance boundary.

Bound the dependency declaration by the existing oracle-document allowance and a
maximum of 128 direct extra dependencies and 4,096 aggregate selected features.
Bound the separate lockfile to 16 MiB. Source custody uses explicit finite budgets
with the Cargo ceilings in ADR 0054; no zero, boolean, unknown or over-ceiling
allowance is accepted. These limits become identity inputs and receive exact-bound
and over-limit acceptance controls.

### Exact offline harness composition

Render the temporary manifest from the typed reviewed declarations. The framework
alone supplies the path and alias of the exact qualified library export. Operator
data must not substitute another library, inject TOML sections or change the
measured compiler command. Keep temporary files under the existing owned harness
workspace and retain current native package/output custody.

Use the reviewed lockfile and run native Cargo with offline and locked resolution.
Require the observed dependency graph to match the reviewed library and registry
resolution, including features and source identities. Missing provisioned crates,
changed extracted source, unexpected packages, changed manifests/locks and source
configuration drift refuse; do not install or fetch dependencies automatically.
Lockfile checksums alone do not qualify an altered extracted source tree.

Capture the complete declared dependency source/configuration scopes and explicit
external inputs around compilation and harness execution. Cargo bookkeeping is
not permission to omit source or configuration from custody. Build-script inputs
and required native tools remain subject to existing explicit input, tool,
execution and sandbox policies. This decision adds no network or sandbox exemption.

### Evidence and independent acceptance

Bind the v2 oracle, harness, lockfile, dependency/source closure, generated manifest,
native tools and actual bounded command results into a versioned acceptance
observation and qualification capture. Reopening archived qualification must verify
that exact version and its dependency custody; a v1 observation cannot stand in for
it. Preserve the existing product-JSON result protocol and exact case comparison.
Dependency resolution or compilation success never substitutes for executed cases.

Required evidence includes a real native Rust library whose independently authored
harness uses `serde_json` to parse case inputs and compare serde-shaped results.
Exercise the public lifecycle with a provisioned offline dependency closure,
non-editable installed framework and exact integrated CI. Include actual source,
lock, manifest, feature and registry drift; missing dependencies; unexpected
resolution; dependency-alias collisions; checksum/extracted-source disagreement;
budget boundaries; and native command/output failures. Retain unchanged v1
positive and refusal controls and honest platform-specific qualification.

## Consequences

Rust library oracles can observe JSON contracts using reviewed ecosystem crates.
Verifier dependencies become explicit, current, bounded evidence rather than an
ambient Cargo cache assumption. This requires coordinated oracle, native execution
and qualification-reopening work. It neither completes retained binary replacement
#216 nor changes the separately included retained Cargo and release requirements.
All remain in the 1.4 scope. The directing user accepted this design under
ADR 0006 on 2026-11-03; implementation and qualification remain open.

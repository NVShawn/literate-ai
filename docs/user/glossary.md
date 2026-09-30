# Glossary

**Acceptance contract** — Exact validation criteria a generated candidate must satisfy
before becoming accepted source.

**Authoring input** — An identity-pinned skill, template, policy, or other input used to
create or revise specifications or source.

**Authority-review marker** — The sole content identity in declared documentation that
records human review of the current project authority and narrative graph. Validation
requires it to be unique and current; the CLI calculates but never writes it.

**Capability** — A versioned semantic contract provided by a Component. Requirements
resolve capabilities rather than filenames or import names.

**Component** — A logical, versioned unit of composition: library, service, application,
framework, adapter, or generated project.

**Component coordinate** — Stable logical name of the form
`component://namespace/name`.

**Component revision** — Immutable binding of a Component definition to exact specs,
authoring inputs, workflow, routing, source, and lineage.

**Content identity** — SHA-256 identity of canonical immutable content.

**Effective Component revision** — Exact result of composing a base Component revision
with a resolved Flavor set and target profile.

**Evidence artifact** — Source-grounded information tied to an exact source snapshot,
such as a structured source-intelligence result.

**Generated implementation-test suite** — Disposable `source/tests/manifest.json`
created with the implementation from the exact current major-rebuild recipe. It checks
current behavior but is not durable specification or independent acceptance authority.

**Intent authority** — Reviewed behavioral requirements allowed to drive generation.
For source-derived specifications this is granted before, and separately from, release
implementation authority.

**Lifecycle driver** — Project-authorized implementation behind `litai rebuild`, bound
by exact file identity, argument template, allowed environment, phase order, timeout,
and specification scope. It is explicit project trusted computing base.

**Host lifecycle facade** — Current compatibility/application surface used by this
repository's project driver to sequence concrete host generation, build, test,
execution, and acceptance adapters. It is not the provider-neutral Standard core.

**Standard lifecycle core** — Implemented provider-neutral application service over an
exact Component execution plan. It schedules per-Component source-only work, realizes
typed build and artifact graphs, revalidates accepted-source cache memberships, publishes
new memberships only after node acceptance, and admits the complete project. Ordinary
CLI adoption is available to Standard-bound projects; migration of the remaining samples
and qualification paths is pending.

**Standard lifecycle membership** — Canonical per-project evidence whose planned-node,
cache-decision, and lifecycle-result Component sets are exactly equal. Every decision
records hit, miss, or forced regeneration plus its input, accepted, and publication
membership identities where applicable. Failed runs retain this evidence but cannot
receive admission or an aggregate receipt.

**Standard aggregate receipt** — Typed successful-run receipt binding the exact
execution plan, Standard lifecycle membership, canonically ordered lifecycle-result
identities, and project admission. The issuer must return this document's exact content
identity rather than an unrelated opaque receipt ID.

**Major rebuild** — Clean replacement generation into a new empty directory. Current
specifications, selected Flavors, and exact skills regenerate both implementation and
implementation tests plus the pre-build CycloneDX SBOM; no prior generated tree is read
or preserved.

**Managed dependency graph** — Exact root Component, composed Component revisions,
repository-source dependencies, and scoped direct edges that Literate AI requires in
every source and resolved CycloneDX document.

**Flavor** — Versioned mix-in for one target axis, such as OS, accelerator, language,
build-system policy, toolchain, packaging, or deployment.

**Flavor set lock** — Exact, explainable record of selected Flavor revisions and the
policy and target constraints that selected them.

**Model endpoint** — Exact provider/model binding with declared capabilities,
constraints, availability, and egress properties.

**Model group** — Short, versioned routing policy over ordered model endpoints.

**Object package** — Immutable build or artifact bundle plus exact direct object
dependencies. Reverse consumers live in a separate index.

**OpenSpec** — The first specification provider used by Literate AI. The framework core
does not infer its directory layout.

**Publication** — Explicit, auditable transfer or registration of exact immutable
objects, separate from local cache usability.

**Regenerative qualification** — The second source-promotion gate: repeated clean,
cache-bypassed spec-only generation, build, freshly generated tests, and independent
source-baseline parity transfer release implementation authority to the specification
only for the policy's complete target/surface matrix.

**Release implementation authority** — The artifact allowed to define releasable
implementation behavior. A reviewed source-derived specification has intent authority,
while its exact original source retains this authority until regenerative qualification.

**Project test receipt** — Canonical passing-only record binding the complete project
authority-review identity, tested subject, compact versioned suite reference, positive
test count, normalized result, and an evidence-kind-to-identity object. Passing is
implicit. A separate project policy pins its admitted suite, exact runner identity,
required evidence, and minimum count. Only one current file is tracked; Git retains
earlier committed versions.

**Finalized receipt candidate** — External envelope emitted by the outer CLI only after
it validates the driver assertion, exact current source-cache lifecycle membership, and
configured publication effects. It is the sole public `update`/`check` input at the
supported API/TCB boundary; promotion stores only its nested raw project test receipt.

**Provisional receipt assertion** — Driver output bound to the exact request, command,
cache control, and nested receipt. It is diagnostic input to outer finalization and is
structurally unpromotable through the public receipt API.

**Resolved SBOM** — Strict CycloneDX 1.7 `post-build` evidence containing exact versions
and the complete observed direct/transitive binary dependency graph.

**Source SBOM** — Strict CycloneDX 1.7 `pre-build` document generated at
`source/.literate/sbom.cdx.json` with the complete intended dependency graph.

**Source package** — Immutable source bundle plus exact direct source dependencies.

**Generated source candidate** — Immutable per-Component source-only result containing
the exact tree, bundle, manifest, source-SBOM, generated-suite, request, recipe, prompt,
workspace, and plan identities. It has not yet been indexed, authorized, built, tested,
accepted, admitted, or published.

**Source tree identity** — Canonical digest of normalized generated paths, sizes, and
file content. It is a byte-verifiable tree fact, not a CAS metadata-record identity.

**Source bundle identity** — Identity consumed by the build boundary for the exact
generated source bundle. It currently equals the source tree identity, while remaining
a distinct semantic field so storage records cannot be substituted for build inputs.

**Build request declaration** — Pre-source declaration of effective revision, builder,
toolchain, sandbox, privileges, and outputs. It is neither a realized request nor an
authorization.

**Build request realization** — Deterministic binding of a build request declaration to
the exact generated source-bundle digest. Policy must still issue a separate current
authorization before compilation.

**Deferred source publication** — Rule that generation may retain a pending immutable
cache result, but only the later exact accepted candidate can publish it. Local CAS
storage alone does not make a candidate reusable.

**Source-derivation cache** — Non-authoritative optimization keyed by the exact recipe,
execution plan, coding CLI, resolved model, and prompt request. Its configured target
may be a local directory, Git-backed directory, monorepo subdirectory, or remote
provider. Only fully accepted source trees may enter it; forced
regeneration bypasses it. Every materialized hit remains current-acceptance-untrusted
until the present lifecycle succeeds.

**Source-cache lifecycle membership** — Canonical complete derivation-key set joining
the cache decision to current tree/index, build, test, independent acceptance,
workspace, provenance, and source/resolved-SBOM evidence. Receipt aggregate identities
are derived from these members; hits cannot be republished as current entries.

**Source snapshot** — Canonical immutable tree manifest for one source revision or an
aggregate of exact source members.

**Specification set** — Exact provider-valid behavioral intent artifacts and their
aggregate identity. A source-derived set requires separate regenerative qualification
before it replaces its source baseline as release implementation authority.

**Target profile** — Requested constraints for resolving Flavors for one operation.

**Uncertainty ledger** — Explicit record separating observation, inferred intent,
suspected defect, compatibility quirk, conflict, and unknown during source derivation.

**Verifier oracle** — Acceptance evidence held outside the generation closure and used
by a trusted verifier to compute or bind known results independently of generated tests.

**Versioned content reference** — Logical identifier, SemVer, and immutable content
identity for any first-class object.

**`yolo`** — Explicit, expiring, audited maximum-risk exception for exact signed source.
It may bypass safety restrictions only for requested privileges; it does not bypass
identity, provenance, expiration, revocation, warning, or audit.

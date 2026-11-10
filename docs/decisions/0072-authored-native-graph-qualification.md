# ADR 0072: Qualify genuinely authored native Component graphs

- Status: Accepted
- Acceptance: The directing user explicitly accepted ADR 0072 on 2026-11-10 ("Accept ADR 0072").
- Date: 2026-11-10
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0
- Roadmap: [AUTHORED-GRAPH-QUALIFICATION-001](../roadmap/active-work.md#authored-graph-qualification-001-admit-genuinely-authored-native-graphs-to-public-qualification)
- Issue: [#393](https://github.com/jordanhubbard/literate-ai/issues/393)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

A genuinely authored native dependency graph can be initialized, locked, planned
and rebuilt, but the public `spec qualify` front door requires a current authority
projection, retained-source or qualified-recertification state, and the exact
verified source-promotion audit. Its lifecycle request and run records require
source snapshot, generation-input audit and promotion-tree identities. Ordinary
authored Components have no such history.

A passing rebuild alone does not provide the closed regenerative qualification
and retained-library evidence required by the public Cargo consumer. Independently
static-promoting providers and consumers does not authorize edits to their audited
graph afterward. Fabricating a promotion audit, private projection or inverse run
would misstate provenance and weaken the source-derived admission contract.

Issue #393 requests a public authored route with the same substantive qualification:
two clean all-node generations, native builds and tests, unchanged independent
acceptance, exact library exports, closed replay and real retained consumption.
The nominally shared Rust provider in the beta/alpha/gamma diamond is the required
bounded fixture. Provider projection (#345), shared raw-case inventory (#392) and
retained-host authoring selection (#379) keep their own ownership and acceptance.

## Decision

### Admit authored intent through an explicit public route

Add `litai project qualify plan` and `litai project qualify apply` for genuinely
authored native graphs. Planning is read-only. It binds the actual reviewed project
and selected root plus its complete dependency closure, exact authored documents,
current lock and typed import/export surfaces, selected Flavors/skills/workflow/
routing, framework and target/toolchain policy, qualification profile, independent
verifier and case map. Use canonical project discovery and path resolution.

Apply requires acknowledgment of that exact current plan and ordinary explicit
host-execution authorization. Reopen all selected authority, private ownership,
source inputs and output reservations at their existing admission boundaries.
Hashing a document does not establish review, importer trust or execution permission.
A saved plan cannot restore a live authority owner after inputs or custody change.

This route qualifies authored intent. It neither changes a source-derived
projection into authored intent nor transfers authority away from retained source.
A node with retained-source authority obligations cannot evade them by selecting
authored origin. The existing `spec qualify` route retains its exact promotion, inverse-evidence,
authority-state and recertification requirements.

### Represent origin truthfully in versioned evidence

Introduce a closed qualification-origin contract with separate authored and
source-derived variants. Authored origin binds current reviewed authoring and its
complete locked graph; source-derived origin binds the genuine promotion and
source evidence required by its existing owner. Never populate legacy source
snapshot, promotion-tree or promotion-audit fields with unrelated authored hashes,
empty placeholders or invented evidence.

Use new versioned profile, request, plan, comparison, run/result and receiving
contracts where these identities participate. Preserve existing wire schemas, identities, readers and
refusals for the source-derived route. Ordinary generation-input auditing still binds the actual authored model context
without asserting a source-promotion event. Both variants must join their original
current authority owner to the common Standard lifecycle observations; decoding
an origin document or selecting a CLI flag does not provide that owner.

Publish an authored qualification reference only after the exact current authoring,
lock, graph, profile and independent verifier still match the completed result.
Keep authored intent and its independent oracle authoritative. This publication
creates no source-retirement history and no arbitrary new source-projection state.
Use the existing guarded project writers and recoverable publication boundaries;
interruption or stale inputs cannot leave a usable positive admission reference.

### Preserve complete generation and independent acceptance

Reuse the shared Standard qualification machinery for two clean runs. Every graph
node must genuinely regenerate under the selected coding CLI/model, build, execute
its generated cases, and pass independent acceptance. Bind actual node membership,
workspaces, receipts, source/test observations, native metadata, typed dependency
edges and exports. Missing, repeated, skipped-required or failed node executions
cannot be replaced by aggregate counts, cached generations or a root-only run.

The authored profile identifies a separately reviewed verifier-owned oracle and
case/surface map. Its exact input closure, expected-result provenance and policy
are sealed before generation and remain unchanged through both runs and replay.
Keep private expected answers outside generator inputs. Bind behavioral comparison
to this oracle explicitly. Its comparison evidence binds the oracle closure and
observed results without claiming an original-source baseline. Preserve original
source-parity schemas and obligations for the source-derived route.
An edited oracle, case map, interface, selected graph or dependency invalidates the
attempt. Generated tests do not replace independent acceptance.

Within each run the diamond must contain one exact nominal beta provider shared
by alpha and gamma, with the declared target/ABI, package/version/features and
managed dependency evidence. The two runs retain their own actual generation and
execution identities. Shared content-addressed raw test records may be unique in
storage while every real node execution and test count remains attributable (#392).

### Carry authored qualification through closed consumption

Extend capture and semantic reopening to recognize the versioned authored origin,
while reusing canonical bounded archive transport and exact library package/export
owners. Retrieval and structural reopening execute no archive content. Archive
integrity alone does not admit an artifact. Receivers must reopen
the origin-specific qualification closure, independent oracle, actual per-node
observations, typed exports and current importer trust before consuming a package.
Unknown origins, inconsistent origin bindings, substituted records, stale authority and incomplete
closures refuse. Failed attempts retain diagnostics only and publish no qualified
package or positive authored reference.

Qualify relocation and replay from the closed archive after the original run
resources are retired. Then use the ordinary public retained Cargo check,
materialize and admit operations with their actual consumer gates and reviewed
binding under ADR 0040. Retained consumption is model-free and cannot substitute
an ambient provider, private Cargo bridge or source-cache directory for an exact
qualified export.

## Acceptance

Run a noneditable installed public authored beta/alpha/gamma diamond through plan,
acknowledged apply, two genuine model-forward all-node clean runs, every generated
test and the unchanged independent oracle. Prove the shared nominal type, actual
per-node counts, target/toolchain identity, managed dependency graph and exact
exports. Retire original resources, reopen the transported archive elsewhere and
complete real public retained Cargo check/materialize/admit with consumer gates.

Require controls for source-derived/authored origin confusion, forged promotion
history, substituted graph membership, missing or failed providers, reused workspaces, stale locks
or interfaces, changed oracle/cases, substituted blobs, target/ABI mismatch,
interrupted publication and missing importer authorization. Preserve existing
source-derived positives and refusals, generated-case uniqueness within each suite,
and unsupported semantic-inverse behavior. Complete independent source review,
full source/native/installed and exact-head/resulting-main gates before closure.

## Consequences

Genuinely authored graphs can earn the same evidence-bearing library qualification
without pretending to have been reverse-engineered. Versioned origin-specific
admission and receiving validation are required; relaxing the current source-derived
front door is insufficient. This is included 1.4.0 work, with no release goal
deferred. The directing user accepted this design on 2026-11-10; implementation
and its complete native/installed/integration acceptance remain open.

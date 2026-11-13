# ADR 0074: Compose authored roots with exact previously qualified dependencies

- Status: Proposed
- Date: 2026-11-13
- Decision owners: directing user and literate-ai maintainers
- Roadmap: [AUTHORED-GRAPH-QUALIFICATION-001](../roadmap/active-work.md#authored-graph-qualification-001-admit-genuinely-authored-native-graphs-to-public-qualification)
- Issue: [#393](https://github.com/jordanhubbard/literate-ai/issues/393#issuecomment-6861321950)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)
- Amends: [ADR 0072](0072-authored-native-graph-qualification.md), only for the explicit new composition mode proposed below
- Release target: 1.4.0; acceptance and implementation remain pending

## Context

ADR 0072 correctly requires two genuine clean generations of every node in an
authored graph. It preserves source-derived authority and refuses to treat retained
source as authored merely to bypass promotion obligations. The current authored
planner accordingly rejects a source-authority projection on any selected node;
its executor invalidates every selected revision in each clean run. The complete
all-authored beta/alpha/gamma fixture remains required acceptance for that route.

A derived project's approved four-module runtime contract has a different dependency
boundary: it must reuse the unchanged, already source-derived qualified foundation
package, and share the same real core Cargo/type identity as retained callers.
Regenerating foundation would change the approved selected package. Omitting its edge,
copying types, supplying an ambient path or adding requirements after audited promotion
would not meet that contract. A normal native build or an independent verifier's
own-path fixture does not establish a current qualified dependency owner.

There are two distinct missing prerequisites. The framework has no explicit authored
qualification mode for reusing such a previously qualified dependency. Separately,
the downstream core has no complete approved core conversion contract or qualified
provider in this proposal. The approved four-module runtime contract/test adapter
neither supplies that core authority nor accepts this framework policy extension.

This changes the all-node rule for a specific new mode and is therefore a significant
capability requiring the real planning review in ADR 0006. This document does not
claim acceptance, implementation, current admission, generation or a released SDK.

## Proposed decision

### Add an explicit qualified dependency boundary to the existing route

Extend the existing public authored plan/apply route and Standard lifecycle rather
than introduce a second integration framework. A new versioned composition plan must
bind the complete dependency closure and distinguish:

- **Authored generation nodes:** every selected authored node must genuinely regenerate,
  build, execute generated cases and pass its unchanged independent oracle in each of
  at least two clean runs.
- **Reused qualified nodes:** each must reopen an actual previously closed qualification
  under its truthful origin and current authority/importer-trust owners. Its exact
  selected package/export and dependency closure remain unchanged. It must not be
  represented or counted as a newly generated node.

The plan must make that partition explicit, complete and mutually exclusive. No
selected node or dependency edge may disappear. A node cannot enter the reused set
merely through a flag, interface hash, source directory or decoded reference. It needs
the original current qualified-provider admission at the existing supported boundary.
The same exact reused dependency identities must bind both clean runs and closed replay.

Preserve the existing all-authored plan/origin/run schemas and their all-node checks.
Do not reinterpret old plans under the new policy or change the default admission
behavior. The new plan identity and acknowledged host-execution permission must
include the explicit composition mode and selected role of every node. Existing
source-derived qualification keeps its complete promotion, source-authority,
recertification and retirement obligations.

### Reuse origin-specific owners and native graph joins

A reused dependency reference must bind its truthful authored or source-derived
origin, exact closed qualification and archive, current owning project/lock/review,
selected artifact/export and full typed/native dependency graph, target/ABI,
package/version/features, and current importer trust. Reading a saved reference or
rehashing an archive never restores a live owner or grants execution/admission.

Use the existing origin-specific archive reopening and exact package/export owners
and the Standard `ArtifactExport` / `LibraryConsumerBinding` join. Native Cargo
projection must still demonstrate one nominal provider shared by all consumers and
preserve qualified registry resolution. Do not add a private manifest-patching bridge,
substitute a source cache, invent an inverse observation or publish another same-byte
crate as if it had the original nominal package identity.

Currentness, ownership, dependency admission, independent oracle and execution guards
remain at their actual effect boundaries. Reuse is evidence-bearing dependency
admission, not caching a successful mutable filesystem observation. The plan and
runtime must refuse rather than infer success when a qualified-provider owner expires
or cannot be independently reopened.

### Keep run and archive evidence truthful

The new run/result contracts must enumerate authored generation/build/test executions
and reused-provider qualification/export/currentness observations separately. Every
node and edge must remain attributable. Aggregate counts cannot conceal missing
execution or replace provider evidence. A reused node contributes no new generated
case count; any required verification of its existing qualification is reported with
that literal historical source and run identity.

Capture the full joined closure for independent archive relocation and replay after
original run resources retire. Receivers must reconstruct the appropriate current
origin-specific admission through public supported operations, then perform actual
retained Cargo check/materialize/admit with the complete consuming gates. A raw Cargo
pass is not this qualification. Failed attempts publish no positive reference/package;
retirement and interrupted-publication recovery retain their guarded custody rules.

### Keep downstream core conversion as separate project work

The project adopting the four-module runtime must prepare its actual core conversion
under its own retained-boundary roadmap work, separate from the runtime approval and
this framework item. For conversion of the real retained core, use truthful
source-derived provenance, a complete source-bound API/semantic contract, independently
reviewed oracles and actual qualification before replacing any retained core consumer.
That prepared semantic contract requires its own explicit owner decision; none is
supplied by this ADR. If an authored core is proposed instead, it likewise requires
its own complete semantic review and genuine authored qualification.

Runtime acceptance must then prove the same actual admitted core package/type identity
is used by the generated runtime and retained callers, plus the unchanged selected
qualified foundation package. Until those prerequisites exist, the runtime remains
unqualified. The broader project's requirement to transfer every remaining retained
boundary and classify all source is unchanged.

## Acceptance before completion

1. Preserve existing all-authored and source-derived positives, schemas, refusals and
   exact all-node generation counts for their unchanged modes.
2. Run a real noneditable public authored root with an actual separately qualified
   source-derived dependency through reviewed plan, acknowledged apply, both genuine
   clean authored generations, every generated case and the unchanged independent
   oracle. Prove reused nodes were not regenerated and were never counted as such.
3. Demonstrate one shared nominal native provider and the exact qualified package,
   features, target, dependency edges and registry resolution through both runs.
4. Refuse missing/duplicate nodes or edges, an authored node mislabeled reused, an
   unqualified dependency, stale/expired or decoded-only owner, changed source
   projection, substituted package/blob, wrong origin/target/ABI/features, edited oracle,
   reused generation workspace and absent importer authorization. Changed dependency
   bindings must invalidate the exact acknowledged plan.
5. Retire original resources; transport/reopen the closed archive under genuine current
   owners elsewhere; complete actual public retained Cargo check/materialize/admit
   with all required consumer gates. Qualify interruption and foreign-file preservation.
6. Complete independent design/source review, focused native and noneditable installed
   controls, all required local/platform gates, exact-head and resulting-main checks.
   A downstream four-module adoption or full release remains a separate claim.

## Consequences and alternatives

This adds explicit mixed qualification provenance to the existing authored route.
Versioned plan/run/archive/receiving changes and their negative controls are real work;
removing the current source-authority guard is insufficient. Implementation begins
only after the directing owner accepts or amends this proposed decision. No upstream
merge, release, global SDK replacement or model/worker execution is authorized by
writing it.

Regenerating all dependencies remains appropriate for a wholly authored graph. It
cannot provide unchanged-package reuse for the approved foundation boundary. Removing
dependency edges, repromoting edited audit bytes, copying core types or redirecting
Cargo to an ambient path would weaken provenance or nominal identity. Changing the
runtime APIs to avoid those dependencies would change an already approved contract.
Those are not substitutes for the explicit composition policy above.

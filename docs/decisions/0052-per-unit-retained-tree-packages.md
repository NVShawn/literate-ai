# ADR 0052: Per-Unit Retained Tree Packages and Verified Worker Transport

- Status: Proposed — directing-user review pending; no implementation authorized by this ADR yet
- Date: 2026-10-31
- Decision owners: literate-ai maintainers
- Roadmap: [RELEASE-INTEGRATION-005](../roadmap/1.4-integration.md); concrete implementation subtasks follow acceptance
- Release target: 1.4.0, already included by the directing user
- Issues: [#235](https://github.com/jordanhubbard/literate-ai/issues/235), [#93](https://github.com/jordanhubbard/literate-ai/issues/93), [#74](https://github.com/jordanhubbard/literate-ai/issues/74)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

The retained-origin package owner constructs one aggregate regular-file archive.
Its frozen `retained-project-package-plan@1` rejects links, required empty
directories and more than 100,000 inputs. Complete native source and ports trees
need per-Component packages, faithful aliases, explicit dependencies and exact
source/lock/recipe/target/license provenance. The measured FreeBSD source example
contains 114,710 retained members and 138 symlinks; a raised catalog ceiling alone
does not make it packageable.

Canonical `NativeSdkTree`, `PackageTreeInput`, `TreePackagePlan`, tree providers
and independent native archive verification already own faithful topology. Their
decoders do not grant producer, runtime, PACKAGE or release authority. The missing
worker integration must retain those boundaries, including a real LINK proof.

## Proposed decision

1. **Explicit opt-in, versioned retained origin.** Add new per-unit and package-set
   plan/result contracts, beginning with
   `literate-ai/retained-unit-package-plan@1` and
   `literate-ai/retained-unit-package-set-plan@1`. Their public schemas and exact
   codecs remain distinct from the frozen aggregate plan and generated Component
   package plans. The existing aggregate path, identities, bounds and refusals stay
   unchanged unless an operator explicitly selects the new capability. Retained
   origin never becomes a fictitious source-free generated or SDK producer claim.

2. **One ownership partition over the actual admitted payload.** Reuse canonical
   Component coordinates, exact authoring/lock identities and retained ownership
   records. Every regular file, alias and required empty directory has exactly one
   declared unit owner; shared structural ancestors are canonical topology, with
   matching type/mode rather than duplicate payload ownership. Output ownership is
   explicit: input-prefix ownership alone cannot infer generated output ownership.
   Refuse missing, overlapping, stale, conflicting or unregistered owners. Never
   silently merge logical units or copy payloads to make a partition pass.

3. **Canonical tree geometry, complete composed closure.** Reuse `NativeSdkTree`,
   `PackageTreeInput` and the canonical tree projection/verifier owners; do not add
   another file inventory or flatten links. Preserve symlink text, hardlink identity,
   empty directories, executable modes and case policy. A link contained in one unit
   uses its ordinary complete internal closure. A link whose target belongs to
   another unit requires an explicit typed tree-link dependency and a complete
   composed target/ancestor closure across the selected package set. Add an explicit
   versioned composed-package projection at the existing tree owner for this case;
   do not weaken frozen standalone tree verification or claim an isolated dangling
   subset is a valid standalone tree package. Refuse escapes, cycles in alias
   resolution, absent targets/ancestors, conflicting namespaces and unselected
   dependencies. Preserve logical/install paths through exact provider projections.

4. **Typed dependencies and non-circular identities.** Edges distinguish build inputs,
   runtime dependencies and tree-link closure, with exact target/unit identities and
   applicable ABI/version constraints. Preserve ADR 0044's verified dependency DAG
   and cycle refusal; a runtime/link edge does not become a compilation barrier.
   Refuse cycles rather than deleting edges, duplicating targets or silently merging
   units. Bind a canonical ownership/dependency graph and local unit definitions
   before archive production; results bind each produced artifact to that exact
   graph, plan and dependency closure.

5. **Finite resource policy and provenance.** Carry explicit per-unit and aggregate
   entry/file-byte/total-byte/unit/dependency limits through planning, transport,
   materialization and verification. Reuse `RetainedProjectLimits` and canonical tree
   bounds; the combined physical tree stays under the existing 1,048,576-entry hard
   ceiling. Bound unit count to at most 1,000,000 and dependency edges to at most
   4,000,000, with declared lower job budgets. Count unique physical payload bytes,
   alias metadata, structural/empty directories, archive overhead and concurrent
   staging explicitly. Bound set-index and per-unit descriptor decoding separately;
   use content-addressed unit-plan/manifest references and bounded descriptor shards
   where needed, not a monolithic unbounded JSON tree. Shards reuse canonical tree
   entry/reference contracts and are rehashed before composing the exact namespace.
   Project scale limits do not silently raise package budgets. Every unit, including alias-only units, binds retained source, selected Component
   lock, recipe, target, resolved BOM, provider/tool and license provenance. A missing
   authoritative license or provenance binding refuses admission.

6. **Live owner and tree-aware worker custody.** Mechanical decoding grants no
   authority. Construction requires the existing live `AdmittedRetainedProject`,
   current selected provider scope and current custody; SDK-containing payloads keep
   their actual SDK producer/runtime owners as well. Add a versioned tree-aware
   PACKAGE worker envelope that carries exact tree plans, partition/dependencies,
   provider selection and artifact identities. Keep legacy file-only transport on
   its current path. On return, reconstruct and independently verify native archive
   entries against the canonical selected tree and per-unit partition, then recheck
   owner, target, tool, runtime and LINK scope immediately before canonical admission.
   Parsed plans, synthetic LINK tokens and successful upload are not admission.

7. **Public path and release boundary.** Expose opt-in plan/inspect/execute/verify
   through the existing retained/package front doors, obtaining live admission
   through verifier-owned lifecycle/readmission rather than reconstructing it from
   JSON assertions. Read-only planning executes nothing. Package production does
   not install, publish, retire source, promote source authority or authorize a
   release. Refusals preserve retained evidence and prior usable aggregate packages.

## Acceptance and qualification

After explicit human acceptance, record concrete implementation subtasks through
`litai work record`, with links back to this decision and existing
NATIVE-TREE-WIRE-001. The accepted 1.4 scope includes every item below:

- Versioned schema/codec checks, exact legacy identity compatibility and selected
  aggregate-versus-unit behavior; malformed, rehashed and over-budget inputs refuse.
- Complete regular-file, file-link, directory-alias, hardlink and empty-directory
  cases, including cross-unit target/ancestor closure and alias-only provenance.
- Typed dependency, missing owner, overlapping payload, namespace/case collision,
  traversal escape, alias-cycle and dependency-cycle refusal; valid cross-unit
  runtime/link dependency cases retain their exact graph and artifacts.
- Real native FreeBSD provider construction and independent reopen verification of
  every unit and the composed package set, retaining source/lock/recipe/target/BOM/
  license/provider identities. No archive-format success substitutes for provenance.
- An actual remote FreeBSD PACKAGE round trip with live LINK and current producer/
  runtime scope. Altered archives, link targets, provider/target identities, missing
  dependency packages, stale owners, failed transport and changed custody refuse.
- A complete real retained-tree exercise beyond the old 100,000-input threshold,
  with all source aliases/required directories accounted for, bounded peak memory,
  stage/archive byte counts, transfer totals, bounded descriptor/shard decode and
  elapsed time. Portable synthetic
  checks remain development evidence rather than this native qualification.
- Exact review/main CI and release gates. No issue closure or release readiness is
  inferred from descriptor decoding, a subset fixture, or a historical receipt.

## Consequences and rejected alternatives

This is new 1.4 capability, not a patch-compatible relaxation of the aggregate
contract. Existing callers can remain on the aggregate path. The implementation
must extend the existing canonical tree/provider owners and their custody checks,
which is more work than removing a link refusal or raising one constant.

Reject global limit increases, link dereferencing, parallel ad hoc inventories,
blind receipt-to-owner reconstruction, copied cross-unit targets, inferred output
ownership, hash-only PACKAGE admission and automatic source retirement. A declared
runtime/link dependency closure can be inspected without inventing build readiness.

The two-host shared-cache and throughput requirements remain mandatory under
[#236](https://github.com/jordanhubbard/literate-ai/issues/236); this decision does
not count one worker or a package-only measurement as those results. They remain
in 1.4 scope and may share the exact qualified package/ownership primitives here.

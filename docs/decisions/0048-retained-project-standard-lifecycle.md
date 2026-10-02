# ADR 0048: Retained projects use explicit Standard lifecycle admission without source-authority transfer

- Status: Proposed
- Date: 2026-10-02
- Decision owners: literate-ai maintainers and directing operator
- Issue: https://github.com/jordanhubbard/literate-ai/issues/22
- Roadmap: pending planning acceptance under ADR 0006

## Context

An adopted project can retain its implementation as source authority while adopting
new build and packaging providers. Today `rebuild` rejects any conversion stage
other than `qualified` before even a retained-source review plan is prepared.
The existing retained-source adapter captures a small UTF-8-only tree and qualifies
it through generated-source custody. It cannot represent a complete retained project
with binary assets, large inputs, locked SDKs, native producers, documentation and
an existing test inventory. Raising its limits or marking conversion qualified would
misrepresent the authority being exercised.

This proposal serves framework Goals 1, 4 and 6. It changes lifecycle admission,
not the authority of retained implementation or the existing conversion criteria.
The ZIP provider in issue #17 remains an independent packaging capability.

## Proposed decision

Add an explicitly selected retained-project execution mode to the Standard lifecycle.
It must have a distinct typed admission record and receipt origin. Existing generated
and small retained-source modes retain their current behavior and fail-closed gates.
No adopted project opts in implicitly, and execution never advances conversion stage.

A reviewed manifest declares the complete input closure, including source, binary
assets, build/test/documentation definitions, dependency locks, SDK/toolchain identities,
platform filters, and the applicable test and package inventories. Every member binds
its canonical relative path, byte length, executable mode, digest and ownership role.
Declared entry/per-file/aggregate byte limits are positive, enforced, and bound to the
plan; capture streams large files into content-addressed storage rather than loading
the project into one text dictionary. Missing, extra, changed, colliding or redirected
inputs fail admission. Links and special files are rejected until a separate explicit
contract qualifies their custody. Mutable caches and credentials are never inputs.
External dependencies are admitted by exact identities and manifests; linking an
ambient checkout or finding an installed SDK does not qualify it.

Read-only planning validates project/Component/Flavor/lock authority and the existing
conversion record without executing build tools or acquiring unreviewed dependencies.
The resulting plan names its exact input, target, worker/toolchain, command-profile,
output, resource-limit and acceptance identities. Execution requires acknowledgment
of that identity plus the existing host/worker authorization. Any changed plan input
requires a new review; the ordinary regenerative release route continues to reject
unqualified conversion authority.

Standard execution materializes an isolated immutable input snapshot and runs the
locked build profile and complete declared test/documentation inventory. Staging,
generation, native compilation and dependency acquisition are explicit graph inputs
and actions; root command names are not evidence of stage coverage. Existing Standard
validation, artifact/SBOM custody, containment, acceptance, cache invalidation and
receipt verification remain mandatory. Failures, missing suites, unauthorized skips,
missing artifacts or changed post-execution inputs prevent acceptance. Retained-project
receipts must bind original test identity/skip policy and packaged-payload provenance
when the project's contract requires them. A baseline exception is not a test pass.

Packaging consumes only the accepted closure through the selected existing package
provider. A separately authorized release can publish that retained-origin artifact
only after its current receipt and original release-authority policy pass. Such a
receipt never asserts regenerative source authority or permits a conversion-stage
transition. Ordinary receipts and retained-project receipts cannot substitute for one
another. Legacy repository commands may adapt argv/exit status to these public verbs;
they may not invoke an independent build/test/publication acceptance path.

## Planned implementation and acceptance, after approval

1. Define typed manifest, plan and retained-origin receipt contracts, bounded streaming
   capture, stale-input rejection and explicit authority-mode validation.
2. Expose read-only planning and integrate Standard execution/verification using locked
   command profiles and compatible workers. Keep runtime acquisition and cache custody
   in existing framework owners; do not duplicate shell pipelines in CLI handlers.
3. Bind package and release verification to the distinct retained-origin evidence;
   reject attempts to use it as regenerative conversion proof.
4. Add a provider-neutral mixed-source/binary-asset fixture whose build, complete tests,
   docs and deterministic package pass through the public installed CLI. Verify exact
   worker/target identity, no-op/repeat behavior, dependency invalidation and relocation.
5. Adversarially reject stale or extra source/assets, mutated manifests, missing/failed
   tests or docs, substituted artifacts, wrong workers/targets, unsafe paths, exceeded
   limits, incomplete dependency closure and unauthorized publication or authority transfer.
   Existing generated and small retained-source rejection tests must still pass.

## Consequences

Adoption can qualify a different build/package provider while remaining honestly
original-source authoritative. The framework gains explicit retained-project custody
rather than weakening conversion gates or treating archived baseline logs as a current
receipt. Projects must author complete manifests and acceptance inventories, and existing
worker observations remain provisional until admitted by this route.

This is a significant capability and remains Proposed until the directing human accepts
or amends the design under ADR 0006. No product implementation or lifecycle bypass is
included in the proposal. Acceptance of the design permits implementation and its tests;
it does not itself authorize a release, claim qualification, or merge a PR.

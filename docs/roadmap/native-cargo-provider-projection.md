# Native Cargo provider projection

- **Status:** active
- **Owning queue item:** [NATIVE-CARGO-001](active-work.md#native-cargo-001-project-qualified-rust-library-provider-graphs-into-cargo-builds)
- **Completion / archival evidence:** pending real Standard graph build, independent acceptance, sealing and relocated model-free consumption.

## Failure and owner

Issue [#345](https://github.com/jordanhubbard/literate-ai/issues/345) preserves the actual two-library E0432 failure. Provider environment variables do not add a Cargo dependency. Current source inspection at `64415762` finds no provider-package projection in the Standard Cargo builder. The Cargo generation skill excludes arbitrary path dependencies, so asking the generator to invent paths would violate the current profile.

The framework owns a disposable dependency projection. Authored and admitted generated source stays immutable. The existing provider revision/export/import-surface bindings determine the inputs; a manifest name or an environment path does not admit a provider.

## Graph and package contract

Admit the exact direct provider exports from the build plan and their complete qualified transitive closure. Every node must be a Rust library on the consumer target, with matching Cargo package and import-surface identities. Reject an absent edge, unexpected export, duplicate alias, ambiguous same-named package, cycle, changed tree or unsafe filesystem node before Cargo starts. Use finite graph/file/byte bounds consistent with existing lifecycle custody.

Assign each qualified provider artifact one canonical package directory in the projection. A diamond graph must resolve one beta package when both alpha and the final consumer refer to beta. Recursively copying alpha together with its private beta and separately copying beta would create two Rust nominal types, even if their bytes match, and is not acceptable.

Generated manifests express registry intent and may omit dependencies supplied by qualified Component edges. The framework adds only those admitted library edges in its disposable projection. It must reject a conflicting generator-authored dependency rather than replace an unrelated declaration. Arbitrary path, Git, workspace and build-script escapes remain outside this profile. Dependency-free projects preserve their behavior and evidence shape.

A sealed library with providers must retain a versioned, identity-bearing portable graph plus immutable original manifests. On later projection, revalidate this record against the actual qualified artifacts and reconstruct the shared canonical graph. No absolute temporary, cache or build path may become a package dependency. Do not edit an admitted or sealed package in place. Provider graph membership, exact package bytes, original and projected manifest identities, lock, Cargo metadata and resolved SBOM must describe the same actual graph.

## Execution and acceptance boundaries

All Cargo operations use measured tool bindings and the existing authorized bounded lifecycle. Source/qualified inputs are checked before and after native execution. Existing cache keys already include provider materials, but a graph-aware projection and its evidence require explicit versioning and revalidation on cache hits; an old evidence file cannot certify a new graph.

Both existing verifier paths need review. The Rust v1 source harness currently compiles the package through its own adapter, and the v2 Cargo harness admits a closed resolution graph. Neither may silently gain ambient provider paths. Independent verifier declarations must cover the qualified native graph while retaining their own expected results, tool ownership and current-input checks.

## Required qualification

1. Generate beta/Event and alpha/read through the actual public Standard lifecycle twice from clean roots. Verify generated-source immutability and exact provider selection.
2. Build an independent consumer which constructs the same beta/Event and passes it to alpha/read. Exercise both a direct edge and the diamond graph; copied types or two beta package identities must fail.
3. Seal, move to an unrelated clean path and consume without a model. Verify no old absolute paths or original cache are required and every provider remains qualified.
4. Refuse missing, substituted, duplicate, cyclic, escaping and modified providers, altered graph/original manifest bytes and conflicting generator declarations. Preserve dependency-free Cargo and existing registry dependency controls.
5. Run canonical Python checks and applicable protected PR checks. Ordinary Cargo success is a diagnostic control, not full framework qualification.

End-to-end qualification remains in progress. The builder integration below is partial evidence; independent native graph acceptance and authority-transfer completion remain outstanding.

## Graph-value development checkpoint

The new finite graph contract passes eight focused cases through canonical `make python-check` with a session-managed environment. They cover a shared diamond, dependency-free input, missing/disconnected nodes, direct/disconnected cycles, changed identity materials, normalized package/alias collisions, canonical ordering/bounds and closed wire decoding. Ruff formatting and lint pass. Log SHA-256: `b9a259e3bb1317990dd73f0a1e7ea7bf6b2c27c51ba477181279499ab478e5f8`. These are graph-value controls; no filesystem/package admission, Cargo execution, full Python gate or complete dependency-projection acceptance is claimed.

## Byte-projection development checkpoint

The pure projector reconstructs one shared provider directory from immutable package-file values, injects only graph-declared edges into disposable manifest bytes, preserves the original manifest and emits a portable graph record. It refuses conflicting declarations, ambient dependency paths, source build scripts and mismatched node/manifest inputs. Sixteen focused graph/projection cases pass through canonical `make python-check`, with zero skips, including an actual ordinary Cargo diamond build and a second clean build after relocation and removal of the original fixture. The runtime uses Rust 1.99.0 and no registry dependency. Log SHA-256: `392ea49b4d4de3252d7ab4fa08585c38d461856431eb647ec8376f8a613fc2b6`.

The projector does not admit qualification or authorize execution. Standard adapter binding, sealed-provider reprojection, exact native graph metadata/SBOM validation, both independent verifier paths and retained-consumer integration remain unfinished. This ordinary Cargo control does not supersede the recorded failing Standard lifecycle.

## Reprojection, metadata and input-binding checkpoint

Twenty-three focused cases now pass with zero skips through the managed Make gate. The real Cargo control reconstructs alpha from its sealed-style package graph, shares beta with the direct consumer, validates actual native package paths/aliases and rebuilds after relocation. Blob/contract/binding controls use the existing fake-tool fixture and do not claim native qualification. Native metadata is checked against the exact disposable directories; duplicate, substituted-path, wrong-root and missing-edge controls refuse. Original-manifest drift and incomplete embedded graph records refuse.

Independent review found case-sensitive implicit build-script detection and an output bound omitting the returned root manifest. The regression first failed on `Build.rs`; the corrected successor rejects casefold-equivalent build scripts and reserved metadata paths. The shared canonical archive validator now bounds output files plus the rewritten manifest, including archive overhead and path aliases. Final focused log SHA-256: `87dcd9df0c73290c698f5ee81a1006679725b26093d5887a8d9a3b6492f89151`.

The input binder reads only exact export blobs and joins their current library contracts/import bindings; it does not select filesystem paths from package data. Standard production invocation, graph-aware lock/SBOM resolution and independent verifier/retained-consumer integration are still pending. The draft remains incomplete.

## Ambient Cargo authority review

A second independent source review identified `package.workspace` as an ambient workspace selector despite the top-level workspace refusal. The successor rejects that selector, inherited package/lint workspace values, escaping explicit readme/license paths and package-local `.cargo` configuration. Canonical relative package metadata paths remain supported. The real metadata control also rejects a same-named registry package alongside a qualified native provider. Twenty-four focused tests pass, zero failures/skips; changed Python files pass Ruff. Log SHA-256: `acbf5652fee31149b4caff4952e3638529d186d92896059cfa762a94771de505`. These remain helper/profile controls; the Standard/SBOM/verifier/retained production integration is not yet complete.


## Standard builder and resolved-SBOM checkpoint

The Standard Cargo builder now joins its current finalized intent and exact export
blobs to the native graph, injects dependencies only into disposable source, and
checks actual Cargo metadata against the shared provider paths and aliases.
Graph-aware cache keys retain the projection profile; cache reuse revalidates the
current provider graph, original manifest, metadata and retained provider-lock
identities. The native graph file is included in the exact dependency-evidence
manifest and evidence recorder.

The resolver admits source-less Cargo packages only through an explicitly supplied
typed graph for one selected manifest/lock. Each native node maps to its existing
managed Component BOM reference; it does not become a fabricated registry package.
Managed Component versions keep their authored meaning. Registry packages still
require checksums. Each qualified provider's prior lock inventory, source,
checksum and outgoing dependency edges must survive fresh resolution unchanged;
a changed resolution refuses before compilation. This also refuses extra optional
registry edges activated on an already qualified provider. Solver selection that
requires changing those constraints needs separate qualification.

Canonical `make python-check` passes all 57 Cargo smoke tests with zero failures
or skips, including 35 focused native graph/profile cases. The actual native
builder control creates beta/Event and alpha/read twice in clean temporary roots,
builds both through Standard ports, checks generated-source immutability and
resolved-SBOM identities, exercises cache reuse, then copies the export to another
path, removes the original export and compiles/runs an independent consumer of
the same nominal beta type. The generated source is a test-owned fixture, not a
new model generation. This is builder integration and ordinary relocated-consumer
coverage; it is not whole Standard lifecycle, independent verifier acceptance,
or retained-product import qualification. The full Python gate remains incomplete: its first failing baseline control
stops further execution, as recorded below. Cargo checks log SHA-256: `4514b1365244e99a8ca068e226da43b8c64b714f677dd4f1c2916314254ebdf5`.

Cargo generation guidance now distinguishes generator-authored registry intent
from the framework-owned native edges and preserves the prohibition on invented
paths and copied nominal types. Root and initialized-template skill bytes match.
The generic Codex skill validator rejects the canonical framework's additional
frontmatter fields; those fields remain intact. The declared pinned framework
skill evaluator passes all six static checks for the root, Cargo and initialized
template skills. No downstream installation or runtime pin
changed.


## Full gate and cache-identity limits

The canonical full Python request selects 2,756 tests. Fail-fast reporting records
797 passed, one failed and 40 skipped; the remaining selection did not run. The
failure is the foreign-owned framework staging clone control: Git 2.43 refuses
the exact source `.git` path despite the workflow command-scoped trust settings.
An isolated unchanged `64415762` baseline reproduces the same failure. This is
not a passing full gate; no global Git trust or test suppression was applied.
Whole Standard lifecycle, both independent verifier paths and retained-product
consumption remain outstanding.

Native graph identity includes the consumer revision, so native workspaces are
conservatively partitioned by that revision. The dependency-free Cargo cache
continues to reuse intermediates across consumer changes. This checkpoint makes
no native cross-revision build-time claim.


## Reviewed source authority

The Standard/native Cargo input, projection, provider-lock, cache and managed-SBOM
integration has independent source review. The only production-source delta from
`03398121` through `501335bb` corrects the cache comment; its Python AST is unchanged.
At the reviewed `cb50ad43` checkpoint, the declared lifecycle-driver review pin was
`sha256:ceb64ae66ff1c95a348d891f734a8d44297971534486484cb40d81df4111354d`;
its documentation authority and unchanged test-runner review were current too.
Canonical whole-tree lint and formatting pass (1,891 formatted Python files).
These reviews authorize no independent library acceptance or execution claim.

A separate Ubuntu worker with distribution Git2.53.0 passes the exact foreign-owned
staging control: one real unittest, no skips, with source-only and unrelated-path
refusals and unchanged operator configuration. This recovers that source check
without changing global trust; it does not repair the reusable readiness gap#305.
The full Python request remains pending on the reviewed successor. The failed
Git2.43 full run and unchanged-baseline reproduction remain retained.


## Independent import working-directory correction intent

The later complete source request at `cb50ad43` selects 2,756 tests and records
2,519 passed, one failed, 104 skipped and 132 unexecuted. Its exact source and
tools remained unchanged and all owned process groups and temporary files
retired. The earlier adopted-Cargo journey passes in this request; the new
failure is the independent Rust library import smoke case. It creates a fresh
consumer manifest but invokes Cargo from the framework checkout, allowing an
unrelated ancestor Cargo configuration to select an unconfigured cache backend.
The TEST driver already enters the exact export; private HOME and CARGO_HOME do
not prevent Cargo's working-directory ancestor lookup.

Run IMPORT from its actual fresh consumer directory, keeping its exact manifest,
target directory, offline mode and capability/import checks. A portable native
regression must put a hostile wrapper in a synthetic caller ancestor, use an
independent consumer temporary root and show the original driver triggers it
while the corrected driver does not. This requires no operator configuration
changes or blanket config suppression. The original failed full request stays
historical; neither the targeted repair nor its fixture proves whole Standard
lifecycle, independent graph acceptance or retained consumption.


The targeted correction now passes all six Standard command projection cases in
canonical `make python-check` (Linux x86-64, native Cargo/rustc1.99.0, no skips,
10.456 seconds). A compiled portable wrapper proves that synthetic caller
configuration is active; the unchanged IMPORT driver invokes that wrapper and
fails with exit86. After setting only Cargo's working directory to the actual
consumer root, the verifier succeeds, reports the exact capability, never invokes
the hostile wrapper, and leaves the synthetic caller configuration and admitted
library source unchanged. The regression uses independent caller and consumer
temporary roots, without disabling configuration globally. Full source and
whole lifecycle/independent graph/retained qualification are still pending.

Causal negative log SHA-256: `27dcdcb8b8ffd4efa50a1f9f343d95828b208764b63f4bacaaf7f80cd18f3422`; focused positive log SHA-256: `1e26882a0ccdfc17287568fb2b7c81301b5846b8f3511f1fbb16cae3faab5cf0`.

The supported driver review records the inspected one-file working-directory
correction at `sha256:478797847c72055e48f14c09032607d84ac420aeb263c7283b8d57c29abf78de`.
Whole-tree lint/format, roadmap dependency validation and unchanged runner review
pass; full source/lifecycle/independent graph/retained qualification remains open.

Independent source review approves the `64bc08fd` working-directory correction
and causal regression; subsequent scheduling prose preserves identical
production bytes. Review record SHA-256:
`f9afba297d14b149e8221bd0ad5a16ee3cdd64b5571f8e80f803949b8ccb9058`.


## Current-main successor source qualification

The ordinary merge `d31e8f095603d659ddf659d87eb66a11cccb8850` includes upstream
`32411a812e27b29495f9bb6d8f3e02073950941b`. Its reconciliation changes only root
Markdown and records the supported documentation review; all production, test,
tool and configuration blobs remain identical to the reviewed `8ca49bdc`
contribution. Independent source-preservation review SHA-256:
`62c436c4ede3c70ae800c2a8292f911bfbc3738a81d81461496478346af39555`.

The canonical managed `make python-check`, with two workers on native macOS,
passes at exact `d31e8f09`: 2,757 selected, 2,628 executed/passed, 129 skipped,
zero failed and zero known-failed. The complete request takes 3,304.660 seconds;
this is one observed duration, not a benchmark. The native Rust/Cargo1.97.1,
Git2.56 and sccache0.19 profile is explicit. The earlier native targeted request
also passes the actual foreign-owned Git control plus seven Rust import,
shared-provider, relocated-type and cache controls without skips.

All 5,291 tracked input representations independently match that Git commit
(5,289 byte-identical and two declared CMD CRLF conversions); before/after source
and native tool hashes remain equal and the checkout is clean. All four bounded
phases exit0 with owned-group cleanup. The exact controller instance and private
temporary root are independently absent; operator Cargo configuration remains
unchanged. Structured report SHA-256:
`9d17db626c9c85d4b8cb630ed6350371680063b4fd960b3338c6011e0bf2b562`;
retained raw archive SHA-256:
`5c4e576638fa09228e9f38c20c8bd34acbb57ff74d41064a77f6a39c0c113a5b`.
The failed `cb50ad43` and earlier Git2.43 requests remain preserved; this new
source-bound result does not rewrite them or complete the lifecycle below.

## Actual installed-public single-library baseline

The canonical wheel-smoke clean-source projection, provenance environment and
copied-runtime constructor produce one private noneditable wheel at the same
`d31e8f09` source. Wheel SHA-256:
`f829ac36fb3cdfa0ac5e2e5cdfa409aff5056e0b34ecd118154a7eea220a1940`;
installed distribution:
`sha256:156feb91b2c0d1a241cea36cc461d08cd1cf431bb8c2d080039db2b607102174`.
No global installation or downstream framework pin changes.

The unchanged `tests/support/installed_rust_harness_journey.py` actually runs
with isolated installed Python. It verifies exact installed authority, rejects
source-framework imports, uses real initialization observers and runs the public
lock, documentation-review and rebuild commands. Its controlled external coding
CLI supplies source from the actual generation request; no framework result,
qualification or receipt observer is replaced. The independent v2 serde oracle
and committed receipt pass, with actual library package blob
`5a06deff0c59dd800c83ede600bd2edbd5b9d5235115946dbc6fd2704f97aaa0`
and receipt `a7087926c88abfb94da202e9fbfed3523f96dd81194484920f158783d2904f7e`.
This is a single-library model-free baseline, not the required native provider
closure, real model generation, whole wheel-smoke report or retained admission.

The native job and all five bounded phases pass in 123.706 seconds. Its outer
wrapper subsequently fails a terminal metadata assertion: a live cgroup equality
guard precedes normal `RemainAfterExit` handling. A separate tiny actual unit
reproduces the same-invocation exit0 transition to an empty terminal cgroup.
The original wrapper remains failed; its missing terminal manager observation
is not reconstructed from that control. A forward terminal-only guard correction
is separately unexecuted and does not rebind the old native result.

Independent readback verifies all tracked source representations, exact wheel
origin/installed identity, receipt, oracle and package blob, unchanged tools,
operator Cargo config and provisioned crate archives, and all 1,272 retained raw
archive members. The resource unit, cgroup and controller instance are absent;
the exact owned RAM workspace is retired after proof collection. Final measured
workspace allocation is 544,747,520 bytes (544,755,712 bytes including the final
result at retirement), within the declared RAM-only profile;
the separate disk-backed full-build hold remains unchanged. Native/public raw
archive SHA-256:
`17773a52bdf15acb70346e3188566d24b657e2f3e829f4eb5eabd12f80096304`;
independent readback SHA-256:
`adc41d42f56618dc04d3e509bef66bcf2bce817804b09045f2242b8e4668b040`.

Next, author the actual public beta/Event, alpha/read and direct-plus-transitive
consumer contracts and disclose controlled external source generation. Qualify
two clean lifecycle roots, both existing independent graph verifier paths, sealed
relocation and the real retained consumer plan/materialize/admit boundary. A
symbol-only oracle, passed builder fixture or copied standalone Cargo consumer
cannot replace those remaining gates. The contribution remains draft; no upstream
main merge, release or downstream framework rebind is claimed.

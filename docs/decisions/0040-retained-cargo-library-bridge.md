# ADR 0040: Bind Qualified Library Packages Into Retained Cargo Workspaces

- Status: Accepted
- Date: 2026-09-11
- Decision owners: literate-ai maintainers
- Release target: 1.1.0
- Roadmap: [RETAINED-LIBRARY-BRIDGE-001](../roadmap/active-work.md#x-retained-library-bridge-001-bind-qualified-native-libraries-into-retained-build-graphs)
- GitHub issue: [#380](https://github.com/NVIDIA-dev/literate-ai/issues/380)
- Approval: Maintainer explicitly accepted ADRs 0040 and 0041 in the development conversation on 2026-09-12.

## Context

[ADR 0038](0038-importable-library-artifact-authority.md) defines exact qualified
library exports and native Component consumers. It explicitly leaves arbitrary
package-manager workspace graphs to another decision. A retained Cargo workspace
is such a graph: converting one provider must not require converting all its
consumers at once. [ADR 0039](0039-1.1-capability-boundaries.md) preserves independent
adoption boundaries but does not select artifact delivery or Cargo integration.

Removing a retained provider crate currently leaves its consumers' path dependencies
and workspace membership pointing at absent source. An ignored local cache is not
a reproducible dependency; committing the generated crate, including an archive of
its source, would make generated output an alternative source authority. Re-running
a model in every clean CI build would neither consume the exact accepted package nor
satisfy the issue's model-free consumption requirement.

Cargo automatically includes in-tree path dependencies as workspace members unless
excluded, and workspace membership affects which packages workspace commands test.
The bridge must therefore review membership as well as dependency paths.
See the [Cargo workspace reference](https://doc.rust-lang.org/cargo/reference/workspaces.html).
Cargo's restricted local path overrides and registry/git patch mechanism are not a
general replacement for explicit retained path edges. The proposed bridge uses
reviewed ordinary manifest changes, not ambient overrides.
See [Cargo dependency overrides](https://doc.rust-lang.org/cargo/reference/overriding-dependencies.html).

## Decision

### Commit binding authority, not generated packages

Add a versioned, provider-neutral retained-library binding owned by the adopting
project. The Rust adapter is its first supported native consumer. A reviewed binding
records:

- exact provider Component revision, interface and import-surface identities;
- qualified package/export identity and byte identity, including executable modes;
- target, Cargo/rustc toolchain requirements, features and exact transitive artifact
  dependency closure;
- the qualification evidence closure and verifier/policy identities which admitted
  that export, plus the importing project's explicit trust decision;
- retained workspace location, intended package names, manifest dependency edges,
  workspace membership and the corresponding reviewed lockfile;
- the exact package materialization destinations and an explicit artifact source.

The binding is created from reopened, current qualification evidence, not from a
manually supplied boolean, a package's own declaration, or a passing build alone.
Import checks revalidate that closed evidence against the binding. Digests provide
integrity and substitution detection, not an assertion that an untrusted producer
is approved. Approval of the checked-in binding is the maintainer trust boundary
under 1.1's same-user execution model; this does not add a signing service or promise
hostile-operator isolation.

Retained manifest/lock identities belong to the reviewed integration plan and
consumer evidence. They must not make every unrelated consumer-source edit require
regenerating or requalifying the provider. Each normal build proves current consumer
inputs against the same exact bound library.

### Make artifact availability explicit and model-free

The first delivery transports an immutable export bundle containing the sealed
library graph and its required qualification evidence. It reuses the existing
library export and canonical archive contracts; it does not introduce another
serialization of generated source or accept a source-cache directory as evidence.

Support an explicitly supplied artifact file (for an isolated/offline CI job) and
an explicitly configured HTTPS artifact location. Both resolve to the exact expected
bundle digest and size bounds. The location is a delivery hint, not package identity;
mutable URLs must still yield the pinned bytes. Every transitive package must be
present in the closed bundle or separately named by exact binding. No semver/latest
selection, fallback registry package, model regeneration or ambient-cache discovery
may repair an absent artifact silently.

Private artifact authentication belongs in explicit local/CI credential configuration,
not committed URLs, binding documents or diagnostic output. Retrieve only from the
reviewed endpoint; do not forward credentials across redirects or allow a redirect
to broaden the approved destination. Offline mode performs no network access and
requires the caller-provided exact artifact inputs. The implementation must validate
TLS, byte/entry limits and paths before publication, reject archive links, traversal,
duplicate destinations and platform case collisions, and clean up only its own
temporary files. No archive member is executed during retrieval or verification.

Artifact publication, retention policy, endpoint/account choice and credentials are
operator responsibilities. Accepting this design does not authorize an upload, a new
service, or access to an external account. A clean CI job may have authenticated
artifact/network access without having model access; fully offline consumption also
requires its ordinary Cargo dependency closure and toolchain to be provisioned.

### Use normal Cargo paths after verified materialization

Materialize verified packages under a short ignored project-relative directory, with
no user-specific absolute path in committed authority. Bind that destination to the
selected target and bundle; switching the target requires explicit revalidation and
must refuse concurrent conflicting use of the same destination. Packages remain
disposable. Reopen their identities before consumption and after the build; keep
Cargo output in a separate target directory.

The reviewed integration delta updates each retained dependency edge to that package
path, preserving dependency aliases, features and dependency kinds. It also updates
explicit and globbed workspace membership/default-members as necessary. The removed
retained path must not remain a required member or a hidden alternate resolution.
Keep the materialized provider as a member when the existing full-workspace gates
require its tests; an exclusion must be explicit and retain equivalent provider test
coverage, not silently reduce the original test set. Standalone package manifests
must not inherit fields or paths from the retired provider workspace.

The bridge verifies the resulting `cargo metadata --locked` graph before build/test:
each declared edge resolves to the expected package root, name and version, with the
reviewed feature/target configuration. Registry/git dependency authority continues
to use the reviewed Cargo.lock and provisioned dependency policy. Registry packages
cannot stand in for a bound library. Transitive bound libraries resolve only to the
same materialized graph. An unresolved workspace-inherited field, missing path edge,
unbound target or incompatible toolchain fails before consumer compilation.

A documented materialization step precedes ordinary Cargo commands in fresh CI.
`cargo build` alone is not promised to fetch Literate AI artifacts. Once provisioned,
the retained workspace remains a native Cargo project, including editor tooling;
no per-call executable bridge or runtime subprocess replaces in-process imports.
Ordinary read-only check commands do not install tools, fetch packages, invoke a
model, rewrite Cargo.lock or mutate manifests.

### Transfer one source boundary only after both sides qualify

Use an acknowledged, content-identified integration plan. It binds the old retained
source inventory, current independent provider qualification, complete manifest and
lockfile delta, artifact bindings, and exact workspace gates. Apply revalidates all
inputs under conversion custody and refuses dirty/conflicting affected files.
Prospective edits and source removal are tested in a disposable full retained
workspace first. No source in the real tree is retired merely to see whether it works.

Provider qualification is necessary but not sufficient. The exact prospective
workspace must also pass its existing full build/test gates, with attributable
positive test results and no omitted members or tests. Shared-source ownership and
other retained Components remain governed by the reviewed monorepo plan. Refuse a
transfer that deletes source another retained boundary still owns or needs.

After that evidence and explicit source-retirement acknowledgement, publish only
the reviewed binding/manifest/lock delta and the exact boundary transfer. Preserve
independent state for all other boundaries. An interrupted apply restores only
owned changes and preserves concurrent foreign edits; incomplete publication cannot
appear qualified. Subsequent provider/interface/package/target/toolchain drift
invalidates consumption; consumer-source drift invalidates consumer receipts without
claiming the original retained source is authoritative again.

## Alternatives and consequences

- Keeping generated source in Git, hiding it in a committed archive, or relying on
  an ambient cache violates the requested authority/reproducibility boundary.
- Requiring whole-workspace conversion first defeats incremental adoption.
- Publishing ordinary crates to a registry may be useful later, but publication is
  a distinct external action and registry package identity alone omits qualification.
- Rewriting manifests invisibly on every Cargo invocation hides the reviewed graph.
  Instead, commit the explicit integration delta and provision the exact package.

Projects gain an artifact availability obligation and a CI provisioning step. The
bridge does not promise offline access to an artifact that was never supplied, or
compatibility across unqualified targets. This decision adds retained Cargo graph
consumption; it does not broaden Python/JavaScript workspace support, native ABI/FFI
support, containment guarantees or release publication authority.

## Validation and implementation order

1. Define and round-trip the binding and closed artifact/evidence contracts; reject
   forged qualification, incomplete closures, wrong interfaces and target drift.
2. Implement bounded authenticated retrieval and transactional materialization.
   Test malformed/truncated archives, substitution, credential redaction, redirects,
   collisions, offline behavior and concurrent/replaced output preservation.
3. Validate real Cargo workspace dependency resolution, alias/feature/target edges,
   workspace membership, inherited metadata and locked transitive dependencies.
4. Integrate reviewed conversion/scope/transfer custody with #365; test rollback and
   independent invalidation before exposing an apply operation.
5. Prove a fresh checkout, empty artifact cache and disabled model access can retrieve
   the exact qualified package and pass the retained workspace's complete gates after
   retiring one provider. Repeat with a supplied artifact and networking disabled.
6. Prove package/evidence/toolchain substitution and missing artifact failures, then
   repeat the first-change journey with a second boundary while the first remains
   valid. Run full local and exact-revision hosted platform qualification.

All of these are required evidence, not results obtained by writing this proposal.
Design approval authorizes implementation. Source retirement and other operations
retain their separate execution acknowledgements.

Implementation contracts are tracked in [retained library bindings](../architecture/retained-library-bindings.md).

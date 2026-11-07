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

Implementation remains in progress. No dependency projection, independent native graph acceptance or authority-transfer completion is claimed by this design record.

## Graph-value development checkpoint

The new finite graph contract passes eight focused cases through canonical `make python-check` with a session-managed environment. They cover a shared diamond, dependency-free input, missing/disconnected nodes, direct/disconnected cycles, changed identity materials, normalized package/alias collisions, canonical ordering/bounds and closed wire decoding. Ruff formatting and lint pass. Log SHA-256: `b9a259e3bb1317990dd73f0a1e7ea7bf6b2c27c51ba477181279499ab478e5f8`. These are graph-value controls; no filesystem/package admission, Cargo execution, full Python gate or complete dependency-projection acceptance is claimed.

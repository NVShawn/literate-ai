# ADR 0036: Bind npm Dependency Replay to Exact Flavor Authority

- Status: Accepted
- Date: 2026-09-05
- Decision owners: literate-ai maintainers
- Roadmap: [JAVASCRIPT-NPM-LIFECYCLE-001](../roadmap/active-work.md)

## Context

`package-npm` permits an enumerated, lockfile-pinned dependency closure, while the
portable JavaScript generation skill unconditionally prohibited npm packages. The
Standard JavaScript builder then syntax-checked and copied source without replaying
the lock, so ignoring the prompt conflict produced an artifact that failed at runtime.
Static package-lock and CycloneDX validation did not make dependencies executable.
Standard npm v3 lockfiles also omit redundant `name` fields from non-root package
records and may retain optional runtime nodes plus required or explicitly optional
peer relationships. Treating those standard shapes as malformed made the first
implementation unable to admit the motivating PostgreSQL client closure.

An older experimental builder inferred permission from the presence of
`package.json`. That reverses the authority boundary: generated content must not grant
itself network or package-manager privileges. It also did not participate in the
current Standard command, cache, evidence, or packaged-runtime contracts.

## Decision

1. JavaScript is dependency-free unless the exact Component lock selects the
   `package-npm` Flavor. The generation prompt expresses this condition directly;
   generated content never selects or installs its own toolchain.
2. `package-npm` contributes a typed Standard npm command profile. Projection binds
   the selected Flavor revision, profile bytes, Node identity, npm CLI identity,
   package manifest path, lock path, and build resolver into one target. Node and npm
   may be installed under different prefixes. The selected Node resolves npm's
   production dependency graph; the target binds the complete npm root tree, every
   resolved package root, and the resulting graph. npm then executes through that
   exact Node runtime. Node, the npm entrypoint, and all bound npm package bytes are
   revalidated across execution.
3. The 0.10 lifecycle initially supports one dependency-bearing package root with
   exact direct semantic versions and a complete package-lock v2 or v3 graph. Non-root
   package names are derived from their canonical `node_modules` paths when npm omits
   redundant names. Exact locked optional runtime nodes remain required in the realized
   artifact. A required peer must resolve to an already admitted locked package; a
   missing peer is allowed only when its `peerDependenciesMeta` entry explicitly marks
   it optional. Workspaces, alternate package managers, non-registry dependencies,
   duplicate package versions, lifecycle scripts, and native-addon builds fail closed
   until separately designed.
4. Before npm runs, the lifecycle validates manifest, lock, imports, and source SBOM
   names, ranges, and graph agreement. A dependency-bearing manifest without an npm
   target fails before tool invocation. The authorized build plan records dependency
   resolution before compile and explicitly requests build-tool and network
   privileges.
5. The exact shell-free npm command replays the lock with scripts, audit, funding, and
   binary links disabled in a fresh object-directory projection. Source and lock bytes
   are rechecked afterward. npm inventory must equal the admitted lock graph.
6. The installed `node_modules` tree is derived state, never admitted source. It is
   copied from object custody into the sealed artifact beside the application so Node
   CommonJS and ESM resolution behave consistently on Linux, macOS, and Windows.
   Library-only tests execute against that dependency-complete export rather than the
   admitted source tree. Ambient `NODE_PATH`, `NODE_OPTIONS`, Node compile-cache state,
   npm configuration and authentication, global installation, login, and registry
   publication are not authorized. Node's compile cache is explicitly disabled.
7. Artifact evidence binds the Flavor, profile, source authority, Node/npm tools,
   install and inventory processes, and installed tree. Partial or inconsistent
   staging is deleted and never enters the content-addressed build cache.
8. npm itself remains an observed host tool, not an unbounded launcher. The observer
   rejects links, reparse points, native/object/archive/WebAssembly payloads, computed
   dynamic imports, entrypoints that escape a package root, and ESM package-subpath or
   external package-import aliases whose exact entrypoint closure cannot yet be
   represented. Literal root-package imports and local package-import targets are
   resolved by the selected Node and bound to the hashed package closure. A CommonJS
   resolution guard rejects execution outside the exact package roots. These checks do
   not claim whole-program proof: arbitrary JavaScript behavior such as direct file
   reads, `eval`, or child-process execution remains part of the explicitly selected
   npm tool's trust boundary.
9. A same-process cache hit is checked against an in-memory identity retained outside
   the artifact. A reopened lifecycle does not trust an artifact's self-manifest: it
   replays the npm build into fresh staging and requires byte-identical output before
   reuse. General durable external cache checkpoints for every Standard builder are
   follow-up work in issue #327.

This decision implements dependency replay and executable application artifacts. It
does not implement construction or publication of an npm registry package; that is a
separate packaging-provider contract.

## Consequences

Selecting `package-npm` now has an observable host prerequisite and an explicit
network-capable build phase. A project without that selection cannot gain npm access by
emitting a manifest. The deliberately narrow initial package shape keeps replay and
inventory comparison deterministic while leaving future workspace or native-addon
support additive. Unsupported npm distributions fail closed; support is a property of
the observed bytes, not just a nominal major-version range.

## Validation

- Compare exact combined generation prompts with and without `package-npm`.
- Prove no-Flavor rejection occurs before npm and that missing/mismatched authority,
  tool drift, failed install, changed lock, source-BOM range disagreement, unsafe npm
  implementation bytes, and incomplete inventory publish nothing.
- Build and execute a locked pure-JavaScript dependency through the Standard lifecycle;
  verify source remains byte-identical and the packaged artifact resolves locally.
- Replay an npm 11 v3 lock for `pg@8.23.0`; prove path-derived names, its exact optional
  `pg-cloudflare` node, its satisfied `pg-pool` peer edge, and its explicitly absent
  optional `pg-native` peer agree with the installed inventory.
- Exercise supported npm 9, 10, and 11 distributions through their selected Node;
  retain fail-closed evidence for an incompatible npm 12 distribution rather than
  inferring compatibility from its version alone.
- Reproduce ESM subpath and package-import authority escapes and prove observation
  rejects them before the npm CLI executes.
- Run focused contract/integration tests, full local validation, and exact-head Linux,
  macOS, and Windows release matrices.

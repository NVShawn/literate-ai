# ADR 0038: Importable Library Artifacts Carry Exact Test, Acceptance, and Consumer Authority

- Status: Accepted
- Date: 2026-09-10
- Accepted: 2026-09-10 (operator direction after Proposed review)
- Decision owners: literate-ai maintainers
- Release target: 1.1.0
- Roadmap: [LIBRARY-ARTIFACT-001](../roadmap/active-work.md#library-artifact-001-admit-an-importable-library-artifact-component)
- GitHub issue: [#280](https://github.com/NVIDIA-dev/literate-ai/issues/280)

## Context

Literate AI already admits `kind: library` as a Component with no process
entrypoint, and its local artifact store can seal either a file or a directory.
Those facts do not yet form a usable library lifecycle. Standard command projection
rejects a Component with no entrypoint because its TEST and EXECUTE phases assume a
subprocess-shaped portable application. Independent acceptance otherwise exempts the
same Component because there is no CLI invocation to give the existing JSON oracle.

An earlier experiment selected Python `unittest` and Node `--test` discovery for the
zero-entrypoint case. Native runner output could not prove that every and only selected
generated case ran, while converting one aggregate exit code into a passing result for
each case would fabricate attribution. The JavaScript inline driver also violated the
bounded command-token contract. Restoring that experiment would therefore weaken the
evidence chain rather than complete it.

The missing product boundary is broader than test dispatch. A qualified library must
be consumable through the language's normal in-process dependency mechanism and the
consumer must bind the exact accepted artifact, public capability, import surface, and
target/toolchain compatibility. Copying ambient generated source into a verifier or
consumer tree loses that identity. Disguising the library as a portable application
preserves acceptance only by imposing a subprocess and JSON round-trip on an
in-process API.

Python and JavaScript are the minimum fixtures in issue #280. The first downstream
adopter also needs Rust crates, so leaving native compiled-language package semantics
unspecified would make the authority-transfer contract incomplete for its motivating
use.

## Decision

### Keep `library` as the lifecycle kind

`kind: library` remains the authored Component kind and continues to require an empty
entrypoint set. This ADR does not add a second `library-artifact` authoring kind and
does not change `cli-application`, `persistent-service`, or their existing command
identities.

Standard projection branches on the reviewed Component kind, never merely on an empty
entrypoint list. Other zero-entrypoint kinds retain their current fail-closed or exempt
behavior until their own lifecycle contracts exist.

### Seal a package-shaped export

A library build produces one immutable directory export with role `library`. Its ABI
identity binds:

- the provider Component revision and exact public-interface identities;
- a typed, language-specific import surface mapping each provided capability to the
  package/module/crate name and exported symbols;
- the target profile, language/build profiles, producer, source tree, toolchain,
  authorization, and dependency artifact identities already carried by Standard
  artifact evidence; and
- the canonical tree blob of the package export.

The export includes only the language-native package contents required by a consumer.
Lifecycle manifests, verifier programs, acceptance answers, object directories, and
ambient source outside the export are not part of the package.

Python exports a package tree imported from an isolated artifact root. JavaScript
exports a package tree whose checked manifest and `exports` map are part of the sealed
bytes. Rust exports a Cargo library package (`Cargo.toml`, lock/dependency authority
when applicable, and `src/`) that a consumer compiles as an exact path dependency.
Rust build objects and verifier targets stay outside the immutable package.

### Separate generated tests from independent acceptance

Library TEST remains generated-candidate evidence. A framework-owned, language-specific
driver loads the sealed package and invokes the generated test adapter for each exact
selected case. The driver, rather than a native test runner's aggregate status, emits
`literate-ai/generated-test-results@1`; the lifecycle still requires every and only
the selected case identities in canonical order. Generated code cannot add, omit, or
rename cases by printing a favorable aggregate.

Independent acceptance is a distinct verifier-owned library oracle under
`verification/acceptance/<component>.json`. It binds the current specification set,
provider public-interface identities, import surface, and exact acceptance-harness
source identity. Its cases are keyed by provided capability. The language-specific
verifier imports the sealed artifact in-process and reports one observation per exact
declared case. A stale oracle, missing capability, unexpected case, import escape,
artifact mismatch, or nonzero harness result fails acceptance.

The verifier runs in fresh custody. It may materialize the sealed export and its exact
declared artifact dependencies, but it must not copy from the generated-source cache or
resolve a same-named ambient package. Python receives a bounded import root, JavaScript
imports the bound export without ambient `NODE_PATH`, and Rust compiles a separate
verifier crate whose path dependencies resolve only to the materialized sealed package
graph.

### Bind consumers to accepted providers

An in-process dependency edge resolves by capability exactly as today, then gains a
library consumer binding. That binding names the provider Component revision, public
interface identity, library export identity and blob, import-surface identity, target,
and dependency closure. Consumer planning and generation receive the import surface;
build materialization supplies the corresponding sealed packages through the normal
language mechanism.

A consumer cannot satisfy the edge from an arbitrary package already on `sys.path`,
`NODE_PATH`, Cargo registries, the working directory, or the retained source tree.
Changing package bytes, import names, exported symbols, target/toolchain authority, or
any transitive library artifact changes the binding and invalidates stale build and
acceptance evidence.

The first implementation proves this binding with Python, JavaScript, and Rust library
fixtures plus a real in-process consumer for each. It supports dependency-free package
fixtures and exact library-to-library edges. Registry publication, dynamic/shared
libraries, native ABI loaders, foreign-function interfaces, arbitrary package-manager
workspace graphs, and cross-language bindings are separate decisions.

### Preserve lifecycle compatibility

The command contract retains BUILD, TEST, and EXECUTE in canonical order for identity
compatibility. For a library, EXECUTE is a non-product lifecycle verification phase:
it loads the exact export through the declared import surface and proves the package is
consumable, but exposes no process entrypoint and performs no application smoke mode.
Command and evidence schemas gain explicit library discriminators rather than
overloading a fabricated entrypoint.

Existing executable Components serialize and hash byte-identically. Existing
zero-entrypoint non-library Components remain unchanged. A library without a complete
package, test, acceptance, or consumer contract fails before host execution; it does
not fall back to independent-acceptance exemption when authority transfer is claimed.

## Consequences

Qualified pure-logic Components can replace retained in-process code without a
subprocess architecture change. Their generated source remains disposable while the
accepted package is reproducible, immutable, independently tested, and consumable by
exact identity.

The framework acquires three verifier drivers and language-specific package rules.
Rust acceptance is necessarily compile-time in a separate verifier crate, while Python
and JavaScript can load at runtime; the common contract is the sealed package and exact
capability observation, not one universal invocation mechanism.

Library generation skills must emit an explicit import surface and generated test
adapter. Derived projects must keep verifier harnesses separate from generated source.
Build caches and package assembly gain library dependency edges, but the underlying
directory-export storage remains unchanged.

The two former roadmap entries for this delivery are consolidated into one execution
checklist linked here. This ADR was accepted by operator direction on 2026-09-10 before
implementation began.

## Validation

- Contract round-trips and negative tests prove library-kind discrimination, canonical
  import mappings, exact consumer binding, and byte-identical executable contracts.
- Python, JavaScript, and Rust fixtures build immutable package exports, run every and
  only selected generated case, earn non-exempt independent acceptance, and execute a
  real in-process consumer through the bound capability.
- Tamper, stale-interface, stale-oracle, wrong-package, ambient-package shadowing,
  missing/extra case, dependency substitution, and target/toolchain mismatch fixtures
  fail closed before acceptance or consumption.
- Tests prove verifier custody contains the sealed artifact graph and verifier-owned
  harness only, never copied ambient generated source.
- Existing portable-application and persistent-service conformance remains unchanged.
- Full local validation and the exact-head Linux, macOS, and Windows CI matrix pass.

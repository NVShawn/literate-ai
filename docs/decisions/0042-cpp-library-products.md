# ADR 0042: C++ libraries export compiled products and public headers

- Status: Accepted; implementation is complete and landing qualification is pending
- Acceptance: Maintainer approval on 2026-09-14
- Decision owners: Literate AI maintainers
- GitHub issue: [#427](https://github.com/NVIDIA-dev/literate-ai/issues/427)
- Extends: [ADR 0038](0038-importable-library-artifact-authority.md)

## Problem

The existing library lifecycle supports Python and JavaScript package imports and
Cargo source packages. It does not support generated C++ library producers. C++
consumers need public headers, linkable binaries, compatible compiler/runtime
settings and, for shared libraries, runtime files. Enabling the language name or
archiving a generated command-line application would not provide these products.

This decision specifies the product, generation, acceptance and packaging contracts
together. The ordinary lifecycle now implements them. The built-in C++ Flavor selects
static-library production by default; a reviewed replacement profile selects shared
production. Vendor SDK admission and source-retained migration are distinct: neither
qualifies a generated C++ producer.

## Product and authored interface

Keep `kind: library`, with no product entrypoint. Retain exact capability and public
interface identities and the existing artifact dependency graph.

Add a C++ import declaration mapping each capability to a public header and one or
more fully qualified C++ names. For example, the `sample.math` capability may map
to `sample/math.hpp` and `sample::add`. Header paths are portable relative paths
under the public include root; names are identifiers separated by `::`, not free
C++ expressions or compiler arguments. Overloads, templates and types are described
by the bound public-interface document and exercised by the independent harness.
A list of names alone cannot prove their signatures or behavior.

The selected native Flavor declares static or shared production. Do not infer
product kind from whichever files the generator happens to emit. A typed layout
binds the exact public header closure, link files and runtime files relative to
one sealed directory export. Link and runtime roles may name the same file on
ELF and Mach-O platforms. Windows shared production declares both its import
library and DLL. A static product has no runtime file of its own, although its
exact dependencies may have runtime files.

The export contains the public include tree, declared binaries, required runtime
files, and reviewed package metadata. Private implementation translation units,
test adapters, test executables, verifier sources, object files and build caches
remain outside the export. Public inline/template implementation is allowed only
within the declared public header closure.

Preserve `ArtifactExport` authority for provider revision, source tree, target,
toolchain, producer, authorization, dependencies and sealed bytes. Its ABI identity
also binds the C++ import surface and product layout to the selected native ABI
profile. That profile includes target architecture, object format, C++ standard,
compiler ABI family, standard library/runtime selection and configuration choices
that affect linkage. No new free-form compiler flag string acts as ABI authority.
The initial compatibility rule requires exact profile identity; any broader
compatibility policy must be explicit and independently tested.

Existing Python, JavaScript, Rust and executable contracts retain byte-identical
wire representations. New C++ shapes use explicit versioned contracts. Missing or
ambiguous native fields fail before compilation; they never select a source-package
fallback. All new fields survive authoring, locks, generation recipes, worker
custody and consumer planning.

## Build and generated tests

The generator emits ordinary C++ implementation and the exact declared public
interface. Native Bazel targets compile a real static or shared library, with a
separate generated test adapter linked against that library. Compiler/linker tools,
Bazel/module inputs and dependency artifacts come from the bound lifecycle plan.
Do not fetch ambient packages or discover undeclared headers from the checkout.

The lifecycle copies only declared outputs into the sealed export, verifies their
closure and records the observed product layout. The selected target determines
platform output rules. Check that files are actual archive/shared-library products
for that target; a filename suffix is insufficient evidence.

Generated tests run from a separate test artifact. The framework dispatches every
and only selected case and validates the existing case-result protocol. Preserve
per-case failures and attribution; an aggregate native exit code must not become
invented passing observations. Neither test source nor executable enters the
consumer library export.

## Independent acceptance and ordinary consumption

Materialize the sealed public headers and compiled binaries into fresh verifier
custody. Compile a verifier-owned C++ harness against those files and the exact
sealed dependency graph. No producer translation unit or source cache is present.
The harness exercises the bound public interface, including namespaced symbols,
types and declared behavior, and reports each required capability/case observation.

Building or loading against a same-named ambient library must not satisfy the gate.
Bind include/link paths to materialized artifact identities. Restrict loader search
to declared runtime dependencies plus the bound platform runtime, then verify the
loaded product identities where runtime loading occurs. Verify the sealed export
before and after both acceptance and consumer execution.

Consumers use native include/link mechanisms through the existing exact capability
edge and `LibraryConsumerBinding`. Their build plans carry the header, binary,
ABI and transitive dependency identities. A changed public header, library binary,
ABI profile or selected dependency invalidates stale plans and evidence. Merely
compiling a header-only call is insufficient to prove compiled-library consumption;
the fixture must call a non-inline implementation from the sealed binary.

## Conan and pip responsibilities

Conan packaging projects the sealed native layout into a native package, including
correct consumer include, link and runtime metadata for the target. Package
verification restores into a fresh cache, resolves exact recipe/package revisions
and verifies the restored payload against its declared closure. The missing generic
payload check is tracked separately in [#429](https://github.com/NVIDIA-dev/literate-ai/issues/429).

Qualification installs the resulting package into a second fresh consumer cache
and builds a Bazel consumer against the restored headers and binaries, without the
producer checkout. Assert actual native symbol invocation and runtime identity;
`conan list` alone proves neither.

Python wheels continue through the existing Python packaging authority. A C++
library does not automatically become a Python extension module or portable wheel.
Bindings and wheel platform/ABI tags require their own declared Python interface
and tests when requested. This decision must not label native payloads `py3-none-any`.

## Required qualification

The feature remains unqualified until the following ordinary lifecycle cases pass:

- Generate, build and seal static and shared C++ libraries with a namespaced public
  API; independently compile and run a consumer calling a non-inline symbol.
- Test Linux ELF, macOS Mach-O and Windows COFF/PE, including the Windows DLL/import
  library pair, using the declared compiler/runtime profiles.
- Exercise real native Bazel clean, no-op, unrelated-edit, public-header-edit and
  implementation-edit builds. Record action counts and identities, incrementally
  restore an edited public header, and verify reproducibility under the declared
  toolchain's reproducible-build settings rather than assuming it.
- Build a Conan package, restore it into fresh custody and build/run the independent
  Bazel consumer. Verify payload and native package metadata against authority.
- Reject changed/missing/extra headers or binaries, wrong target/ABI, undeclared
  include or link inputs, ambient package shadowing, dependency substitution,
  stale interfaces/oracles and missing/extra test observations.
- Prove verifier custody lacks producer translation units and generated test code.
  Prove source deletion does not prevent use of the qualified compiled artifact.
- Preserve existing library and executable contract identities and behavior; pass
  focused tests, the full framework gate, installed qualification and hosted CI.

The macOS qualification records seven actions for each clean static/shared
product build, zero for both the immediate no-op and an unrelated-file edit, three
after changing the public header, three after restoring that header incrementally,
four after changing the non-inline implementation, and seven after restoring the
source and cleaning Bazel state. The header edit changes only the exported header in
the sealed product, while incremental restoration reproduces the complete original
product. The implementation edit preserves the header and changes the library digest;
the restored-clean header and library digests also exactly equal the first clean build
for both product kinds. The exact `387f857e` implementation also passes the complete
static/shared gate in a disposable Debian Bookworm Linux ARM64 container with Bazel
9.2.0 and Conan 2.32.0: the same action counts and header identity apply, restored
products reproduce exactly, and both native products proceed through the fresh-cache
Conan consumer lifecycle in 65.210 seconds.

The local fresh-cache Conan qualification also derives valid alternate settings
through Conan itself, then requires `--build=never` resolution to report a missing
binary after independently changing target architecture, build type, compiler
version or the compiler ABI runtime. It performs these four rejection checks for
both static and shared products before the exact matching profile is allowed to
generate Bazel metadata and run the consumer. This closes target/ABI profile
substitution on macOS, disposable Linux ARM64, hosted Linux x86_64 and hosted Windows.

Consumer qualification additionally records the full restored Conan reference and
compares it with the install graph's recipe revision, package ID and package revision.
The negative case publishes a newer same-name/version recipe into the isolated cache,
demonstrates that an unpinned install selects it, and requires exact-reference
verification to reject that graph. Only a recipe-revision-pinned install whose full
binary reference equals the verified archive may generate Bazel metadata and execute.
This closes ambient dependency substitution on macOS, disposable Linux ARM64, hosted
Linux x86_64 and hosted Windows.

The current implementation also passes the repository's isolated `make install-check`:
it builds a wheel, installs it into a temporary prefix without source-path imports,
and exercises the installed init, validation, lock, plan, release, lineage and
synthetic build/test/run contracts. This satisfies the installed-framework gate; it
does not replace platform-native Conan consumer qualification.

Exact-head hosted run
[`35092994515`](https://github.com/NVIDIA-dev/literate-ai/actions/runs/35092994515)
at `558861b5` passed all 17 jobs. Its dedicated Linux x86_64 and Windows native jobs
provisioned Bazel 9.2.0 and Conan 2.32.0, built and consumed both product kinds, and
required the parent test plus all ten profile/product subtests with no skip, failure
or error. The broader Linux, macOS and Windows matrix, installed-wheel checks,
documentation jobs and sample-composition jobs passed on the same head. Enabling the
qualified built-in static profile changes flavor authority, so that landing candidate
still requires its own exact-head hosted run.

These requirements are framework-owned and use neutral fixtures. Derived Kit
projects retain responsibility for their extension/runtime interfaces and their
own qualification against the accepted framework products.

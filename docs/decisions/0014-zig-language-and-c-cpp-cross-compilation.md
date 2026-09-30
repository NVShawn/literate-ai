# ADR 0014: Zig Is Both a First-Class Language Flavor and the Preferred C/C++ Cross-Compilation Toolchain

- Status: Proposed
- Date: 2026-08-23
- Decision owners: literate-ai maintainers
- Roadmap: `FLAVOR-ZIG-001`

## Context

Literate AI models implementation language, build system, operating system,
architecture, and toolchain as separate Flavor axes. That separation is necessary for
Zig because Zig has two legitimate, materially different roles:

1. Zig is a language with its own source semantics, standard library, package/build
   model, tests, ABI choices, and generated-code guidance.
2. A Zig distribution also supplies `zig cc` and `zig c++`, Clang-compatible compiler
   drivers backed by Zig's bundled LLVM/LLD and target libc support. They can compile and
   link C or C++ for many non-host OS/architecture targets without requiring a separately
   assembled cross compiler and sysroot for each target.

Treating both roles as one `lang-zig` Flavor would be dishonest. C source compiled by
`zig cc` remains C; C++ source compiled by `zig c++` remains C++. Its public language
contract, generated source rules, dependency semantics, and compatibility promises do
not become Zig merely because the executable named `zig` invokes the compiler.

Conversely, treating Zig only as a C/C++ compiler wrapper would omit a useful first-class
language whose explicit target model is particularly well suited to portable systems
Components. A greenfield Component that may be implemented in Zig, C, or C++ and must
cross-compile often has a lower and more reproducible toolchain burden when Zig is
selected from the start.

The phrase “cross-compilation is required” is also insufficient unless target authority
names at least a build host and a destination OS/architecture. In practice, ABI, libc,
minimum OS version, CPU baseline/features, object format, and linkage mode can also be
load-bearing. Producing bytes is not enough: Literate AI must know what those bytes claim
to target, how they are inspected, and where they may be executed for acceptance.

## Decision

### Model two separate Flavors

The framework will add two canonical Flavors.

| Flavor | Primary axis | Meaning |
| --- | --- | --- |
| `flavor://literate-ai/lang-zig` | `implementation.language-ecosystem` | Generate, build, test, and package Zig source as Zig. |
| `flavor://literate-ai/toolchain-zig-cc` | `toolchain` | Compile C with `zig cc` or C++ with `zig c++` for an exact native or cross target. |

`lang-zig` does not imply that all C/C++ work should be rewritten in Zig.
`toolchain-zig-cc` does not change the selected language axis. The lock, generation
recipe, build authorization, SBOM, artifact metadata, and receipt preserve both facts:

```text
language = cpp
toolchain = zig-cc
target = x86_64-linux-gnu
```

is different authority from:

```text
language = zig
toolchain = zig
target = x86_64-linux-gnu
```

### Flavor specification: `lang-zig`

The first Zig language Flavor has this normative shape:

| Field | Decision |
| --- | --- |
| Canonical name | `lang-zig` |
| Display name | `Zig portable application` |
| Primary axis | `implementation.language-ecosystem` |
| Target value | `zig` |
| Provided capability | `implementation.language.zig` |
| Applicable capabilities | portable applications and libraries whose public contracts do not require another language |
| Required toolchain | one exact supported Zig distribution |
| Generated root | `source/` only, following the repository-layout skill |
| Default build entry | `build.zig` when the Component needs multiple source files, tests, dependencies, generated options, or install artifacts |
| Minimal build exception | direct `zig build-exe`, `zig build-lib`, or `zig test` only when the selected build contract explicitly permits a single-unit build |
| Test boundary | `zig test` or a declared `zig build test` step plus independent Component acceptance |

The Zig specification-to-source skill must require generated code to:

- use the Component's exact public entrypoint and wire contract;
- use explicit integer widths and checked conversions at external boundaries;
- make allocation ownership and deinitialization visible;
- avoid undefined behavior, sentinel assumptions, and pointer lifetime leakage across
  public interfaces;
- preserve error sets or map them deterministically into the Component's typed error
  contract;
- avoid host introspection when target authority already supplies OS, architecture,
  ABI, or CPU facts;
- keep target selection in the build invocation or generated build configuration, never
  infer it from the build host;
- generate current-state tests without treating them as the independent acceptance
  oracle; and
- avoid fetching packages or toolchains during the authorized build unless an exact
  dependency resolver and policy explicitly permits it.

The first supported Zig version is selected and pinned during implementation rather than
hard-coded by this ADR. Zig remains pre-1.0 and has historically changed language,
standard-library, package-manager, and build APIs between releases. Therefore a lock
must bind the exact Zig executable bytes, reported version, selected distribution, and
the versioned Zig generation skill. A minimum-version range alone is insufficient build
authority.

### Flavor specification: `toolchain-zig-cc`

The Zig C/C++ toolchain Flavor has this normative shape:

| Field | Decision |
| --- | --- |
| Canonical name | `toolchain-zig-cc` |
| Primary axis | `toolchain` |
| Target value | `zig-cc` |
| Compatible languages | C and C++ only; Zig uses its native Zig toolchain realization |
| C compiler driver | exact Zig executable invoked as `zig cc` |
| C++ compiler driver | exact Zig executable invoked as `zig c++` |
| Linker/archiver | selected through Zig's supported driver/build surface, not ambient `ld`/`ar` discovery |
| Target input | one canonical target triple plus any separately declared CPU, ABI/libc, minimum OS, and linkage constraints |
| Build-system integration | explicit compiler/target arguments projected into the selected build-system Flavor without shell interpolation |

The toolchain skill must preserve C/C++ semantics and conventions. It may adapt flags
needed to express the exact target, sysroot/libc, CPU baseline, object format, and link
mode, but it must not translate source into Zig, introduce Zig runtime APIs into a public
C/C++ contract, or silently replace unsupported compiler flags with approximate ones.

For build systems such as CMake, Make, or Bazel, Literate AI supplies exact compiler
launchers and target arguments through the build plan. Generated project files must not
rediscover a host compiler from `PATH`. Compiler launcher paths and arguments remain
argv-structured data; no generated shell string may concatenate a target triple or
untrusted path.

### Prefer Zig when cross-compilation is explicit

Cross-compilation preference applies only when target authority says the destination
differs from the build host or explicitly requests a cross toolchain. It is not inferred
from a vague portability goal.

Selection follows this order:

1. An explicit user or Component language/toolchain selection wins if it satisfies all
   constraints. Literate AI never overrides an explicit `lang-cpp` with `lang-zig`.
2. Existing authored C or C++ source retains its language Flavor. If Zig supports the
   exact requested destination, `toolchain-zig-cc` is preferred over constructing an
   unpinned host-specific cross GCC/Clang toolchain.
3. For a greenfield Component whose language slot admits Zig, C, and C++ as semantically
   equivalent implementation choices, an explicit cross-compilation requirement makes
   `lang-zig` the preferred candidate, because the language and cross toolchain share one
   pinned distribution and target model.
4. If the Component contract requires a C ABI but not C source, `lang-zig` may remain
   eligible only when its public interface contract explicitly permits a Zig
   implementation exporting that exact C ABI.
5. If Zig cannot represent or ship the exact target, ABI/libc, CPU feature set, link
   mode, or required compiler behavior, selection fails closed or proceeds to an
   explicitly declared fallback order. It never drops `-target`, compiles for the host,
   or substitutes a nearby libc silently.

This preference must be represented as deterministic selection policy and recorded in
the lock report, including evaluated candidates and the reason Zig won or was rejected.
It must not be hidden in a generation skill or prompt.

### Bind the complete cross target

A cross build plan binds:

- build-host OS and architecture;
- destination OS and architecture;
- canonical Zig target triple;
- CPU baseline and explicitly enabled/disabled features when relevant;
- ABI and libc family, including GNU, musl, MSVC, or freestanding distinctions;
- minimum destination OS/libc compatibility when supported and required;
- static, dynamic, or freestanding linkage intent;
- artifact kind and object format;
- exact Zig distribution/tool identity;
- selected language and build-system Flavors; and
- external sysroot/SDK identity when Zig's bundled target support is insufficient and
  an explicit external input is permitted.

The destination is not reduced to an architecture token such as `arm64`. For example,
`aarch64-linux-musl`, `aarch64-linux-gnu`, `aarch64-macos`, and `aarch64-windows-gnu`
have different runtime and ABI obligations despite sharing a processor architecture.

The framework will maintain a tested supported-target subset rather than claiming every
target string accepted by a particular Zig release. A new target joins that subset only
with compile, artifact-inspection, and destination-execution evidence or an explicit
non-executing acceptance profile appropriate to a library/object artifact.

### Separate build-host and destination execution

Cross-compiled artifacts must never be executed on the build host merely because the
build completed. Lifecycle planning distinguishes:

```text
build host -> compile and inspect destination artifact
destination worker or emulator -> run generated tests and independent acceptance
```

Artifact inspection verifies at least the expected format, architecture, and linkage
metadata before transfer. Execution requires a worker whose observed OS/architecture
and required runtime capabilities satisfy the destination target. Emulation is allowed
only through a separately selected, content-pinned execution Flavor and must be recorded
as emulated evidence rather than physical-host evidence.

A cross build may still be useful when execution is impossible, such as producing a
static library for an embedded target, but that Component must declare an honest
non-executing acceptance profile. Successful linking alone cannot masquerade as runtime
qualification.

### Dependencies, C interoperability, and packaging

Zig package dependencies and C/C++ dependencies remain exact dependency authority:

- `build.zig.zon`, when generated, is part of the generated source/build description,
  not a license to resolve arbitrary network content during build.
- External packages, repositories, sysroots, SDKs, and headers are declared and bound
  through the existing dependency and SBOM mechanisms.
- `@cImport` inputs and C headers are direct public/build dependencies with exact
  provenance; host include directories are not ambient authority.
- C++ interoperability must use an explicit C ABI shim or another reviewed public
  boundary unless the selected Zig release and contract provide a supported direct
  mechanism. The framework does not promise a stable Zig-to-C++ ABI.
- Package plans retain destination target metadata. A host wheel, Conan package, archive,
  or executable cannot be relabeled as a destination package after compilation.

The source and resolved CycloneDX SBOMs record Zig itself as a build tool and record the
actual runtime/link dependencies of the destination artifact. Zig's presence as a build
tool does not imply that a C/C++ artifact has a Zig runtime dependency.

### Discovery and security boundary

Discovery is detect-first and side-effect free. It may execute only the selected Zig
binary with bounded version/target discovery arguments. It records:

- canonical executable path and byte identity;
- version output and parsed exact version;
- distribution provenance when managed by Literate AI;
- supported target evidence needed by the selected plan; and
- before/after identity checks around the build.

Automatic installation or upgrade requires user authorization and uses a pinned,
verified distribution. A project-local executable named `zig` is not trusted merely
because it appears earlier on `PATH`. Build arguments are passed without a shell, output
and duration are bounded, and generated build scripts receive no credential values.

Cross compilation increases supply-chain sensitivity because one tool can emit binaries
for many destinations. Build authorization therefore binds both tool and destination;
authorization for `x86_64-linux-gnu` cannot be replayed for `aarch64-windows-gnu`.

## Initial Catalog Shape

The planned authoring records are equivalent to the following abbreviated declarations.
Exact contribution schemas and content identities are supplied by implementation:

```yaml
# flavors/zig/flavor.md
name: lang-zig
version: 1.0.0
primary_axis: implementation.language-ecosystem
target: zig
provides:
  - name: implementation.language.zig
    version: 1.0.0
authoring_inputs:
  - kind: specification-to-source-skill
    uri: ../../skills/specification-to-source/zig-portable-application/SKILL.md
conflicts:
  - flavor://literate-ai/lang-cpp
  - flavor://literate-ai/lang-go
  - flavor://literate-ai/lang-python
```

```yaml
# flavors/zig-cc/flavor.md
name: toolchain-zig-cc
version: 1.0.0
primary_axis: toolchain
target: zig-cc
provides:
  - name: toolchain.c-family.cross
    version: 1.0.0
authoring_inputs:
  - kind: specification-to-source-skill
    uri: ../../skills/specification-to-source/zig-c-cpp-cross-toolchain/SKILL.md
```

Directory names follow the repository's currently implemented catalog migration rules;
the comments show logical catalog locations, not permission to bypass ADR 0010's
canonical naming and compatibility machinery.

## Acceptance

Implementation is complete only when all of the following hold:

1. `lang-zig` is present in the framework and initialized-project catalogs with
   byte-equivalent authority and one pinned Zig generation skill.
2. `toolchain-zig-cc` composes with C and C++ and is rejected with unrelated language
   Flavors.
3. Native Zig generation, `zig test`, build, independent acceptance, indexing, and SBOM
   resolution pass on every declared host platform.
4. At least one C and one C++ Component cross-compile through Zig to a non-host
   architecture or OS, and the output format/architecture is independently inspected.
5. At least one cross artifact executes generated tests and independent acceptance on a
   matching physical destination worker; an emulated lane, if supplied, is labeled.
6. Explicit cross-compilation selects Zig according to the deterministic policy, while
   native builds and explicit non-Zig selections remain unchanged.
7. Unsupported triples, ABIs, libc variants, CPU features, SDK requirements, and compiler
   flags fail before compilation or with a typed build error, never by host fallback.
8. Cache identities differ across language role, Zig version, target triple, CPU/ABI,
   build system, dependencies, and source authority.
9. Installed-wheel and initialized-project tests prove modules, Flavors, skills, and
   package data are complete.
10. The changed skills pass the pinned SkillEvaluator gate, and full cross-platform CI
    retains all existing C++, Go, JavaScript, Python, Rust, and Swift behavior.

## Alternatives Considered

### Make `lang-zig` replace C and C++ universally

Rejected. Language is product and implementation authority, not merely compiler choice.
Existing C/C++ source, APIs, dependencies, and compatibility contracts cannot be
silently rewritten or relabeled.

### Add only a Zig language Flavor

Rejected. It would omit Zig's immediate value as a reproducible cross compiler for
existing C/C++ Components and encourage users to encode `zig cc` as an ad hoc build
command outside toolchain authority.

### Add only a Zig toolchain Flavor

Rejected. Zig is a useful implementation language in its own right, with different
generation, testing, dependency, and safety conventions from C/C++.

### Prefer Zig for every native C/C++ build

Rejected. Native toolchains may be explicitly required for ABI, SDK, compiler extension,
debugger, certification, or performance reasons. The preference is triggered by exact
cross-compilation authority, not by Zig's availability.

### Put cross-target selection in the Zig skill

Rejected. Skills guide conversion but do not own target selection. Hidden prompt policy
would make locks non-reproducible and allow different models to choose different
destinations or compilers.

### Claim all Zig-supported targets immediately

Rejected. A compiler accepting a target triple is not complete qualification evidence.
The framework ships a bounded tested matrix and expands it with explicit evidence.

## Consequences

Zig becomes the seventh first-class language ecosystem and adds a reusable cross-
toolchain path for C and C++. Cross-target builds become easier to reproduce because a
single pinned distribution can replace many ambient host compiler/sysroot combinations.

The cost is a wider builder and target model. Build and destination hosts must be
represented separately, artifact inspection becomes mandatory, and test execution may
require worker routing or explicit emulation. Cache and SBOM identities also become more
specific because the destination ABI and CPU are load-bearing.

Zig's pre-1.0 evolution creates maintenance cost. Exact version binding and versioned
skills intentionally trade broad compatibility for reproducibility. Supporting a new
Zig release requires qualification and a skill/toolchain update rather than silently
accepting it through an open-ended minimum version.

This ADR does not authorize immediate replacement of existing C++ samples or default
project language selections. Those changes require their own reviewed migration and
evidence. It authorizes the two Flavor contracts and deterministic cross-compilation
preference needed to evaluate such migrations honestly.

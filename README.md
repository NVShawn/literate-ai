# Literate AI 0.3.0 (historical release marker)

Literate AI 0.3.0 was released on 2026-08-15 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `d1af57ab383e31e15ea07a89cef7989171648a06`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.3.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.3.0 - 2026-08-15

- Structured operation logging remains project/CLI configuration and no longer mutates
  the immutable published v1 workflow contract. Workflow normalization and runtime
  definitions again match the content-identity-protected schema.

- CodeGraph source indexing no longer treats `build`, `dist`, `out`, `target`, `obj`,
  `coverage`, and `.build` as ignored directory names when snapshotting a non-Git
  (generated) source tree. Those names are ambiguous build-artifact conventions that a
  generation skill can legitimately use for real generated source — the JavaScript
  sample's `source/build/bundle.js` and `source/build/clean.js` were previously omitted
  from the indexed snapshot while the caller's exact file manifest still declared them,
  so indexing failed closed with `source-index.codegraph-failed` and the `samples`
  release gate could never pass. Unambiguous dependency/tool caches (`node_modules`,
  `.venv`, `Pods`, `.cache`, ...) remain ignored.

- Standard worker bootstrap now installs the real wheel into a native isolated
  environment, preserving wheel data-file placement so installed Standard lifecycle
  resolution can attest package, policy, and schema authority. Linux and Windows
  workers also consume a separate digest-pinned CodeGraph 1.1.1 companion manifest,
  reject platform or runtime drift, and report capability plus executable identities;
  installed-wheel qualification executes the lifecycle through that worker-equivalent
  distribution and source-intelligence path.

- Locked-Component (`litai rebuild`) source generation now preserves an adapter's
  stable `.code` on an unexpected runner exception, matching the independently
  generatable (`litai generate`) path. Previously every unexpected exception in
  `component_generation_scheduling.py` collapsed to the opaque `runner-failed` code
  even when the raised exception already carried a specific, safe diagnostic code
  (for example a coding-CLI adapter's `coding_cli.unexpected_output`), discarding
  information a caller could otherwise use to tell failure classes apart.

- Standard SSH source revalidation now carries the accepted archive manifest through
  execution, so Windows no longer reports `execution.remote_source_mismatch` merely
  because it cannot reproduce POSIX executable mode bits. POSIX still reapplies and
  verifies canonical executable intent, while every host rehashes paths and content.
  Standard bootstrap now exports a target-specific binary wheelhouse and canonical
  manifest for the framework's complete runtime dependency closure. Workers verify every
  wheel digest, install only from that offline closure, verify installed RECORD content,
  and retain separate framework-distribution and dependency-closure identities through
  atomic activation. The legacy single-wheel path fails closed when dependencies exist.
  Built wheels now admit the complete nested project-template resource tree and retain
  the installed remote source guard used by Windows and POSIX worker launchers. Windows
  C++ discovery now initializes the vendor environment for the selected compiler's
  toolset version, preventing mismatched compiler and Standard Library headers.

- `_build` and `BUILD_DIR`/`generated/` are now the advisory default prefixes for
  fungible generated application source, not a mandatory cage for load-bearing project
  authority. Generated Component application source still belongs in the cache/`source/`
  tree; a coding CLI root Makefile as generated output remains rejected, while a
  self-modifying project's Makefile remains allowed project authority. Catalog and
  initialized-project template copies of `make-build-system` are byte-identical again.

- Authority graph: repository initialization, update, and reparent lineage resolution
  now use the canonical graph solver for deterministic ordering and shortest-cycle
  diagnostics before inherited authority is materialized. Inherited-catalog composition
  and provenance use the same graph closure instead of a parallel ancestry walk. Locked
  Component planning and Standard lifecycle scheduling also use canonical dependency
  layers with exact cycle paths.

- Added an inheritable MAC project-contract skill and `litai project mac-contract`
  command that deterministically projects `.mac/project.yaml` from one exact resolved
  CycloneDX BOM, with a compact identity sidecar and no parallel dependency scanner.

- Added global `litai -v` and `litai --verbose` diagnostics, with inherited,
  secret-redacted subprocess commands and bounded output on stderr while preserving
  stable command and JSON output on stdout.

- Standard generated-source admission now binds the component-orchestration request
  separately from each node's exact planned coding-CLI request. Deterministic source
  cache membership remains keyed by the planned request, while candidate provenance
  and worker continuation verify both identities. Multi-node generation no longer
  fails with `source_admission.cache_key_mismatch`, and swapped node evidence,
  orchestration drift, and transcript tampering still fail closed. Installed-wheel
  qualification now runs 12 generated nodes and 36 verifier tests through source-only
  admission, durable publication, and target-independent Linux/Windows restore, while
  proving a final receipt cannot claim downstream success before all node results exist.

- Added structured, secret-redacted operation and subprocess timing logs plus
  `litai profile` reports for build, test, generate, and rebuild workflows. Claude
  skill edits now invoke SkillSpector through a fail-closed hook: missing tooling
  and scanner failures are surfaced instead of silently bypassing admission.

- CI now actually exercises `pytest-xdist`/`pytest-randomly` parallel test execution:
  the Windows job's test step ran plain sequential `python -m unittest discover`,
  bypassing the new parallel-test configuration entirely and making it the workflow's
  long pole. It now runs `pytest` over the same `tests/unit`, `tests/conformance`, and
  `tests/test_package.py` closure so it benefits from `-n auto`. Also added a
  workflow-level `concurrency` group with `cancel-in-progress: true` so pushing new
  commits to a PR cancels its superseded in-flight run instead of leaving it to
  consume a runner slot to completion.

- Standard generated source can now be independently admitted before any target build.
  `litai generate --admit` executes verifier-owned source test commands, records the
  exact generation/cache/Flavor/skill/coding-CLI/framework identities, and publishes a
  source-only immutable cache member. `litai rebuild --from-accepted-source` requires
  one exact admitted member for every planned Component, starts at indexing/object
  construction, records source-admission identity in downstream lifecycle results, and
  fails with `source_cache.runtime_absent` instead of invoking a worker coding CLI.

- The README now provides a concise end-to-end setup path from installing `litai` and
  initializing a project through local hello build/test/run, private worker setup,
  explicit CPU/GPU requirements, capability probing, and optional cross-OS fan-out.

- OpenCode generation now probes the exact pinned executable's bounded
  `--pure run --help` surface before model egress and reports
  `coding_cli.incompatible` with upgrade guidance when required isolation or
  noninteractive options are unavailable, instead of misclassifying old command
  surfaces as a generic generation failure.

- README onboarding now gives one direct install/init/build/test/run path for the
  inherited hello application and a private worker configuration example for optional
  OS/CPU/GPU fan-out without implying that LitAI is a fleet scheduler.

- Locked generation now reproduces the current Component resolution plan from its one
  captured catalog, requires the exact persisted lock/audit pair to bind the effective
  pre-lock authority graph, and rechecks both plan and audit throughout generation
  preparation. Unselected catalog changes require a model-free re-lock without changing
  the selected derivation identity.

- Canonical repository graph identities no longer contain checkout paths, so exact
  graph-bound locks survive atomic staging, publication, and ordinary project moves.
  Initialization publishes lineage and inherited-template baseline evidence before it
  creates graph-bound starter locks.

- Project initialization now persists its already-resolved repository lineage before
  creating initial Component locks, and records its origin plus provisional framework
  baseline before that first graph-bound plan. Lock planning therefore observes the same
  root, inherited catalogs, and ownership selected by initialization; the finalized
  baseline is enriched only after derived qualification and documentation evidence is
  created.

- Repository catalog inheritance now excludes a Component's generated
  `component.lock.json` and target/host-specific resolution-audit files. Every descendant
  resolves and owns its own qualification evidence instead of importing an ancestor's
  machine and target decisions as authoring authority.

- Coding-CLI invocation unit coverage now isolates mocked coding processes from the
  real Linux Codex/AppArmor prerequisite check at the fixture boundary; dedicated
  selection tests continue to exercise the production host-policy guard.

- Standard filesystem rebuilds now preserve and emit the finalized receipt envelope,
  including the exact planned Component-lock set and lifecycle/cache identities, instead
  of discarding that authority when committing or writing a candidate.

- Bounded sample candidate replacement now treats the typed Swift generated-source
  rejection as attributable model output, matching the existing C++ repair path without
  admitting generic compiler or build failures.

- Standard native command projection now carries an observed compiler's exact
  environment into the isolated Python build driver. This preserves MSVC's captured
  `INCLUDE`, `LIB`, `LIBPATH`, and `PATH` authority instead of locating `cl.exe` and then
  silently dropping the SDK/linker closure at compile time. The exact environment is
  compressed into a bounded shell-free command token, avoiding Windows' per-argument
  limit without weakening command identity. Windows-only conformance fixtures now mock
  POSIX identity calls portably, report complete native/PowerShell failure diagnostics,
  and give the two-generation self-host replay an outer deadline longer than CodeGraph's
  own bounded command on slower hosted Windows filesystems.

- Remote Windows sample fan-out now uses the already-selected first compatible Python
  from `PATH` for source and guard hashing instead of depending on a PowerShell
  `Get-FileHash` command that is not present in every supported worker shell. SSH runner
  tests likewise invoke the active Python interpreter rather than assuming a
  `python3` command name.

- `litai init` now emits an explicit versioned prerequisite report for its active Python
  runtime, mandatory PATH-first or managed CodeGraph dependency, and conditional coding
  CLI. The root and inherited onboarding skills document CodeGraph's pinned, user-local
  installation path and keep optional Flavor toolchains out of eager bootstrap.

- Component/sample taxonomy now makes the portable hello demo's sole inheritance
  exception explicit in both framework and initialized-project authority; every other
  demo remains private while reusable building blocks remain under `components/`.
  Repository inheritance now excludes parent-local Component locks and resolution
  audits from inherited authority, allowing each descendant to resolve fresh evidence
  against its own complete repository graph while preserving specifications and
  acceptance assets.

- Repository catalog updates now recognize exact stale parent imports after a
  same-repository revision reparent, so the supported `reparent --apply` followed by
  `update --apply` sequence advances inherited bytes and provenance without treating
  them as local coordinate conflicts. Repository URL and project ID must both match;
  unrelated imports remain fail-closed. When a parent stops exporting an imported
  catalog item, update now removes it only if every surviving local file still equals
  the exact recorded provenance; one local divergence preserves the complete surviving
  item as local authority, and validation failure restores removed files, provenance,
  and lineage atomically.

- Contributor Node tooling now keeps Puppeteer's disposable browser cache beneath
  `OBJ_DIR` instead of reading or mutating host-global cache state.
- Installed-wheel qualification now verifies the complete Python, Make, pip-wheel, and
  host-OS starter Flavor authority selected by `litai init`.

- The inherited work-recording skill now routes strictly parent-owned improvements to a
  reviewable upstream contribution when Git access exists, retains them locally when it
  does not, and keeps downstream product recovery out of generic framework gates.

- Detailed roadmap documents now declare an enforced lifecycle state, a live owning
  queue item or program, and terminal evidence before completion or archival. The
  initialized work-recording skill and starter queue carry the same governance contract,
  preventing `docs/roadmap/` from becoming an ambiguous plan archive.

- Refreshed the exact self-hosting test-runner identity after expanding the sample and
  packaging authority closure, restoring version-authority agreement.

- Starter-project conformance now asserts the intentional default pip-wheel packaging
  Flavor alongside the selected language, OS, and build-system Flavors.

- Added non-publishing `litai package build` and `litai package verify` execution over
  accepted Standard lifecycle custody. The first executable providers create and
  independently verify deterministic pip wheels and portable Conan cache archives
  beneath `OBJ_DIR`; both retain the exact root Component specification, accepted
  artifact/SBOM tree, native package plan, and result evidence.

- Added opt-in `make samples-packages` conformance. It reuses each selected sample's
  accepted Standard lifecycle custody to construct and independently verify native pip
  wheels or Conan cache archives beneath `OBJ_DIR`, while ordinary sample execution
  remains package-construction free. User documentation, Mermaid flows, the checked-in
  PowerPoint, and the in-place native Google Slides deck now describe that live boundary.

- Added an exact deterministic wheel provider seam that validates materialized package
  inputs against `PackagePlan`, emits standards-valid wheel metadata and `RECORD`
  hashes, supports independent byte verification, and rejects changed or undeclared
  files before native package admission.

- Sample conformance no longer treats the historical acceptance-identity locks as a
  closed sample inventory, so new platform-pinned Components can define meaningful
  case names without weakening the original migration evidence.

- Parallel sample-runner conformance now constructs the required explicit host target
  contract, keeping platform pinning fail-closed without making synthetic fixtures
  depend on missing files.

- Added first-class multi-value packaging Flavors for pip wheels, Conan, apt,
  Homebrew, WinGet, and Chocolatey. Package Flavors carry exact host compatibility,
  use keyed composition so compatible formats can be selected together, and ship with
  initialized projects through a shared package-artifact skill. New Python projects
  default to `package.pip`; explicit compatible selections such as pip plus Conan lock
  independently, while apt/macOS and equivalent invalid target pairs fail before
  generation. `litai package plan` now emits a content-identified, read-only
  multi-provider declaration over the exact lock and specification/input closure.

- Preserve ordered subtractive Flavor selectors across local, POSIX SSH, and PowerShell
  sample fan-out by transporting each selector as one `--flavor=<selector>` argument.
  A leading `-` can no longer be misparsed as a new remote CLI option. Subtractive
  language selectors also narrow an explicitly authorized sample matrix without
  expanding its authority; removing every pinned language fails before generation.

- Framework coding-CLI generation and inverse model tasks now support OpenCode as an
  explicit or PATH-discovered provider, with exact model forwarding, provider-scoped
  authentication, a pure deny-by-default tool policy, detached generation workspaces,
  and honest non-sandbox isolation evidence.

- Multi-repository sample conformance now verifies every exact selected language
  variant instead of requiring the retired implicit Python/C++ default fanout.

- Full-stack sample conformance now resolves its pinned Rust and JavaScript roles from
  canonical Flavor coordinates instead of comparing them with retired short aliases.

- Standard sample and composed service-stack conformance now resolve lexical model
  identities through a bound model-selection adapter, restoring live sample execution
  after model selection became instance-configurable.

- Repository-DAG inheritance now carries declared workflow and routing catalogs in
  addition to Components, Flavors, and skills. Each workflow or routing document keeps
  exact per-file provenance and normal ancestor precedence, so inherited Components
  retain resolvable global generation authority during initialization and updates.

- Bind Windows native C++ compilation to the same platform-native MSVC family required
  by vanilla-worker bootstrap. Unless `CXX` explicitly overrides the choice, Windows no
  longer selects a MinGW `c++` launcher merely because that command name is present.
  The builder initializes the vendor x64 developer environment through `vcvars64.bat`,
  binds `INCLUDE`, `LIB`, `LIBPATH`, and `PATH` into the exact toolchain identity, and
  uses that environment for both real compilation and compiler-health canaries. This
  prevents content-addressed executables from silently acquiring unshipped
  `libgcc_s_seh-1.dll` and `libstdc++-6.dll` runtime dependencies. The hosted Windows
  gate now discovers and preflights Visual Studio's x64 `cl.exe` instead of installing
  MinGW and bypassing the platform default through `CXX=g++`. MSVC compilation also
  selects `/utf-8`, so UTF-8 generated source produces UTF-8 JSON at the host-execution
  boundary instead of locale-dependent Windows code-page bytes.
  Authorized Windows C++ execution also transports the exact JSON argument in its
  ASCII-escaped JSON form, avoiding the host active-code-page conversion imposed by a
  portable narrow `main(int, char**)` while preserving the authorized Unicode value.
  MSVC environment initialization redirects both vendor-setup streams away from the
  bounded wrapper pipes and gives all output readers one shared five-second EOF grace
  after the parent exits. Short-lived Visual Studio helpers may close inherited handles;
  persistent descendants still trigger process-tree termination and a closed failure.

- Reject unsupported `hdrs` attributes and workspace-root `includes = ["."]` exposure
  on generated rules_cc `cc_binary` and `cc_test` targets before source indexing, cache
  admission, dependency resolution, or Bazel analysis. Candidate replacement receives
  the exact stable diagnostic and message, while the Bazel skill and Flavor contract
  explicitly require package-relative private headers in `srcs` or shared headers owned
  by a `cc_library` dependency.

- Reject generated C++ Bazel graphs whose `genrule` output collides with the native
  `cc_binary` output before indexing, dependency resolution, or Bazel analysis. The
  Standard lifecycle consumes the native `//:run` output and generation receives the
  exact bounded replacement diagnostic. C++ parser lifetime findings are also eligible
  for the same bounded candidate replacement instead of terminating a recoverable
  generation attempt.

- Corrected vanilla Windows C++ bootstrap semantics for Bazel: MinGW no longer satisfies
  the compiler capability, MSVC is discovered through `PATH`, `vswhere`, or standard
  Visual Studio roots, and the official VCTools workload is selected when absent. Bazel
  temporary-tree teardown now uses the Win32 extended-length namespace so derived
  runfiles leaves beyond the classic path limit can be removed without weakening retry
  or lock failures. The MSVC `dumpbin /?` status `1100` is admitted only for its bounded
  version-binding probe; real image inspection remains strict-success-only.

- Added a bounded migration path for canonical projects created before repository-DAG
  evidence existed. An explicit `litai reparent` now plans their missing evidence as a
  typed legacy-root state, compare-and-swaps only while both lineage documents remain
  absent, and restores absence if validation fails. Partial, malformed, or concurrent
  evidence still fails closed, and `reparent none` materializes explicit root authority.

- Made ordinary `make clean` concurrency-safe for managed Python environments. It now
  removes transient object and artifact state while preserving
  `OBJ_DIR/python-envs/<session>`, so one agent cannot delete another agent's interpreter;
  `really-clean` remains the explicit exclusive reset.

- Closed Windows portability gaps found by the full gate: release transactions now
  restore exact original bytes and do not require POSIX `fchmod`; local repository
  parents recognize drive-letter paths; Bazel output admission no longer opens a fresh
  executable or zipapp merely to canonicalize its already-validated leaf; and portable
  tests compare native paths and line endings without weakening their contracts.
  Bazel output custody also retries only Windows sharing violations for a bounded
  6.35-second window when copying a freshly emitted regular file; persistent locks and
  every other copy failure still fail closed.

- Made installed-project qualification independent of private-network credentials by
  supplying the exact local checkout revision as its explicit repository parent. The
  product default still inherits the upstream encoded in the installed distribution.
  The upstream language and OS Flavor contracts now also admit the portable starter
  capability, so inherited catalogs can resolve the initial lock without falling back to
  bundled catalog definitions. Installed-project qualification scopes its lock check to
  the starter Component; upstream catalog entries remain available without being
  incorrectly re-qualified under the child's target.
  The starter now selects the same layered portable planning and implementation skills
  as live samples, with byte-identical bundled fallbacks for parentless/offline projects.
  Starter validation tests now assert unique required contracts and dependency order
  instead of hard-coding the complete evolving Component, Flavor, or skill inventory.
  The installed-distribution qualification gate passes with the complete inherited
  repository DAG and starter skill closure.
  Wheel qualification now supplies that same exact local checkout revision explicitly,
  so hosted runners do not require private-repository credentials after checkout
  authentication is deliberately removed, and scopes lock qualification to its starter
  Component rather than re-qualifying every inherited upstream sample.

- Added immutable lexical model scopes across `litai plan`, `build`, `test`,
  `generate`, and `rebuild`. A command-line `--model` is now an enclosing default;
  Component, selected-Flavor-role, and selected-skill declarations may override it for
  one bounded DAG task without leaking into siblings. Generation plans, cache keys,
  prompts, lifecycle evidence, and remote dispatch bind the exact resolution trace.
  Model-backed inverse translation applies the same immutable pipeline and
  Skill-invocation scopes independently to each language partition, rejects disagreeing
  selected skills before egress, and retains the binding in its journaled request.

- Added a project-owned `literate.release.json`, provider-neutral `release-project`
  skill, and `litai release plan|prepare|check|publish` phases to this framework and
  initialized projects. Policies declare canonical SemVer or PEP 440 authority,
  exact mirrors and gates, annotated/signing rules, Git remotes, and optional provider
  publication. Preparation validates before mutation and rolls back failed replacement;
  publication is explicitly authorized, non-forced, and safely retryable only when
  branch, tag, revision, policy, and provider state remain exact.

- Added executable command-worker routing for `litai build`, `litai test`, and
  `litai run`, including bounded application arguments/output, exact observed
  toolchain identities, and durable content-addressed artifact references whose
  authority is revalidated before execution. Artifact records retain the authoritative
  Component specification path separately from their short filesystem lookup key, so a
  later `run` reconstructs the same generation authority used by `build`. Installed-CLI
  CI qualification now exercises this deterministic command-worker lane without coding-
  agent credentials; the separate authenticated installed-project target retains live
  specification-to-source generation.

- Added complete repository-DAG inheritance. `litai init --from URL[#REVISION]`
  resolves every ancestor without executing repository code, composes Components,
  Flavors, and skills ancestor-first, and records exact revisions plus per-file
  provenance. `litai update --apply` now reconciles that lineage transactionally while
  preserving local conflicts and requiring `--adopt-added` for new authority. The new
  `litai reparent URL[#REVISION]|none` command plans and compare-and-swaps explicit
  parent changes; an explicit root makes update a typed no-op. Public schemas,
  installed-project smoke coverage, CLI help, onboarding skills, and architecture
  diagrams carry the same contract. Installed distributions use their embedded exact
  Git revision for default-parent resolution rather than drifting to the remote HEAD.

- Separate operational execution workers from generation targets: the new dispatch
  contracts use worker IDs and worker identities, while every worker binds one exact
  `target_profile` and declared OS/CPU/memory/GPU requirements. External dispatchers
  receive those requirements but retain all fleet matching and provisioning policy.
  The stable GPU qualifier carries explicit nullable minimum-count, minimum-memory,
  and capability fields, so omitted accelerator requirements remain unambiguous.
  Cross-platform fan-out now keeps those private worker definitions in
  `literate.workers.json`; `literate.test.json` references exact worker IDs and contains
  only sample, platform-Flavor, and source-transport selections.
  Lifecycle CLI parsing now keeps `--target` for generation profiles and uses
  `--worker` plus bounded `--worker-param NAME=VALUE` values for execution. The new
  standalone `litai test` follows the same current build-and-test lifecycle as `build`.
  Local and SSH lifecycle lanes now share typed exact-worker adapter seams with the
  command dispatcher, and durable artifact exports bind both worker identity and target
  profile before `run` may consume them. The built-in SSH lane now captures a
  deterministic identity-bound authored-source archive, invokes the installed worker
  receiver, drives the existing lifecycle in an isolated remote runtime, persists only
  accepted artifacts and exact execution commands in a worker-local CAS, and makes
  `run` consume that same digest. Bounded typed receiver failures retain their stable
  code and message without exposing arbitrary remote diagnostics. An installed derived
  project has passed live `build`, `test`, and known-output `run` over SSH on Ubuntu
  26.04; Ubuntu 24.04 correctly fails closed when host AppArmor policy prevents the
  coding CLI's workspace-write sandbox.
- Admit identity-bound local asset metadata into bounded Component generation context;
  raw asset bytes remain excluded while the existing forbidden-authority boundary stays
  fail closed.
- Carry one exact CAS-admitted authored-asset set through Standard rebuild planning and
  source execution, materialize those exact bytes into the registered source workspace,
  and retain stable trusted-runner error codes instead of obscuring custody failures as
  generic `runner-failed` results.
- Canonicalized built-in language, build-system, operating-system, and Swift
  realization Flavor coordinates as `lang-*`, `build-*`, `os-*`, and
  `toolchain-*`. New projects default to
  `flavor://literate-ai/lang-python`, `flavor://literate-ai/build-make`, and the
  current host OS. Unpinned samples now run only that bounded tuple; explicit
  full-coordinate wildcards opt into the language/build/OS Cartesian fleet
  matrix while preserving exact tuple-bound checkpoints and reusable caches.
- Added portable Swift generation with distinct Apple, Linux, and Windows toolchain
  realization Flavors, pre-generation host probes, and platform-specific mitigation.
  Added exactly-one alternative Flavor co-requisites plus short, axis-prefixed,
  axis-qualified, and canonical-coordinate selector forms. `litai init --flavor
  flavor://literate-ai/lang-swift` now selects the matching host realization and
  incompatible combinations fail during lock planning.
- Refreshed and republished the maintained 22-slide overview from current repository
  evidence: the live language/build/OS matrix, real end-to-end samples, arbitrary
  Component DAGs, the `litai` lifecycle surface, and checkpointed cross-platform fan-out.
  Removed elapsed-development-time and retired predecessor-product claims.
- Made document authoring connector-independent by default. Local editable artifacts,
  reproducible packages, and acceptance evidence require no proprietary MCP; Google and
  Microsoft documentation Flavors now own their optional tool discovery, authorized
  installation guidance, login diagnosis, service publication, and read-back checks.
- Resolve `FlavorDefinition.requires` as selected-only Component composition authority.
  Exact locked Flavor/requirement bindings now extend the Component DAG recursively,
  emit ordinary phase-specific edges, reject missing or ambiguous providers and ID
  collisions, and leave existing lock bytes unchanged when no selected Flavor declares
  a requirement.
- Require a healthy current project-level CodeGraph before `litai build`, `litai run`,
  or `litai rebuild` can invoke a coding agent, compiler, or executable. Canonical init
  no longer exposes a no-index provider, the lifecycle invariant remains independent of
  softer validation-stage preferences, and exact project-index evidence is carried in
  lifecycle results separately from generated-source evidence.
- Make `litai version check --project` understand installed Standard lifecycle bindings
  through their exact distribution and policy resolver instead of assuming every project
  declares an external driver implementation closure.
- Reuse one managed `PYTHON_ENV` across recursive release-gate Make invocations, so a
  single release session does not create and reinstall a fresh virtual environment for
  every gate while parallel sessions remain isolated beneath `_build/python-envs`.
- Direct Python bytecode produced by repository checks into `_build/pycache` so test
  compilation cannot pollute authored package trees, lifecycle identities, or CodeGraph.
- Preserve Windows `SystemDrive` across the scrubbed coding-CLI subprocess boundary, so
  tools cannot mistake the unresolved `%SystemDrive%` token for a relative output path.
- Keep Bazel's temporary projection name short enough for deeply nested rules-python
  runfiles to remain below the classic Windows path limit during fail-closed cleanup,
  and clear Bazel's read-only output bit without suppressing active locks or other
  removal failures. Genuine Windows directory-entry latency receives a separately
  bounded approximately 25-second retry budget before the cleanup still fails hard.
  Bazel analysis and build now use its platform-native runfiles policy instead of
  forcing full runfiles trees that the self-contained artifact contract cannot consume.
- Compact the build-result cache to `t/<full-target-digest>/{a,r,s}` while retaining the
  complete readable platform namespace in evidence, preventing redundant path labels
  from crossing classic Windows limits without truncating any identity.
- Shorten CodeGraph's disposable frozen-database filename while retaining the canonical
  `.codegraph/codegraph.db` path and exact snapshot digest.
- Made POSIX remote workers prefer a valid Python 3.11+ from noninteractive `PATH`, then
  probe bounded Homebrew and `/usr/local` locations before reporting it missing, so a
  system Python 3.9 cannot conceal an installed supported interpreter on macOS.
- Preserve package-manager launcher paths in host-bootstrap evidence, so symlinked Codex
  and CodeGraph commands remain callable after their discovered directories are added to
  a remote worker's `PATH`.
- Detect the Microsoft VC++ 14 runtime as a distinct vanilla-Windows prerequisite and
  install `vcredist140` only when Bazel's required runtime DLL set is incomplete.

- Preserve outer-finalized test-receipt envelopes when promoting rebuild evidence, so
  currentness checks bind the exact dynamically planned Component-lock set instead of
  requiring ephemeral locks to be checked into the authored project tree.

- Revised COMPOSE-001 to a provenance DAG architecture: every `litai catalog copy`
  records the full transitive ancestor chain back to the literate-ai root, and DAG
  cycle detection is a hard gate before any copy. `.literate/initialization-origin.json`
  (written by every `litai init`) is the root edge; `.literate/imports.json` accumulates
  additional edges. A new `litai catalog graph` command renders the DAG. Schema bumped
  to `literate-ai/catalog-imports@2`.

- Regenerated and republished the maintained Literate-AI overview in place as a verified
  22-slide native Google presentation. Presentation skills now acquire a fresh Google API
  bearer token with `gcloud auth print-access-token` for an explicitly authorized Docs or
  Slides URL and forbid persisting that credential.

- Made OpenSpec, Mermaid documentation, and CodeGraph synchronization gates stage the
  repository-pinned Node tool closure before execution, including clean release runs
  with an empty `_build` directory.

- Reject generated Bazel Rust targets before source indexing or cache admission when
  their declared inputs do not provably contain every source reachable through Rust
  `mod` and `#[path]` edges.

- Added `flavor://literate-ai/build-make` (`flavors/make/`, `make-build-system` skill) and
  established smart init defaults: `litai init` with no `--flavor` flags now produces
  the canonical Python, Make, and host-OS coordinates instead of an error. Explicit
  `--flavor` values override the relevant axis
  (`--flavor flavor://literate-ai/lang-javascript` drops the Python default;
  `--flavor flavor://literate-ai/build-bazel` drops the Make default). The host OS is
  always auto-detected from
  `sys.platform`. Unknown explicit flavors still fail with a known-selector diagnostic.
  Changed literate-ai's own `default_flavor_selectors` from
  `+flavor://literate-ai/build-bazel` to `+flavor://literate-ai/build-make`
  because the framework builds with Make; the qualification samples continue to
  explicitly select `+flavor://literate-ai/build-bazel` per Component as needed, and
  the conformance harness now carries that exact build selection instead of inheriting
  the project default.
- Recorded COMPOSE-001: selective Flavor/skill/Component extraction between literate-ai
  projects (`litai catalog copy`, `litai init --from`, `litai update --import`), with
  `.literate/imports.json` provenance and pre-copy source validation.

- Made source-to-spec project promotion honor explicit initialization shape: it now
  bootstraps the selected standard Flavors, host OS, and removable Bazel preference in
  empty-project mode instead of relying on hidden init defaults or retaining unrelated
  tutorial/document-service Components.

- Corrected Python repair checkpoints to record skips and expected failures, reconcile
  schema-v1 checkpoints that omitted skips, and bind schema-v2 successes to per-test-file
  identities. Adding or repairing one test now preserves unrelated provisional
  successes while still requiring a final from-zero release run.

- Made build-cache cleanup race tests hermetic under release-level `BUILD_DIR` and
  `OBJ_DIR` overrides, so they exercise their marked temporary cache roots without ever
  targeting the repository's shared object directory.

- Added compact, Git-ignored fail-fast checkpoints to repository and initialized-project
  test runners. Repaired runs resume exact provisional successes, then reset and require
  one authoritative from-zero pass before their results can qualify a release.

- Recorded INIT-002 and INIT-003: `litai init` must require explicit Flavor/project-type
  declarations before scaffolding (no silent defaults), and `litai init --convert` must
  perform a git-aware, scan-driven in-place adoption of existing repositories. Both
  requirements surfaced from a domain-specific evaluation (AI-to-OpenSCAD-to-STL) that
  exposed silent wrong defaults, template noise, and blocked recovery from partial inits.

- Made scoped `litai init help` name the complete `litai init --convert` invocation so
  the adoption workflow is discoverable from either supported help-verb position.

- Exposed deliberate runtime-to-project source-cache publication as a typed application
  service and filesystem project adapter shared by `litai cache publish` and product
  UIs, preserving exact entry identities, detached intelligence, idempotent replay, and
  the rule that committed generated source remains acceptance-untrusted.
- Preserved schema-v2 source-cache write authority in Standard rebuilds: only the
  configured `write_target_id` is opened writable, while every ordered read target
  remains read-only and an absent read-only target is not created.
- Aligned this repository with its own generic and Python layout skills: moved the
  in-tree PEP 517 backend under `tools/build_backend`, colocated the maintained slide
  deck with its authoring package, staged Node tooling beneath disposable `_build`, and
  added a gate against generated dependency/build state in current or historical Git.
- Isolated framework-managed Python environments beneath
  `_build/python-envs/<session>` so parallel agents cannot replace a shared root
  `.venv`; cache cleanup now runs from the system interpreter and can safely remove the
  complete object tree, including its managed environment.
- Consolidated disposable objects, executables, package staging, and matrix reports
  beneath the root `_build/` boundary; Git, CodeGraph, source custody, remote archives,
  and initialized projects exclude it, while `clean` removes it without touching the
  separate accepted-source cache in `generated/`.
- Packaged complete Python, JavaScript, Rust, and C++ language Flavor closures in every
  initialized project, including exact generation skills, specifications, toolchain
  constraints where applicable, and typed Standard command profiles, so promoted
  projects can regenerate in their reviewed implementation language.
- Derived Standard host-tool authority from the exact selected build, compiler,
  runtime, and phase-command closure, avoiding discovery or admission of an unused
  Python lifecycle driver for Bazel-built JavaScript applications.
- Completed an installed-wheel JavaScript source-to-specification round trip with
  semantic equivalence across all required surfaces, including Unicode code-point
  ordering, followed by two independent clean regenerations, Bazel builds, generated
  tests, runtime probes, and verifier-owned acceptance cases.
- Bound every promoted qualification case to a verifier-owned expected result and
  supplied that contract only to Standard independent acceptance, so a regenerated
  application can no longer fail for lack of an oracle or appoint its generated tests
  as the oracle. Standard parity now runs from the profile's safe native generated root
  rather than its outer custody workspace. Unreleased legacy profiles without expected
  outcomes now fail closed.
- Kept generated and repository test exports as inverse-translation evidence without
  misclassifying their classes, functions, or methods as required product surfaces, and
  strengthened the Python inverse skill to recover missing-required-field failures from
  direct mapping access.
- Preserved canonical language/platform command profiles and toolchain constraints when
  inverse Flavor proposals are promoted, rebound selected operational Flavors to the
  recovered graph before locking, and made composed live generation create its exact
  lock in a disposable project before model egress. Composed inverse qualification now
  selects the exact root source tree from per-Component custody instead of assuming a
  flattened output directory.
- Made user-directed planning an intrinsic Agent Skill and durable Markdown queue in
  the framework and every initialized project; `litai init` also preserves harmless Git
  bootstrap state and introduces ignored, user-owned cross-platform test-matrix routing.
- Added a cross-platform `detect-before-install` Agent Skill and worker bootstrap report
  for vanilla macOS, Linux, and Windows hosts; workers install only missing sample
  prerequisites through the native package manager.
- Made Windows Bazel discovery export Git-for-Windows `bash.exe` as `BAZEL_SH` and use
  batch mode so analysis and cleanup do not depend on a persistent locked server JVM.
- Made `samples/` a specification-and-metadata-only catalog, moved framework runners to
  `scripts/` and conformance support/fixtures to `tests/`, and added a gate that rejects
  implementation source or bytecode beneath the sample catalog.
- Added an explicit Windows-VM Codex mode that combines
  `--sandbox danger-full-access` with mandatory `--ask-for-approval never`; ordinary
  hosts retain the workspace-write sandbox and non-Windows full access fails closed.
- Added one validated `LITERATE_AI_SCHEMA_CATALOG_ROOT` contract for embedded catalogs
  and source/editable catalog discovery, so persisted source-to-specification bundles
  review against the same complete schema snapshot without wheel-layout assumptions.
- Made all 14 forward samples complete specification-generated applications with
  portable Python, C++, Rust, JavaScript, and full-stack Rust/JavaScript implementations,
  Linux/macOS/Windows Flavors, guarded native build and execution, and exact known-output
  checks across 25 host recipes.
- Added coding-CLI generation through Codex, Claude, or Cursor Agent with deterministic
  `CODING_CLI`/`PATH` selection and provider-correct model arguments.
- Added exact specification-to-source skill manifests, dependency identity pins,
  content-pinned workflow and routing compilation, and explicit host-execution
  authorization at the process boundary.
- Added the canonical project taxonomy, provider-neutral root `SKILL.md`, thin agent
  shims, public project/skill schemas, and `litai init`, `litai project validate`,
  and `litai plan` commands.
- Added `litai rebuild` as the full-SDLC front door, including current-test regeneration,
  source-cache selection/publication, CodeGraph sidecars, pre-build dependency admission,
  authorized native builds, independent acceptance, and compact Git test receipts.
- Required canonical CycloneDX 1.7 source and resolved SBOMs containing the complete
  Literate-AI-managed Component/repository graph plus exact package, toolchain, runtime,
  and binary dependency evidence; resolved graphs must preserve and bind their source
  graph before tests may run.
- Kept generated application source and build artifacts outside the repository; sample
  validation now rejects retained generated Component source.
- Added explicit-egress, CodeGraph-and-coding-agent source-to-specification translators
  for Python, C++, Rust, and JavaScript/TypeScript, complete versioned model-call
  journals, and an operational three-provider clean-regeneration/parity qualification
  service with fresh host build/run conformance for all four language toolchains;
  exact skills now fail before model egress and clean-run minima apply per target.

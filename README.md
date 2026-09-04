# Literate AI 0.9.0 (historical release marker)

Literate AI 0.9.0 was released on 2026-09-04 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `fedda1a74dcc0cab58f2fe14f4e47a97d5936b02`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.9.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.9.0 - 2026-09-04

- Bound source-generation progress independently from small JSON model tasks: stdout
  and JSON-task stderr remain capped at 1 MiB, while source-generation stderr now uses
  the existing 16 MiB generated-tree budget. Normal Codex progress can no longer kill
  an otherwise materialized generation, and every stream remains finite (#304).
- Provision and use the managed runtime for `make release-check-reset`, so a clean
  host can discard stale release and Python checkpoints without requiring project
  dependencies in the bootstrap interpreter.
- Discover a C++ compiler only inside a selected C++ sample execution instead of
  treating it as a universal sample-runner prerequisite. Non-C++ worker portfolios no
  longer require the `lang-cpp` toolchain to be present by accident (#303).
- Install Codex from the official per-platform package assets rather than standalone
  executable archives, preserving its code-mode host and other runtime companions.
  Managed-artifact SBOMs now declare and verify their required runtime closure while
  placing the primary executable in the package's declared `PATH` directory (#302).
- Add explicit, bounded `--harness-workspace-link DESTINATION=SOURCE` projections for
  source-linked sibling directories. One cross-platform host adapter applies the same
  portable topology to conversion baseline, both parity passes, and retained receipts;
  committed evidence omits host paths while runtime requests bind their opaque locator
  identity (#299).
- Retry a POSIX manifest lock when ordinary idle cleanup removes the just-opened inode
  before acquisition. Concurrent exact-snapshot writers again produce one update and
  one stale-snapshot result without weakening symlink or inode-replacement rejection
  (#300).
- Preserve complete stdout/stderr identities and bounded head, first-error-context,
  and physical-tail diagnostics for conversion baselines. Mixed-stream failures no
  longer let a generic stderr wrapper hide an actionable stdout compiler error; the
  finite diagnostic budget is recorded, accepts `--baseline-diagnostic-chars`, and
  can default from `LITAI_CONVERT_DIAGNOSTIC_CHARS` in CI. That validated budget now
  survives the final CLI error-envelope boundary while unrelated errors retain their
  512-character cap (#298).
- Exclude unprotected repo_man `_repo` runtime state from retained authored-source
  parity while continuing to hash every initially tracked path beneath that directory.
  Timestamped `repo.log` churn can no longer reject two successful equivalent builds,
  and unrelated generated source remains fail-closed (#297).
- Prefer a runnable platform-native `build.sh` or `build.bat` at each detected
  repo_man root, preserving repository-owned dependency and schema preparation before
  the lower-level driver runs. Direct baseline, generated Make delegation, and parity
  now consume the same recorded wrapper command and evidence, with the repo driver
  retained as the fallback (#294).
- Complete conversion parent, lineage, inherited-catalog, lifecycle-binding, and
  template preflight before quarantining operator-owned source. An unavailable
  framework parent now leaves an existing repository and its Git status untouched
  instead of stranding the tree under `_legacy/` (#293).
- Run detected repo_man baselines with one explicit release configuration and keep
  their disposable direct/parity workspaces beneath the shared project root, projecting
  `CI_PROJECT_DIR` to the exact observed copy so nested Docker builders can reach it
  (#288, #290).
- Add `litai init --convert --baseline-timeout-seconds` for explicitly bounded
  host-heavy conversion. The validated bound applies identically to direct baseline
  and wrapper parity, appears in their canonical evidence and per-phase results, and
  retains 1,800 seconds as the default (#292).
- Make `os-base` the single logical SBOM authority for the universal host
  toolchain: Python/venv, Git/Git LFS, and the supported coding-agent capability
  group. Node, compilers, and build tools belong to their language, package, and
  build-system Flavor mix-ins. OS Flavors own OS-only requirements plus
  tuple-specific native-package and managed-artifact realizations. Both `make
  install` and worker bootstrap consume the same composition, which removes
  identical duplicate dependencies with a diagnostic and rejects conflicting
  declarations (#285).
- Preserve each fan-out worker's configured SSH transport and send encoded PowerShell
  directly to a Windows OpenSSH worker's native shell. Windows release qualification
  no longer imposes the POSIX-only `bash -lic` boundary that hardware probing already
  avoided (#284).
- Make the cache provider the sole owner of the durable split-service SQLite schema,
  specify its complete public column and key contract, and verify those deterministic
  mechanics in Python before independently generated peers execute. The expired-lease
  verifier now injects the contract's active `running` state rather than an undeclared
  state that strict cache constraints correctly reject (#282).
- Bind debug spec-map instrumentation into the generated candidate before file
  collection, optional source indexing, and tree identity calculation. Standard
  lifecycle custody now sees the same sidecar and Python helper bytes that the
  generator records instead of rejecting debug-enabled source as mutated (#281).
- Bind the installed-E2E success sentinel to the exact exported `HEAD` surface that
  was installed and exercised. A dirty checkout may still prove its clean committed
  predecessor, but can no longer mark uncommitted CLI bytes as tested and then skip
  the exact proof after those bytes are committed; pre-fix sentinel schema v1 is
  rejected so prior false-positive evidence cannot survive the repair (#277).
- Keep mocked SSH timeout tests out of real Windows Job Object APIs, use native `.cmd`
  launchers for PATH-level coding-CLI doubles, and resolve the user profile only through
  the centralized host-path policy (`HOME` on POSIX; `USERPROFILE` or the Profile Known
  Folder on Windows). Exact Windows validation can now exercise the intended process,
  quota, cache-safety, and 0.8.x migration contracts instead of crashing or failing in
  platform-incompatible fixtures (#273, #274, #275).
- Isolate the nested-Make evidence regression from inherited `MAKEFLAGS` and
  `MAKEOVERRIDES`, so an explicit outer `OBJ_DIR` cannot silently replace the
  fixture-owned evidence custody while the test is proving step attachment (#276).
- Resolve the exact live coding CLI/model before both derivation planning and execution
  while keeping planning non-executing, so model-scoped Standard recipes bind the same
  source-cache keys during final candidate projection (#270).
- Treat the durable split-service envelope as two Standard lifecycle roots across
  metrics, five-node derivation planning, Component-lock accounting, receipt projection,
  and source-cache lifecycle validation instead of falling back to one legacy run
  (#271).
- Include the dynamically imported durable portfolio authority in the explicitly
  reviewed lifecycle-driver and sample test-runner source closures, so any change to
  its planning, execution, verification, or receipt behavior invalidates both pins
  (#272).
- Keep dependency import/BOM admission fail-closed while routing its exact generated
  build-topology mismatch through the bounded, fresh-workspace repair paths for both
  Standard nodes and retained host samples. Replacement requests bind their complete
  predecessor-attempt chain, while retained samples carry a fixed non-secret failure
  summary, so persistent mismatches exhaust without cache or publication (#269).
- Preserve canonical user configuration when the production sample lifecycle driver
  imports its test-hosted conformance implementation; test-only MCP/config isolation
  no longer masks the project-scoped live CLI/model selection during rebuild (#268).
- Detect Gitlink entries before legacy conversion and return a typed blocked plan even
  when the nested submodule is initialized; conversion cannot yet transactionally
  remap `.gitmodules`, Gitlinks, and relative Git-directory pointers (#266).
- Prefer the current branch's configured upstream over remote HEAD when deriving a
  conversion repository policy, including absorbed submodule worktrees whose `.git`
  boundary is a pointer file (#267).
- Discover retained test authority from repository-owned tasks and aggregate scripts,
  preserve their working directories, require statically provable pytest collection,
  and reject successful zero-test baselines instead of inventing root pytest commands
  (#262).
- Define retained parity over one captured authored-source scope, excluding and
  accounting for caches, dependencies, virtual environments, and build outputs without
  repeatedly copying or hashing them (#263).
- Add a framework-owned retained-harness receipt runner that executes only the admitted
  commands in a disposable source copy, binds exact project/lock/worker/runner/command/
  result evidence, folds current retained-source bytes into project authority, and emits
  an outer-finalized candidate consumable by the existing receipt promotion boundary;
  source-mutating, skipped, failed, expected-failure, uncounted, and empty suites remain
  ineligible (#264).
- Lexically project JavaScript before dependency admission so import-shaped text in
  comments, regular expressions, strings, and templates cannot become artificial
  source-BOM dependencies, while executable static, dynamic, and CommonJS literal
  imports remain fail-closed (#265).
- Move the DMN and SCXML samples' normative portable call/result contracts out of
  descriptive `component.md` prose and into exact local public-interface authority,
  ensuring each contract reaches bounded source generation exactly once (#261).
- Admit exact locked authored-asset bytes into the same candidate CAS used by Standard
  planning and source materialization, including the production sample path; missing or
  corrupt custody now produces a stable source-generation diagnostic instead of an
  untyped runner failure, and the coding-provider envelope explicitly forbids recreating
  post-generation immutable-overlay paths (#260).
- Accept Git LFS 3.8's `{"files": null}` as an empty current-tree manifest while
  retaining fail-closed rejection of other malformed manifest shapes (#257).
- Preserve Codex's captured workspace-write bootstrap diagnosis when a zero-exit
  preflight writes no response, instead of misreporting the runner isolation failure
  as a missing task response or unavailable model (#258).
- Keep POSIX remote Python discovery inside one current-shell command group so an
  earlier fetch, revision, or workspace failure cannot resume after an internal
  semicolon and mask the cause by executing with unset path variables (#256).
- Bind provisional remote sample checkpoints to each worker's exact source identity
  and commit plus the admitted coding CLI/model selection, and invalidate the prior
  under-specified checkpoint format (#254).
- Preserve an explicit remote fan-out coding-CLI/model override through the nested
  worker boundary so authenticated Codex and Cursor Agent (`--yolo`) qualifications
  do not lose CLI-flag provenance and fail the opencode-default guard (#255).
- Bind GitHub CI-status guidance to the checkout's exact immutable commit instead of
  the literal token `HEAD`, which the GitHub CLI can interpret as no matching runs
  (#250).
- Correct the durable split-service verifier at three integration boundaries: accept
  an ordinary strict product JSON object without imposing metadata-only canonical key
  order, encode finite floating-point metrics as tagged decimals before evidence
  identity, and aggregate both nested Standard root reports (#252).
- Run the changed-skill evaluator through the managed Literate AI runtime that
  `skills-check` prepares, rather than an unmanaged bootstrap Python that may not have
  the framework installed (#253).
- Add a reusable durable split-service portfolio: independently generated frontend,
  read-only API, single-writer collector, and SQLite snapshot cache Components compose
  through public interfaces while a verifier-owned flow proves restart persistence,
  atomic publication, bounded retry, and lease recovery (#216).
- Make persistent-service acceptance own and monitor the packaged Python,
  JavaScript, or native server process directly instead of a runtime wrapper whose
  liveness could outlast a missing server child (#247).
- Let release checking for legacy policies admit the sole documentation-authority
  marker document identified by project validation, resolving the impossible
  prepare/review scope sequence without admitting unrelated files (#249).
- Exclude uv's `uv_build.json` installer record from the exact framework payload
  identity, restoring pip/uv and macOS/Linux reconstruction parity without excluding
  framework-owned distribution metadata (#248).
- Apply parent-selector, inherited-catalog, and framework-template migrations as one
  rollback-safe project update with one final validation. Untouched retired authority
  beneath declared catalog roots can now be removed, while conflicts and explicitly
  protected local parent paths remain untouched (#244).
- Replay Component-scoped Flavor selectors only against their named lock nodes, so
  planner-supported heterogeneous locks retain the same authority through generation
  preparation (#243).
- Keep public-interface edges in dependency ordering and generation context without
  injecting their provider executables into Standard build/runtime environments; only
  artifact-export edges receive that binding (#245).
- Preserve strict rejection of non-UTF-8 and workspace-root coding-CLI output, but give
  those empirically nondeterministic generation shapes the existing bounded,
  fresh-workspace retry before failing closed (#246).
- Keep Windows host-install tests native to the platform and make shared Job Object
  termination single-owner across concurrent output readers, preventing double-close
  races from surfacing as unhandled builder-thread failures (#241, #242).
- Make the browser-readiness lifecycle fixture deterministic under hosted-runner load:
  it now simulates delayed readiness directly and proves bounded-timeout cleanup
  without relying on child-process scheduling latency (#237).
- Isolate the host installer's private Python closure from ambient `PYTHON*` startup
  policy. In particular, repository-scoped `PYTHONPYCACHEPREFIX` no longer makes
  `make install` record bytecode outside the installed runtime.
- Scope accepted-source reuse to effective per-target authority while retaining the
  complete reviewed project identity in coding-transaction provenance. Receipt-policy,
  narrative-document, and unrelated sibling-catalog changes no longer force another
  model invocation; Component/lock/plan/Flavor/skill/context/prompt/tool/model and
  repository-lineage drift still fail closed.

- Prefer the verified native worker-bootstrap launcher over legacy or ambient
  `litai` commands for both SSH execution and acknowledgement. An executable stale
  user launcher can no longer intercept an exact bootstrapped worker lifecycle.
- Preserve accepted-source-only build authority in durable artifact exports so a
  freshly accepted local or remote multi-entrypoint build remains runnable; `run`
  still re-derives the current provider binding and rejects genuine authority drift.
- Add exact operator-authored known-failure annotations to the existing repair
  checkpoint ledger, with strict Python and external-runner accounting, explicit
  inspect/revalidate/clear operations, fail-closed invalidation, and release runs that
  always execute the complete suite (#230).
- Keep the shipped v2 compatibility matrix self-consistent with its exact
  framework-writer allowlist, align packaged-service argv test doubles with the
  current three-argument port contract, refresh the frozen component-schema guard
  after the intentional deployment-unit addition, and re-pin the reviewed lifecycle
  driver after the latest multi-entrypoint changes (#231, #232, #233). Keep simulated
  POSIX user-path policy portable on Windows and make installer/uninstaller path
  fixtures compare semantic paths rather than host-specific serialization (#235).
- Preserve packaging-only provider provenance in a separately typed intent, build
  manifest, artifact-graph, and package-closure channel: package inputs no longer
  require a process binding, but they can no longer disappear from package and
  acceptance authority either (#110).
- Use Cursor Agent's documented `--yolo` unattended flag. Live qualification still
  fails closed when the Cursor Agent CLI has no authenticated session, even when the
  desktop application is signed in.
- Require Git LFS alongside Git on every supported worker OS and add a reusable
  shallow exact-revision checkout skill that hydrates recursive submodules and LFS
  content before build or test work.
- Make host reinstall and removal truthful under real pre-release upgrades: force pip
  to replace same-version console scripts, and remove only manifest-named launcher and
  runtime paths while preserving sibling tools, caches, configuration, and state.
- Route worker capability probes through the shared transport policy, including each
  worker's configured SSH-compatible executable and native Windows command boundary.
- Preserve the entrypoint-aware packaged-service call contract in its focused test
  doubles so service assertions cannot be bypassed by an obsolete mock signature.
- Canonically order multiple source-admission verifier results and reject duplicate
  verifier commands before execution.
- Encode finite lifecycle timing telemetry as tagged canonical decimals in remote
  evidence, and fail closed on non-finite values instead of rejecting an otherwise
  successful worker lifecycle during evidence construction. Canonically order
  directory-artifact members by their portable logical paths so multi-entrypoint
  evidence verifies identically on the worker and coordinator.
- Drive `make install` from strict OS/architecture/accelerator CycloneDX prerequisite
  SBOMs with native package provenance, version probes, interactive or explicit
  noninteractive consent, declarative APT/Homebrew/WinGet installation, host-native
  default prefixes, and actionable unsupported-tuple porting guidance.
- Centralize Linux, macOS, and Windows operator paths behind an injectable `HostPaths`
  policy, move durable private worker/test/MCP assets into per-user configuration,
  separate mutable observations/events into state, and provide a dry-run-first,
  conflict-preserving `litai config migrate` path from 0.8.x.
- Make the skill directory hierarchy executable through one `AgentSkillCatalog` used
  by validation, initialization, authority graphs, authoring, and changed-skill
  evaluation; nested sentinels now own disjoint resources and inherit explicit parent
  context instead of being copied recursively.
- Add reviewed sample-runner re-pinning; typed work-record, operator-MCP,
  channel-admission, NVIDIA compatibility, and document-verification CLI/MCP
  connectors; and MCP resource list/read for inherited skills, project documentation,
  owned resources, and packaged static assets.

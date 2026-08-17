# Literate AI 0.4.0 (historical release marker)

Literate AI 0.4.0 was released on 2026-08-17 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `3a0fd17e6e5ab7d08f6ad448a5cbd6bd3cb7be0d`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.4.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.4.0 - 2026-08-17

- Fixed accepted-source continuation across fresh inherited sessions. Filesystem
  membership now uses a versioned semantic lookup identity over recipe, execution plan,
  portable coding-tool, provider/model, and sanitized Component-context bindings, while
  orchestration and exact coding-request identities remain in immutable
  admission/provenance custody. New request-keyed membership misses fail closed and
  require readmission; no cache miss can invoke a generator during
  `--from-accepted-source`. (#104)

- Refreshed the manager/engineering overview deck's factual-claim ledger
  (`docs/presentations/literate-ai-manager-overview/source-notes.md`) for 0.4.0's new
  capability (`build-cmake` Flavor, `litai release check --target`, `ci_targets`, two
  new default skills). The PPTX itself was not re-rendered and the published Google
  Slides copy was not re-imported, since the required `presentations` artifact-tool
  plugin isn't installed in this environment; the README now links the published copy
  and notes it's pending that refresh.

- Fixed remote accepted-source continuation restoring content-addressed memberships
  beneath the arbitrarily long SSH request checkout, which could put a valid Windows
  key path exactly at the legacy 260-character boundary. Worker restore now uses one
  short attempt-scoped custody root for build, object, runtime, receipt, temporary, and
  package paths; source capture excludes the explicitly bound coordinator build and
  object roots so accepted cache entries travel only in their separately verified
  archive; passes custody identity directly through worker CLI and Standard lifecycle
  requests without ambient environment rediscovery; preserves all key and membership
  identities; and removes it after every outcome without enabling coding-CLI fallback.
  (#101)

- Added an explicit, durable `--target local|github|gitlab` choice for `litai
  release check`'s execution target. `--target local` dispatches the declared
  release gate through the configured private worker fleet
  (`literate.workers.json`) over SSH and fails closed when the fleet is
  unconfigured, misconfigured, or unreachable; `--target github` polls GitHub
  Actions for the checked revision and fails the gate on a non-success
  conclusion; `--target gitlab` is recognized but rejected with a typed
  `release.target_unsupported` error since this project has no GitLab
  integration to dispatch through yet. Without an explicit `--target`, a
  project's declared `ci_targets` preference is used when present; otherwise
  the gate keeps running on the invoking machine exactly as before. The
  prepared-release evidence now always records which target produced the
  passing gate. (#58)

- Added `python-service-application` and `react-dashboard-application` to the default
  `specification-to-source` skill catalog, and closed out their remaining evidence gap:
  two new sample Components, `samples/python-service-example` and
  `samples/react-dashboard-example`, pin each skill and were generated live end to end
  via `litai lock`/`litai generate`, with the generated source's own test suites passing
  (20/20 and 11/11 respectively). Also fixed two non-blocking polish items found during
  review: a dangling provider-neutrality reference and a hardcoded "fixed daily
  schedule" phrase in `python-service-application/SKILL.md`, and a missing
  relative-import-with-extension rule in `react-dashboard-application/SKILL.md`. (#37)

- Added a `build-cmake` Flavor for the `build.system` axis (`flavors/cmake/`, a
  sibling `cmake-build-system` specification-to-source skill, and a
  `src/literate_ai/adapters/builders/cmake.py` builder), mirroring the existing
  `build-bazel`/`build-make` Flavors. Live end-to-end validation against
  `samples/hello-component` found and fixed a real generation bug: coding CLIs
  sometimes invent `${CMAKE_COMMAND} -E chmod`, a subcommand that doesn't exist in
  any CMake release; the skill now explicitly warns against inventing nonexistent
  `cmake -E` subcommands. (#56)

- Fixed generated source SBOMs drifting from the framework's own canonical managed
  projection: coding agents routinely reinterpreted the managed `literate-ai:*`
  classification, dropped the root kind, or added fields even when introducing no
  real third-party dependency. When the model declares no component outside the
  managed and authority reference set, the generated CycloneDX source SBOM is now
  replaced with the framework's own canonical bytes; a genuine third-party
  dependency the framework didn't project is left untouched for normal validation.

- Fixed cache-only Standard continuation crashing when acceptance or generation
  preparation read `LockedComponentRevision.definition`. Exact typed Component
  definitions now survive the mandatory v2 lock decode boundary as non-wire,
  non-identity validation context; missing, substituted, and selector-mismatched
  definitions still fail closed. (#98)

- Fixed inherited-session source admission rejecting the single stage-bound provider
  evidence record and provider trees that verifier-owned generation correctly augments
  with exact locked assets. (#97)

- Added `ci_targets` to `literate.project.json` (`ProjectDefinition.ci_targets`, new
  `CiTargetPreference`/`CiTarget`/`CiExecutionMode` contracts): an ordered CI target
  preference list (`local`, `github`, `gitlab`), array order is fallback/priority
  order, each entry's `mode` (`serial`/`parallel`) says whether it runs concurrently
  with the entries before it. `github` and `gitlab` are mutually exclusive within one
  project (only one can be the repository's native forge-triggered CI); `local` (a
  private worker fleet, which can cover more OS/CPU/GPU combinations than either
  hosted remote) may combine with either. Defaults to empty (no explicit preference)
  when omitted.

- Fixed a floating-point overshoot in remote-worker timeout budgeting: computing
  `deadline - time.monotonic()` on a `deadline` struck from a large monotonic-clock
  base could round a hair past the original whole-second budget, in principle handing
  a subprocess a timeout larger than what was configured. `scripts/fanout_samples.py`'s
  `_remaining_timeout` and the new `ssh_execution._Deadline.remaining()` now clamp to
  the original `timeout_seconds` and return `int`, since nothing here schedules
  subprocess timeouts to sub-second precision anyway. Caught by an existing test
  flaking under real clock skew (`test_remote_subprocesses_receive_the_remaining_
  target_deadline`); added deterministic regression tests for both call sites that
  mock `time.monotonic` to reproduce the exact overshoot instead of depending on
  real timing.

- Added `scripts/select_smoke_tests.py`, selecting a bounded-duration smoke subset
  from pytest-split's recorded `.test_durations` for the `develop-in-dev-workflow`
  local-verify step. Selection is breadth-first across test modules (each module's
  cheapest remaining test first, so coverage spreads across the whole tree before any
  one module gets a second test) rather than plain fastest-first, which would bias
  toward a handful of trivial tests from a few files. The budget defaults to
  `[tool.smoke_tests] budget_seconds` in `pyproject.toml` (600s) and is overridable
  with `--budget-seconds`; a budget of 0 or less is refused with an explicit error
  rather than silently selecting nothing.

- Added three agent-facing skills documenting how an agent should develop against this
  project under each of three nested postures -- `develop-in-dev-workflow` (fast local
  iteration, remote CI as async confirmation), `develop-in-staging-workflow` (batch
  integration gated by one full remote CI pass per batch), and
  `develop-in-production-workflow` (every change individually gated, formal evidence
  retained) -- with production as the outer loop, staging nested inside it, and dev the
  innermost. Which posture applies to a project is now selectable via a new optional
  `agent_development_workflow` field on `literate.project.json`
  (`ProjectDefinition.agent_development_workflow`, one of `dev`/`staging`/`production`),
  defaulting to `dev` when omitted.

- Fixed authenticated inherited-session source generation failing after a successful
  handoff because provider evidence was counted as a second model-stage output. The
  evidence is now closed over by the single stage-output record, preserving custody and
  route/output cardinality. (#94)

- Fixed the independent-acceptance-oracle requirement applying unconditionally to
  every Component, forcing a persistent-service Component (no single-shot CLI
  invocation at all) to fabricate a fake CLI entrypoint just to satisfy
  `component_acceptance.oracle_missing`. `accept_project_independently` now reads the
  packaged entrypoint's kind and, when it isn't `portable-application`, returns a
  typed, reproducible exemption identity instead of requiring a hand-authored
  `verification/acceptance/<name>.json` oracle or invoking a packaged command at all.
  (#32)

- Fixed `coding_cli.generated_metadata_invalid` requiring a manual retry: it's a
  non-deterministic coding-CLI output-quality failure (malformed JSON in a
  framework-owned metadata file), not a specification or environment problem, and a
  plain retry resolves it most of the time. Generation now retries up to 2 additional
  times specifically on this error code before surfacing it; any other error code, or
  exhausting the bound, still fails closed immediately with the original typed error.
  (#35)

- Fixed a Component's lock being invalidated by unrelated changes elsewhere in the
  project. The lock's `catalog_identity` bound the identity of the entire project's
  Component/Flavor/skill catalog rather than just the locked Component's own
  transitive closure (already correctly bound separately), so any sibling entity
  change anywhere invalidated every other Component's lock. Now binds only the
  project's repository-lineage identity, the one piece of graph-wide state a lock
  legitimately must still fail closed on. (#36)

- Root-caused #39 (multi-source-root JS Components failing SBOM composition
  validation): not a bug. The JavaScript Flavor forbids npm dependencies entirely by
  design, independent of source-root count; the "multi-toolchain frontend role"
  documentation was ambiguous in a way that made the opposite premise look reasonable,
  and has been clarified. The real gap this surfaces — no capability exists for real
  npm dependencies in JS Components at all — is tracked separately as #87. (#39)

- Added a mechanical (non-blocking, advisory) coverage-gap scan for local `litai
  build`: flags entrypoints/capabilities declared in a Component's authoring that
  have no live reference in the generated source, or that resolve only to a stub
  marker (`NotImplementedError`, `TODO`/`FIXME`, a route/tool table entry bound to
  `None`). A green generated test suite is not evidence the implementation is
  complete, since the same coding-CLI call writes both — this is a first, narrow,
  honest signal toward closing that gap, not a full spec-coverage checker; worker-
  dispatched builds don't get a scan yet (source isn't retained locally by design),
  and false-positive-rate characterization before any fail-closed promotion is
  tracked as follow-up (COVERAGE-GAP-001). (#64)

- SSH workers may bind an exact lifecycle executable in private worker configuration.
  The path is covered by worker identity and overrides ambient launcher discovery, so a
  stale `litai` installation cannot intercept accepted-source cache restoration. (#89)

- Added the contract/adapter layer for a provider-neutral authenticated inherited
  IDE-session handoff with no nested coding CLI. Exact provider/session/request/plan/
  context/workspace and source/evidence digests, single-owner custody, typed
  timeout/cancellation, replay rejection, and verifier-owned source admission keep
  credentials and private prompt contents out of durable evidence while preventing
  handoff success from granting later authority. Standard generation now selects this
  provider through `LITAI_CODING_PROVIDER=inherited-session`; a create-once private
  directory protocol connects the CLI to the current IDE coordinator, retains the
  authenticated handoff in provenance, and rejects disconnect, replacement, stale
  state, or replay before source admission. (#43)

- Added canonical concurrent target matrices. `litai matrix` runs versioned
  declarations of Component/target/ordered-Flavor cells with cell-scoped locks and
  audits, bounded in-place lifecycle execution, same-cell predecessor-bound evidence,
  per-cell receipts, and fail-closed aggregate receipts. Standard and custom lifecycle
  results expose the same required evidence, disposable runtimes remain outside the
  project, failed aggregates return nonzero, and each cell has an explicit timeout.
  Scope: same-host cell concurrency (each cell is a local `litai rebuild` subprocess),
  not remote worker dispatch — use `--worker`/`literate.workers.json` for execution on
  different hardware. (#42)

- Fixed fresh `litai matrix` cells failing `component_lock.missing`: each cell now
  atomically materializes or validates its exact target/ordered-Flavor scoped lock
  before rebuild, and rejects a rebuild that reports any other lock identity. (#42)

- Fixed target-matrix rebuilds capturing the committed host Component lock after
  selecting a target-scoped lock. Closure capture and lifecycle revalidation now retain
  the exact scoped store path and canonical bytes, reject mid-run replacement
  explicitly, and keep downstream evidence bound to the same lock identity. Installed
  wheel qualification also compares independent clean builds from the same revision and
  origin, not only repeated installs of one artifact. (#42)

- Fixed `storage/events.py`'s `FileLock` duplicating the same write-before-lock
  bug just fixed in `exclusive_cache_lock`, plus its own divergence: POSIX
  `flock(LOCK_EX)` blocked indefinitely with no deadline, while Windows raised
  after ~10s, and neither path had `exclusive_cache_lock`'s symlink/reparse or
  device/inode TOCTOU checks. `FileLock` now shares `exclusive_cache_lock`
  directly instead of maintaining a second lock backend, so every caller (event
  log and reference/dependency index metadata stores) gets the same bounded
  30s non-blocking acquire, lock-byte-after-acquire ordering, and safety checks
  on every platform. (#75)

- Added `python-service-application` and `react-dashboard-application` to the default
  `specification-to-source` skill catalog: HTTP API/embedded-MCP-server/scheduled-
  worker conventions, and interactive-dashboard conventions (local UI-only selection
  state, stable identity-derived keys, multi-key table sort/rank, display-mode
  switching without discarding state), covering multi-surface service Components the
  existing single-shot-CLI-oriented catalog didn't. Both pass NVIDIA SkillEvaluator.
  (#37)

- Fixed `exclusive_cache_lock` writing its lock file's initial placeholder byte
  before acquiring the OS-level lock, not after. Windows enforces mandatory
  byte-range locking: once one thread/process holds the lock, any other handle's
  write that touches the locked byte raises `PermissionError` instead of blocking —
  so a second thread that observed the file as still-empty in the pre-lock window
  could collide with a lock another thread already held, surfacing as a spurious
  `[Errno 13] Permission denied` under heavy concurrent-thread contention on
  Windows. The placeholder write now happens only after the lock is held, with the
  emptiness re-checked under the lock.

- Fixed `make release` leaking `PYTHONPATH=src` into the release gate subprocess and
  everything it spawns, so a revision that passed a direct `make release-check` could
  fail `make release` with `standard_binding.distribution_ambiguous` — the gate
  resolved `literate_ai` from both the source tree and the installed distribution at
  once. Interpreter-resolution variables (`PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`)
  no longer cross into the gate subprocess.

- Made the private worker fleet's SSH transport binary per-worker, data-driven
  configuration instead of a hardcoded `"ssh"`/`"scp"` literal. A private fleet may
  route some workers through a drop-in SSH-flag-compatible wrapper (e.g. a
  VPN/Tailscale-aware launcher) instead of plain OpenSSH, while other workers keep
  using `ssh` directly. Add an optional `transport` field to `ExecutionWorker`
  (default `"ssh"`, backward compatible); only SSH-kind workers may set it to
  something else. File staging still always uses `scp`. Verified live against a real
  private Windows worker over both the default and a custom transport. (#65)

- Fixed `source_admission.tests_failed` naming only the failing verifier command's
  argv[0] (e.g. `"python3"`), with no failing Component revision, exit status, or
  output — impossible to distinguish a generated-test defect from an invocation/cwd
  defect. The error now names the Component revision, exit status, and a bounded
  stderr/stdout excerpt. (#65)

- Fixed a remote `--worker` dispatch intended to continue only from
  verifier-admitted source having no way to express that intent, so it could fall
  through to worker coding-CLI generation instead of failing closed. `build`/`test`
  gained a `--from-accepted-source` flag (matching `rebuild`'s existing one);
  `ExecutionDispatchRequest` carries the intent to the worker over SSH dispatch. A
  worker with no staged membership now fails closed with a typed
  accepted-source-unavailable error instead of silently regenerating. (#65)

- Fixed remote `--from-accepted-source` dispatch never being able to get an actual
  worker-side cache hit, and Windows remote restore referencing cache-key membership
  files that were never staged into the archive (a missing-path `cli.invalid_input`).
  The project's writable accepted-source-cache tree is now staged as a fifth
  transferred archive alongside the project source, bound to its own transport
  identity on `ExecutionSourceMaterialization`, and safely restored archive-relatively
  into the worker's own cache root (rejecting traversal, symlinks, and
  case-collisions) before the worker's rebuild runs — independent of separators or
  launcher cwd on both Linux and Windows. (#65)

- Fixed two accepted source-cache entries at the same key crashing `litai test`/`litai
  build` with an uncaught `SourceCacheError`, unlike almost every other lifecycle
  failure. `SourceCacheError` was already typed with `.code`/`.message`, but nothing
  in the `litai test`/`build`/`generate` call chain caught it, so it escaped as a raw
  traceback — the same class of gap as `StandardCommandProjectionError` (#33). The
  CLI's top-level backstop now catches it too. `litai build`/`litai test` also gained
  `--source-cache-entry` to disambiguate, matching `litai rebuild`'s existing flag
  (both are thin wrappers over the same rebuild call). (#54)

- Wired `litai profile`'s structured NDJSON operation/subprocess sink into the Windows
  CI job's test step through a new `scripts/profile_pytest.py`, since `litai profile`
  itself only wraps `rebuild`/`build`/`test`/`generate` and CI invokes `pytest`
  directly — the sink was never opened for a CI run. The trace and its
  `literate-ai/profile-report@1` hotspot summary now upload as a
  `windows-conformance-profile` artifact on every run. Note: pytest-xdist workers run
  in separate processes, so only what happens in the coordinating process is captured
  under this repo's default `-n auto` distribution. (#57)

- Widened the CI `conformance` job's Python matrix from `3.11`/`3.12` to `3.11`/`3.14` —
  the floor version plus the current latest minor exercises real stdlib-behavior
  diversity (3.11/3.12 are close enough that they largely didn't; PR #40's
  `test_cli_help` hermeticity fix for Python 3.14's default `argparse` colorizing was
  only found by running the suite locally under 3.14, not by this matrix). Kept minor-
  only version resolution, consistent with this workflow's existing Node pin.
  `windows-conformance` and `sample-composition` stay pinned to 3.12 for now. (#61)

- Made coding-CLI selection order configurable. `select_coding_cli` iterated a fixed
  `("codex", "claude", "cursor-agent", "opencode")` tuple and returned the first name
  found on `PATH`, so a host with more than one coding CLI installed always picked the
  earlier-listed one regardless of which was actually preferred; `CODING_CLI=<name>`
  could pin one exactly but only per invocation. Set
  `LITERATE_AI_CODING_CLI_PRIORITY` to a comma-separated permutation of the four names
  to change the search order project-wide; an incomplete or malformed list fails
  closed. (#31)

- Fixed every `StandardCommandProjectionError` raise site (multiple entrypoints,
  incomplete toolchain authority, and others) escaping as an uncaught Python traceback
  instead of the typed `{"command":..., "error": {...}, "ok": false}` envelope every
  other lifecycle failure produces: nothing anywhere caught this exception type.
  `StandardCommandProjectionError` now carries `.message` like every other typed
  adapter error, and the CLI's top-level backstop converts it. A Component composed of
  multiple cooperating surfaces (HTTP API, MCP server, worker, ...) should still be
  expressed as separate single-entrypoint Components joined by the existing
  `provides`/`requires` capability-edge mechanism, not a single multi-entrypoint
  Component; first-class multi-entrypoint support remains a separate, larger decision.
  (#33)

- Made the per-document coding-CLI generation timeout configurable. It was hardcoded to
  900 seconds with nothing reaching `CodingCliSourceGenerator`'s constructor from
  `litai build`/`litai generate`/`litai rebuild`, so a Component whose spec legitimately
  needed more than 15 minutes of generation time could not be built locally at all. Set
  `LITERATE_AI_CODING_CLI_TIMEOUT_SECONDS` to override; the default remains 900. An
  invalid value fails closed through a typed
  `coding_cli.generation_timeout_configuration_invalid` error instead of an uncaught
  exception. (#34)

- Fixed Flavor resolution rejecting more than one default selector on a singleton-
  cardinality axis before any `--flavor` override was consulted, so no override could
  ever disambiguate it. `apply_flavor_selectors` now validates mutual exclusion once,
  after every selector has been applied, instead of unconditionally on the initial
  preference set. An unresolved conflict still fails closed. (#46)

- Fixed `litai cache publish` crashing with an uncaught `AttributeError` for every real
  accepted entry: `SourceCachePublicationService.publish` read
  `entry.source_tree_identity` directly, but only `StandardSourceAdmissionCacheEntry`
  exposed that as a top-level accessor — `AcceptedSourceCacheEntry` only carried it
  nested at `entry.derivation.source_tree_identity`. `AcceptedSourceCacheEntry` now
  exposes the same uniform `source_tree_identity` accessor its sibling type already
  had. (#63)

- Fixed a deadlock between `litai release check`'s declared-scope enforcement and the
  mandatory documentation-authority-review precondition: bumping the release version
  legitimately changes `literate.project.json` (in scope), which staled the separate
  documentation-authority marker (out of scope), so the release gate's own `litai test`/
  `litai build` invocation could never pass against a validly-scoped prepared commit.
  `ReleasePolicy` now accepts an optional `documentation_authority` path list;
  `litai release prepare` refreshes a declared, stale marker in the same pass as the
  version bump, and `litai release check`'s scope enforcement permits those paths.
  (#55)

- Added the contract/identity layer for canonical provider-neutral capability-based
  provider resolution: a deterministic resolver selects a sufficient preferred
  provider, validates exact Flavor overrides, falls back only after preferred
  insufficiency, and fails closed when no provider satisfies every requirement.
  Components now author named catalogs and policies in `component.md`; selected Flavors
  author exact overrides in `flavor.md`. `litai lock` resolves and reports the complete
  result and override provenance, while `litai plan` projects it from the admitted lock.
  Catalog/policy/selection provenance identity-binds into `ComponentLock` and every
  `ComponentGenerationKey`, so capability drift returns deterministically to a newly
  sufficient preferred provider. (#41)

- Fixed a source-cache candidate restored from an earlier, separate `litai test`
  process's accepted cache entry having no derivation cache key captured in the new
  process, which made the ordinary publish/revalidation step raise
  `source_generation.cache_key_unavailable` and turned every cross-process cache hit
  into a hard failure — defeating the source cache's point for sequential `litai test`
  invocations such as a release-gate script running one call per Component.
  `FilesystemStandardSourceRestorer.restore_prepared` now hands the exact key it used
  to look the entry up back to the runner via a new `record_restored_cache_key` method,
  so a later publish call in the same process can find it. (#106)

- Widened `component://literate-ai/literate-ai-overview` to declare both members of
  `literate-ai.document-pair`: the existing `presentation` (Google Slides deck) and a
  new `narrative` (comprehensive multi-page Google Doc), with both required to be
  realized whenever a publication is authorized. Documented the document-pair
  convention — both members, one shared factual ledger, README links near the top —
  in the general authoring skill for future Google Workspace/Microsoft 365 publishing.
  Actual generation of the narrative's real content is blocked on an organizational
  Codex spend-cap limit encountered while probing the `documents` plugin; that is
  recorded honestly in the authoring package rather than faked, and remains open.

- Fixed `scripts/wheel_smoke.py`'s installed-CLI assertions breaking whenever the
  invoking shell had `FORCE_COLOR` (or a similar variable) set: Python 3.13+'s
  argparse colorizes help output in that case even when stdout is not a tty, which
  broke every exact `"usage: litai "` prefix check. Subprocess calls into the
  installed CLI now default to a sanitized environment (color-forcing variables
  stripped, `NO_COLOR=1`) unless the caller passes one explicitly.

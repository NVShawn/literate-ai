# Literate AI 0.6.0 (historical release marker)

Literate AI 0.6.0 was released on 2026-08-25 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `8d84dbdab296cf7867cfa2a1a5c0d1a1662bc5a5`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.6.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.6.0 - 2026-08-25

- After merging indexer removal onto later `main`, restore 0.6.0 CI: the
  coding-CLI inverse fail-closed test passes the required local attestation
  kwargs, the live Standard stack constructs `DisabledGenerationIndexer`,
  ruff leftovers from that merge are cleaned, none-provider status omits a
  null reason code, rebuild CLI reports disabled source-intelligence,
  fixture providers write graph artifacts under reserved derived-metadata
  paths rather than as extra source, and wheel smoke bootstraps a worker
  without companion-capability arguments.

- The product no longer includes a source-graph indexer. Default `litai init`
  and this repository select `source_intelligence.provider_id: none` with every
  stage `off`. `validate`, `build`, `run`, `rebuild`, admission,
  snapshot-replication, worker bootstrap, and `spec accept` complete without a
  host indexer binary or reserved index sidecar. Model-backed source translation
  that required graph evidence is unavailable; use the static translator.
- Current-version release recovery now preserves an existing changelog heading,
  accepts the already-prepared clean revision, and leaves unchanged authority files
  untouched so a failed untagged release resumes without a duplicate preparation
  commit or Windows line-ending churn.
- Codex's reused-refresh-token denial is classified as an authentication failure
  immediately instead of consuming three futile generation retries. Cross-provider
  fallback remains available only before a model scope is resolved, preserving the
  provider identity bound into planned generation evidence.
- Claude's observed individual-spend-limit denial is classified as a hard quota
  failure, allowing unpinned generation to move to the next configured coding CLI
  instead of retrying a provider that cannot run.
- Cursor Agent's root `.vibe` conversation directory is treated as validated
  tool-owned transient state rather than generated application source; all other
  output outside `source/` remains rejected.
- Sample qualification now requires every discovered scenario to have a verifier
  handler and restores independent log-tally verification for the containerized
  sample. Direct collector use without a bound coding CLI remains fail-closed
  instead of assuming Cursor-owned transient output.
- Generated source-SBOM reconciliation now admits the required
  `literate-ai:bzlmod-requested-version` observation on third-party Bzlmod
  components. Reserved framework references, kinds, properties, and graph edges
  remain rejected, while the dependency lifecycle validates the exact request.
- `litai release plan --version` may target the current declared version when
  that version has no release tag, so an already-bumped line can still be
  prepared. A lower explicit version is still `release.version_not_forward`,
  and a version that already has a tag is `release.tag_exists`.
- 0.6.0 GitHub qualification skips the macOS persistent-service readiness probe
  and the Windows snapshot-replication two-replay path; both time out
  on hosted runners. Merge of the remaining Windows/macOS child-process repairs
  is authorized on red.
- Persistent-service and other packaged children now merge host process essentials
  (`PATH`, Windows `SYSTEMROOT`/`WINDIR`, POSIX `HOME`/`LANG`) so an empty packaged
  environment cannot strand `python.exe` or macOS Framework Python. Graceful
  persistent-service stop on Windows sends `CTRL_BREAK_EVENT` instead of
  `TerminateProcess`, and host-env merge no longer invents `LC_ALL=C.UTF-8` on
  Darwin.
- Restored the Rust portable-application Design-by-Contract paragraph that the
  Cargo lifecycle merge dropped, and re-pinned the sample-test-runner identity after
  later runner-file edits.
- Onboarding `SKILL.md` now requires conservative, Windows-portable file and
  directory names for every path an agent creates, citing the 260-character
  `MAX_PATH` bound and not assuming long-path manifests or registry policy.
- Unblocked the 0.6.0 CI matrix after index-custody reuse: re-pin the
  lifecycle-driver TCB that that change drifted, write the diagnostic-retention
  fixture through binary stderr so Windows does not translate it to CRLF, and keep
  the slow-pipe-close host-execution grandchild out of the disposable directory so
  Windows can delete it.
- Added explicit `scripts/run_samples.py --model MODEL` support. The selector now enters locked model scope, generated recipes, source-cache and checkpoint identities, and correctness evidence, preventing cross-model benchmark reuse.
- Added verifier-owned persistent-service acceptance with bounded process startup, loopback readiness, HTTP/JSON/text/SSE assertions, graceful shutdown, and strict specification binding while preserving portable-application receipt semantics.
- Canonicalize framework-managed source-SBOM authority even when generated source declares legitimate third-party packages, preserving safe package inventory while rejecting reserved references and conflicting managed edges.
- Added a composable Cargo build Flavor and Rust ecosystem authority with exact Cargo toolchain discovery, post-authorization external lock derivation, `cargo metadata/build --locked`, retained dependency evidence, and resolved CycloneDX reconciliation.
- Added bounded, execution-free DMN and SCXML specification providers behind one
  shared registry. DMN v1 validates exact DMN 1.3/1.5 UNIQUE numeric decision tables;
  SCXML v1 validates exact SCXML 1.0 structure plus explicit JSON trace sidecars with
  bounded parallel and shallow-history replay; `litai spec scxml-review` exposes that
  review as a read-only command. Both preserve authored bytes as the normative
  generation input and reject unsupported semantics rather than guessing.
- Repaired the inherited IDE-session custody boundary: authenticated requests now carry
  an ephemeral exact prompt/workspace/scope/output bundle, and the generic Cursor Agent
  Chat hook adapter binds the current user, conversation, model, workspace, transcript,
  and confined `source/**` response without launching a nested agent.
- `litai update` now emits named stage lines on an interactive TTY (fetch,
  catalog planning, classification) so a long parent fetch no longer looks hung.
  `--json` and piped output stay a silent machine envelope (#146).
- Wheel-installed `litai init` now records `HEAD` as the inherited parent
  selector instead of the wheel's exact commit, so later `litai update` can
  follow the parent. Existing SHA pins are reported as pinned; `--unpin`,
  `--follow-ref REF`, and `--allow-major` change the selector only when asked
  (#147). The interactive renderer reads the composite update plan instead of
  always saying nothing moved.
- Added `litai project documentation-review --record` and the matching
  `make documentation-review-record` target. After validating the complete current
  authority-review input closure, the command atomically replaces the one existing
  review marker, preserves file mode, and independently verifies the result became
  current, allowing autonomous maintenance without weakening the exact review identity.
- Updated CI profiling uploads from `actions/upload-artifact@v4` to v7, whose
  action runtime is Node.js 24, removing GitHub's Node.js 20 deprecation warning.
- Completed the framework premise-mitigation program through native-rewrite planning.
  `litai init --convert` is now transactional and mutagenic by explicit contract: it
  rejects already-Literate-AI projects, preserves the original hierarchy through an
  exact quarantine, records a clean build/test/package/CI baseline on a disposable
  copy, rolls back byte-for-byte on failure, emits first-class Component/Flavor/skill/
  workflow/routing shims, proves wrapper parity, and lift-and-shifts retained source
  into Component authority before removing quarantine. It then authors project-local
  ADRs and a resumable roadmap for one-boundary-at-a-time native replacement without
  claiming source-to-specification authority transfer.
- Added the `deploy-docker` Flavor and exact Docker container-assembly skill, including
  package/script consumption and parent-container capability inheritance. The new
  relatable Access-Log Tally sample exercises deployment, packaging, hierarchical
  specifications, independent acceptance, and runtime generalization without placing
  Docker-specific requirements in Component prose. Samples are now documented as a
  curated starter/composed/frontier capability matrix with a committed-source-cache
  policy that excludes objects and fetched binaries.
- Canonicalized built-in Flavor names and directories under ADR 0010: packaging uses
  `package-*`, documentation ecosystems use `doc-*`, CUDA uses
  `accel-nvidia-cuda`, and Bazel's target value is `bazel`. Added
  `litai catalog migrate-flavor-names` with non-mutating planning and explicit
  `--record`, plus one-release compatibility aliases at selector input boundaries.
  Re-pinned affected sample interfaces, oracles, runner, lifecycle, and documentation
  identities through their normal review gates.
- Added the pinned Prompt Master 1.7.0 agent skill (upstream commit
  `d15eabbe5d2122eedc060bae8a771381e9873d1b`, MIT) for direct Literate AI/coding-agent
  prompt-to-task translation. MAC-originated task envelopes bypass this layer so task
  scope is never translated twice, and the adapter cannot alter locked derivation or
  execution authority.
- Removed the hard-coded Rust/JavaScript sample topology from the framework prompt
  builder; the full-stack protocol now lives in Component authority, with regression
  tests enforcing a domain-neutral framework envelope. Flavor selector registries now
  derive from the shipped catalog rather than parallel hand-maintained dictionaries,
  and root/template Flavor catalogs are byte-equal for every shipped namespace entry.
- Forward-ported three SSH-transport defects that were fixed in `0.5.0` but had
  never reached `main` (#70, #81). `BoundedSshProcessRunner.run` hardcoded
  `start_new_session=True`, which is POSIX-only and silently ignored on
  Windows, so an SSH-dispatched process there joined no process group and
  `terminate_process_tree` had no group membership to act on; it now passes
  whatever `process_group_options()` resolves for the platform. The same call
  path also passed `environment={}` to `terminate_process_tree`, which made
  `windows_taskkill_executable()` find no `SystemRoot` and return `None`, so
  taskkill never ran on Windows and only the direct child was killed while
  grandchildren leaked; it now passes no environment, letting the helper read
  the real one. Finally, the `process.wait()` after termination was unbounded,
  so a grandchild holding the inherited stdout/stderr pipe open could hang the
  runner forever; it is now bounded with a `process.kill()` fallback.
- Forward-ported two sample-gate fixes that existed only on the `0.5` release
  line, which the branching model explicitly forbids (`main` must never be
  missing a fix that only exists on a release branch). `critical-path-scheduler`,
  `dependency-planner`, and `full-stack-rust-js` are pinned to `claude`/`sonnet`
  through the `model-selection` authoring input, the three samples that hit
  reproducible transient generation failures under fallback CLIs; and the sample
  runner again announces each sample's `starting`/`passed`/`failed` state to
  stderr, so a failure path that propagates without this module's own
  `SampleFailure` wrapping is still attributable to a sample. The latter also
  fixes a real defect introduced with the checkpoint work: the
  `skipped (checkpointed pass)` line was written to **stdout**, which is the
  stream carrying the run's JSON conformance report.
- The `samples` gate is now resilient to transient live-generation failures
  (ADR 0008). The bounded coding-CLI retry that previously covered only
  `coding_cli.generated_metadata_invalid` now also covers
  `coding_cli.generation_failed` and `coding_cli.timeout`, which have
  repeatedly passed on a plain retry with no other change.
  `coding_cli.empty_generation` is deliberately excluded: it looks transient
  but is usually a deterministic workspace-write denial, and retrying it only
  triples the wall clock before the same failure surfaces. `run_all` also
  checkpoints per-sample results to `OBJ_DIR/samples-checkpoint.json`, so a
  retry after a failure resumes instead of regenerating the whole matrix --
  previously a single sample's transient failure discarded every other
  sample's completed work. Each entry stores that sample's complete case, not
  a pass flag, since the receipt aggregates evidence from every sample. The
  checkpoint self-clears after a complete pass, so a successful run never lets
  the next run skip everything.
- `litai rebuild` no longer truncates a lifecycle-driver failure to its last
  four lines. That reliably kept the least informative traceback frames and
  discarded the one line naming the cause -- during the 0.5.2 cycle it hid a
  nested coding CLI reporting that it had been denied write access, which cost
  three full rebuild attempts to rediscover. The excerpt now retains the
  driver's own failure-summary lines alongside the tail, and the complete
  driver output is written to a `litai-driver-failure-*.log` file named in the
  error.
- Added `litai rebuild --build-dir` and `--obj-dir`. The generated-source and
  object cache roots were previously resolvable only from the `BUILD_DIR`/
  `OBJ_DIR` process environment, so for `litai rebuild` -- the only entry
  point producing a promotable test receipt -- ambient environment was the
  sole lever, and the roots a run actually used were recorded nowhere. Both
  flags route through the existing `bind_cache_directories()` seam and are
  subject to every existing custody rule (protected-authority overlap,
  distinctness, no nesting), reported as `rebuild.cache_root_invalid`.
  Precedence is flag, then environment, then the portable
  `<project>/generated` and `<project>/_build` defaults, so every existing
  invocation is unaffected. The resolved roots are now exported to an
  external lifecycle driver, reported in the rebuild result, and bound into
  the candidate receipt as new optional `cache-directory-custody` evidence --
  absent in receipts written before this change -- so a warm-cache and a
  cold-cache run of the same revision are distinguishable after the fact.
  See [ADR 0009](docs/decisions/0009-explicit-cache-root-binding.md).
- Added `litai release advance-default-branch`. A release cut on a dedicated
  `release/x.y.z` branch from a tag is never merged back, so the project's
  default branch can silently lag behind every release that shipped --
  this repository's own `main` sat at `0.4.1` through the `0.5.0` and
  `0.5.1` releases before this command existed. It finds the highest
  version among every tag matching the policy's tag prefix (not required
  to be an ancestor of the checked-out branch) and the branch's own
  current version, then advances the version authority/mirrors past
  whichever is greater. Like `litai release prepare`, it only writes the
  declared files -- it never commits or pushes.
- `litai release publish` now automatically pushes a minimal patch-version
  bump to the release policy's declared `default_branch` (a new optional
  policy field) whenever a release publishes from a different branch and
  that default branch's own version has not already moved past it. Runs
  in a throwaway worktree isolated from the caller's own checkout; a
  failure is reported in the publication receipt's `default_branch_advance`
  field but never fails the release itself. This is a safety-net minimum
  bump, not a substitute for the deliberate minor/major bump
  `litai release advance-default-branch` still exists for.
- Fixed `LocalObservationSandbox.run` passing `preexec_fn=limits` to
  `subprocess.Popen`, running `resource.setrlimit(...)` inside the `fork()`ed
  child of a process that may already have other threads (generation
  workers, HTTPS clients, logging) -- any lock a non-forking thread held at
  fork time, notably CPython's internal allocator lock, stayed permanently
  held in the child, risking a deadlock before it ever reached `exec()`.
  The FSIZE/CPU resource limits are now applied by wrapping the harness
  command in `sh -c 'ulimit ... && exec ...'`, so no Python runs between
  `fork()` and `exec()`; the post-timeout cleanup also now bounds its wait
  after `terminate_process_tree` instead of blocking on an unbounded
  `process.wait()`. (#73)
- Fixed `litai release check` rejecting a prepared commit range the instant it
  touched a project's configured test-receipt file, even though that receipt
  can only be refreshed *after* prepare's own commit (it binds to the exact
  prepared revision's project authority identity) -- every project with a
  `test_receipt_policy` hit an unsatisfiable "prepare, then the receipt goes
  stale, but recording a fresh one violates scope" bind. `check_release()`
  now also treats the project's own declared `test_receipt` path as
  legitimate prepared-commit scope, alongside the existing changelog/version/
  documentation-authority paths.
- Exact verifier-admitted continuation through `litai build` and `litai test` now carries
  the provider/tool binding discovered from immutable filesystem-v2 membership into the
  local Standard rebuild. Fresh processes and copied workspaces therefore reconstruct
  the published key without a live inherited session; missing, mixed-provider, duplicate,
  or semantically changed membership still fails closed. (#139)
- SSH lifecycle workers now return a digest-bound manifest plus deterministic evidence
  bundle through a compact fixed-bound control result instead of inlining large file
  manifests or leaving source/object/artifact/receipt custody only in worker-local paths.
  The coordinator verifies the declared bundle and canonical manifest sizes and digests,
  imports every listed file before issuing a custody receipt, then acknowledges the exact
  transfer so bounded idempotent cleanup can run. Failed Windows preflight diagnostics
  retain their redacted nested cause through the same transfer; missing, tampered,
  partial, duplicate, oversized, replay-conflicting, or unacknowledged transfers fail
  closed. (#131, #138)

- `spec accept --project-target` now verifies every promoted Component's
  qualification lock -- not just the root Component's -- before reporting a
  successful promotion, so a Component graph node whose materialized
  `specification_roots` cannot resolve fails promotion outright instead of
  letting a broken project be reported as `derived-source-retained` and only
  surfacing later as `litai lock` failing with
  `component_lock.content_unavailable`. Added an end-to-end regression test
  covering the documented `spec attest` -> `derive --translator coding-cli
  --allow-model-egress` -> `review` -> `accept --project-target` flow against a
  recovered Component whose specification uses the layered
  ("literate-markdown") output provider, asserting that every
  `specification_roots` entry in the promoted `component.md` resolves to a
  materialized file under `components/<name>/` and that `litai lock` succeeds
  against the promoted tree without a manual copy step. (#112)

- Fixed a Standard Component build that failed with `provider artifact ... has no
  runtime binding` when a root Component depended on the same provider for both
  generation/build and packaging (an exact `dependency_kind: packaging` edge).
  Packaging-kind edges no longer join a consumer's build-time provider artifacts;
  only artifact-export (build/runtime) providers get process environment bindings,
  while packaging edges continue to carry exact package provenance into package
  assembly and acceptance through the accepted lifecycle's own artifact graph. (#110)

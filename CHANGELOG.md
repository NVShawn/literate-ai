# Changelog

## Unreleased

- Windows installation: report inaccessible declared native-tool search directories
  as actionable host-install errors instead of uncaught tracebacks. Document the
  PowerShell source-install entry point and activation-free wheel commands.

- Testing: keep the POSIX worker termination fixture alive for a bounded child
  signal-handler acknowledgement before removing its temporary directory.

- Documentation: 1.0.x prefix installs cannot self-update across the repository move,
  because the archived `NVIDIA-dev/literate-ai` is internal and 1.0.x downloads wheels
  from a URL that refuses non-public repositories. The migration guide and ADR 0049 now
  give the one-time `make install` reinstall from the public repository (#18).

## 1.1.0 - 2026-10-04

[README.md](https://github.com/jordanhubbard/literate-ai/blob/v1.1.0/README.md)

- Initialization: seed the starter acceptance oracle from the `hello-component`
  contract the project actually received. Projects derived with `init --from` the
  framework repository inherit its greeting-card sample (`name` plus `messages`) and
  previously got name-only expectations, so every live rebuild failed acceptance.

- Documentation: point getting started, installation, worker registration and
  adoption guidance at the onboarding film and instructional courses, and link the
  course index to repository files instead of a feature branch.

- Continue existing installations across the repository move: projects initialized
  from `NVIDIA-dev/literate-ai` plan `litai update` against public-repository builds,
  and self-update follows the declared successor. Other repository origins are still
  refused. See the
  [migration guide](docs/user/repository-migration.md).

- macOS dependency observation reuses `dyld_info` facts within a process only for
  identical inspector bytes, arguments and image identity: content and symlink chain
  for on-disk images, the boot session for shared-cache images. Retained Cargo native
  test guards no longer re-inspect ~700 unchanged system images per check (#13).

- Elixir: add `lang-elixir` with portable script-tree generation, syntax validation,
  Standard runtime/test dispatch, exact Elixir/OTP toolchain identities, and
  `init`/`flavor add` assets. Requires Elixir 1.18+ and Erlang/OTP 27+; native
  macOS smoke verification passes with Elixir 1.20.4/OTP 29. Mix/Hex/Phoenix
  are outside this dependency-free profile.

- Documentation tooling: update DOMPurify to 3.4.16. Gate the dependency audit
  through reviewed, expiring per-advisory exceptions. The only exception covers the
  unpatched braces advisory GHSA-vfj7-8cjw-p6xm in OpenSpec's glob dependencies (#24).

- CLI self-update: refresh bytecode in the caller-selected cache and optimization
  mode so a successful wheel upgrade cannot immediately execute stale code from
  an equal-size, equal-timestamp previous version. Keep import-path overrides
  excluded from the installer environment.

- Retained adoption: add explicit read-only Standard planning with bounded binary input
  custody and complete action inventories. Execution and release qualification remain
  unavailable until the retained-origin receipt path is complete (ADR 0048).

- CLI: keep pip installation and command re-execution on the same configured
  bytecode cache during self-update, preventing stale code after equal-size,
  equal-timestamp upgrades while preserving unrelated caches.

- CI: run one full Linux suite plus cross-platform native/install smoke on ordinary
  PRs, consolidate documentation jobs, cache dependency downloads, and preserve
  independent failure diagnostics. Full multi-platform release qualification remains
  required. Publication subprocess timeouts now stay within their reviewed limit
  even when floating-point deadline arithmetic rounds upward.

- CI: separate focused macOS PR smoke checks from full main/release qualification,
  bound conformance steps, and retain verbose test names and self-update failure
  diagnostics. Full release gates remain unchanged.

- Onboarding: feature the continuous narrated video in the README, with a clickable
  preview, chapters, subtitles, and a public-feedback link.

- Move the canonical repository to
  [jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai): release
  publication, the Homebrew formula, install documentation, and initialized project
  READMEs now reference it. Earlier issue and CI links refer to the archived
  `NVIDIA-dev/literate-ai` repository.

- CLI: identify Git/source builds and their exact revision in version output,
  without treating package metadata as published-release evidence. Explicit
  `litai update` now checks for and applies a newer stable release in an enrolled
  prefix before continuing project reconciliation; opt-outs remain effective.

- Bind TEST dispatch to the exact worker, deadline, planned predecessors and
  BUILD/result handoff before child execution, then re-admit returned evidence.
  Configured receiver and scheduling integration remain pending.

- Add a supervised TEST child with current grant/deadline checks, cancellation,
  bounded streams and descendant cleanup, sharing BUILD's process supervision.
  Configured receiver dispatch and production TEST scheduling remain pending.

- Import worker TEST results through explicit verified return transport, retaining
  evidence before registration and refusing corrupted records or changed worker
  authority. Production TEST dispatch remains pending.

- Add a worker TEST operation over verified transferred source and BUILD artifacts,
  returning bounded evidence tied to the exact handoff. Supervised command/SSH
  dispatch and production queue integration remain pending.

- Add controller admission for transferred generated-test evidence, checking exact
  suite, build and entrypoint authority before registration. Worker TEST execution
  and production dispatch remain pending.

- Remote tool discovery verifies authored command names against a worker's private
  search environment and rechecks them before reuse. Changed resolution, including
  a newly shadowing executable, is refused without running the supplied command.

- Make locked-command BUILD export and provider checks reusable from portable
  records, preserving local refusal before command execution. Remote BUILD
  execution and artifact transfer remain pending.

- Standard rebuild dispatches AUTHORIZE through the shared admitted worker pool,
  captures controller time after reserving capacity, and rechecks returned grants
  against current SDK custody and expiry without local retry or grant renewal.

- Add bounded command-worker AUTHORIZE execution using the controller-selected
  issuance time and fixed constrained policy. Changed index custody, future/expired
  grants and payload substitutions are refused; production queue wiring is pending.

- Report hardware probe error codes when no candidate can be observed, preserving
  healthy-worker admission and excluding raw exception messages from the refusal.

- Construct Standard authorization from explicit intent, index and controller time,
  preserving the existing grant policy while retaining local SDK admission and
  evidence recording. AUTHORIZE transport remains pending.

- Retain remote PLAN result evidence before registering local plan or SDK authority;
  an evidence-store failure leaves registration unchanged and frees worker capacity.

- Command workers can construct build intent from an exact indexed-source handoff
  and typed provider acceptance records, checking the canonical DAG and bounded
  predecessor custody. Standard rebuild now dispatches BUILD_INTENT through the
  shared INDEX/PLAN worker slots, retaining results before local admission and
  refusing missing provider acceptance evidence without local retry.

- Build-intent admission rechecks current inputs and registers provider bindings
  only after library validation and required evidence recording succeed. Rejected
  intents no longer leave partially registered provider state.

- Standard rebuild can finalize plans through admitted command/SSH workers, sharing
  capacity with source indexing and independently verifying returned plans before
  local registration. Native Linux and Windows PLAN receiver and SSH checks pass;
  remaining lifecycle phases and release qualification stay open.

- Construct build intent from exact portable source, command, dependency and SDK
  inputs, rejecting mismatched candidate and generation-plan bindings. Local source
  admission, live SDK checks and library-import custody remain enforced.

- Test qualification: preserve the long coordinator-path regression on native
  Windows by creating its fixture through the existing native filesystem boundary.
  Real SSH PLAN checks now confirm exact results and authorization refusals on
  Linux and Windows without executing a build.

- Add opt-in local worker provisioning through an organization-owned command, with
  credential bindings, help discovery, bounded request/response validation, durable
  duplicate-allocation protection, and registration recovery. Static worker CRUD
  and SSH tests remain independent of provisioning.

- Windows action dispatch preserves case-insensitive runtime environment names.
  Capability probes allow up to 60 seconds for native identity checks and startup,
  while retaining any shorter action deadline and the existing environment allowlist.

- Framework core: local builds no longer wait for packaging-only providers or bind
  their artifacts into compilation. Final packages still require accepted providers;
  provider failure prevents publication.

- Framework core: artifact graphs retain explicit runtime and packaging dependencies
  with locked-edge and provider-acceptance evidence, preserving compiled artifact
  identities while validating exact package closure.

- Framework core: execution evidence records exact provider artifacts; lifecycle
  admission and independent evidence reopening reject omitted or substituted inputs.

- Framework core: toolchain dependencies now bind accepted provider exports into
  consumer build intent, build actions, worker imports, process bindings, and
  resulting artifact provenance.

- Framework core: consumers of locked public interfaces can build while providers
  are still running. Artifact consumers retain acceptance barriers, and any failed
  component still prevents project acceptance and publication.

- Framework core: private action workers can configure an SSH receiver for capability
  probes and source indexing, using bounded stdin and exact worker/receiver checks.

- Framework core: action plans now bind accepted build/toolchain inputs before build
  intent and authorization, while preserving runtime and packaging phase overlap.

- Select admitted command indexing from private action-execution configuration,
  with real health checks, pinned policy custody, and refusal of stale observations
  or changed configuration. Command-worker hardware probing remains under qualification.

- Bound command-index source reads before publication and after worker execution;
  reject source growth before retaining qualification evidence.

- Allow the Standard rebuild factory to index through an admitted command-worker
  pool using verified source evidence custody and an explicit publication CAS;
  preserve that port through source-cache and checkpoint composition.

- Compose live command-worker phase admission into Standard indexing, checking
  hardware freshness, source handoff, health evidence, and unchanged runtime
  capabilities before dispatch and result acceptance. Automatic CLI routing remains
  under qualification.

- Add opt-in command-worker phase capability probes with exact worker/runtime
  binding, replay refusal, and unchanged legacy worker identities.

- Add a command-backed Standard generation indexer that publishes verified source,
  respects admitted worker slots, and retains exact qualification evidence.
  Automatic CLI routing remains under qualification.

- Let command-worker source indexing fetch missing source blobs from a privately
  configured HTTP(S) CAS, verifying exact bytes before local publication.

- Execute preplanned source-index actions in a command worker using verified CAS files and
  disposable source custody. Automatic routing and the remaining worker phases
  remain under qualification.

- Add a data-only command-worker action protocol with verified records, deadlines,
  and cancellation. Production phase routing remains under qualification.

- Enforce local shared-artifact expiry and aggregate storage quotas while preserving
  deduplicated payloads, read-only access, and verified remote fallback.

- Index local consumer source while providers build, then wait for accepted provider
  exports before creating build intent; preserve completed indexing on cancellation.

- Dispatch local Component build phases separately and recheck authorization when
  each queued host phase starts, refusing expired grants before execution.

- Repository refresh: validate existing files against their captured physical
  permissions so group-writable checkouts remain valid while later permission
  changes still fail custody checks.

- Worker operations: add private catalog list/show/add/update/remove commands with
  validated atomic writes and stale-identity protection, plus bounded SSH tests
  that report each selected worker independently. Failed capability probes retain
  successful peer results and remove stale observations for failed workers.

- Overlap local consumer source generation with provider build work while preserving
  accepted artifact imports and one shared concurrency limit.

- Make action-DAG plans wait for provider acceptance before consuming build,
  toolchain, or runtime artifacts, while keeping interface-only generation independent.

- Include the compiler-cache tool's recursive native dependencies in build evidence
  and cache identity, refusing dependency changes before and after compilation.

- Cache native C++ translation units through sccache while linking each declared
  executable normally, including Components with multiple entrypoints.

- Run configured Cargo compiler caching through an identity-bound sccache tool
  and an owned private server, retaining cache counters with build observations.

- Bind private shared-cache configuration into Standard Bazel builds, with
  temporary credential custody and disposable read-only disk-cache views.

- Preserve bounded, redacted SSH probe diagnostics with the failing worker, exit
  status, and actionable cause; retain worker identity on invalid observations.

- Start ready Component lifecycles as soon as their own dependencies finish,
  without waiting for unrelated work in a previous dependency layer.

- Add bounded HTTP shared-artifact transport with immutable publication checks,
  verified local fallback, and read-only access to writer entries. Production
  lifecycle integration remains part of the 1.2 qualification program.

- Project updates now merge non-overlapping local and upstream text edits using
  verified base content, preserve overlays across consecutive updates, and apply
  explicitly reviewed conflict resolutions with `--apply --resolutions FILE`.
  Update baselines and files roll back together on validation failure.

- Instructional narrative: add an Opus-reviewed ten-minute skeptical-adopter
  script and a new local expressive-voice audition with separate acronym
  pronunciation text. The existing public film remains unchanged pending human
  listening review and full replacement production.

- Instructional media: replace the short-course viewing path with one continuous,
  two-voice feedback-edition video play, character introductions, fresh recorded
  greenfield/TinyXML2 execution, visible captions, and an updates/lineage closing.
  Keep the discovered inherited-starter oracle defect and demo-local correction
  explicit; this does not claim a framework release or an upstream defect repair.

- Separate inheritable video-production craft from project-owned narrative:
  neutral course initialization, project-selected presenters and voices, visual
  shots, and a quality-review skill that calls for an audition before full production.
- Add rebuildable instructional courses through `litai video init`, `plan`,
  `build`, and `verify`: two-speaker narration, recordings, captions, and
  source-bound media receipts, with a packaged video-authoring skill.
- Repair two defects exposed by live course demos: starter independent acceptance
  now matches its greeting specification, and retained test receipts recognize
  CTest's newer exact all-passed summary without accepting zero or unknown counts.


- CI: run one full Linux suite plus cross-platform native/install smoke on ordinary
  PRs, consolidate documentation jobs, cache dependency downloads, and preserve
  independent failure diagnostics. Full multi-platform release qualification remains
  required. Publication subprocess timeouts now stay within their reviewed limit
  even when floating-point deadline arithmetic rounds upward.

- CI: separate focused macOS PR smoke checks from full main/release qualification,
  bound conformance steps, and retain verbose test names and self-update failure
  diagnostics. Full release gates remain unchanged.

- Onboarding: feature the continuous narrated video in the README, with a clickable
  preview, chapters, subtitles, and a public-feedback link.

- CLI: identify Git/source builds and their exact revision in version output,
  without treating package metadata as published-release evidence. Explicit
  `litai update` now checks for and applies a newer stable release in an enrolled
  prefix before continuing project reconciliation; opt-outs remain effective.

- Preserve an adopted project's repository parent selection when signed source
  promotion creates its nested native project. This keeps promotion usable from
  clean public checkouts without relying on unrelated local release tags.

- Recognize absorbed submodule primary worktrees during attached orchestration
  refresh, while refusing shared-branch changes affecting external checkouts (#503).

- Document capability-first NVIDIA library discovery, language integration, and
  admission criteria, with CUDA Python as the first planned realization. Library
  execution qualification remains tracked separately.

- Compare installed-wheel initialization origins in the same canonical repository
  form embedded by the build backend, so equivalent GitHub SSH and HTTPS URLs do
  not invalidate exact-origin qualification.

- Make public export explicit and repeatable: private downstream and organization
  infrastructure references are replaced with synthetic examples,
  `make public-export-check` scans tracked text and embedded Office content, and
  host self-update derives its GitHub Releases source from the installed wheel's
  immutable origin instead of a hard-coded owner. First-time `make bootstrap` now
  installs the pinned, isolated public SkillEvaluator before running validation.
- Let local source generation raise its finite stderr progress allowance with
  `LITERATE_AI_CODING_CLI_GENERATION_STDERR_LIMIT_BYTES`, capped at 256 MiB. The
  default remains 16 MiB, and stdout, JSON-task, generated-tree, timeout, overflow,
  and process-tree termination contracts remain unchanged (#495).

- Add the first accepted 1.2 action-throughput slice: project exact lifecycle action
  DAGs, admit fresh capability-matched worker slots, dispatch newly ready work without
  waiting for unrelated components, isolate descendant failure, and bind local or
  credential-free shared-cache policy to Bazel, sccache, and verified immutable
  package artifacts.

- Add verifier-owned native CLI application acceptance with exact argv, stdin, exit,
  stdout, stderr, and filesystem assertions over package-custodied executables. Native
  CLI Components no longer need to masquerade as portable JSON applications.

- Add the first real Debian-package slice: exact architecture, install-path, mode,
  documentation, SBOM, and explicit runtime-dependency projection; digest-bound
  `dpkg-deb` construction; and independent ar/control/data archive inspection. Local
  construction refuses non-Linux hosts while production remote package dispatch
  remains gated. Debian architecture projection also normalizes the `AMD64` spelling
  reported by Windows controllers when validating the public package path.

- Derive one canonical repository-owned `.worktrees/` root from Git common-directory
  and registration custody, expose it through `litai project worktree location`, and
  provide collision-safe atomic reservations shared with generated agent guidance.

- Add verifier-owned native gRPC IPC acceptance under accepted ADR 0029: load
  bounded `@2` descriptor closures and all four RPC cardinalities, compare complete
  reflection output, enforce one remaining lifecycle deadline, retain raw evidence,
  shut services down on success or refusal, and fail closed when the optional pinned
  gRPC runtime is unavailable. Equivalent implicit and explicit protobuf default JSON
  names normalize together while custom-name drift still refuses.

- Recognize the retained repo_man Standard command profile already shipped by the
  `build-repo-man` Flavor. Its repository-relative driver remains recorded authority
  while disposable generated wrappers build with the selected language toolchain;
  exact target, driver, path, schema, and public-catalog validation now fail closed.

- Bind implicit macOS developer-directory and SDK selections into C++ toolchain
  identity, and declare the selected SDK consistently to Bazel repository probes,
  compile actions, and independent consumer checks.

- Preserve exact refresh custody and planning identities when Windows exposes
  unsigned 64-bit filesystem node identifiers outside canonical JSON's signed
  range.

- Give the generated Bazel C++ library stager an explicit macOS 10.15
  deployment floor so C++17 filesystem APIs remain available under new SDKs.

- Restore explicit retained-harness stage selection during readmission. Reviewed
  test/package commands remain in the identity-bound plan when fresh inspection
  cannot rediscover them; apply rejects a changed or omitted selection (#488).

- Keep direct CLI help contract tests stable when developer shells force ANSI
  color, including CI-plan, parent-checkout, and tracker-inspect assertions,
  without disabling colored CLI output for users.

- Keep the packaged worktree survey skill synchronized with its authored repository-placement and evidence-preservation rules.

- Keep agent worktrees inside the owning repository under `.worktrees/`; extend
  the existing peer-work survey with evidence-preserving checkout cleanup.

- Permit directory-shaped package results to retain their complete bounded logical
  file closure above 256 entries while preserving the stricter outer-package limit
  for archives, installers, and container images (#480).

- Retry a generated JavaScript single-file bundle in a fresh bounded workspace when
  handoff validation proves that it omitted a reachable module, while preserving the
  existing fail-closed retry limit.

- Resolve GitLab release dispositions in creation order across all notes pages, so newer scope decisions supersede older records; reject invalid or repeated note identities (#471).

- Add exact target-specific Python wheel locks, offline hash-verified acquisition,
  retained runtime closure and isolated Standard lifecycle execution. Bind source,
  dependency graph, interpreter, installer, artifact and SBOM evidence before use;
  reject ambient packages and changed payloads. Packaged-runtime custody remains
  under repair for #473 before this feature is release-ready.

- Add an internal live repository-refresh transaction over reviewed publication
  custody. It journals before mutation, installs verified object packs additively,
  applies exact staged child/source/Git metadata with the root manifest last, and
  cleans staging only after validated commit or conservative rollback. Foreign
  concurrent edits are preserved with recovery staging retained. Add the reviewed
  `onboard orchestrate refresh plan|check|apply` front door over canonical requests,
  exact local/publication custody, shared bounded fetch deadlines, stale-plan checks
  and explicit acknowledgement. Results distinguish commit from exact no-op without
  claiming child acceptance or crash replay. Harden live replacement against
  concurrent pathname winners, prove complete worktree membership and unchanged
  members before/after the manifest boundary, preserve directory modes, verify
  terminal objects with bounded no-follow reads under keep ownership, and report
  Windows symlink selections as non-applicable. Opaque initialized nested Gitlinks
  now retain their independently revalidated custody, admitted hydrated LFS
  payloads retain exact node/hash proof, and required same-filesystem hardlinks
  are proven before mutation. Descriptor-held originals are revalidated before
  discard. Hydrated LFS custody now compares the descriptor's final metadata to
  that same descriptor's opening observation, avoiding Windows pathname/handle
  `ctime` differences without weakening pathname, identity, or digest checks.
  A committed journal is never rolled back; ambiguous failures after its
  publication retain the terminal staging evidence. Terminal stage deletion is now
  deferred until stage-marker and outer refresh reservations release; release or
  final-cleanup failure returns committed-with-cleanup-retained evidence without
  replay authority, while bounded custody-revalidating retries absorb Windows'
  transient delete-pending marker retirement. Windows plans and apply guards also refuse directory
  add/remove/type transitions while retaining ordinary file-only support. Hydrated
  LFS admission now requires an actual typed observation after identity proof.
  Canonical Git LFS clean/process configuration is admitted without executing
  `git-lfs`: status uses empty command-local clean/process values and disables the
  required bit so Git compares raw bytes, while exact indexed pointer size/hash
  custody remains mandatory and every other external clean/process filter still
  refuses. LFS subsection spelling is case-sensitive even though Git section and
  terminal variable names are not. Publication verification, tree capture and
  object-pack capture now share a three-attempt transient retry boundary over fresh
  disposable proof storage. Git/transport/deadline failures may retry without sleep
  under the one original total deadline; semantic publication failures never retry,
  and returned evidence remains bound to the reviewed deadline policy. Disposable
  proof-storage cleanup can no longer mask a primary publication refusal: the
  primary typed failure controls retry classification and any simultaneous sanitized
  cleanup failure remains its explicit cause. Cleanup failure after an otherwise
  successful proof still refuses that attempt as a transport failure.
  Publication proof now selects a deterministic exact branch-tip witness when the
  requested commit is directly advertised by `refs/heads/*`; that path fetches only
  the fully qualified branch into an exact private destination with depth one and no
  tags. Tags, pseudo-refs and other namespaces retain the full advertised-history
  proof, as do ancestor targets. Exact-tip tree capture remains shallow, while pack
  capture unshallows only that exact branch and still requires an empty-store
  self-contained connectivity check. If a transport rejects the depth-one fetch,
  publication discards that proof store and falls back once to the pre-existing full
  ancestry strategy within the same attempt cap and original absolute deadline;
  timeout/no-progress failures remain typed retries and semantic failures after a
  successful shallow fetch never select fallback. Witness selection is independent
  of that transport choice: both shallow and full fallback retain the first
  canonical exact branch-tip witness ahead of any earlier-sorting generally
  reachable ref.
  Add a publication-bound `root-pin-only` mode when a changed Gitlink's clean
  independent child is already at the exact reviewed target commit. This mode skips
  all local/remote full-tree and object-pack capture, stages no child bytes, installs
  no child objects, and changes only root index/lock/manifest authority. Mixed
  selections retain the existing `source-transition` path per child. Modes, physical
  custody and aggregate source-write reporting are identity-bound and revalidated
  with exact child/root Git, Gitlink URL/branch and fresh publication custody before
  terminal root commit. Committed terminal validation now repeats exact application
  input custody immediately before journal publication; rollback terminal proof
  remains based on restored transaction targets and does not demand prospective
  Gitlink/publication inputs. Root-pin-only custody now includes every observed
  initialized nested descendant selected by component-aware path ancestry, without
  accidentally including similarly prefixed sibling paths.
  Cleanup conversion is limited to the recognized reservation-cleanup error;
  unexpected base exceptions propagate after evidence retention. Results separate
  authority changes from persistent filesystem writes and distinguish retained
  stage-marker artifacts from outer refresh reservation artifacts, including
  truthful retained no-op rendering. Simultaneous body and release failures now
  propagate together in stable body-then-release exception-group order.
  Focused Makefile-driven application, CLI surface/planning, staging, publication
  ownership and reservation tests pass, as do format and lint; installed/native
  cross-platform, hosted and fresh full-suite qualification remain open.
- Add explicit retained-source qualification under an existing Standard
  Component, with exact review authorization and separate provenance/cache custody.
  Preserve source bytes, specifications, and all existing classification,
  dependency, build, and independent-acceptance gates (#463).

- Accept Git-valid underscore-prefixed branch names when recording exact-head
  peer-work lifecycle markers, while retaining unsafe-ref and option-like-name
  refusal (#465).

- Complete the read-only HTML observability fan-out with bounded performance/run
  history and exact workflow/routing catalog views. Add an offline multi-project
  dashboard shell over declared, verified generated artifacts, binding every pane's
  digest and preserving foreign output. Generalize derived-output recognition so
  the existing health surfaces can be safely replaced as well as the DAG view.

- Use the existing finite 30-minute Bazel command budget during Standard dependency
  analysis and native builds, avoiding premature 60-second failures while preserving
  ordinary lifecycle command limits and process-tree termination.

- Package accepted C++ static/shared libraries as native Conan products with exact
  include, link and runtime metadata derived from lifecycle authority. Verify one
  exact restored recipe/binary revision and every declared payload byte, then support
  fresh-cache BazelDeps consumers without producer source. Library archives no longer
  require a fictitious executable entrypoint, and native C++ wheels fail closed until
  real Python bindings and platform ABI tags are declared. Qualify real Bazel clean,
  no-op, unrelated-edit, public-header-edit, implementation-edit and restored-clean
  builds for both product kinds, requiring correct invalidation and byte-identical
  incrementally restored and clean-restored products.
  Fresh-cache native consumers also reject valid but different target architecture,
  build type, compiler version and C++ ABI profiles instead of substituting the
  available Conan binary. Verify the complete dependency reference selected by the
  consumer graph and reject a newer ambient same-coordinate recipe before Bazel
  metadata or execution can qualify it. Canonicalize COFF archive member timestamps
  during Windows product staging so clean-restored static libraries retain exact byte
  identity instead of inheriting wall-clock metadata from the MSVC archiver.

- Canonicalize recognized GitHub SSH/HTTPS origin aliases when building wheels,
  so clone transport spelling does not change the installed framework identity.
  Installed payload verification and existing exact pins remain unchanged.

- Add `cargo-lock-package` release mirrors for uniquely named workspace packages
  and lowercase quoted version keys for Cargo authorities. Atomic preparation
  preserves unrelated dependency bytes and rejects ambiguous, sourced, or drifted
  package entries before changing the release files. Refuse malformed dependency
  lists and versions Cargo cannot accept, including PEP 440-only prereleases.

- Monorepo adoption: allow an explicitly reviewed multi-root plan to apply only with
  retained baseline execution. Publish independently receipted, source-free Component
  custody after transactional lift-shift; include it in project validation and refresh
  only stale boundaries with separate execution consent while reusing current receipts.

- Rebuild every committed Component lock when `litai rebuild .` uses the Standard
  driver, instead of failing closed for a missing single Component entry point.
  Keep `litai rebuild components/NAME` unchanged, skip unlocked catalog entries,
  fail before publication if any Component fails or authority changes, and bind
  every Component lock and accepted result into the combined candidate, receipt,
  DAG and test count.

- Preserve full bounded retained-run failure diagnostics through the CLI. Prioritize
  reported disk/quota exhaustion and both stream tails, keep secrets redacted, and
  distinguish direct OS storage exhaustion from unverified child output.

- Run `generate --admit` source tests in a disposable copy so successful build
  output and bytecode do not cause source-cache tree mismatches. Preserve exact
  candidate publication and reject changed source in either tree.

- Add explicit bounded worker alert history with deduplicated incidents, escalation
  and recovery events. Missing measurements remain unknown, reporting state never
  changes admission, and storage inspection stays read-only without `--alert-state`.

- Add read-only `litai worker health` storage inspection with private role/footprint
  configuration, current worker binding, redacted failures, actionable deficit
  alerts and explicit proceed/hold/recheck results. Pressure and dispatch integration
  remain under development.

- Retain native dependency custody with bounded streaming hashes instead of
  in-memory image copies. Support images up to 512 MiB within a 2 GiB aggregate
  budget while preserving file, directory and loader-alias mutation refusal.
  Close the streaming descriptor before the final named-path comparison so
  Windows cannot defer a same-size writer's timestamp past the custody check.

- Preserve overlapping worker quota domains and independent inode limits. Add
  read-only Linux ext user/group quota queries, conservative soft-limit headroom,
  and explicit unsupported filesystem/project-quota states. Earlier unreleased
  private observations require fresh collection.

- Add explicit command-worker storage receivers with bounded stdin, minimal
  credential mappings, exact worker/policy/job request identities and request-level
  deadlines. Missing health capability or required credentials cannot admit work.

- Extend private worker-storage probing to configured SSH workers, with bounded
  stdin and worker-side deadlines. Request-bound timeout results survive Windows
  SSH exit-code translation; native Linux, macOS and Windows probes pass.

- Add bounded local worker-storage probes with private path bindings, caller-visible
  capacity, explicit unsupported measurements, deadline/output refusal and no target
  creation. Late failed observations remain recordable and cannot admit work.

- Add the worker-capacity policy and observation foundation: account for shared
  volumes and quotas, preserve allocation reserves, refuse stale or mismatched
  observations, and keep missing measurements explicit. Worker collection and
  dispatch integration remain under development in accepted ADR 0043.

- Inspect macOS dependency UUIDs, links and loader paths in one dyld_info call per
  image, preserving the complete dependency graph and mutation checks while reducing
  inspector subprocess launches.

- Bind service, frontend-readiness and IPC acceptance HTTP probes directly to the
  selected loopback service; bypass ambient proxies and global openers, preserve
  same-origin redirects and refuse redirects to another origin.

- Prevent repository-lineage fetches from launching automatic Git maintenance that
  can outlive the cache lock and race later shallow fetches.

- Run all fifteen required CI checks in one fail-fast matrix, retaining check names,
  platform coverage, pinned tools and evidence uploads while cancelling unfinished
  checks after a sibling failure.

- Move the model-routing sample to the nested development workflow and routing
  catalog, with live public lock/plan/generate proof, current harness metadata,
  complete host-recipe composition and stale-workflow rejection.

- Bootstrap pinned contributor tools before the Python gate, including Conan for
  native package acceptance in a fresh session environment.
- Reconcile all current review and issue ownership, including native npm packaging,
  and preserve explicit acceptance gates for the full 1.1 review process.

- Construct deterministic native npm `.tgz` packages for `package-npm` Components,
  binding the exact accepted product closure, authored specification, source and
  resolved CycloneDX documents, package metadata, executable modes and archive
  digest. Independent verification rejects stale plans, changed bytes and
  undeclared tar entries without installing or publishing (#411).

- Show bounded, redacted coding-provider failure details in Standard sample errors.
- Reconcile quota fallback proof for source generation and JSON tasks, including
  explicit provider pins and resolved model-scope restrictions.
- Verify multi-output skill authoring through initialized-project CLI validation,
  including refusal of output paths outside the generated source boundary.

- Retry persistent-service readiness with bounded one-second connection attempts,
  so one TCP timeout before the service begins listening cannot consume the whole
  startup deadline. Ordinary acceptance requests retain their declared timeout.

- Component Markdown preserves colon-bearing scalar list values when rendering and reparsing authored documents, including namespaced symbols, digests and URLs (#432).

- Route one canonical Component lifecycle DAG across exact workers. Assignments bind
  the plan, lock, private catalog and locked Flavor selections; command and SSH
  handlers execute scheduler-owned nodes only after verified predecessor custody.
  Result correlation, failed-predecessor blocking, cancellation, and exact-current
  recovery fail closed without changing the existing local lifecycle path.

- Preserve retained Cargo rollback when a concurrent process creates a package
  destination during publication, and report that race through the retained
  materialization boundary without relabeling unrelated lifecycle failures.

- Preserve retained Cargo input custody when older Cargo rewrites cache-tag
  timestamps. Keep exact marker bytes and physical file identity checked, and
  retain strict timestamp checks for all package source files.

- Stop and reap owned release-gate process trees on interruption as well as
  timeout, including captured-output gates. Finalize unfinished descendant
  evidence after termination while preserving completed child results.
  Preserve Unicode characters split across pipe reads in returned and retained
  output for both captured and streamed gates.
  Keep the command deadline active while draining output after the root exits.

- Include the shipped Go authoring skill in initialization and framework updates,
  so adopting the Go Flavor preserves its required inputs and passes validation.
  Real Git upgrade tests cover old projects, local conflicts and exact rollback.

- Plan explicit parent-follow updates against the prospective catalog without
  changing the child. Match apply's inherited classifications and parent selectors,
  preserve local conflicts, and reject stale parent or project authority.

- Compare finite application numbers by exact value across integer and floating
  JSON representations, including persistent-service HTTP responses, while retaining
  original invocation and result bytes.
  Reopened library evidence validates the actual recorded result; booleans, signed
  zero changes, rounding differences and non-finite values remain distinct or invalid.

- Preserve verified Windows external delay-import absence as conditional runtime
  evidence. SDK-owned missing imports, missing load-time dependencies, inconsistent
  delay metadata and unverified external image bytes still refuse. Unavailable
  API-set hosts require matching delay-only external importers.
- Expose exact native SDK snapshot and target identities through a read-only
  Python runtime binding after SDK custody verification. Generated consumers and
  independent acceptance can compare these identities without transcribing opaque
  hashes. Admit the framework-supplied Python import only through the verified SDK
  dependency path, retaining ordinary import and package-declaration checks.
  Preserve bounded diagnostics when public rebuild wraps acceptance errors.

- Compile Standard SDK consumers with verified SDK custody and current build grants.
  SDK dependency identities separate build caches; independent readers distinguish
  retained compilation evidence from native runtime execution. Missing or revoked
  grants and changed SDK bytes refuse before new artifact publication. Full live
  generated-application qualification remains open.

- Carry native SDK imports across linked Component runtime execution using exact
  artifact, plan and command scope, with live producer revocation checks and
  independently readable scope records. Declared library providers may expose
  directory exports during unpackaged execution. Direct generation SDK ownership
  remains unchanged; full generated-application qualification remains open.

- Acquire locked native SDK dependencies during acknowledged Standard rebuild
  preparation and carry verified inputs through generation and runtime assembly.
  Missing tools, incompatible targets and unavailable source-intelligence policy
  refuse before compilation. Python generation guidance permits declared SDK
  imports and reserves runtime test claims for authorized lifecycle evidence.
  Full generated consumer and linked-provider execution qualification remains open.

- Add explicit SDK import manifests to Standard's command path. The isolated Python
  driver verifies SDK files before application import, while fresh command grants
  bind the observed runtime and check current revocations around each launch.
  SDK-aware qualification verification and public generated/package execution
  remain under development.

- Retain verified native SDK files, executable modes and portable input manifests in
  Standard directory packages. Relocated packages verify their exact SDK file table
  without producer storage. SDK files are supplied by the package; native loader
  environments still require fresh host validation before execution.

- Record verified SDK products and their complete native dependency graphs in the
  Standard resolved CycloneDX BOM. Bind each SDK to its source admission and consumer,
  preserve shared/transitive repository evidence, and use relative SDK image paths
  across relocations. Generated SDK imports, tests and packaged execution remain open.

- Bind exact completed SDK inputs into Standard build requests, intents and composite
  materialization authority. Changed custody, altered input lists and foreign grants
  refuse before backend dispatch; plans without SDK inputs preserve their wire form.
  SDK imports, generated tests and packaged consumer execution remain pending.

- Bind completed native SDK source builds to exact consumer dependencies and targets,
  retain their input manifests, and materialize fresh verified SDK directories with
  relocation-time runtime observation and cleanup. Consumer input preparation grants
  no execution permission; Standard import and packaging integration remains open.

- Recheck captured generation authority using its admitted exact repository locks
  without fetching source again. Local file, lock and audit drift still refuse;
  explicit new lock/read operations continue to resolve repository selectors.

- Compose native SDK source builds with actual captured-file inspection, policy
  authorization, live revocation checks and quarantine cache admission. The retained
  native product is available to subsequent lifecycle integration; generated
  Standard consumer imports and ABI/API acceptance remain under development.

- Select native SDK build recipes from pinned Flavor builder contributions. Recipe
  validation binds public integration contracts, targets, licenses, commands and
  named tools; deterministic planning retains the selected authority before build.
  Standard consumer SDK admission remains under development.

- Resolve Component repository source selectors to exact Git source locks through
  `litai lock`, including public integration-contract bytes. Locking runs no build;
  admission reacquires the locked commit and rejects changed source identities.
  Standard generation still requires the pending SDK admission integration.

- Standard portable and library acceptance now preserve finite fractional JSON
  arguments and results through invocation, exact comparison and evidence identity.
  Integer-only acceptance identities remain unchanged; canonical contract JSON v1
  still rejects floats, and non-finite product values fail before acceptance.

- Keep neutral CUDA recipe-composition tests independent of live GPU tooling,
  while checking both CUDA sample recipes and their bound execution documents.
  Live CUDA execution still requires its measured hardware and installed stack.

- Capture compiler runtime file aliases and resolved target bytes under bounded
  custody, supporting distro Rust library symlinks. Recheck links, files and parents
  around retained tests; generated runtime directories still reject symlinks.
  The native fixture uses build-script syntax compatible with Cargo 1.75.

- Declare the native Cargo regression fixture's library target name explicitly,
  so Cargo 1.75 metadata and newer toolchains match the same reviewed target.
  Production target-name drift checks remain exact.

- Bind each retained-Cargo test's composed Linux environment to native dependency
  guards, including measured Rust library directories and package overlays.
  Six native Linux C cases verify the underlying loader precedence, inheritance
  and absolute-import behavior; complete Linux Cargo admission remains open.

- Pass each retained-Cargo test target's composed macOS environment into native
  observation, including compiler/runtime paths and package overlays. Unsupported
  loader controls fail before the test process rather than being ignored.

- Connect per-target native graph and file guards to internal retained-Cargo test
  discovery and execution. Bind observed native authority to process observations;
  complete caller loader-environment coverage and durable admission remain open.

- Observe explicit Linux `LD_LIBRARY_PATH` after inherited `RPATH` and before
  direct `RUNPATH`, preserving sanitized inspector context. Freeze the bounded
  absolute-path selection for retained native re-observation; reject unmodeled
  loader controls and context-dependent paths.

- Preserve inherited ELF `RPATH` directories in each executable's dependency
  traversal, with `$ORIGIN` tied to the declaring image. Keep `RUNPATH` direct-only
  and separate application, inspector and interpreter traversal contexts.

- Honor ELF `RUNPATH` over `RPATH`, including an empty `RUNPATH` tag. Refuse
  nonempty loader paths with empty directory entries instead of silently removing
  their dependence on the process working directory.

- Resolve ELF imports containing a slash as direct paths, including `$ORIGIN`
  expansion, without loader-cache or library-root fallback. Refuse imports needing
  an unmodeled working directory or dynamic token. Complete Linux search-path and
  execution-environment qualification remains open.

- Observe explicit macOS library/framework override and fallback paths in application
  dependency graphs while preserving inspector-tool context. Freeze path selection
  during native re-observation and reject unmodeled loader controls. Complete dyld
  policy and retained-consumer admission remain under development.

- Apply the same explicit-library-root bounds to direct Linux and Windows native
  observers as to the portable and macOS observers. Reject relative paths, parent
  traversal and more than 128 supplied roots before invoking inspectors.

- Qualify an installed 1.0.1-to-1.1 candidate upgrade on the macOS Python starter.
  After installing the candidate wheel, use `litai update --follow-ref REVISION
  --apply --adopt-added`, review and apply `project lifecycle rebind-standard`,
  refresh the Component lock and documentation review, then rebuild and verify the
  new receipt. The prior receipt correctly becomes stale. Live baseline and
  candidate rebuilds each passed three tests at candidate `a98277dd`; final release
  qualification remains required. The published 0.9.0 CLI still cannot complete the
  separate historical 0.8.4 upgrade because its origin comparison rejects equivalent
  HTTPS/SSH URLs and it lacks a Standard rebind command; no pins were hand-edited.

- Compose native dependency observation with file custody and fresh graph checks.
  Detect changed system-image records and dependency edges at execution boundaries,
  even when materialized file bytes are unchanged.

- Add materialized native-file custody tied to observed dependency hashes, physical
  paths and Mach-O loader links. Reject file, parent and link drift while keeping
  unmaterialized system-image components explicit for separate host qualification.

- Allow native dependency observation to select exact artifact files at their
  original paths. Reject unsafe or non-native selections and preserve whole-tree
  discovery for existing callers.

- Capture recursive Cargo build-script output trees before retained test execution.
  Bind generated runtime data to executable authority and reject file, directory
  or parent replacement around test processes, with shared entry and byte limits.

- Supply package-specific Cargo build-script environment values to retained test
  binaries and bind them to executable authority. Reject malformed, oversized or
  ambiguous projections and package-directory overrides.

- Resolve Windows native dependencies using an explicitly supplied runtime
  environment, with no ambient repair of missing settings. Reject ambiguous keys
  and unsafe paths while preserving existing callers that use the host environment.

- Allow portable native dependency observation to use explicit library search roots,
  including dynamically linked Rust libraries without embedded Mach-O search paths.
  Preserve rejection of ambiguous library resolution.

- Let retained-Cargo gate policies declare external input directories through
  execution environment variables. Capture their files before consumer gates and
  reject missing inputs, drift and tool overrides without changing provider
  qualification for ordinary external-source edits.

- Conan packaging verifies the exact restored binary revision and payload membership,
  file hashes, sizes and executable modes. It rejects ambiguous catalogs and native
  metadata collisions instead of accepting a matching package name alone.

- Preserve replaced lifecycle lock markers, refuse further child commands after
  ownership changes, and release owned reservations when receipt setup fails.

- Refresh staging can arm recovery before logical application and retain all
  before-state after normal exit or failure, including a partial journal write.
  Complete live application now validates terminal commit or conservative rollback
  before owned staging cleanup.
- Add explicit retained-child lifecycle planning and sequential execution for
  independent Gitlink roots. Bind commands to current pins and a native target,
  retain logs and product hashes, propagate failures, terminate handled cancelled
  process trees, and verify outputs before resuming. Hydrated Git LFS inputs are
  compared with their indexed content hashes. Multi-worker dispatch, coherent
  refresh and independent product acceptance remain separate work.
  Share filter-free LFS verification with refresh preflight, including file
  identity, mode and hash checks; retain refusal of real edits and staged work.
  Record cumulative child command time, including failed or interrupted commands,
  rather than reporting only the last command's duration.
- Add a reviewed `project retained-harness readmit` transaction for conversions whose
  original source still owns release authority, including retained projects whose
  detected commands or final inventory metadata were wrong. Require
  fresh direct and generated-wrapper qualification before atomically rebinding the
  inventory, shim authority, Component lock, receipt policy, and evidence while
  preserving conversion history. Bind remote worker selection into the reviewed plan,
  refuse unsupported remote workspace links, treat a standalone `BUILD` or
  `BUILD.bazel` file as a package file rather than a Bazel workspace, and derive
  allow-unready policies from the final admitted inventory.

- Make non-local `project test-receipt run-retained --worker-id` selections execute
  on the exact configured POSIX SSH worker. Add `--worker-config`, a bounded internal
  retained receiver, source/request/worker identity checks, sanitized remote phases,
  actual remote platform evidence, and attempt cleanup while preserving `local`.
  Bridge installed-worker version skew by staging a bounded deterministic copy of
  the coordinator's `literate_ai` package, binding and rechecking its exact archive,
  and running the configured launcher with only that archive on `PYTHONPATH` without
  updating the worker installation. Preserve canonical sanitized receiver errors at
  the controller boundary so failed remote attempts retain their actionable code, and
  report non-portable retained source projections as typed failures before transport.
  Admit identity-bound retained inventories up to 32 MiB independently of the 1 MiB
  control-result bound, and keep runtime-bridge Python controls out of retained child
  processes. Carry pinned, digest-verified Linux `uv` and Ninja binaries in the same
  runtime archive when admitted stages or their evidence scripts require them, expose
  them only to retained child gates, and bind those exact requirements into readmission
  plans. Preserve up to 32 KiB of redacted receiver diagnostics so dependency setup
  cannot hide a failing test tail. Allow mandatory cleanup up to five minutes for
  large generated trees. Full remote evidence-bundle custody and remote external
  workspace links remain open.

- Capture compiler and generated runtime-library search directories for retained
  Cargo test execution. Support dynamically linked Rust test harnesses and refuse
  runtime-file drift before accepting their observations.

- Execute reviewed retained-Cargo test inventories after the original consumer gates,
  using fresh compilation and binary custody around every per-target list/run.
  Retain failed observations and refuse binary or inventory drift. Public consumer
  admission and durable receipts remain under development.

- Add reviewed retained-Cargo test inventories bound to the importer, workspace
  plan and gate policy. Require complete target coverage and positive expected
  cases, independently of test-binary discovery, before guarded test admission.

- Add internal Cargo test-artifact matching and strict per-binary named-result
  checks. Preserve explicit all-target coverage even for `test = false` targets,
  and refuse missing, duplicated, ignored or filtered results. Consumer receipt
  finalization remains separate integration work.

- Require captured consumer inputs for internal retained-Cargo execution, including
  provisioned registry/Git sources and external Cargo configuration. Refuse metadata
  paths outside that capture and later source, configuration or environment drift.
  Support Cargo's internal Git pack hard links only when all aliases remain captured.

- Allow retained-Cargo checks and provisioning to read exact qualification archives
  from an explicitly selected HTTPS evidence store, with optional environment-based
  bearer authentication and offline refusal. Remote bytes use the same qualification
  checks as local archives; failed delivery never falls back to another source.

- Bind retained-Cargo gate policy to its importing project and reviewed commands
  and tools, separately from mutable consumer source. Reject foreign-project
  policies and repository-dependency plans at this boundary.

- Observe retained package and consumer file identity with fresh no-follow stat
  calls on Windows, preserving hard-link and replacement detection.

- Add explicit retained-Cargo `check` and `materialize` CLI operations for reviewed
  local archives and current provider authority. Read-only checks leave project
  files unchanged; provisioning preserves exact existing packages and foreign work.
  Consumer execution, admission receipts and source retirement remain separate.

- Add bounded local retained-Cargo consumer file custody, including local Cargo
  configuration and detection of additions, removals and physical replacements.

- Require observer custody for every measured tool used by retained-Cargo execution;
  an asserted tool identity alone no longer reaches the process runner.

- Add bounded retained-Cargo execution under package/input custody, with reviewed
  metadata checks around the ordered gates, measured tools and preserved failed
  process observations. Positive test acceptance and source transfer remain separate.

- Add read-only retained-Cargo importer authority composition from explicitly
  reviewed build-plan files and current provider/tool measurements, preserving
  full gate order and refusing later file or tool drift.

- Add internal retained-Cargo package provisioning with verified archive bytes, exact
  existing-package reuse, exclusive publication and rollback that preserves foreign
  changes. The returned custody guard still requires native consumer qualification;
  this does not complete importer admission or source retirement.

- Reopen retained library command authority without requesting an executable
  entrypoint. Verify every library command record and preserve executable
  entrypoint verification for executable contracts.

- Add an internal directory-publication primitive that refuses concurrent destination
  creation without replacing existing files or directories. Retained-package
  materializer composition and cross-platform qualification remain open.

- Verify retained Cargo authority in a fresh checkout before package provisioning.
  Check authored manifest/lock bytes and absent artifact destinations without writes;
  preserve existing or concurrently created files and directories.

- Distinguish retained Cargo package roots and package names from artifact destinations
  and Rust import names. Require exact reviewed package manifests when reopening
  qualified Cargo archives; filesystem composition and native consumer execution
  remain unfinished.

- Retain native Cargo build and dependency evidence for library qualification,
  and verify it after generated workspaces have been removed.

- Retain the selected generated-source index evidence during library qualification
  and validate its exact component and source bindings when reopening retained products.

- Standardize the Make Flavor profile filename as `standard-command-profile.json`
  in the shipped catalog and newly initialized projects, preserving its commands.

- Preserve native library import declarations through source-derived graph review
  and promotion. Signed reviews bind the names; graph combination rejects conflicts,
  while graphs without declarations retain their existing identities.

- Support reviewed native library import declarations in Component authoring, so
  generation and execution can agree on package, module and symbol names that differ
  from Component naming conventions. Components without these declarations keep
  their existing identities.

- Capture importable-library build evidence without requesting an executable
  entrypoint contract, allowing retained library qualification to pass build intent.

- Resolve retained providers’ native commands and independent library oracles from
  current locked authority. Preserve bounded oracle/harness custody and reject
  changed toolchains, import surfaces, cases or files before archive admission.

- Repair retained-library schema references so installed catalog validation and
  HTML authority-graph rendering can resolve the new contracts.

- Add `spec qualify --model` to bind an explicit pipeline model in both public
  planning and every clean Standard runtime. Existing authored model scopes still
  apply; omitting the flag preserves the configured CLI default.

- Compose retained-provider admission from current project, locked generation,
  parity profile, persisted promotion and measured installed Standard authority.
  Refuse changed project or installed payload bytes throughout revalidation.
  Native tool/oracle resolution and transactional consumer execution remain open.

- Derive retained-provider generation closure from current locked references and
  verified audit files. Require audited entries for every selected Flavor and
  recheck input custody before returning the closure; full admission remains open.

- Reopen current qualified provider heads and persisted promotion evidence with a
  repeatable filesystem guard. Reject invalidation, changed inputs and concurrent
  head changes without appending authority or altering evidence. Full importer
  composition and native consumer execution remain required.

- Resolve current qualification verifier authority from bounded, guarded profile
  files. Share the existing verifier and case-map derivation across execution and
  archive reopening, so current expectations need no original source tree or model
  execution. Full provider admission remains unfinished.

- Reopen current retained-library provider recipes from filesystem lock authority
  without invoking a model or creating generation output. Preserve each node's
  current model binding and refuse input drift before returning the recipe map.
  Provider promotion/lifecycle, tool/gate resolution and consumption remain open.

- Verify the retained-product and observability integration at `495ab34b`: all
  4,811 tests pass with 30 skips, with terminal evidence and output hashes checked.
  Publication and importer follow-ups require separate full qualification.

- Add read-only filesystem custody for retained-Cargo binding, plan and manifest
  inputs. Verify exact before/after bytes and required absences; refuse links and
  concurrent additions while preserving foreign files. Provider/tool/gate resolution
  and transactional consumption remain required.

- Compose retained-Cargo importer preflight with bounded archive and product
  verification. Recheck input custody and current authority before returning
  package bytes; reject changed inputs, substituted archives and missing evidence.
  The filesystem resolver and transactional consumer path remain unfinished.

- Revalidate existing qualified provider projections without creating another
  qualification event. Retained-Cargo input preflight now checks independently
  reviewed importer authority, current provider qualification, tools and exact gate
  commands. Archive verification and filesystem custody remain required.

- Add a pinned retained-Cargo workspace plan for manifest and lockfile changes,
  native graph expectations, tool identities, target/features and consumer commands.
  Reopening rejects substituted plan bytes, mismatched library destinations and
  overlapping Cargo output. Current qualification and execution remain required.

- Add a retained-library importer binding contract that pins qualification evidence,
  reviewed policy and workspace-plan references, and destinations for every native
  export. Reject missing exports, overlapping paths and ambient store URLs. This
  records intent; current-evidence admission and materialization remain required.

- Preserve requested lifecycle parallelism through remote worker dispatch, with
  bounded request validation and unchanged retained-artifact replay authority.

- Allow installed HTML qualification up to five minutes per render command so
  Windows can complete both required currentness reads, retaining timeout diagnostics.

- Avoid a discarded preliminary source collection during HTML rendering. Build
  the artifact from its first source read and retain currentness checks before
  publication and at the final write boundary. Source-stage diagnostics now cover
  each actual emission read.

- Read Git index location and object format in one fresh metadata command during
  repository inspection. Keep independent inventory rechecks and index validation
  while reducing process launches.

- Wrap long verifier diagnostics inside expanded HTML detail panels so content
  identities and diagnostic codes remain readable on narrow screens.

- Allow Standard library qualification to retain captured packages and evidence in
  an explicitly selected local immutable store. Stored bytes are verified before
  scratch cleanup; publication failure prevents admission. Successful output names
  exact archive and run/export identities. Importer trust and source retirement
  remain separate requirements.

- Retain the last installed HTML child diagnostics when wheel qualification times
  out, and add opt-in render-stage timing for source, emission, wheel/catalog
  observation and publication. The command deadline and verification checks remain
  unchanged; this improves diagnosis without claiming the Windows timeout is fixed.

- Add an offline verification-health dashboard with per-gate diagnostics and
  separate pass, fail and skipped counts. It shares the CLI verifier and explicitly
  excludes the HTML-artifact gate to avoid recursive freshness checks; full
  verification continues to check retained HTML artifacts.

- Fix reopening of qualified source-promotion evidence: compare typed lifecycle
  identities consistently so correctly pinned qualification is accepted.

- Include exact captured package bytes in retained qualification evidence output
  so producer output can be archived without separately reconstructing its contents.

- Add internal canonical archive transport for retained qualification records and
  package bytes, with pinned archive identity, bounded reads and record checks.

- Refuse retained qualification capture when provider authority, Standard binding
  or the independent oracle changes during package reads.

- Reject ambiguous qualification parity results: duplicate JSON keys, non-finite
  numbers and boolean/number substitutions no longer count as matching output.
  Matching finite decimals and large integers remain valid; whitespace and
  object-key order remain immaterial.
  Retained-product reopening now recomputes parity from bounded raw observations
  against the current profile, exact commands and complete clean-run case map.
  It also requires the original baseline inventory retained under its pinned
  snapshot identity.
  Every retained run must match the caller's current Standard driver, lifecycle
  policy and framework distribution before product bytes are returned.

- Add internal bounded retention and reopening of qualification records and exact
  library package bytes after scratch cleanup, including npm process/build and
  dependency records, both artifact tree manifests, and exact build metadata
  bytes. Reopening checks the exact artifact-tree transition, resolved SBOM
  identity and metadata references before exposing package bytes. Each run also
  binds its retained source custody and candidate to the accepted generation
  result. Producer capture also preserves the existing CAS source bundle,
  generation manifest, file bytes and final model-stage record before cleanup.
  The existing invocation, execution plan, stage request and selected route
  records are also preserved for generation-authority verification.
  Reopening binds the final stage, selected route and invocation to accepted
  provenance and source identities before exposing retained package bytes.
  It also binds source-index, build-request, intent and historical grant records
  to each realized plan, without authorizing new execution from old grants.
  Product reopening now requires current command contracts for all locked
  Components and verifies retained commands, tool and export bindings against them.
  Every clean run must also reopen packaged-library test and smoke records, with
  complete passing case results and observable execution output.
  Every retained-product run reopens that source closure without CAS access,
  checking file sizes and the declared BOM/test-suite paths before package reads.
  Complete lifecycle membership for
  every clean run, command/npm build, generated-test and execution process/output
  records, current independent library
  acceptance, source custody,
  root/package and receipt binding checks support
  the unfinished retained-library bridge. Explicit local publication is available;
  importer admission remains unfinished.

- Recheck installed Standard authority and verifier-owned acceptance before
  exposing retained products. Final regenerative qualification admission repeats
  those checks and reopens promotion evidence, refusing drift during a run.
  Producer capture also validates retained generated-suite bytes and exact case
  membership against recipes projected from current locked authority. Retained
  product reopening requires those current recipes for every Component and clean
  run, including matching source-candidate and provenance recipe identities.
  It revalidates raw source/resolved CycloneDX documents and their transition
  against current locked dependency graphs before exposing package bytes.

- Fix retained-bundle file delivery on Windows Python 3.12 when file creation
  and metadata-change times differ. Compare matching timestamp meanings across
  pathname and descriptor queries while retaining both mutation checks.

- Stop claiming that Rust names found by static inventory are proven public APIs.
  Preserve them as candidates requiring review of public reachability.

- Add durable single-use build-grant admission for trusted launcher composition.
  Replay, missing storage, and revocation or expiry during admission refuse before
  launch; this prerequisite does not enable a production containment backend.
  Production build requests can bind the exact isolation policy and runtime inputs;
  substituting them requires a new authorization.
  A Linux cgroup preflight rejects missing or unlimited provisioned budgets without
  creating a hierarchy, changing limits or launching work.

- Add an internal retained-Cargo graph check for exact local packages, dependency
  edges, features, workspace membership and preserved build/test targets. It prepares
  importer validation without enabling source retirement or qualifying artifacts.

- Add an offline lock-health HTML view with per-Component lock and resolution-audit
  reports. Verification and HTML share currentness checks, provider diagnostics and
  refusals; stale inputs and edited output invalidate retained views. Installed
  qualification retains current, not-current, error and empty fixture artifacts
  with their exact reports for browser inspection.

- Checking missing matrix-scoped Component locks and resolution audits no longer
  creates storage directories. Explicit updates still create their owned storage,
  and reads reject unsafe storage ancestors. Shared lock operations retain the same
  input and artifact revalidation for CLI and observability consumers. Verification
  retains detailed internal lock reports without changing its public gate results.

- Add a single-file version-check HTML health view over the canonical version
  report. Expandable results and diagnostics work offline without JavaScript;
  exact report and renderer bindings detect stale or edited output.

- Add bounded retained-bundle delivery from explicit files or configured HTTPS CAS
  endpoints. Offline mode refuses network delivery; exact archive validation preserves
  bytes and modes without extraction or qualification admission.

- Add a retained-library export-set contract that binds the existing artifact graph,
  selected library link plan and complete import metadata. It preserves native
  dependency closure and remains separate from qualification and Cargo consumption.

- Add compact signed-evidence map preparation and verified immutable bundle storage.
  Signed matrices must bind canonical finalized receipts that pass existing project
  and suite checks. Explicit publication retains the full bundle and atomically
  replaces the current map after fresh verification; authenticated current-receipt
  gates and optional release-policy binding recheck that evidence on use.

- Add `litai project test-receipt verify-evidence` to authenticate complete retained
  evidence against an independent project-bound plan and signer policy. It rechecks
  revocations and authority after retrieval and leaves the current receipt unchanged.

- Dependency observation now records Python import aliases from verified installed
  payloads, allowing distributions such as PyJWT to declare their `jwt` module
  without weakening source-BOM reconciliation.

- CI identity: add a bounded GitHub OIDC live verifier with exact issuer, audience,
  repository/workflow/job scope and fresh key/token checks. Token headers cannot
  discover keys. Fresh key loading verifies fixed GitHub HTTPS endpoints and rejects
  stale, malformed or redirected responses. Live evidence checks now require the
  exact envelope signature and expected run details as well as the CI token.
  Persistent receipt integration remains open.

- Retained evidence verification now authenticates store claims before retrieval and
  rechecks their deadlines and signers afterward. Detached proofs and run evidence
  share resource limits, and missing proof or unavailable bytes refuse verification.

- Signed run-graph verification now checks exact planned matrix/platform/derivation
  links and named artifacts, bounds signature work across the whole graph, and
  rechecks trusted time and revocations after storage reads.

- Evidence resolution now supports one cumulative object/byte budget across graph
  reads, retains exact verified bytes for shared objects, and refuses further reads
  after a failed batch.

- Evidence policy: add explicit pinned-key signer/issuer scope, revocations, validity
  and retention checks against independently required run context and matrix coverage.
  Correct signatures cannot admit substituted subjects, stale runs or prior invocations.
  Receipt promotion remains a separate requirement.

- Tests: verify inactivity-heartbeat resets with deterministic clocked observations
  on both output streams, keeping production deadlines and real-process cleanup tests.

- Evidence storage: add immutable filesystem, monorepo and HTTPS stores, with explicit
  routing, bounded reads, conditional remote publication and resolver-side integrity
  checks. Conflicting locators, wrong media, redirects and corrupted mirrors fail closed.

- Evidence: define closed derivation, platform, matrix and locator predicates in the
  public schema catalog, with in-toto subject binding and exact-byte DSSE verification.
  Matrix records reject missing or duplicated declared cells. Signature-authenticated
  assertions still require independent trust-policy and evidence-closure admission.

- Workers: support locked dependency wheels containing canonical empty ZIP directory
  entries, with explicit rejection of colliding paths and disguised directory payloads.

- Evidence: add bounded DSSE envelopes and local Ed25519 signing/verification over
  explicitly trusted public keys. Signature verification returns the exact payload
  bytes and cannot be upgraded through key hints or duplicate signatures.

- Storage: reject oversized or growing content-addressed files with bounded reads
  before returning evidence bytes, while retaining digest verification.

- Security: expose the isolation policy, request, observation and decision contracts
  through the public security API and current schema catalog. Local reports remain
  explicitly unauthenticated and cannot authorize execution.

- JavaScript library acceptance: retain the exact Node verifier binding alongside
  command-driver bindings, reject missing or extra launchers, and revalidate the
  selected launcher before and after independent function evaluation.

- Release engineering: recognize retained, already-merged topic worktrees from
  any checkout using exact local revisions and remote-default ancestry. Preserve
  dirty/prunable worktree blockers and reject branch or reference drift.

- Repository lineage: qualify per-entry cache serialization, independent fetches,
  concurrent snapshot reads and abrupt-owner recovery on Linux, macOS and Windows;
  reconcile the remaining #324 roadmap evidence with the landed hosted matrix.

- Orchestration: add internal, exact commit-only authority-delta preparation for
  reviewed refresh. Publication checks, transactional apply and public refresh
  commands remain separate unfinished stages.
- Orchestration: prepare read-only root/child refresh observations with exact
  manifest, index, lock and HEAD custody; reject dirty/missing children, hidden
  index flags, external filters, ongoing Git operations and changed inputs.
  Detect split indexes before Git can refresh shared-index timestamps; support
  for nonmutating inspection of this index mode remains unfinished.
- Orchestration: add an internal bounded Git publication verifier over fresh
  disposable storage. Require advertised-history reachability, not local object
  availability; detect remote-reference drift without writing child checkouts.
  Bind internal refresh proofs to root-owned URL resolution and exact requested
  pins, revalidating custody and remote observations. Protect root/child metadata
  from scratch-storage placement. Acknowledged apply and the public command remain open.
- Orchestration: add internal acknowledged Git/repository-lock write reservations
  bound to exact refresh custody, including resolved symbolic references. Acquire
  deduplicated markers in fixed order, revalidate live ownership, and remove only
  matching owned markers and new empty directories. Retain foreign or crash-left
  markers. Preserve exact target spelling before deduplication so Windows path
  equality cannot hide portable case aliases or conflicting anchor spelling.
  Integrate root/child manifest and lifecycle advisory locks without
  rewriting retained lock files or hiding foreign metadata-directory contents.
  Renew reviewed publication proofs under the complete reservation set with
  acknowledgement bound to exact local and remote evidence; reject stale handles
  and input drift. Transactional apply and rollback remain unfinished.
- Orchestration: capture bounded raw published trees in disposable storage and
  verify commit, tree and blob hashes before deriving immutable path deltas.
  Preserve executable modes, symlink payloads, Gitlinks and empty directory records
  without checkout, filters or child execution. Bind complete multi-child capture
  to live root ownership and reviewed proofs with aggregate resource limits.
  Physical-file staging and apply/rollback remain open.
- Orchestration: add internal physical before-file custody and collision preflight
  under live refresh ownership. Retain exact disk bytes rather than normalized Git
  blobs; preserve ignored data, inspect links without following them and reject
  unsafe nodes, path aliases and transaction-metadata overlaps. Bound old-object
  and physical capture across children and refuse stale plans. Observe directory
  members freshly without following links, preserving Windows file identities
  instead of relying on cached enumeration metadata. This does not yet
  apply or roll back refreshes, expose a public refresh command or qualify releases.
- Orchestration: stage exact rollback inputs, inert prospective file/link bytes and
  root/child indexes under exclusive live ownership. Validate index checksums and
  formats, preserve unrelated entries and recovery data, and refuse unsupported
  extensions. Bound staging bytes, retain crash-left or altered staging and clean
  only owned files. Live source/ref/pin application and rollback remain unfinished.
- Orchestration: export bounded, self-contained history packs for proven commits
  and validate their object connectivity in an independent empty Git store. Bind
  complete multi-child exports to reviewed proofs and physical custody, then add
  content-addressed pack/index files without replacing existing data or advancing
  refs, indexes or pins. Owned keep markers protect installation; immutable object
  cache additions may remain after failure or rollback. Public refresh stays pending.
- Orchestration: bind complete symbolic-ref chains and loose/packed backing bytes
  to refresh custody, reserving intermediate refs and the packed-ref writer too.
  Stage inert ref/manifest transitions and explicit repository-lock invalidation;
  preserve exact no-op bytes and reject conflicting shared-ref changes across
  observed worktrees. Live application, recovery and public refresh remain open.
- Orchestration: bind registered-worktree listings, directory identities and
  HEAD/registration bytes to refresh custody. Refuse prospective shared-ref updates
  affecting unobserved checkouts, active foreign operations or unavailable
  registrations; preserve unrelated idle worktrees without inspecting their source
  or repairing registrations. Changed registry state invalidates reviewed custody.
- Orchestration: bind metadata-parent identities and stage bounded, append-only
  reflog transitions under the current Git logging policy. Preserve original logs,
  disabled/missing logs and exact no-ops; deduplicate shared-log updates using one
  root committer identity and timestamp. Reserve applicable log writers and normalize
  only proved owner-created directories. Refuse parent substitution during live use
  and retain changed directories during cleanup. Live apply and rollback remain open.

- Conversion harness: apply one explicit disposable Git environment to identity
  setup and phase commands, excluding inherited Git controls while preserving
  non-Git toolchain settings and the invoking process's environment. Qualify the
  exact candidate on the complete Linux/macOS/Windows matrix; native nested
  linbuild acceptance remains a separate open obligation.

- Roadmap: reconcile shipped cache/origin, acceptance-before-publication,
  retry-key, workspace-link and inherited release-skill fixes with historical
  validation, including duplicate queue items. Record merged quality-gate evidence
  for single-writer cache composition and public deliberate cache promotion.
  Expose unfinished mitigation phases as explicit checklists; current 1.1 live
  qualification and other unresolved obligations remain open.

- Git qualification fixtures: write LF source bytes explicitly before committing,
  and exercise Windows text defaults even on non-Windows test hosts. Git's
  no-conversion byte-preservation assertions remain unchanged. Disable automatic
  post-receive maintenance in disposable publication receivers so background Git
  lock cleanup cannot race complete no-write snapshots.
- Lock qualification fixtures: inspect complete bytes outside held Windows
  byte-range locks, retain live exclusion checks and distinguish existing-marker
  handoff from typed cleanup refusal for a new marker with an open waiter handle.
  Production reservation and conservative retention behavior are unchanged.

- Parent updates: preserve import timestamps for identical source/file provenance
  and avoid rewriting an unchanged imports file. Changed provenance still receives
  a fresh timestamp, and final validation remains mandatory.

- Health-probe tests: stop and join the owned server thread before closing its
  socket, including cleanup after a failed test body; a failed thread start still
  closes the socket without waiting for a server that never ran.

- Portable qualification fixtures: preserve exact Git checkout bytes despite a host
  CRLF default, and mutate rendered HTML as explicit UTF-8 instead of the host code
  page. Runtime byte-custody and staleness checks remain unchanged.

- Orchestration tests: disable automatic Git maintenance only in fixture commands
  so setup-time background writers cannot race strict metadata-preservation checks.
  Keep the full Git metadata snapshots; do not exclude transient files from evidence.

- Orchestration CLI: compose root repository and root-owned Component locks through
  `lock`, require current locks for `plan`, and check the repository lock during
  `verify --gate locks`. Preserve independent child catalogs, expose exact pins and
  suppress implicit host side effects. Component check/diff no longer acquires a
  writing mutex. These commands do not refresh pins or qualify child releases.

- Onboarding: retain an empty declared `components/` catalog across Git clone,
  avoiding a layout-validation failure when the example lives under `samples/`.

- Orchestration lock custody: add bounded canonical root lock storage with no-write
  reads/checks, no-op preservation, fail-fast writer exclusion, no-clobber creation
  and atomic stale-lock replacement. Detect input and destination drift, preserve
  concurrent edits and report retained staging or unqualified post-write state.

- Orchestration groundwork: define a portable repository-lock contract and read-only
  candidate preparation. Bind reviewed root authority, its graph and exact declared
  child pins; keep checkout observations separate, refuse manifest/index drift and
  reobserve inputs without refreshing authority. This is not child execution or
  release evidence.

- Orchestration graphs: expose exact declared child pins and independent repository
  boundaries in every authority-graph export. Relationship nodes preserve explicit
  consumer/provider roles, including cyclic declarations, without inventing a build
  order. Graph and HTML source inspection do not read child catalogs or verify live
  checkout/publication state. Reviewed refresh remains open.

- Portable input custody: apply independent pathname/descriptor clock checks to
  monorepo input and read-only reference-index readers as well as HTML publication.
  Preserve bounded reads, exact bytes, replacement refusal and no-write lookup behavior.

- HTML publication: compare pathname and descriptor observations using shared
  identity fields while checking each clock independently for drift. This handles
  Windows Python's differing ctime meanings without dropping either change check.
  Local regression and publication tests pass; hosted confirmation remains open.

- HTML publication: verify the created file and exact bytes after closing its
  writer, accommodating close-time timestamp finalization without relaxing
  no-clobber or concurrent-change checks. Make the installed-wheel parent-update
  fixture use explicit LF checkout bytes regardless of ambient Git CRLF settings.
  Local regressions pass; hosted Windows confirmation remains required.

- Orchestration reads: keep pathname and descriptor change clocks independently
  stable while binding their common file identity, size, mode and write time. Preserve
  exact configuration bytes across platforms and continue refusing either clock's drift.

- Skill admission: automatically render and evaluate both orchestration onboarding
  views when their templates or renderer change. Use the exact selected checkout
  assets and all six non-model checks; deleted templates and evaluator refusals fail
  the gate. CI discovery includes these generated views rather than relying on the
  generic template catalog's result.

- Orchestration initialization: add an explicitly reviewed, acknowledged root-only
  transaction. Bind exact planned files and project identity, preserve child Gitlinks
  and existing root files, validate before manifest-last publication, and remove only
  unchanged owned additions on failure. Concurrent replacements are preserved and
  incomplete rollback is reported. Root lock/plan integration, reviewed refresh and full
  release qualification remain open.

- Orchestration groundwork: prepare an exact, canonical root file set from an
  explicit project ID/version and typed child pins. Installed template resources
  supply root onboarding and isolated skill/documentation catalogs; no child
  catalogs, execution driver or test receipt are fabricated. The real validator
  accepts the planned files; the root-only transaction now consumes this file set.

- Orchestration: add a typed optional modern-project binding for exact child pins
  and explicit dependencies. Plan/check reports that persistent authority separately
  from local checkout observations; root catalogs and declared outputs cannot overlap
  child paths. Existing manifests keep their identities when the field is absent.
  Root lock/plan integration and reviewed refresh remain unfinished.

- Orchestration planning: expose read-only `litai onboard orchestrate plan|check`
  with explicit dependency declarations and exact inventory/declaration identities.
  Recompute reviewed plans and refuse drift without host configuration, telemetry,
  fetches, child execution or conversion mutations. Reviewed refresh remains
  unfinished; a current plan check is not build or publication qualification.

- Orchestration groundwork: add an internal read-only inventory of exact Gitlink
  pins, configured URLs/branches and child checkout revisions. Refuse inconsistent
  index/configuration and indirect inputs without fetching or changing repositories.
  Reviewed refresh remains unfinished; ordinary adoption behavior is unchanged.

- Release qualification: add a real local-parent update to the installed-wheel
  HTML probe. Require changed inherited authority, current/stale/current verdicts,
  and regenerated provenance bound to the new graph; retain the post-update HTML
  separately. The clean `23898155` wheel passes this journey, and its retained
  post-update artifact passes desktop/mobile interaction, offline and no-script
  inspection from an otherwise empty directory. Hosted qualification remains open.

- HTML observability: `litai verify` now checks declared artifacts against current
  source and the actual installed renderer. Missing, unreadable, unpinned, stale,
  or concurrently changed evidence fails; projects without declarations skip.
  Current verdicts require matching completed bytes, not borrowed provenance.
  Verification does not publish HTML, refresh timestamps, write cache telemetry,
  self-update the host, or initialize operator configuration. Admit exactly the ten
  HTML contracts now written by the CLI and project serializer.
- Agent guidance: add a compact HTML rendering/verification wrapper to both the
  framework catalog and initialized projects; generated HTML never becomes authority.

- HTML observability groundwork: add immutable staleness reports and optional
  project-declared render requests with bounded, case-insensitively unique output
  paths. Empty declarations preserve existing project identities; the read-only
  verification gate consumes these declarations.

- Release qualification: require the installed-wheel HTML CLI to bind the canonical
  graph and actual distribution, preserve cached bytes and observation time, and
  return nonzero typed refusals. Keep graph telemetry separate from no-write render
  cache tests. Preserve verified HTML and its typed artifact record outside the
  disposable workspace before retaining the qualified wheel manifest; this probe
  does not claim post-update browser acceptance.

- Release planning: make the complete submodule-orchestration acceptance scope
  explicit for 1.1, including root lock/plan/graph integration, reviewed child re-pins,
  Git-layout preservation and clean-clone qualification. The capability remains open.

- CI skill admission: install the framework before changed-skill discovery and
  propagate discovery failures instead of silently treating them as no changes.
- CI: consolidate immutable checkout, Python, Node and uv Action updates; exercise
  the pinned uv bootstrap even when no skills change, with cross-run caching off.
  SkillEvaluator admission remains conditional on changed skills.
- Adoption: bind monorepo selection, source and retained-evidence reads to unchanged
  regular-file descriptors; reject replacement and special-file races, and bound
  source hashing against continuous growth. Multi-root apply remains gated on the
  unfinished conversion transaction and first-change qualification.

- Release planning: keep independent section checklists out of the preceding
  roadmap item's acceptance, preserve nested checks, and ignore example headings
  in fenced code or comments when finding item boundaries.
- HTML observability: add `litai render html` with exact imported-wheel/catalog
  binding, verified timestamp-preserving cache hits, non-writing cache lookups and
  atomic file publication. Preserve foreign/edited output and refuse changed inputs
  before publication. Installed-wheel CLI/cache qualification passes locally;
  hosted and post-update browser qualification remain pending.
- HTML observability: render an interactive single-file DAG with one SRI-pinned
  graph library, local catalog filters, keyboard navigation, focus/zoom/pan and
  identity-checked source previews. Unsafe or changed source files refuse before
  emission; native disclosures remain readable when the library or JavaScript is
  unavailable. Exact inline code, presentation profile and asset/source bindings
  are inspected. CLI publication/caching pass installed-wheel qualification locally;
  post-update browser qualification remains pending.
- HTML observability: add a read-only authority-graph surface and schema for its
  unchanged JSON. Preserve the existing graph identity, reject unsupported views
  and unavailable authority, and retain immutable snapshots.
- HTML observability groundwork: add immutable render request/result, provenance
  and artifact records using the accepted contracts. Bind ordered inputs and
  renderer identities, reject tampered provenance, and keep completed HTML-byte
  hashes outside the embedded provenance. These records do not publish artifacts.
- HTML observability contracts: retain a per-requirement Phase 0 review and
  repeatable mutation checks for single-file declarations, pinned assets,
  provenance and conditional result/staleness rules. Prospective surface probes
  do not register a renderer or change canonical JSON outputs (#295, #296).
- Documentation tooling: update Puppeteer to remove the audited archive/YAML
  dependency chains and fail documentation checks on high-severity audit findings.
  The integrated fix passes the full hosted platform matrix with its reviewed
  dependency lock unchanged.
  The contributor documentation tools now require
  Node 22.12 or newer; generated-application runtime requirements are unchanged.
- Rust libraries: run Cargo's native test targets before the Standard case-result
  driver, rejecting invalid integration-test imports and retaining command/case
  diagnostics. Clarify crate-name imports and binary-private helper placement in
  Cargo generation guidance (#379).
- Adoption groundwork: verify every staged monorepo Component receipt against the
  reviewed bundle and current whole-source membership. Missing or stale receipts,
  altered projections and concurrent source changes refuse. This read-only internal
  check does not enable multi-root conversion or publish a project release receipt.
- Library artifacts: local and remote `build`/`test` retain typed package/import
  metadata without fabricating a command; both operator and worker `run` refuse
  libraries. Failed publication preserves the previous export. SSH evidence carries
  the exact sealed package ZIP, checks its correspondence with transported files,
  and recovers metadata from the verified manifest. Worker locators bind package
  metadata and observed toolchains; existing executable wire bytes stay unchanged.
  Full combined release qualification remains a separate gate.
- Release engineering: retain the exact smoke-tested wheel with a portable
  revision/version/SHA-256 manifest and upload it from each wheel CI job.
  CI custody remains distinct from release publication approval.
- Lifecycle upgrades: preserve custom receipt suites, evidence requirements and
  test floors during Standard rebind; update only Standard-derived runner pins
  and reject plans that would replace project-owned receipt authority (#376).
- Maintenance: preserve LF bytes in skill-update plan test fixtures on Windows;
  normative Markdown validation remains strict.
- Repository lineage: strengthen shared-cache regression coverage with separate
  processes, concurrent immutable snapshot reads, credential-safe lock failures,
  and abrupt-owner recovery tests that also run on Windows. Native platform
  qualification of this follow-up remains pending.
- Release integration: reconcile the reviewed HTTP 204 health-check runner identity
  and make unfinished installed-wheel fanout and trusted CI/promotion explicit 1.1
  obligations. Correct the repair roadmap to reflect implemented Standard support;
  current receipts and release qualification are not implied by these updates.
- Release planning: expose previously unprojected score-improvement milestones in
  the 1.1 queue and add an optional full-integration scope check to its summary.
- Release engineering: keep unfinished GitHub checks pending and malformed check
  results unknown in peer-work surveys; completed jobs no longer hide incomplete CI.
- Release engineering: distinguish branch integration into the current checkout
  from landing on the default branch, without relaxing branch cleanup gates.
- Adoption groundwork: stage source-free monorepo Component projections and earn
  separate retained-harness receipts with scoped shared-input invalidation. This
  internal adapter does not initialize projects or enable multi-root apply yet.
- Onboarding: expose monorepo build-root candidates and validate explicit source
  ownership, shared consumers and per-root commands with `--root-plan`. Selection
  binds exact source bytes; refined apply refuses pending independent receipts.
- Adoption: add acknowledged retained-source scope refresh with exact plan identities,
  path deltas, receipt-policy rebinding, historical conversion evidence preservation,
  and serialized receipt execution/publication. Refreshed scope requires a new receipt.
- Adoption: reject symlinked native-project registry ancestors, serialize child
  registration, and coordinate integrated acceptance with conversion-stage mutation.
  Failed promotion preserves a child created by another caller.
- Onboarding: keep suggested create/adopt commands bound to the selected project,
  including paths with spaces; clarify that host readiness differs from authority.
- MCP: validate tool inputs and project paths, require explicit mutation
  acknowledgement, and correctly report failed verification gates.
- Release engineering: add retained artifact manifests and downloaded-byte
  verification; generate reproducible Codex/Claude plugin packages from canonical
  skills. Full 1.1 release and provider-session qualification remains in progress.
- Maintenance: add skill update plan/check modes and explicit template mirroring;
  pin CI Actions to immutable commits and provide paired research-trial summaries.
- Fixed regenerative qualification for importable library Components by selecting the
  verifier-owned library oracle and rejecting profile/oracle case drift before clean
  generation. Case binding preserves nested JSON types; boolean/integer drift is
  rejected through the public CLI before lifecycle execution or authority admission.

- Return a successful typed `litai rebuild` result after an importable library passes
  generation, build, tests, packaging, and independent acceptance. Library results
  expose the exact artifact export and import surface instead of trying to construct a
  nonexistent executable entrypoint command after committing the receipt (#373).
- Admit `kind: library` as a first-class importable Standard artifact with no product
  entrypoint. Python package trees, dependency-free CommonJS packages with exact export
  maps, and Cargo library packages now retain a typed capability-to-symbol surface,
  attributable generated tests, verifier-owned harness acceptance against sealed
  package custody, and exact consumer bindings that reject ambient or substituted
  dependencies (#280).
- Allow deterministic-static inverse bundles to become source-free projects through an
  explicit `spec review --component-graph FILE` gate. The signed review binds one
  complete source/evidence-closed Component boundary; acceptance revalidates it,
  renders its public capability contracts, and creates retained-source authority
  projections without pretending inert inventory or model custody supplied semantics.
  `spec accept --integrate-project` can install the source-free authority as an exact
  registered child of a retained adopted root, whose conversion gate rejects registry
  drift and Component-coordinate collisions before advancing to drafted (#367).
- Publish the Phase 0 wire contract for single-file HTML5 visual observability artifacts
  as `urn:literate-ai:schema:v1:html-observability-contracts` (eleven public contracts:
  view, source binding, external asset, renderer binding, provenance, artifact, staleness
  report, plus the rendering entrypoint's surface registration, render request, typed
  render refusal, and render result).
  Every artifact names the exact JSON surfaces and content identities
  it derives from, plus the framework distribution that rendered it, so mixed-version
  panes are detectable rather than silently inconsistent. JSON stays canonical; HTML is a
  derived, regenerable view. The single-file constraint and Subresource-Integrity pinning
  of CDN libraries are fail-closed in the schema. The embedded provenance block excludes
  the rendered file's own digest, which cannot exist while the file is being written; that
  digest lives in the enclosing artifact record and the verify gate compares
  `render_inputs_identity` instead. No renderer, CLI verb, or daemon is added, and the
  contract is intentionally not yet in `compatibility.json` `write_contracts` because
  nothing writes it. One registered surface plus one named view renders one `.html`;
  a render that cannot be pinned refuses with a typed, closed-set code instead of
  emitting a partial artifact (#295, #296).

## 1.0.1 - 2026-09-10

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v1.0.1/README.md)

- Allow `litai onboard adopt` to convert repositories containing nested Git
  submodules, relocating Gitlink-containing trees with `git mv` so initialized
  submodule worktrees and their metadata remain valid (#366).
- Reuse verified sealed build artifacts across fresh lifecycle authorizations,
  rebinding current evidence instead of colliding on repeat `build`/`test`
  invocations (#362).
- Keep the installed-project release smoke test on its resolved live model for
  `rebuild`, `build`, and `test` proofs.
- Register isolated generated Python modules before executing them so standard
  runtime features such as `@dataclass` work through the authorized host runner.
- Allow compiler version discovery up to 60 seconds so parallel macOS CI does
  not reject a healthy Xcode toolchain during sample validation.

## 1.0.0 - 2026-09-08

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v1.0.0/README.md)

- Add a portable, project-scoped lifecycle mutation lock covering `litai rebuild`
  (including `--update-receipt`) so a second concurrent mutation against the same
  project fails closed with a typed `lifecycle.project_locked` diagnostic naming the
  active holder (operation, pid, host) before either side pays for an expensive
  model stage, instead of both racing shared checkpoint/finalized-receipt
  publication and one later failing an unrelated-looking predecessor-contract error.
  Read-only commands are unaffected. Failure-safe: the advisory OS-level lock
  releases automatically if the holder crashes (#318).
## 0.11.0 - 2026-09-07

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.11.0/README.md)

- Treat generated `GET /health` HTTP 204 as ready for the durable split-service
  cross-process flow. Snapshot and page fetches still require HTTP 200. Live
  JavaScript servers commonly answer health with empty No Content.
- Bind LocalStandard cache hits to an external artifact checkpoint outside the
  candidate object so a coordinated rewrite of artifact bytes and the self-declared
  tree manifest cannot authenticate itself. Missing checkpoints rebuild in disposable
  custody; same-run and restarted hits remain observable when the sealed tree matches
  (#327).
- Make Standard npm discovery consume the selected `package-npm` Flavor toolchain
  constraint as the only version authority, matching the host SBOM `>=9,<13` range
  and binding that constraint identity into the npm toolchain. Missing or incomplete
  bounds fail closed instead of using a duplicated Python constant (#328).
- Reject a generated JavaScript single-file bundle at source handoff when local
  CommonJS specifiers omit their file extension, the `__litaiModules` map misses a
  reachable module, a wrapped module body embeds a shebang, or registry load never
  dispatches the exported CLI. Direct `node source/main.js --litai-test` is no longer
  treated as proof that the selected artifact works (#332).
- Map an independent persistent-service process that exits before readiness to
  `literate-ai/cli-error@1` with code `lifecycle.persistent-service.exited`, the
  acceptance phase, child status, contract identity, and bounded sanitized stderr
  instead of an unstructured traceback (#333).
- Prefix-installed `litai` (from `make install`) stages newer GitHub Release wheels in
  the background and, on the next invocation of that same prefix, installs the staged
  wheel and re-execs the original arguments (ADR 0037). Opt out with
  `LITAI_NO_SELF_UPDATE=1`. `litai update` remains project-file reconciliation.
## 0.10.1 - 2026-09-05

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.10.1/README.md)

- Fail closed before launching an external project lifecycle driver when
  `--from-accepted-source` is requested. Accepted-source-only continuation is a
  Standard lifecycle contract; the shared rebuild parser previously accepted the flag
  for external drivers without carrying its no-coding-CLI invariant into their request
  authority, allowing normal model generation to proceed (#331).
- Make `package-npm` an executable, selection-bound Standard lifecycle contract.
  JavaScript generation remains dependency-free unless that Flavor is selected;
  selected projects may replay an exact `package-lock.json` through the detected npm
  CLI into disposable object storage, package the installed runtime closure, and
  never authorize global installation or registry publication. Standard npm v3
  package paths, exact optional runtime nodes, satisfied required peers, and explicitly
  absent optional peers are projected without requiring nonstandard redundant fields
  (#326, #329, #330).
- Reject zero-entrypoint Standard command projection explicitly until an attributable
  importable-library test and acceptance harness exists. The former Python unittest
  and Node test-discovery commands emitted runner-native output that the Standard
  evidence contract could never accept, so they promised a lifecycle that always
  failed later instead of implementing the library artifact requested by #280.
- Treat a recorded root repo-man build as the automatic baseline, parity, and retained
  receipt authority for nested `build.*` stages. Internal nested drivers remain visible
  and manually callable through `litai.harness.mk`, but conversion no longer repeats
  them after the canonical root orchestration; unrelated dotted stages and repo-man
  inventories without a root build remain independently executable (#325).
- Keep release contribution closure valid when the selected patch line lives in a
  linked worktree: the clean attached default-branch checkout is release authority,
  not an undisposed peer contribution. Use a platform-native synthetic executable in
  the Codex authentication regression so Windows reaches the no-model-request contract
  instead of rejecting a POSIX-only test path.
- Keep the release skill's mandatory guidance self-contained in initialized projects.
  Significant mid-release features still require an ADR, explicit human acceptance,
  and durable work recording, without routing derived projects to a framework-only
  documentation path (#322).
- Delay accepted-source cache visibility until root packaging, packaged execution, and
  independent project acceptance all succeed. A project-level rejection can no longer
  leave a reusable but verifier-rejected coding-agent result that deterministic retries
  replay forever; publication evidence is now recorded only after the cache write.
- Keep an outer-planned derivation key stable when a bounded generated-candidate retry
  adds rejection feedback to the accepted attempt's coding-agent prompt. The final
  prompt remains exact provenance, while receipt membership continues to close over
  the immutable pre-generation manifest.
- Read a canonical format-only committed source-cache target as an empty, non-mutating
  miss. Git cannot preserve the writable layout's empty directories; nonempty targets
  still require their entry, key, and CAS namespaces, while absent optional
  source-intelligence namespaces remain empty and partial layouts fail closed with the
  exact rejected path.
- Require a first stable initial, major, or minor release candidate to contain the live
  remote default branch throughout plan, prepare, check, and publish. A release line
  can no longer silently omit approved pre-release trunk work, while patch releases
  remain selective (ADR 0034). Make the checked-in Homebrew formula a declared version
  mirror derived from one Ruby constant, restoring 0.11.0 synchronization after the
  concurrent stable-line publication and allowing release preparation to update it
  atomically. Bind repair checkpoints to the exact global gate-plan prefix so a newly
  inserted prerequisite cannot inherit an out-of-order prior pass, and require a clean
  from-gate-one rerun after repaired completion before accepting release evidence.
- Treat the canonical GitHub HTTPS, SCP-style SSH, and `ssh://` spellings of the same
  owner/repository as one initialization origin during `litai update`, while preserving
  exact distinctions for other hosts, repositories, credentials, ports, and local
  origins (#316).
- Make operator adoption the 0.10 front door: project-independent `doctor`,
  composed `status`, acknowledged plan-then-apply `onboard create|adopt`, explicit
  evidence-bound brownfield stages, and release-candidate-wheel create/adopt golden
  paths. Original source remains release authority until current regenerative
  qualification, and deterministic host/filesystem work stays in Python adapters
  rather than model prompts (ADR 0035).
- Distinguish inherited Component catalogs from the active persisted Component-lock
  set. Retained brownfield receipts now validate every selected lock and require their
  own current legacy-wrapper lock without demanding target-local locks for unrelated
  reusable Components inherited from the framework.
- Regenerate the terminal manager/engineering document pair as the 0.11.0 edition
  so the last minor before 1.0 binds prefix CLI self-update, JavaScript bundle
  self-check, npm Flavor version authority, LocalStandard cache identity, and
  structured persistent-service errors, with authenticated preflight and export-back
  evidence (ADR 0034).

## 0.10.0 - 2026-09-05

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.10.0/README.md)

- Regenerate the terminal manager/engineering document pair as the 0.10.0 edition
  so publication can bind authenticated preflight and export-back evidence (ADR 0034).
- Close completed CI tracker residue (#83–#86) and delete superseded closed-PR
  remotes; keep release lines and remaining open product issues on the queue.
- Make release closure executable and continuous: plans bind release-class collateral
  and a live contribution sweep; publication requires explicit Google account/resource
  preflight and export-back evidence; tracker dispositions and branch lifecycle survive
  into the next release cycle (ADR 0034).
- Add an explicit plan/apply Standard lifecycle rebind for intentional wheel upgrades.
  Application policy depends on an injected installed-distribution protocol while the
  CLI owns the concrete host adapter, preserving hexagonal dependency direction (#309).
- Require reusable ancestor defects to be searched and filed or linked in the owning
  upstream tracker before implementation, with sanitized fail-closed proposals and
  explicit external-write authorization (#308).
- Teach exact quoted Make language-tool expansion in the common portable-application
  authority and every applicable language specialization, with validator-to-skill and
  initialized-template parity checks (#310).
- Preserve project-owned Component workflow, routing, and skill inputs during
  framework-template retirement. Converted projects can now update without losing
  their local generation closure, while genuinely missing inputs still fail atomically
  and the downstream `telemetry-dashboard` reproduction succeeds (#306).
- Separate conversion source membership from post-build artifact observation. Files
  created by a legacy build remain visible as artifacts without becoming authored
  source authority, while modifications and removals of pre-existing source remain
  detectable (#305).
- Render conversion-aware starter documentation through one initialization/update
  helper so framework updates retain the lift-and-shift ADR link, preserve local edit
  conflicts, validate documentation reachability, and become idempotent (#283).
- Preserve complete stdout/stderr identities and bounded diagnostics without failing a
  successful host-heavy baseline solely because its output exceeds the retained excerpt
  limit (#311).
- Keep direct pytest profiling runs and their xdist workers from writing interpreter
  bytecode into the authored checkout. CI now preserves the same source-tree custody
  invariant as Makefile-driven tests without weakening documentation validation (#312).
- Keep the installed release-state-machine proof offline after canonical initialized
  projects gained continuous contribution closure. The fixture now omits only that
  external tracker policy while real project policies remain fail-closed (#314).
- Require published GitHub releases to be stable, carry non-empty notes, and expose a
  non-empty version-matching wheel. Release contribution sweeps now consume durable
  exact-head Git branch-lifecycle markers, so an obsolete branch remains dispositioned
  after its tracker issue closes.
- Archive completed program documents under `docs/history/roadmap/` and map remaining
  open GitHub issues into the work queue.

## 0.9.0 - 2026-09-04

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.9.0/README.md)

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

## 0.8.3 - 2026-09-01

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.8.3/README.md)

- **Validate the live-qualification model before trusting it, and surface the
  real coding-CLI error.** A wrong or mis-qualified model id in
  `literate.test.json` (or `--model` / `LITAI_LIVE_MODEL`) was accepted
  unvalidated and only failed deep inside a generation run, masked as a generic
  "Unexpected server error". Two fixes: the opencode invocation now runs with
  `--print-logs --log-level ERROR`, so the real cause (e.g.
  `ProviderModelNotFoundError`) reaches the captured output; and a new preflight
  runs one minimal bounded task through the selected coding CLI + model before any
  generation, failing closed with `coding_cli.model_unavailable` (naming the model
  and the underlying cause) in seconds instead of minutes into a run. A new `litai
  worker verify-model` command lets an operator validate a `literate.test.json`
  before trusting it; the `configure-test-workers` skill directs operators to run
  it. Skippable via `LITAI_SKIP_MODEL_PREFLIGHT` for a deliberate dry run.

## 0.8.2 - 2026-09-01

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.8.2/README.md)

- **Fix the checkpointed unittest runner crash for Standard-bound projects
  (#215).** `lifecycle_driver_implementation_identity` iterated a nonexistent
  `implementation_paths` list for a `StandardProjectLifecycleDriver`, raising an
  `AttributeError` before test discovery in any Standard-bound derived project.
  A Standard driver's implementation identity is now its framework distribution
  identity.
- **Fix persistent-service acceptance so it can pass (#214).** The
  persistent-service oracle launched the artifact in one-shot `--litai-smoke`
  mode, so it ran a single case and exited before readiness
  (`persistent-service exited before acceptance completed`). Add a
  `--litai-serve` launch mode: the acceptance now launches the artifact as a
  listening server, and the Node/native runtime drivers stream inherited stdio
  so a served process stays attached and pollable. The service generation skills
  mandate that a persistent-service artifact honor `--litai-serve` (bind
  host/port, serve, expose `GET /health`, stay alive). Fails closed if a service
  does not bind/serve.

## 0.8.1 - 2026-09-01

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.8.1/README.md)

- **Windows support restored (0.8.0 known issue fixed).** The 0.8.0 "manifest
  lock failed" failure on Windows was not the lock: `_replace` called
  `os.fsync` on a directory handle, which Windows rejects (`FlushFileBuffers`
  is invalid on a directory), and it surfaced through the lock's error handler.
  Guard the directory fsync to POSIX; converge the manifest lock acquire on the
  proven cache-lock primitive (write the placeholder byte before locking, no
  fsync/`O_CLOEXEC` before `LockFile`); and retry the Windows delete-pending
  `EACCES` on concurrent lock-file open. `litai init` and repository updates now
  work on Windows (full Windows CI green).
- Fix `litai project validate` DOC-IDENTITY advisory paths to use forward
  slashes on Windows.
- Make the release wheel-asset upload idempotent (`gh release upload --clobber`)
  so a retried or resumed publish does not report a false
  `release.wheel_upload_failed` after the wheel is already attached.

## 0.8.0 - 2026-08-31

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.8.0/README.md)

- **Known issue (Windows):** the manifest/append write-lock introduced during
  0.8.0-RC preparation does not yet work on Windows (`msvcrt.locking` /
  file-identity on empty lock files); `litai init` and repository updates can
  fail with `manifest lock failed` on Windows. 0.8.0 is verified on Linux and
  macOS; the Windows fix lands in 0.8.1 (WINDOWS-LOCK-001).
- Run a persistent-service Component's acceptance oracle instead of silently
  taking the exempt path: the package plan now carries the Component's real
  declared entrypoint kind, so a service's readiness/auth acceptance actually
  runs rather than passing without evaluation (#213).
- Add `litai flavor add --no-default` so a polyglot project can install a second
  language Flavor's files for per-Component `flavor_slots` selection without
  making it a project-wide default (#208).
- Preserve declared-but-empty project roots (`samples/`, `mcps/`,
  `verification/`) across `git clone` with a `.gitkeep` sentinel so a converted
  project validates in a fresh checkout (#207).
- Keep the global `--debug` flag from injecting unplanned generation authority:
  the debug spec-map skill is framework instrumentation and no longer aborts
  generation with `generation_context.authority_unselected` (#212).
- Publish a pip wheel as a GitHub Release asset on every release so downstream
  users can `pip install` the distribution instead of building from source
  (RELEASE-018).
- Prefer a clean upgrade path between consecutive minor releases while allowing the
  human release owner to defer that proof explicitly, without claiming compatibility,
  and retain a migration or compatibility-patch follow-up (RELEASE-017).
- Lead a project's own documentation with its identity — what it is, how to
  install and use it — before framework mechanics; `litai project validate`
  flags docs still carrying the template placeholder (DOC-IDENTITY-001).
- Make generated frontends browser-instrumentable and add an operator-side desktop/mobile
  verification skill that checks rendered structure, runtime failures, responsive
  overflow, invalid numeric output, request behavior, and specified interactions.
- Apply persisted `component_flavor_selectors` consistently across lock, plan,
  generate, rebuild, worker dispatch, build/test, and verify so mixed-language
  projects no longer need to repeat per-Component selectors on every command.
- Keep isolated OpenCode generation provider-neutral while forwarding only the
  credentials selected for the operator's configured provider.
- Admit Zig and zig-cc through Standard host toolchain discovery so missing Zig
  fails closed with an install URL instead of an unknown-toolchain reject.
- Accept lockfile-pinned npm as isolated Flavor axis `js-npm` (requires
  JavaScript; detect-before-install from `package-lock.json`). React dashboard
  locks under `ui-react` and fails closed without JavaScript.
- Wire fail-closed pytest-testmon so unchanged tests skip only with a trusted
  impact map; missing, stale, or durations-based maps run the full suite.
  `make python-check` and release gates stay full-suite.
- Scan coverage gaps on workers and fail closed on path-keyed `None` handlers
  and product `NotImplementedError`; TODO/FIXME and pass-bodied abstracts stay
  advisory.
- Replace name-only gate skip and test-file fingerprints with pin-hash dispatch
  keyed off existing authority-graph identities and the project receipt.
- `litai catalog copy` clones git URLs at an optional revision, records
  `git:<url>@<commit>` provenance, and refuses embedded credentials.
- `litai package build` / `verify` take `--worker`, reject OS/provider
  mismatches, dispatch remote BUILD without artifact export, and independently
  verify apt/brew/winget/chocolatey metadata ZIPs.
- Command-line `--coding-cli` / `--model` overrides the ignored live-test pin for
  one invocation without rewriting `literate.test.json`; POSIX `--target local`
  still fails closed until worker login environments provide `OPENAI_API_KEY`.
- Prefer the pinned `OBJ_DIR` python-pptx toolchain when regenerating the overview
  document pair so a vanilla worker needs no Codex presentations plugin; plugin
  discovery stays fallback until three-OS visual parity closes.
- Bind a lint-plus-render-snapshot acceptance oracle for hand-authored library
  Components so render drift fails closed instead of depending on visual
  inspection (`component_acceptance.render_drift`).
- Pin `publication-import` to JavaScript on the default sample gate so the
  portfolio's live coverage is not Python-only, with re-pinned harness
  identities from a passing `cursor-agent` / `gpt-5.6-sol-high` run.
- Bind generation-plan cache lookup to the portable coding-CLI tool identity so
  copied filesystem-v2 accepted-source membership is found on continuation
  instead of missing as `source_cache.runtime_absent` (`execution_plan_identity`).
- Canonicalize generated source-SBOM composition authority during coding-CLI
  reconciliation, preventing a model-authored non-Bazel composition from surviving
  until the build phase while retaining the exact deferred-Bzlmod form.
- Name the Cluster Health Service sample-host JSON probe (seeded pagination,
  `next_cursor` of the last first-page `cluster_id`, typed 404) so live
  generation cannot treat HTTP-only behavior as the verifier surface, and
  re-pin its harness identities.
- Bind locked authored assets when `StandardProjectApplicationService.plan`
  omits the `assets` argument, so samples such as `loan-risk-gate` no longer
  fail closed on `component_preparation.generation_key_mismatch` for `assets`.
- Default `litai init` parent is the highest published `vX.Y.Z` tag at or below the
  installed CLI version, never floating `HEAD`. Invalid parent project JSON now names
  the contract error instead of a generic authority failure.
- Keep the root onboarding `SKILL.md` a routing index that wraps `litai` and nested
  agent skills instead of restating CLI protocol (SkillEvaluator quality cap).
  `litai --help` stays cp1252-encodable. Wheel-smoke `init --from` uses a local
  remotes-stripped parent so CI does not fetch GitHub over HTTPS.
- Close the critical RelEng gaps around a cut: nested `release-project`
  skills wrap backport, evidence, default-branch advance, hosted CI
  status, published-identity verify, and descendant notify.
  `litai release verify-published` proves the remote tag, release line,
  and optional GitHub release match one prepared identity without
  retagging. Tracker inspect names `ci_status`, `land_create`, and
  `land_merge` argv. Landing on the trunk is a nested `dev/land` skill.
  `litai project peer-work` surveys green open PRs and issues at cycle start
  and leftover worktrees or unmerged branches at cycle end.
- Position Literate AI as a release-engineering SDLC harness: `litai init` and
  `litai init --convert` plus the evidence-gated `dev` workflow and versioned
  `release` are the operator front door. Spec-to-binary generation remains
  authority and arrives in waves.
- Group `litai help` into SDLC catalog bands without merging or deleting verbs.
  The configuration-and-cli inventory matches the installed parser, including
  `design`, and no longer documents `worker capability`.
- Global `--debug` / `--debug=FILE` traces harness SDLC stages, redacted child
  argv, and spec↔source maps on stderr or a truncated NDJSON file. It is not a
  `--verbose` superset (verbose still dumps child stdout/stderr) and never
  contaminates the stdout command envelope. Inherit via `LITAI_DEBUG`.
- `debug-spec-map` is an identity-bearing generation skill when `--debug` is on
  (no generation-key schema bump). A framework scanner writes
  `source/.literate/spec-map.json`; a Python helper prints maps on stderr.
- Re-pin `python-service-example` and `react-dashboard-example` harness
  identities after nested app-stack skill URIs, and close Flavor skill parents
  against the admitted catalog when composing sample recipes.
- Isolate operator MCP catalog during tests, bind stdio MCP child processes
  to process-tree ownership, and add runtime-oracle probes for
  `python-service-example` and `react-dashboard-example` so `make python-check`
  does not spawn `~/.config/literate-ai` servers or reject the new harness catalog.
- Wheel-smoke subprocesses drop `PYTHONPATH` / `PYTHONHOME` / `VIRTUAL_ENV` so
  `make wheel-check` does not fail `standard_binding.distribution_ambiguous`
  when the developer session has an editable checkout on `PYTHONPATH`.
- Omit the PLUGIN-001 `litai-mcp` console launcher from the installed-wheel
  payload identity the same way `litai` already is, and admit five-level nested
  RelEng `SKILL.md` files in wheel package-data, so `make wheel-check` does not
  fail `standard_binding.distribution_payload_invalid`.
- Installed-wheel smoke looks for FLAVOR-005 axis-qualified directories
  (`flavors/lang-python`, `flavors/os-macos`) after an explicit `python`/`macos`
  alias init.
- Convert a synthetic Python+Make tree, `project validate` it, and parse a
  spec derived from observed behavior without implementation paths (INIT-003).
- `litai release verify-published` is a read-only remote identity check for a
  prepared cut (RELEASE-015). Nested RelEng skills wrap the existing CLI.

- Associate the 0.8.0 line with an institutional Jira issue. Mutagenic fan-out maps
  an organization MCP service's `jira_update_issue` onto a comment-add, and omitted optional
  catalog fields such as `mcp_roots` no longer fail cache-root binding.
- `litai` is the operator-catalog MCP client (ADR 0025): after a mutagenic
  journal write it fan-outs through `$HOME/.config/literate-ai/mcps.json` when
  `institutional_channels` names the destination. Global `--discover-mcps` is
  off by default and never writes tokens. The official Python module is `mcp`
  (optional extra `literate-ai[mcp]`); the CLI uses bounded stdio JSON-RPC so
  the wheel stays small. Session skills wrap `litai` and do not re-post.
  Embedded application MCP generation names official liberal-license SDKs only
  for selected language Flavors (C++ uses MIT `hkr04/cpp-mcp` or Apache-2.0
  `gopher-mcp`; no GPL).
- Documentation-authority review no longer hashes `docs/roadmap/**`, so queue checkbox
  churn is not marker drift. Graph validation still admits those files. Record the
  marker with `litai project documentation-review --record` /
  `make documentation-review-record` instead of transcribing a digest.
- `litai init` with a document-service Flavor now installs the sample-host workflow and
  routing files document-pair locking requires.
- Cache directory binding treats omitted optional catalog roots such as `mcp_roots` as
  absent instead of invalid, matching manifests that do not declare an MCP catalog.
- `litai init --type` persists optional project shape (default `application`) without
  making `--flavor` or `--type` required. Smart Flavor defaults remain Python, GNU Make,
  pip, and the host OS.
- Initialization continues to report `tool_bootstrap.state: disabled` when no
  source-intelligence provider is configured.
- The manager-overview authoring toolchain is pinned under `tools/doc-toolchain/` and
  bootstraps into ignored `OBJ_DIR` via `make doc-toolchain-bootstrap`.
  `regenerate_python.sh` detects those imports and does not pip-install on its own.
- The root README lifecycle diagram names OpenCode and local/SSH/command workers. The
  CLI reference lists every public verb exactly once and no longer documents the
  won't-fix `worker capability` command.
- Rename remaining Flavor catalog directories to axis-qualified names
  (`lang-cpp`, `os-linux`, `build-make`, `toolchain-swift-apple`, …). Bare
  selectors such as `+cpp` still select the same Flavor as `+lang-cpp`. Dotted
  selectors such as `+package.pip` fail closed; `+pip` remains valid.
- WebMCP is an in-page MCP delta of the MCP parent skill, not a second protocol
  and not operator `~/.config/literate-ai`. A reusable JavaScript front-end parent
  hosts React and WebMCP deltas; `ui-react` occupies `implementation.ui-framework`
  and requires `lang-javascript`. A back-end parent indexes Python and Rust
  service chapters off selected Flavors and records a named skip when
  `lang-elixir` is absent. Peer samples `backend-base`, `frontend-base`, and
  `webmcp-page` share SAMPLE-MATRIX-002 defaults; live sample generation still
  needs model-egress acknowledgement.
- `cli-application` is an explicit Component subclass, not the implicit base.
  Omitted `kind` infers from entrypoints so existing authoring identities stay
  stable. Accepted kinds are `component`, `cli-application`, `library`,
  `persistent-service`, `packaged-module`, `ui`, and `schema-only`. Declined:
  `wrapped-source`, `batch`, and `event`. Default init Flavors are unchanged.
- Generation recipes close exact transitive specification-to-source skill
  dependencies from the admitted catalog, unify duplicates, and fail closed on
  cycles, identity mismatch, and stage incompatibility before a prompt is
  assembled. Changing a closed skill changes recipe identity.
- `litai prompt translate` wraps prompt-master as a deterministic, no-model
  command. MAC task envelopes fail closed so Literate AI does not apply a second
  translation layer.
- Inherited-session generation fails closed with
  `inherited_session.current_session_unavailable` when no Cursor stop-hook is
  present. Live inject into an already-running conversation remains a vendor gap.
- Specification-to-source skills may declare optional `output_trees` under
  `source/` for multi-language generation from one shared schema. Omitted keeps
  existing skill identities.
- Claude and Codex can load Literate AI as a plugin: `.claude-plugin/plugin.json`
  plus `make plugin-bundle` copies root `SKILL.md` and `skills/agent/` into
  `$(OBJ_DIR)/plugins/literate-ai` without checking in a second skill tree.
  `litai-mcp` is a stdio JSON-RPC adapter over in-process `litai` (`verify`,
  `lock`, `plan`, `rebuild`, `project validate`, `catalog copy`).
- Homebrew can install literate-ai itself from `packaging/homebrew/literate-ai.rb`
  (`Language::Python::Virtualenv`, no CodeGraph or Node dependency). The formula
  URL/sha256 wait on the published 0.8.0 tarball.
- `litai package` constructs apt/brew/winget/chocolatey as deterministic ZIP
  archives with native metadata. Init defaults to `package-conan` when two or more
  language Flavors are selected.
- `litai flavor add` copies a shipped Flavor into an existing project, appends
  its selector, and invalidates Component locks. Bidirectional `conflicts:` fail
  closed without mutation. OS linux+macos remains allowed.
- New axis-qualified Flavors `lang-typescript`, `lang-zig`, and
  `toolchain-zig-cc` ship in both catalogs. Docker Flavor/skill now stamp from
  init. Nested `staging`/`production` workflows compose with `extends`.
- Catalog copy admits `workflow:` and `routing:` items. Non-git source inventory
  keeps `source/build/` visible. CodeGraph stays opt-in (`provider_id: none`).
- `litai design refine`, `explain`, and `accept` turn an abstract mission into a
  reviewable design draft with blocking questions. Acceptance records a receipt and
  does not generate source.
- Sample harness admits `persistent-service` and `web-application` entrypoints.
  Cluster Health Service and Cluster Metrics Dashboard now have pinned harness
  contracts; the dashboard pins JavaScript. Live generation still needs explicit
  model-egress acknowledgement.
- JSON coding-CLI tasks fall back to the next configured CLI on quota or
  authentication denial unless `CODING_CLI` is pinned.
- JavaScript and C++ ecosystem layout skills ship in the catalog and init template.
- Downstream-sized nested Component-lock reviews paginate three 512-entry pages
  before one atomic replacement. Optional omitted `mcp_roots` no longer makes
  review object storage look unsafe.
- Interactive coding sessions follow `skills/agent/configure-operator-mcp` to
  discover connected MCPs and write `$HOME/.config/literate-ai/mcps.json`. `litai`
  without an agent still offers a thin TTY ids-only catalog. Jira, Slack, and
  Outlook posting stay gated on that list. Mutagenic commands journal a shared
  author/recipient envelope for institutional comments and mail. Project-owned
  MCP servers use `mcps/<id>/mcp.md` with hygiene that rejects secrets, operator
  ids, and generation-prompt MCP requirements. There is no drain or discovery
  daemon: the root `SKILL.md` points at `skills/agent/` so an interactive session
  follows those skills.
- The 0.8.0 manager/engineering overview is regenerated and published. After
  Google Slides import, the deck is read so text boxes do not overlap and
  workflow slides stay picture-led. Document-pair acceptance now fails overlapping
  text-bearing frames; geometry-escape is not an overflow pass. The authoring
  skill, document-pair contract, and documentation-ecosystem Flavor bindings
  carry those rules so derived projects inherit them.
- Convert records `make test` only when the Makefile declares that target, so a
  Python `tests/` tree is not shadowed by a missing Make rule that exits 2.
  Gate failures name the recorded command and a bounded diagnostic excerpt.
- Convert omits local virtualenvs and `node_modules` from the disposable baseline
  copy. A recorded gate that still names those paths fails with
  `project.convert_environment_bound_command` instead of a copied-interpreter 127.
  The environment-bound recipe check follows a Make goal's transitive prerequisite
  closure, so an aggregate `test: test-python` still fails closed when the bound
  command lives in a prerequisite (issue #187).
- `litai update` classifies catalog-inherited files against the resolved parent
  catalog at the target revision, not the installed init-template snapshot.
  Remaining conflicts print ours/theirs unified diffs (and the same text in
  `--json`). Optional `--review-conflicts` asks the selected coding CLI for a
  plan-only keep-local / take-upstream / merge proposal. Repeatable
  `--take-upstream PATH` applies explicitly reviewed inherited-catalog conflicts in
  the same dependency-closed, validate-once rollback transaction;
  `--keep-local PATH` preserves reviewed retired compatibility inputs. Both choices
  are restricted to their exact planned classification and recorded separately in
  the apply receipt.
- `litai spec merge` reverse-adopts one hand-authored island in an already-managed
  project without quarantining the tree. `litai init --convert` resumes an
  interrupted scaffold when `.literate/initialization-baseline.json` exists
  without `literate.project.json`.
- Sample catalog now includes Loan Risk Gate (DMN + pinned `assets:`) and Playback
  Controller (SCXML + trace sidecar), so every non-OpenSpec specification type has
  a genuine portable-application sample. `_load_sample` dispatches through the
  shared provider registry; live generation of the new samples still needs
  explicit model-egress acknowledgement.
- Bind Win32 Job Objects at remaining in-package timeout spawn sites so late
  grandchildren die with the parent. Standalone worker scripts stay stdlib-only
  and keep validated `taskkill`.
- Agents decide patch content on the current release line; humans decide when
  the next minor or major is cut.
- 0.8.0 remains a SemVer minor number, but this cut is a one-time break-glass
  major-equivalent: every remaining open queue item on `main` is in scope,
  including breaking changes. That scoping is not durable SemVer advice.
- Withdraw the incorrect "do not build a native IDE" rule. That was a local
  catalog choice recorded as if it bound derived projects; it does not. Derived
  projects may ship IDEs and complete applications. This repository simply has
  no IDE sample yet.
- Classify a project's issue tracker from Git remotes: GitHub uses `gh`, GitLab
  uses `glab`. Check parent repositories out under `parents/<id>/` in the current
  project, with submodules and Git LFS, instead of `/tmp` clones.
- `litai project ci-plan` detects test frameworks and emits a fail-closed CI
  shard/impact plan. Language research from issues #83–#86 is the availability
  table, not a runtime for every Flavor. Missing impact maps keep the full
  suite; this repository's checkpointed Linux/macOS jobs stay unsharded.
- Parent checkout, tracker inspect, and source capture run Git through
  process-tree kill and bounded output custody so late Git/SSH descendants
  cannot hang pipes after the direct command exits.

## 0.7.2 - 2026-08-28

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.7.2/README.md)

- Fail closed when a Flavor catalog keeps a deprecated alias directory beside its
  canonical `package-*` or `doc-*` Flavor. `litai catalog migrate-flavor-names`
  removes the alias tree when the canonical directory already exists; init from
  `+pip` or `+google-workspace` stamps only the canonical directory.
- Regenerate the manager/engineering document pair on every major or minor cut;
  patch cuts keep README citations on that last edition. `0.7.1` is the catch-up
  edition because `0.7.0` skipped the refresh. The 0.7.1 pair is published: 26-slide
  Slides resource `1V9mt1JpEst_2ucC0eJIIw7dff64MrtFRfut8MSiukeE` updated in place, and
  narrative Google Doc `1fMxy0NTT54MV4T9AwmA8F0VahNet93Xhp0d0pszI6GA` created.

## 0.7.1 - 2026-08-27

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.7.1/README.md)

- Host bootstrap POSIX search path reads `HOME` from the worker environment instead of
  `Path.home()`, so Linux/macOS discovery still works when the process has no usable
  home directory.
- Bind Component-lock review tests to the temp project's cache roots so they do not
  adopt an xdist worker `OBJ_DIR` that is already non-empty and unmarked.
- Add explicit paginated review transactions for genuine Component-lock transitions
  larger than 512 semantic differences. Every page is bounded and identity-chained;
  apply revalidates all authority and performs one atomic final replacement without
  ever writing a partial lock.
- Establish public-API-first test design as the contributor standard and begin a
  suite-wide migration toward contract and complete workflow tests with realistic
  persistence, retaining isolated unit tests only for distinct complex or
  safety-critical logic.
- Keep the Python test suite on public CLI, schema, and script contracts, complete
  workflows, and high-risk boundaries; drop private-helper, call-sequence, and
  coverage-only tests that do not protect a distinct failure mode.
- When a release policy names `default_branch`, `litai release plan` may run on
  that trunk only to cut a missing `release/<major>.<minor>.x` line;
  `prepare` creates and checks it out. `check` and `publish` refuse the default
  branch and any other name. The `release-project` skill no longer treats "the
  default-branch tip is the release" as a valid publish checkout, and no longer
  asks the operator to create the line with raw Git before `plan`.

## 0.7.0 - 2026-08-27

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.7.0/README.md)

- Restore opt-in external project source intelligence under ADR 0019:
  `provider_id: none` remains dependency-free, while explicitly configured
  `codegraph-cli` projects regain bounded `project source-intelligence sync|check`,
  validation, verification, and lifecycle enforcement without provider installation
  or worker capabilities.
- Each OS Flavor requires remote-worker host configuration to attempt NTP
  synchronization to `time.nist.gov`; detect-before-install records failures in
  `clock_sync` evidence without failing sample-worker readiness.
- Plan and generate now take a project-local language Flavor's source entrypoint
  from its Standard command profile, so a target such as `cpp-posix` does not
  have to be added to the catalog language allowlist and does not receive the
  hardcoded C++ JSON `argv[1]` prompt.
- Installed wheels include nested specification-to-source skill `agents/*.yaml`
  files, so CUDA generator provider metadata is present after `litai update`.
- Require documentation for deployable services to include an end-user installation,
  configuration, startup, readiness, verification, upgrade/rollback, and cleanup path;
  contributor rebuild commands and internal smoke harnesses no longer qualify as getting
  started instructions.
- Component-lock semantic diffs now bound emitted review entries independently of
  traversal work, so large mostly-equal locks stay reviewable without raising the
  512-entry review cap.
- `make samples` still requires an explicit live coding CLI and model; unit-test
  invocations of the sample runner no longer fail closed when those pins are
  absent, matching ADR 0017.
- Component lock catalog walks skip a sibling `implementation/` directory next to
  `component.md`, so convert retained source (symlinks, host trees) is not treated
  as lock catalog.
- Add `litai project documentation-update`: deterministic drift planning is read-only
  and model-free, while explicit `--apply --allow-model-egress` permits bounded,
  identity-checked edits only to existing declared Markdown and leaves final authority
  review recording separate.
- Agent skills under `skills/agent/` now inherit a parent wrap-Python rule, and
  the three development postures nest as `develop-in-production-workflow/staging/dev`
  so the filesystem is the scoping mechanism. Generation workflows nest the same
  way, and routing policies use a matching `routing.json` sentinel in each
  nested directory instead of a sibling `.json` file beside a child directory.
  `release-project` wraps `litai release` instead of restating the CLI, and
  GitHub CI runs on `release/**` branch pushes as well as `main`.
- Source generation now retries a bounded number of times when a coding CLI
  emits a well-formed generated-test suite that still fails uniqueness,
  acceptance-signature, or expected-result-shape admission. Those failures stay
  hard after the retry bound; empty generation is still not retried.
- Convert inspects a live tree before quarantine: `litai init --convert --plan`
  reports readiness without writing; mutating convert records remote CI instead of
  requiring a local `make ci`, wraps nested `repo.sh` stages, runs local-cheap
  baselines (Make, CMake, cargo, python, Bazel, npm, Go) unless `--run-baseline`
  for host-heavy `repo.sh`, and stamps one language-ecosystem Flavor on mixed
  trees.
- Live qualification (`installed-project-e2e --live`, `make samples`, and remote
  fan-out) requires an explicit coding CLI and model from ignored
  `literate.test.json`, with `--coding-cli` / `--model` then `CODING_CLI` /
  `LITAI_LIVE_MODEL` taking precedence. PATH first-available search is not a
  live-test default. Remote workers require `opencode` and `OPENAI_API_KEY` in
  the login environment.
- Each coding-CLI session keeps a per-agent model stack that never reaches
  depth 0 ([ADR 0018](docs/decisions/0018-never-empty-per-agent-model-stack.md)).
  Nested Component, Flavor, skill, and specification-language pushes restore on
  pop; attempting to pop the only remaining frame warns and logs the precise
  caller and specification location.
- Owned sample, installed-project, wheel, roundtrip, and installed-E2E runtime
  roots are now registered in release evidence when created, so failures before
  execution still point to surviving scratch; successful cleanup records the
  historical pointer as pruned.
- Initialization now reports an explicit disabled tool-bootstrap state when no
  source-intelligence provider is configured, unblocking the isolated installed
  project gate without provisioning a source-graph tool.
- A release now closes over its own evidence. Every stage of a release — the
  release root, each gate, each host or worker dialog, each sample variant and
  phase — records one node in an append-only ledger under
  `<obj-root>/evidence/<run>/`, carrying that stage's exact pins and pointers to
  its retained outputs with digest, size, and retention. Child processes inherit
  the run through `LITAI_EVIDENCE_RUN` and `LITAI_EVIDENCE_PARENT`, so a release
  is a hierarchy of steps rather than a flat pile of logs, and
  `litai release evidence explain` answers "what failed and where is its
  evidence" in one bounded page instead of a filesystem search. The ledger
  indexes and points; it never authorizes a build or accepts a release.
- Failure custody replaces delete-on-exit. Sample scratch, installed-CLI
  workspaces, wheel and roundtrip workspaces, lifecycle driver diagnostics, and
  lifecycle failure diagnostics are retained when their step fails, reported by
  absolute path, and registered as pointers; successful steps still clean up.
  This closes the `v0.5.2` host-E2E case, where the failing sample's scratch was
  deleted with the run that produced it and the crash could not be reconstructed
  afterwards. Sample scratch stays outside the checkout in every case: custody is
  a pointer to that external scratch, not a reason to relocate a host build.
- Host and target dialogs are first-class evidence. SSH worker probes and gate
  transcripts, per-worker fan-out logs and failed workspaces, and GitHub Actions
  failed-job logs are captured under bounded envelopes, with worker identity
  recorded as an endpoint digest so no private hostname enters persisted
  evidence. Remote custody is marked `host-only` rather than copied.
- A single-host VM remains a complete release path. The default target runs the
  gate directly on the invoking machine and records the fan-out and provider
  paths as explicit `skipped`/`unavailable` nodes with reasons, so a release on
  one VM is honest about what it did not run instead of silently omitting it. A
  fan-out invoked without a configured fleet reports `authoritative: false`.
- Steps that used to run blind now run through an instrumented harness.
  Single-command release gates, the checkpointed unit-test run, and the installed
  launcher route through `scripts/litai_step.py`, which records a node with live
  teed output and retained redacted transcripts when a run is attached and is a
  pure passthrough when it is not. The Python bootstrap shells keep only their
  candidate loop and delegate the version decision to `scripts/python_resolver.py`
  run by the candidate interpreter itself. CI uploads the evidence root and
  explains the failure in the job log.

## 0.6.4 - 2026-08-27

- Restore opt-in external project source intelligence under ADR 0019 while
  retaining dependency-free defaults and bounded provider execution.

## 0.6.3 - 2026-08-26

- Bound Component-lock semantic differences independently of traversal work so
  large, mostly equal locks remain reviewable.

## 0.6.2 - 2026-08-25

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.6.2/README.md)

- Record that `v0.6.1` shipped from `cd2c9229`: close RELEASE-007 so the
  queue no longer holds a patch that already published.

## 0.6.1 - 2026-08-25

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.6.1/README.md)

- Record that `v0.6.0` shipped from `8d84dbda`: close RELEASE-006,
  CODEGRAPH-DEPENDENCY-001, and PR-CI-060 so the queue no longer holds a
  cut that already published.

## 0.6.0 - 2026-08-25

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.6.0/README.md)

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

## 0.4.1 - 2026-08-18

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.4.1/README.md)

- SSH lifecycle dispatch now sends Windows PowerShell receivers directly instead of
  requiring an unavailable remote Bash, while POSIX workers retain login-shell PATH
  semantics. GPU worker results also preserve exact human-readable model names such as
  `NVIDIA RTX PRO 4500 Blackwell Generation`, so a completed remote lifecycle is no
  longer rejected by the portable-scalar validator. Invalid remote contracts now name
  the exact failing field. (#126, #127)

- Repository-lineage fetches now use a content-identified bounded policy: 300 seconds
  total, 180 seconds without forced Git progress, and 30 seconds for one
  non-interactive SSH connection by default. `init`, `update`, and `reparent` accept
  validated bounded overrides; results and actionable timeout diagnostics identify the
  policy and provenance, while exact-revision authority, process-tree cleanup, and
  cache-independent shallow fetches remain unchanged. (#122)

- Bounded tool execution now terminates post-exit descendants that retain inherited
  output streams, drains the resulting EOF, and preserves the completed direct tool's
  status and bounded output. Repository-lineage Git/SSH fetches no longer fail merely
  because a transport helper outlives Git. (#120)

- Added `litai perf show --dir PATH` / `litai perf chart --dir PATH`, letting either
  command report on an exact directory of archived `*.jsonl` span files directly
  instead of resolving `OBJ_DIR` through `--project`. `OBJ_DIR` is disposable and
  wiped by `make clean`/`really-clean`, so this is what lets a copy of
  `OBJ_DIR/.litai/perf` made before a clean stay reportable afterward.

- Fixed SSH-dispatched remote commands (worker probes, `release check --target
  local`, build/test/run dispatch) running under a non-login, non-interactive
  remote shell, which silently drops any `PATH`/toolchain setup a worker only
  performs in `.bash_profile`/`.bashrc` (a real fleet worker hit this as a spurious
  "no compatible Python found in PATH" failure despite the tool being installed).
  `ssh_arguments()` now wraps the remote command as `bash -lic '...'`, forcing both
  login and interactive shell semantics on the remote end regardless of how sshd
  would otherwise invoke the worker's default shell -- no worker-side dotfile
  changes required.

- Fixed remote accepted-source restore from a fresh workspace when admission used an
  inherited IDE session. Dispatch now discovers the non-secret provider/tool identity
  from verified transported cache membership, binds it into request authority, and lets
  workers reproduce the admitted key without session credentials or a generator
  transport. Missing membership and mixed providers fail closed, while worker ambient
  provider selection can no longer turn an exact copied cache into
  `source_cache.runtime_absent`. (#104)

- Fixed `standard_command.entrypoint_cardinality` unconditionally rejecting any
  Standard executable Component that declares zero entrypoints, which made
  pure-library/capability-only Components unbuildable and forced projects to
  hand-author synthetic CLI entrypoints purely to satisfy the framework (#114,
  confirmed independently by a synthetic telemetry-dashboard fixture). A
  Component with no declared entrypoint now builds normally; its `test`/`execute`
  phases run the language's own dependency-free test discovery
  (`unittest discover`/`node --test`) directly against generated source instead of
  the single-shot `__main__` self-check dispatch used for CLI entrypoints, and
  independent project acceptance exempts it the same way it already exempts a
  non-`portable-application` entrypoint (#32). Declaring two or more entrypoints on
  one Component is still rejected; see #114/#115 for the fuller Component-subclass
  architecture this is a narrower, real step toward. Native/compiled languages
  without a stdlib test runner fail closed with a typed
  `standard_command.library_native_unsupported` error rather than silently doing
  nothing.

- Fixed `make install`/`scripts/install_litai.py` reporting a working tree with
  uncommitted or untracked changes as an opaque "installation command 'python' failed
  with status 1" wrapped around a buried pip/build-backend traceback. The underlying
  refusal (a distribution build from Git requires a clean, exact revision) was already
  correct; the installer now checks the same condition itself, first, and fails with a
  clear, actionable message naming the changed files before attempting the build.

- Synced the root `README.md` with 0.4.0: added `CMake` to the build-system Flavor list
  (missed when `build-cmake` landed), documented `litai release check`'s `--target
  local|github|gitlab` choice, and linked the published manager/engineering overview
  deck near the top.

- `litai release check --target local` now fans the declared gate out in parallel to
  every non-Windows SSH worker configured in `literate.workers.json` instead of
  deterministically picking just one; every dispatched worker must pass, and a failure
  names the first failing worker deterministically. Windows workers are reported as
  excluded with a typed reason (`release.windows_gate_unsupported`) rather than
  silently vanishing from the fleet, since the declared gate (`make release-check`) has
  no Windows-native equivalent yet — Windows CI in this project already avoids `make`
  entirely for the same reason.

- Added `litai perf`, structured per-step performance telemetry recorded as a standard
  part of every `litai` invocation: schema, run id, stage, target kind/id, coding CLI,
  model, start/end timestamps, duration, and pass/fail outcome, written as JSON-Lines
  under the disposable `OBJ_DIR/.litai/perf/` directory (diagnostic, not authority — a
  read-only or missing build root never fails the run it observes). A `--target local`
  fan-out additionally records one span per dispatched worker in the same log. `litai
  perf show [--group-by stage|target_id|target_kind|coding_cli|model|run_id] [--stage
  ...] [--run-id ...]` prints an aggregated table (count/failed/total/mean/min/max);
  `litai perf chart --output FILE.svg` renders a dependency-free SVG bar chart with no
  new third-party graphing library.

## 0.4.0 - 2026-08-17

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.4.0/README.md)

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

## 0.3.0 - 2026-08-15

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.3.0/README.md)

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

## 0.1.1 - 2026-08-02

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.1.1/README.md)

- Added provider-neutral typed contracts and a fail-closed application bridge for
  independently migrating all nine Component lifecycle seams.
- Bound lifecycle requests and results to the exact framework release, canonical
  semantic payload identity, and original wire-byte digest.
- Added read-only shadow enforcement, immutable downstream comparison evidence, and
  reversible framework-authority conformance coverage.
- Added downstream OVA evidence for exact Flavor policy, clean-cache signed and
  classified self-hosting, stable two-generation identities, rollback, and a real
  sandboxed macOS Granian launch against the local framework checkout.
- Added strict SemVer 2.0 domain versions (separate from the PEP 440 distribution),
  exact `ComponentRevisionRef` and `VersionedContentRef` contracts, multi-revision
  Component/model/skill resolution, and exact profile/Flavor/model routing locks.
- Upgraded bundle and publication manifests to bind coordinate, semantic version, and
  immutable revision; imports now require the expected Component revision and reject
  mismatches before transfer.
- Added deterministic compatibility readers that reject future schemas and fail closed
  when legacy documents lack enough information for an unambiguous exact migration.
- Added an evidence-bound final-cutover manifest that proves framework write ownership,
  downstream duplicate removal, retained compatibility reads, and rollback rehearsal
  independently for every lifecycle seam.
- Recorded the repository owner's explicit Phase 2 cutover direction as a visible risk
  acceptance, not as evidence that an elapsed production observation window occurred.

This stable release makes Literate AI the canonical owner of provider-neutral Component
versioning and lifecycle contracts. Downstream products retain their product policy and
read-only compatibility adapters. The production observation window was not completed;
the final report preserves that fact and the repository owner's explicit direction to
complete the cutover without it.

## 0.1.0a1 - 2026-08-02

[README.md](https://github.com/NVIDIA-dev/literate-ai/blob/v0.1.0a1/README.md)

- Added the software-neutral Component, Flavor, source, intelligence, workflow, model,
  security, artifact, publication, settings, and source-to-specification contracts.
- Added deterministic composition, Flavor resolution, content-addressed storage,
  append-only events, atomic workspace acceptance, and filesystem publication.
- Added exact Git/local-source, CodeGraph, OpenSpec, OpenAI-compatible Responses API,
  multi-model portfolio, guarded Python build, and local lifecycle adapters.
- Added read-only OVA compatibility readers and semantic shadow comparison.
- Added six packaged source-to-specification skills and an executable sample/conformance
  ladder, including target Flavor and security-policy cases.
- Added executed two-generation literate-ai self-hosting through the real generation,
  validation, classification, authorization, build, workspace, and event lifecycle.
- Isolated the pinned Node.js OpenSpec contributor tool from the zero-runtime-dependency
  Python distribution.

This alpha proves bounded offline self-hosting with an exact-snapshot structured model
provider. It does not claim a universally hardened build sandbox, general semantic
rewriting by an arbitrary remote model, or completion of OVA's two-release migration
observation period. Separately authorized dynamic observation has concrete fail-closed
`sandbox-exec` and `bwrap` adapters; the Python build adapter remains authorization-gated
but is not a universally hardened operating-system sandbox.

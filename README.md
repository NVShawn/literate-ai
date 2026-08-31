# Literate AI 0.8.0 (historical release marker)

Literate AI 0.8.0 was released on 2026-08-31 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `136e652a984453a4054dd44b3ab8bf78b53264d6`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.8.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.8.0 - 2026-08-31

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

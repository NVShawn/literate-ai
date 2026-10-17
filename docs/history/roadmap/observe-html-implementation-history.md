# HTML observability implementation and qualification history

- **Status:** historical
- **Owning queue item:** [OBSERVE-HTML-001](../../roadmap/active-work.md#observe-html-001-generate-single-file-html5-visual-observability-artifacts)
- **Completion / archival evidence:** [Scope reconciliation](../../roadmap/html-observability-completion.md). This is the superseded queue narrative, retained
  for its exact revisions and recorded artifact identities. Its pending/next-action
  prose describes past checkpoints, not current readiness. The
  [reconciled audit](../../roadmap/html-observability-completion.md) owns current scope.
  Recorded external/ignored artifacts were not reopened by this documentation audit.

### [ ] OBSERVE-HTML-001 — Generate single-file HTML5 visual observability artifacts

- **HTML redundant-read repair installed evidence:** At exact `25b8d8df`, the
  installed-wheel gate passes, including lock and verification health. Run
  `20260914T014307Z-9e524c` ended with all three nodes passed; retained stdout is
  35,620 bytes with SHA-256
  `205d26b878a44235aea67b2ddee64a83bc2351d9d732e8ae5e0216acab59cc25`
  and stderr is empty. The 2,679,202-byte wheel has SHA-256
  `81e00519c0a443e07afc94e0c158fdb081551195712f088ae4bee95dc03f7fc2`.
  Terminal records, actual log/wheel hashes and exact clean revision were checked.
  Full-suite qualification remains running; fresh hosted Windows proof is required.

- **Windows repair (landed and hosted-qualified):** PR #398 run `34794386006`,
  Windows wheel job `103824568738`, again exceeded the 120-second installed
  lock-health render deadline. Its retained diagnostics measure initial source
  collection at 40.865 seconds, first emission at 23.576 seconds and pre-publication
  emission at 35.276 seconds. Framework observation took 1.008 seconds initially;
  final publication started before timeout. The renderer discards its preliminary
  source value, then independently loads the source again for its first emission.
  Remove that redundant read; retain initial artifact source collection, current
  source checks under the lifecycle lock and the last publication callback, plus
  installed-authority checks. Record local regressions and a fresh installed wheel
  before hosted qualification; timing evidence does not itself prove the fix.
  The duplicate probe is removed, and source timing now surrounds each emission's
  actual read. All 96 HTML render/emitter/DAG/staleness, installed-smoke and
  architecture regressions pass in 37.857 seconds, including source changes at the
  final write boundary and installed-authority drift. Lint, formatting and OpenSpec
  checks pass. Exact installed/full and hosted qualification remain required.
  At `25b8d8df`, local installed-wheel and full qualification passed, but hosted
  run `34802718742` Windows job `103848456948` still failed the installed HTML
  verification-health command. After removing the unused read, the first necessary
  source read took 49.676 seconds, with authority 9.876, locks 26.406 and receipt
  13.345 seconds. The mandatory second read reached receipt verification after
  authority 15.117 and locks 32.891 seconds; process termination followed at the
  120-second command limit. The two required reads leave insufficient headroom.
  Adopt only PR #399's bounded 300-second installed HTML command budget, preserving
  final currentness checks and timeout diagnostics. Keep the Git deadline unchanged:
  the available local reproduction passed its existing bound. A larger HTML budget
  is not qualification; require new exact installed and hosted evidence. PR #399's
  independent branch remains preserved for separate disposition.
  The final `73dd5809a2f2b2a65db862a7e4a0cb59cf6a1ec1` revision passes all 15
  checks in completed CI run `34806944053`, including every installed-wheel and
  Windows check. PR #398 merged as `d273686f16981915d1ffe240f5b1d619794f7561`.

- **Post-merge Windows qualification investigation:** Run `34784705303`, job
  `103797976591`, exceeded the installed HTML command's 120-second deadline during
  the initial lock-health render. The cause remains unproven. Add stage diagnostics
  for source, emission, installed distribution/catalog checks and publication;
  preserve the last child diagnostics on timeout before another hosted attempt.
  Keep the existing deadline and all currentness checks.
  The same main run's Windows test shard also failed a 10-second bounded Git
  configuration observation. Local reproduction passed with 3,176 subprocesses.
  Batching index path and object format into one fresh metadata command retains
  independent rechecks and reduces that test to 2,780 processes (396 fewer).
  Thirty-two repository-inspection tests and the exact refresh-capture test pass;
  neither this local result nor the process reduction proves the Windows timeout
  resolved. Both hosted failures remain recorded for subsequent qualification.


- **Priority:** P2 (1.1 product; out of operator-adoption)
- **Release target:** 1.1. Superseded scoping (2026-09-08): this program was carried on the
      `1.0` milestone through four deferral passes and never entered a 1.0 cut. `v1.0.0` is
      tagged, so 1.0 is closed to it. Both issues move to the `1.1` milestone; no part of the
      HTML5/JS observability layer is 1.0 content.
- **GitHub issue:** [#295](https://github.com/NVIDIA-dev/literate-ai/issues/295),
      [#296](https://github.com/NVIDIA-dev/literate-ai/issues/296) (phased plan for #295)
- **Owner:** HTML observability rendering skill and artifact schema
- **Direction:** Operators can inspect project health only as JSON. #295 and #296 propose
      single-file HTML5 artifacts generated from the same identities, not a live dashboard daemon.
- **Conclusion:** Record the observability program out of 0.9.0 and out of operator-adoption. Reuse
      existing identity machinery. Do not add a second planner or a persistent UI process.
- **Depends on:** RELEASE-0.9-CUT-001 (complete; `v1.0.0` tagged, so the gate is discharged)
- **Detailed plan:** [Phase 0 and Phase 1 execution plan](observe-html-phase-0-1-plan.md) —
      the twelve-item work breakdown, its dependency graph, and per-item acceptance contracts.
- **Contract acceptance (2026-09-08):** the maintainer accepted the Phase 0 contract, including
      the decision that provenance excludes the artifact's own digest. Phase 1 of #296 is
      therefore unblocked. The schema is published but nothing writes it yet: it stays out of
      `schemas/v2/compatibility.json` `write_contracts` until the Phase 1 renderer exists (P1.7).
- **Fleet execution:** the twelve items are filed in the MAC ledger under project `literate-ai`
      (repository registered from `.mac/project.yaml`, on `main` since PR #363). The plan is
      executable by hand regardless; nothing in Phase 0 or Phase 1 depends on MAC.
- **P0.1 closed out of band (2026-09-09):** the contract merged to `main` as `89a13d7a` via
      PR [#363](https://github.com/NVIDIA-dev/literate-ai/pull/363). The MAC task was cancelled
      rather than completed because its framing was unsatisfiable: it asked a worker to land a
      pre-existing remote branch, but each worker gets its own worktree and MAC's canonical
      integration proof requires `reviewed_sha == head_sha`. That worker's reviewed head was
      never pushed and #363 carries different commits, so no proof could ever match. Later
      "land an existing branch" items must instead be authored as ordinary change tasks that
      push their own branch. Dependents were detached from it and requeued.
- **Phase 2/3 completion implementation (qualification pending):** The existing
      verification, version and lock health views are joined by
      `performance-history/history`, which reads the bounded durable performance
      span log including build/test failures, and `workflow-routing/catalog`, which
      binds normalized declared workflows, exact routing JSON and every source-byte
      identity. The offline `render dashboard` command composes one or more contained
      projects' declared and already-verified artifacts as panes, binds every input
      digest, uses relative references, and preserves foreign output. Derived-output
      recognition now selects the registered surface/renderer rather than assuming
      every replaceable artifact is an authority DAG. The new unit, schema and HTML
      regression batches pass locally; installed-wheel/browser, full-suite and
      hosted exact-head qualification remain required before closing #295/#296.
- **Next action:** Complete installed-wheel and isolated-browser qualification for
      the new timing, workflow/routing and multi-project shell paths, then run the
      consolidated full gate and hosted stack before closing #295/#296.
- **Installed dashboard browser correction:** Installed wheel `c34e1095` passes
      CLI current/tampered/stale/current checks with verified wheel and retained
      artifact bytes, but browser inspection exposed mobile overflow in expanded
      authority diagnostics containing long hashes. Add wrapping to detail panels.
      Eight candidate-CSS browser cases against the exact installed DOM pass at
      desktop/mobile sizes with JavaScript on/off and every check/provenance panel
      expanded. Eighteen renderer tests pass. Candidate CSS inspection is not new
      installed-wheel qualification; regenerate and qualify the corrected wheel.
      Reproduction and candidate captures remain in `vh-dev` under ignored
      `_build/verification-installed-browser/`.
    - **Verification dashboard (local implementation; qualification pending):**
      The `verification-health` / `health` view binds the existing selected-gate
      verifier report through a shared read-only adapter. It excludes the
      HTML-artifact gate to avoid self-recursion and discloses that coverage;
      ordinary full verification remains responsible for artifact freshness.
      CLI/verifier and HTML surface tests pass (33), dashboard integrity and
      refusal tests pass (4), and schema/HTML regressions pass (95). Twelve
      authored browser cases cover desktop/mobile, JavaScript on/off and
      pass/fail/skipped reports; details open and close, with no overflow,
      browser errors or external requests. Screenshots and accessibility trees
      remain under ignored `_build/verification-browser/` in `vh-dev`.
      Full-suite, installed-wheel and hosted qualification remain pending.
- **Phase 1 landing reconciled:** PR #386 merged as `cf71a4fc` on 2026-09-12,
      after all 15 hosted checks passed at exact head `33630a4f` in run
      `34664827991`. The merge is an ancestor of the current integration.
      Reopening the retained P1.9 wheel and post-update HTML reproduces their
      documented SHA-256 identities. The browser report still records all six
      scenarios passing and hashes to
      `sha256:56acd588db168be463860b25af323a09bc106fbae08e2f16443deac89360d010`.
      This records the delivered graph slice; later phases and issues #295/#296
      remain open. The [Phase 1 tracker handoff](https://github.com/NVIDIA-dev/literate-ai/issues/296#issuecomment-5651388557)
      cites the reachable commit-pinned exit evidence. Documentation review,
      rendering (121 diagrams, 539 Markdown files) and repository layout pass.
- **Phase 2 shared-input prerequisite implemented:** Centralize source/view/asset/renderer
      observation shared by emission and staleness verification. Both currently
      select DAG requirements independently; adding health views in only one
      path could admit output the other cannot reproduce. Preserve existing
      static-test and interactive-DAG profiles, exact byte reproduction, asset
      refusal and source-drift checks. All 98 existing emitter, DAG, publication,
      rendering, surface and staleness tests pass in 37.662 seconds. Lint,
      formatting, OpenSpec, repository layout and current lifecycle/documentation
      reviews pass; documentation renders 121 diagrams from 539 Markdown files.
      `litai verify` passes authority, retains the stale-receipt failure and
      skips undeclared locks/HTML and disabled source intelligence. This shared
      loader is not a health dashboard; the version/verification surface, browser
      acceptance and full integration qualification remain open.
- **Version-check health implementation:** Register `version-check` / `health`
      over the existing canonical producer and publish its current-v2 source
      schema, preserving the legacy wire discriminator. Emit native, expandable
      pass/fail and diagnostic content without external assets. The existing
      publication and staleness paths reproduce its exact report and markup.
      All 102 HTML surface, emitter, DAG, publication, rendering, staleness and
      new version-health tests pass in 34.609 seconds, including real-producer
      identity, warning/failure preservation, malformed and wrong-project refusal,
      escaping, changed-report staleness and visible-byte tampering. Lint,
      formatting, OpenSpec, layout and current lifecycle/documentation reviews
      pass; documentation renders 121 diagrams from 539 Markdown files. Project
      verification passes authority, retains the stale-receipt failure and skips
      undeclared locks/HTML and disabled source intelligence. This is local
      implementation evidence. With the installed/browser proof below complete,
      the next acceptance action is full consolidated Python qualification and
      its own reviewed Phase 2 landing with screenshots. The installed-wheel harness now invokes
      the public version-check/render/verify commands, compares exact report and
      wheel bindings, tests current/stale/current after a controlled project-version
      edit, restores the owned fixture, and retains the resulting HTML for browser
      inspection. Its 17 existing harness tests and the actual wheel execution
      below pass. The verification dashboard and
      remaining Phase 2/3 surfaces are still open.
- **Version-health installed/browser proof:** At `b17d189b`, the complete
      installed-wheel gate passes with retained wheel
      `sha256:99a715bdb27b036682b617657af033b96000de9173ea20eae37e2bffd849a8e3`
      (2,632,962 bytes). Run `20260913T065620Z-11e68f` records the pass;
      retained stdout is 21,110 bytes with digest
      `sha256:9f2ca29cf092dda50271f2da53118e088b9a29ffff4c49206121aec95c0ea328`.
      The public CLI's exact version report is bound by
      `sha256:6829dc861e653bd8cee2a25f72ea445a40def6d892990a8effbe0db80b503794`;
      the version HTML is 15,205 bytes with digest
      `sha256:880d752f57e3f6b4b46edad8d4ff0e250ea61a88d43cfd17e88390a39fc8048a`.
      Retained wheel and HTML bytes were independently rehashed. Installed
      verification observes current/stale/current across the owned version edit.
      All four isolated-file browser scenarios pass (1280px desktop and 375px
      mobile, JavaScript enabled/disabled, all external requests forbidden):
      five disclosure controls expand visibly, exact provenance matches, and
      console/page/request/HTTP errors and document overflow remain absent.
      Collapsed/expanded screenshots and accessibility trees are retained;
      mobile collapsed and desktop expanded screenshots were visually inspected.
      Browser result digest:
      `sha256:9402a5a7858f0d3d7b3250c854cae99fcdff826c3e694e11c7062d9a7be506c6`.
      This qualifies the version-check slice locally. Its full consolidated
      Python/hosted matrix, separate reviewed landing with screenshots, and
      verification-dashboard contract remain outstanding.
- **Consolidated version-health wheel/browser proof:** At `330dd21a`, the
      retained installed-wheel run `20260913T070847Z-98b5b5` and its root step are
      passed. Wheel `sha256:c472ecff3f6de841eda4d34f6eb1155a120ecda6acb5da30e9e179478f9a7cdc`
      is 2,632,964 bytes; its retained manifest binds the exact full Git revision.
      Root stdout is 21,110 bytes,
      `sha256:ce70a55016e51e2928f88842092608a43e5eb290becf875b9add25e2ebfb8dc4`;
      stderr is empty. The installed CLI records current/stale/current for the
      owned version edit. Exact report identity is
      `sha256:ee6aa5278370e832dc1143203be33858b774bd8f34c069e097f331efe4a5a0f1`.
      Retained HTML `_build/ci-html/run-pdfo4_rv/version.html` is 15,205 bytes,
      `sha256:98ee4906cdd3b003cb1095333db5608f2164c93559690e2acbb13a2a10c92904`;
      its 2,102-byte `artifact.json` has digest
      `sha256:b4e926de82759c9214e5f102418dd038c4dc8a24067df1f915964fc3e07eea02`.
      All four isolated-file browser scenarios pass on those exact bytes:
      desktop/mobile with scripts enabled/disabled, five visible expanded
      disclosures, exact provenance and no console/page/request/HTTP errors.
      Browser results are retained in the Cargo worktree at
      `_build/version-health-consolidated-browser-proof/results.json`, with digest
      `sha256:ec4276139bc4de69fb864ef8b9f355078758fffb9f755ff1c9b9e14f296d4446`.
      Wheel, HTML, artifact record, browser results and both root log streams were
      independently rehashed before recording this evidence. These are local
      installed/browser results; the full Python result is recorded below.
      Hosted qualification and the separate reviewable Phase 2 landing remain
      required. Later production-admission work is not part
      of this revision or proof.
- **Consolidated version-health Python qualification:** The frozen `330dd21a`
      full run completed successfully: 4,687 tests in 5,187.502 seconds, 30 skips.
      Run `20260913T070831Z-7a0bad` and its root Python step are passed; the
      checkpoint is removed. Retained stdout is 4,191 bytes,
      `sha256:fbd22f112b26bb0324c03993196f8668d3f89a437450c63774e86cac4f90eba0`;
      stderr is 8,342 bytes,
      `sha256:45dac68c16cd61cc42f6f24f1b168caf3dee3af89bed2071c666543255cfe1d0`.
      Both streams were independently rehashed against the ledger. After this
      terminal pass, integration `f0677dc7` adds the already Windows-tested
      Windows ZIP fixture repair and the wheel/browser evidence record. All 10
      archive-reader/version-health tests pass in 4.755 seconds; lint, formatting,
      current driver/documentation reviews, OpenSpec and layout pass. The full
      suite result applies to `330dd21a`, not a fabricated run at the later merge.
      Desktop/mobile screenshots before and after disclosure expansion are now
      retained in `docs/architecture/assets/version-health/` and linked from the
      architecture guide. The new view has no earlier HTML version-health surface
      to present as a pre-feature screenshot. Hosted qualification and reviewed
      landing remain open; the security-admission branch is outside this proof.
- **Lock-health read-only prerequisite:** The existing Component checker constructs
      both lock and resolution-audit stores before checking currentness. Their
      matrix storage previously created directories even in check mode. The stores
      now report missing artifacts without initialization, revalidate safe storage
      ancestors on reads, and leave directory creation to explicit updates and
      operation locks. Four regression cases cover missing-state reads, actual
      update/readback, non-directory/traversal refusal and post-construction storage
      aliases. The store, cache-lock, CLI-lock and project-verification suite passes:
      45 tests in 30.051 seconds, one Windows-only junction test skipped on this host.
      Lint, formatting, OpenSpec, layout and current authority reviews pass; all
      121 diagrams render from 539 Markdown files. Project verification passes
      authority and still rejects the stale receipt, with undeclared locks/HTML
      and provider `none` source intelligence skipped. Full integration and hosted
      qualification remain open for this change.
      Shared preparation, input revalidation, paired checks and publication now
      live in `adapters/component_lock_operations.py`, with CLI delegation. An AST
      comparison verifies that all six extracted definitions are unchanged apart
      from symbol renaming. All 24 CLI-lock, large-review and architecture tests
      pass in 8.208 seconds. This extraction adds no new public wire format.
      The verifier now retains per-Component observations: full existing checker
      reports, distinct project-relative paths and structured refusals. Its public
      gate envelope and verdict rules remain unchanged, including the early
      repository-lock failure and undeclared-lock skip. All 36 observation,
      project-verification, CLI-lock, matrix-read and architecture tests passed in
      29.509 seconds before HTML integration.
- **Lock-health installed browser-state qualification:** Wheel `64e6dbe0`
      passed installed qualification with actual current, missing-audit/not-current,
      malformed-lock/error and no-committed-lock/skipped states. Each report agrees
      with the public verifier, has an exact retained HTML record, and restores the
      original lock/audit/configuration bytes. The 24 focused tests passed in 15.915
      seconds; lint, format, OpenSpec, layout and reviews passed, and documentation
      rendered 121 diagrams from 539 Markdown files.
      Run `20260913T112352Z-1dbe3e` retained wheel manifest
      `_build/ci-wheels/run-6f2_l3ff/manifest.json`: wheel 2,638,873 bytes, SHA-256
      `8c37ba24bc4ded0d5398a9c05e7507d0a2f3f8e6d6bbf86ffdb5ed722732a4ab`.
      The 30,733-byte stdout digest is
      `2350bec4338b10d89cc174d33a0f4619fba7b0cab23c9040a9b2fa17551cf8db`;
      stderr is empty. Independent rehashing verified the wheel and all four HTML
      files, records and source identities. All 16 isolated-file browser cases passed
      at 1280px/375px with scripts enabled and disabled: exact visible JSON, native
      disclosure interactions, no external requests, runtime failures or horizontal
      overflow. All eight expanded no-script screenshots were manually inspected and
      retained in [the architecture guide](../../architecture/html-observability.md).
      Full Python qualification at consolidated `d3199b67` passes: 4,702 tests
      in 6,115.726 seconds, 30 skips, run `20260913T114359Z-cabfdd`. The terminal
      ledger, retained stdout/stderr sizes and hashes, clean revision and removed
      checkpoint were independently checked. Stdout: 4,188 bytes, SHA-256
      `9def2befe7120edb96e1d3732b1bb3c185b9dfab3f5c777c80d7d20e700b4555`;
      stderr: 8,339 bytes, SHA-256
      `3a99cc7325cbfface6522c9b1088e80ab8c53467d2a73bd558859c90941148df`.
      Hosted CI remains required before merge.
      `healthy`: HTML 10,041 bytes, SHA-256
      `7c2a368e211ffae7b87b741872bb4b9d693714641573f37e7e913a8c7876c71d`; browser result SHA-256
      `ba882ec81dedb85ef8a90a79d88775356de514edd5c1b338feec30fd56f98b15`.
      `not-current`: HTML 9,905 bytes, SHA-256
      `e0444be60595bcafd7159956a929e401cf486c6c094e2bebbf218f6a1edbdc52`; browser result SHA-256
      `b97a65d2d9c2cd8b73e3fe8a0c00c0d36a9101e410afd048ea4481b8213261ae`.
      `error`: HTML 6,701 bytes, SHA-256
      `2cc43710068dc8e0c1a1c8a7028e81e2b8a09431231388cdfd3561aa7fb8a495`; browser result SHA-256
      `a1d26b15f233e03549d5ca2e3c786d817427437b69849a6a3f116afd4ae5b2dc`.
      `empty`: HTML 6,349 bytes, SHA-256
      `b01aabee725b402629bcd8b954dc53e561e32d6b6e20cf8af310e8acdfeb3957`; browser result SHA-256
      `46802abae6955ccec4b360ffce07e146732c2e4ce2a8f9ede6d539bcaad2e4b5`.
- **Lock-health qualification repair:** The full suite at `5366a12a` stopped
      after 1,405 tests (23 skips, one failure) in 1,360.735 seconds. The catalog
      closure check rejected a partial structured-object declaration inside the
      intentionally open retained command report. Keep the outer health row closed,
      describe the report's required discriminators as dictionary constraints and
      preserve all producer-owned diagnostics. Also require `current` and
      `not-current` rows to agree with the report's boolean. The existing full-suite
      failure and checkpoint remain retained; qualification must restart fresh after
      this repair. Earlier wheel/browser results remain attributed to their earlier
      exact source revisions. All 14 catalog, lock-health HTML and observation
      tests now pass, including the previously failing catalog closure check and
      explicit preservation of dynamic diagnostics with strict row currentness.
      Lint/format (1,022 files), OpenSpec, layout and current driver/documentation
      reviews pass; 121 diagrams render from 539 Markdown files.
- **Lock-health HTML implementation:** The new `lock-health` / `health` 1.0.0
      surface uses the shared current observation, with the new v2
      `project-lock-health` schema registered in the current catalog and compatibility
      policy. Component and repository lock orchestration moved to adapters; CLI
      wrappers retain error translation. An AST comparison verifies all 16 moved
      definitions unchanged except neutral error/import names. All 55 shared
      orchestration, verifier, repository, provider and architecture tests pass in
      124.362 seconds. The native expandable view preserves gate state, each
      Component's distinct path, artifact checks, identities, provider decisions and
      refusals. It has no external assets and does not call full verification.
      All 166 HTML tests pass in 40.723 seconds, including an actual lock/audit pair:
      current rendering preserves the verifier result; removing its audit fails
      lock verification and stales retained HTML; visible tampering fails validation.
      The installed-wheel harness now covers current/missing-audit/restored currency
      through the CLI and retains the artifact. This new harness has not yet passed
      installed-wheel qualification. The first wheel run at `99720040` reached
      this harness and refused its redundant `+python` selector on the already
      configured starter Component. The harness now preserves the starter's
      existing selectors instead of injecting an OS/language selection. This
      fixture repair still requires a new installed-wheel run.
      The repaired run at `3b0f941d29118c668512726b6abf85bf7d6be7f2` passes:
      retained wheel `c2db2f5d8de0a12e526e4789f24bb29af83254a18110336e25922b0991a59009`
      is 2,638,760 bytes, independently rehashed from
      `_build/ci-wheels/run-ebcszkuu/manifest.json`. Durable run
      `20260913T100134Z-382de9` and its root wheel step both pass;
      retained stdout is 23,575 bytes with SHA-256
      `b5f389f35e4dbcb29132521c4695f6cac27ac4e544de92f3c2c6c7fa8b0d6cb3`,
      and stderr is empty. Both logs were independently rehashed.
      The installed CLI proves current/missing-audit-stale/restored-current currency
      and exact verifier rollup agreement. The retained HTML is 10,041 bytes,
      SHA-256 `ab71b940ad2e2bfce23821f3af35acadb23de0580225200071c81db11382dcad`;
      its artifact record is 2,102 bytes,
      SHA-256 `d8b3c07f95b713c2e2e2a7a81a89536d7e378c0a8703f305839ac55fce4b8687`.
      All four desktop/mobile and scripts-enabled/disabled browser cases pass on
      that isolated single file, with exact visible report/provenance agreement,
      working native disclosures, no extra requests, runtime errors or overflow.
      Four inspected no-script screenshots are retained under
      `docs/architecture/assets/lock-health/`. Full local integration, hosted CI
      and a separate reviewed PR remain outstanding.
- **Phase 2 producer inspection:** `verify_project_from_args` includes the
      `html-observability` gate, whose staleness adapter reloads sources and
      reproduces rendered bytes. Calling this producer from a verification HTML
      source would recurse through every declared verification artifact. Do not
      suppress that gate or translate a missing observation into a passing result.
      Implement the nonrecursive `check_versions` health view first, preserving
      its canonical report, overall `ok`, editable-metadata diagnostic and
      unavailable-project state. Publish its actual source schema in the current
      v2 catalog before assigning a source-schema URN. The verification view
      additionally needs an explicit report-snapshot and input-currency contract;
      neither historical report integrity nor current HTML bytes alone proves
      that the project currently passes verification. This inspection does not
      complete either dashboard or change any release requirement.
- **Historical Phase 0–1 progress:** P0.3 passed after PR #375 landed as main `2f09c295` with
      all 15 exact-head hosted checks passing. The detailed plan retains the
      per-requirement report and distinguishes the 12 landed schema tests from
      15 locally verified follow-up tests (including seven schema mutations).
      P1.1 is implemented and fully qualified locally: immutable typed
      records bind ordered sources/view/renderer and reject unknown fields,
      tampered provenance and contradictory artifacts/results. Fourteen new core
      tests pass (49 with HTML/schema-catalog coverage), along with lint/format,
      layout, OpenSpec and documentation gates. The fresh full Python gate passed
      3,844 tests (3,814 passed, 30 skipped, zero failures) in 2,166.675 seconds;
      its checkpoint was absent before the run and removed on success.
      P1.2 now implements the actual authority-graph surface, preserving the existing
      producer JSON and identity and refusing unsupported views or invalid authority.
      Its source adapter and schema pass the 80-test combined graph/HTML/catalog
      batch and all fast checks. P1.3 now emits and inspects single-file static
      HTML bytes, with 14 emitter tests and desktop/mobile browser checks over
      the real graph, empty graph and hostile-label fixtures. P1.4 now integrates
      the pinned interactive DAG and bound source excerpts. Combined P1.2–P1.4
      local qualification passes; proceed to the P1.5 publication/cache CLI.
      Hosted integration remains pending. Development browser fixtures explicitly
      use a synthetic distribution binding, not installed-release evidence.
      P1.4's read-only excerpt adapter now passes ten focused tests (104 in the
      combined graph/HTML/catalog batch), including complete-file hash checks before
      truncation, source changes, aggregate limits and unsafe-path refusal. The
      selected Cytoscape.js CDN bytes match the exact upstream distribution and
      have a reviewed SRI pin in the detailed plan. The complete emitter now binds
      its presentation profile and all projection modules, embeds source previews,
      and retains native readable content when scripts or the CDN are unavailable.
      Eight DAG-specific tests cover actual emission, nested project roots, source
      drift, profile separation and byte mutations. Browser qualification exercises
      filtering, exact source selection, focus/pan/zoom, real pointer selection,
      offline and SRI-refusal states, no-script disclosures and iframe embedding.
      The detailed plan retains 14 passing real-browser scenarios and the fresh full
      Python gate: 3,887 tests run, 3,857 passed, 30 expected skips, zero failures in
      2,127.324 seconds, with no checkpoint reuse and an unchanged tested snapshot.
      CLI/cache publication, installed-distribution binding, staleness checks and
      the derived-project/update exit journey are not yet qualified.
      P1.5 CLI publication/cache custody is now in progress under the detailed plan:
      use existing CAS/reference machinery, non-writing cache reads, actual imported
      wheel/catalog binding and revalidated atomic derived-output publication.
      The non-writing index and imported-wheel/catalog observer now pass a combined
      73-test local batch with existing storage, Standard-binding and HTML checks.
      Development checkout observation refuses as intended; synthetic installation
      fixtures do not claim installed-release proof. The cache/publication service
      and CLI are now implemented locally: 34 new tests exercise all
      cache modes, exact cached-byte reproduction, typed CLI results, safe output
      replacement and failure preservation; 129 combined tests pass. The fresh full
      Python gate passed 3,941 tests (3,911 passed, 30 expected skips) in 2,164.802
      seconds against the unchanged candidate, without checkpoint reuse. Public CLI
      acceptance now passes on the actual `46d8b3fd` wheel. The explicit HTML
      retention follow-up also passes on `09dff624`, including reopening the retained
      bytes after runtime cleanup. P1.6 now has the declared-artifact verification
      gate, with current-byte reproduction and no implicit host/config/cache writes.
      Its CLI and contract regressions pass; P1.7 admits exactly the ten observed
      wire contracts, excluding the internal surface record. The final focused
      batch passes 250 tests; fast checks and the unchanged-diagram retry pass.
      P1.8 now provides a byte-identical framework/template agent wrapper, passing
      static skill admission. The clean `23898155` installed wheel now passes
      the real P1.9 parent-update journey: changed inherited source and graph,
      current/stale/current verification, and independently retained HTML.
      Six actual-browser desktop/mobile scenarios pass from an otherwise empty
      directory, including source selection, zoom/pan, pointer interaction,
      offline and no-script fallback. The detailed plan records exact identities
      and visual/accessibility review. The clean `1e16d879` full Python gate now
      passes 4,030 tests (4,006 passed, 24 skipped, no failures) in 2,158.871 seconds;
      no checkpoint was reused and it was removed on success. Retained run
      `20260911T185753Z-87b0a3` has a passing outer `n0001` node and terminal stderr
      summary. That result covers the consolidated HTML work, not later orchestration.
      PR [#386](https://github.com/NVIDIA-dev/literate-ai/pull/386) first ran hosted
      qualification on that head; the current combined candidate is `873426f2`,
      recorded in the landing queue below. Land only after its full matrix passes,
      then annotate #296 with the
      reachable commit-pinned evidence. Later phases and the parent item stay open.
      Nothing waits on MAC-CONTRACT-002; only the
      CycloneDX projection path does.
- **Hosted repair history (superseded candidates):** PR #386 at `1e16d879` could not land:
      Windows Python 3.12 wheel qualification refuses the initial cache-off/read-only
      HTML publication, and test shard 3 finds a CRLF assumption in the real parent
      fixture. Preserve both failing jobs in run `34640229582`; cancelled shards are
      not passes. First repair the publication snapshot across writer close (Windows
      only guarantees final write timestamps after closing the writing handle),
      retaining descriptor-bound identity, exact-byte readback, no-follow checks,
      no-clobber publication and foreign-file preservation. Then make the owned Git
      fixture's byte policy explicit and test it under ambient automatic CRLF
      conversion. Require focused regressions, fast gates, a fresh full local gate
      and all exact-head hosted checks before landing; local simulation is not
      Windows qualification. The timestamp diagnosis is inferred from the failing
      publication path and the platform contract, pending hosted confirmation.
      The repaired publication, render, staleness and installed-HTML harness batch
      passes 89 tests. The close-time timestamp regression fails against the old
      publisher; the explicit ambient-CRLF fixture reproduces the hosted assertion
      against the old harness. Both pass after repair. Changed bytes or a replaced
      temporary file at writer close still refuse, preserving foreign occupants.
      Lint/format (905 files), repository layout, OpenSpec (two changes), and
      documentation checks pass (zero audit findings, nine fence tests, 260 diagrams
      across 804 Markdown files). The reviewed driver pin is refreshed; a fresh
      complete Python run on the committed repair is the next qualification gate.
      `litai verify` passes authority, skips undeclared locks/HTML and disabled
      source intelligence, and still fails the existing stale receipt; it was not
      rewritten to claim these focused checks are full release evidence.
      The clean `4c3d2758` full local run passes 4,033 tests (24 skipped), but hosted
      run `34646533636` still refuses the first installed HTML render in Windows job
      `103418541952`. Writer-close handling alone did not resolve that failure.
      Inspection of CPython 3.12.10's `win32_xstat` and `_Py_fstat_noraise` identifies
      a distinct cross-API timestamp mismatch: pathname stat copies birth time into
      `st_ctime`, while descriptor stat retains metadata change time. The HTML reader
      currently compares these different clocks directly. Add a regression for that
      condition, compare shared identity/size/mode/write-time fields across APIs, and
      retain independent full before/after snapshots (including each API's ctime) so
      neither descriptor changes nor pathname replacements are admitted. Hosted
      confirmation remains required; do not mark the failed run green or merge it.
      The distinct-clock regression fails at the reader's cross-API comparison
      before the repair and passes afterwards. Separate descriptor-clock and
      pathname-clock drift regressions still refuse. The 92-test HTML publication,
      rendering, staleness and installed-harness batch passes in 6.245 seconds.
      Lint/format, layout, OpenSpec and documentation checks pass. Reapply the same
      independently observed-clock rule to the new orchestration document reader
      before consolidating the locally qualified slices for a fresh complete run.
      The same hosted run's Windows test shard 2 also fails 35 cases (463 passed,
      15 skipped): the monorepo input reader and read-only reference-index snapshot
      compare pathname ctime directly with descriptor ctime. Apply the same common-field
      binding plus independent full-snapshot checks there and add stable-clock/drift
      regressions before the consolidated full run. Two cancelled shards are not passes.
      The stable-clock regression reproduces both additional readers' refusals before
      repair. After repair, both independently drifting clock sequences still refuse.
      The consolidated HTML/monorepo/index/orchestration/skill-gate batch passes 191
      tests in 75.656 seconds. Lint/format (917 files), layout, both OpenSpec checks,
      documentation (260 diagrams / 808 Markdown files), zero audit findings and
      nine fence tests pass. Root/template and both generated orchestration skills
      separately pass all six non-model checks. The local consolidation preserves
      both branches' changelog and roadmap entries and recomputes review pins;
      a fresh full run of the committed combined tree is required before pushing.
- **Current consolidation action:** the full-suite-qualified renderer `fb5f1055`
      is integrated with the main/monorepo checkpoint in local merge `0afa3e66`.
      All 118 HTML tests pass on the combined tree, together with lint/format
      (898 files), layout, OpenSpec and documentation checks (zero audit findings,
      nine fence tests, 260 diagrams across 802 Markdown files). The
      installed-wheel smoke probe invokes only the
      isolated wheel CLI on the existing `onboard create` fixture, compare embedded
      source/provenance with canonical graph and observed distribution identities, and
      prove cache-off/read-only misses and exact repeat cache hits. Retain the artifact
      in the smoke workspace. This is P1.5 qualification, not P1.9 update/browser proof;
      the probe must not mutate the host installation, publish, or claim completion
      before the consolidated wheel actually passes it.
      The probe and existing wheel-custody unit tests pass together (28 tests), using
      the frozen candidate's contract implementation. Its synthetic oracle tests
      reject byte/provenance/graph/distribution drift, ambiguous JSON, cache-hit writes
      and zero-exit refusals. These are harness tests, not an installed-wheel success.
      The graph CLI's performance observations use a separate test object directory
      so they cannot mask the render cache-off/read-only no-write assertions.
      After restoration into the consolidated checkout, 68 probe/wheel/storage/
      installation/public-CLI tests pass together in 3.016 seconds, with lint and
      formatting across 900 files. The actual clean `46d8b3fd` wheel run now passes,
      including the derived-project HTML/cache probe; exact wheel/distribution/HTML
      identities are retained in the detailed plan. Successful workspace cleanup
      removed the HTML but retained the wheel and result record. The `09dff624`
      follow-up now retains and rechecks HTML bytes and the typed artifact record
      independently of workspace cleanup; the detailed plan records exact identities.
      The staleness gate and exact wire admission are now implemented locally;
      finish their qualification and the remaining Phase 1 checks.
- **Implementation:**
  - [x] Define a versioned HTML observability artifact schema bound to existing identities
        (`schemas/v2/html-observability.schema.json`, eleven public contracts under
        `urn:literate-ai:schema:v1:html-observability-contracts`, registered in
        `schemas/v2/index.json`)
  - [x] Keep Phase 1 artifacts single-file with embedded JS/CSS; no companion asset trees — the contract
        enforces this fail-closed (`embedding.single_file` const `true`,
        `companion_asset_count` const `0`; CDN libraries require `https` plus Subresource
        Integrity). P1.3's byte emitter now inspects actual tags, asset declarations,
        counts and embedded provenance; local browser fixtures open without companion
        files or unexpected network requests. The `23898155` installed-wheel
        post-update artifact opens alone in desktop/mobile browsers; only the
        declared SRI-pinned CDN is requested, and offline/no-script fallback passes.
  - [x] Define the rendering-skill contract (JSON surface + named view in, one `.html` out)
        as four further contracts: `…-surface` (a registered JSON surface and the views it
        supports), `…-render-request`, `…-render-refusal` (typed, closed code set), and
        `…-render-result` (exactly one of artifact or refusal). Rendering fails closed
        rather than emitting a partial or unpinned artifact, and the request carries no
        model, provider, or network authority of its own.
  - [x] Author `skills/agent/render-html-observability/SKILL.md` in Phase 1; both
        framework/template copies pass static admission and match byte-for-byte.
        `docs/architecture/skills.md` defines agent skills as wrappers over `litai`
        verbs; the wrapper now follows the implemented render/help/verify commands.
  - [ ] Follow the phased plan in #296 only after the contract in #295 is accepted
- **Evidence:**
  - [x] A focused artifact test binds HTML provenance to lock or content identities
        (`tests/unit/test_html_observability_schema.py`, 9 tests; provenance `source_bindings`
        and the renderer binding both resolve to `urn:literate-ai:schema:v1:content-identity`).
        Verified non-vacuous by mutation: relaxing `companion_asset_count`, dropping the
        `integrity` requirement, and emptying the staleness `allOf` fails 3 of the 9.
  - [x] No daemon, second planner, or operator-adoption verb is introduced (schema and test
        only; no `src/` or CLI change in this pass)
  - [x] Phase 1 exit: real derived-project update changes inherited authority;
        regenerated `graph.html` has matching embedded provenance and opens alone.
        Clean wheel `23898155`, current/stale/current gate observations and six
        desktop/mobile browser scenarios pass; see the detailed plan's P1.9 evidence.

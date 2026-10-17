# HTML observability — reconciled completion scope

- **Status:** partial
- **Owning queue item:** [OBSERVE-HTML-001](active-work.md#observe-html-001-generate-single-file-html5-visual-observability-artifacts)
- **Completion / archival evidence:** Audit of `59ec8fae` against the original
  [proposal #295](https://github.com/NVIDIA-dev/literate-ai/issues/295) and
  [phased plan #296](https://github.com/NVIDIA-dev/literate-ai/issues/296).
  Implementation and historical qualification are distinguished below. This audit
  is not a new installed-wheel, browser, hosted CI or release attestation.

## Scope and tracker reconciliation

Both historical issues closed on 2026-09-17. Their last scope comments included the
work in 1.1 while explicitly leaving implementation and qualification obligations
open. No comment inspected provides a requirement-by-requirement completion record.
The old unchecked roadmap and references to those issues being open are stale.
The current public tracker search did not identify a replacement HTML umbrella.
The checkout's `origin` still identifies the archived repository, so the audit
also queried `jordanhubbard/literate-ai` explicitly. Neither tracker was mutated.

The graph is delivered. That does not close the separately requested later phases.
The original Phase 4 explicitly has no fixed scope: instructional media and 3D are
ideas to revisit after Phases 1–3 establish useful requirements. Existing instructional
videos do not by themselves prove an observability multimedia acceptance contract,
and an unspecified 3D feature must not become a manufactured release blocker.

## Requirement and evidence matrix

| Original requirement | Current evidence | Audit conclusion |
| --- | --- | --- |
| Phase 0: common artifact/provenance and rendering contracts; single-file policy; pinned external assets | `contracts/html_observability.py`, v2 HTML schemas, registry and emitter | Implemented; historical contract acceptance is recorded. |
| Phase 1: interactive repository DAG, filtering, pan/zoom, source excerpts; real derived-project update journey | `html_dag_view.py`, `html_source_excerpts.py`, `html_render.py`, `cli/render.py`; archived P1.9 record | Implemented. Historical wheel `23898155`, six browser scenarios, hosted head `33630a4f` and merged PR #386 are recorded; artifacts were not reopened in this audit. |
| Cross-cutting: exact version/provenance, cache custody, regeneration and stale-output detection | `html_framework.py`, `html_publication.py`, `html_staleness.py`, installed HTML smoke harness | Implemented; current focused checks can corroborate these boundaries but cannot replace installed/browser proof. |
| Phase 2.1–2.2: version/verification and lock/audit health | Three registered health surfaces; installed version, lock and verification smoke paths | Implemented. Later recorded wheel/browser and hosted repair evidence supersedes early pending notes for these paths. |
| Phase 2.3: timing data by target/stage | `_load_performance_history` reads bounded `PerformanceSpan` records; emitter shows duration bars, run, target, timestamps and errors | Implemented as diagnostic timing history. Native bars satisfy the visualization without requiring a second charting library. No evidence found of installed/browser qualification for this surface. |
| Phase 2.4: workflow/routing hierarchy and policies | `_load_workflow_routing` reads declared, normalized workflow and routing catalogs; emitter exposes stage dependencies and routing records | Implemented as a browsable catalog. Installed/browser acceptance must show production/staging/dev nesting and routing sentinels remain understandable and accurate. |
| Phase 2.5: active/historical build and test runs, backed by a durable log | Durable run/step evidence exists in `evidence_ledger.py`; the six-surface HTML registry has no run/receipt producer. Timing history reads spans only | Incomplete. Timing spans do not prove run completeness or test receipt outcomes. Add a producer/view bound to durable run evidence, with explicit missing and interrupted states. |
| Phase 3: offline multi-project panes | `html_dashboard.py` observes declared HTML artifacts and emits digest-bound relative iframes | Implemented. The installed HTML smoke script does not call `render dashboard`; shell acceptance is not established by health-view tests. |
| Phase 3: cross-project filtering/menus | Dashboard markup contains static panes and an inert JSON metadata script; no selection controls | Not implemented. Add project/surface controls with keyboard access and a readable no-script fallback. |
| Phase 4: multimedia/3D extensions | Original plan explicitly leaves scope open-ended | Future design opportunity, not an enumerated acceptance gate. A concrete requirement needs separate admission. |

Implementation paths in the matrix are beneath
[`src/literate_ai`](../../src/literate_ai/); the registry and producers are in
[`html_surfaces.py`](../../src/literate_ai/adapters/html_surfaces.py), the shell in
[`html_dashboard.py`](../../src/literate_ai/adapters/html_dashboard.py), and installed
acceptance in [`scripts/installed_html_smoke.py`](../../scripts/installed_html_smoke.py).

## Qualification reconciliation

The [archived queue narrative](../history/roadmap/observe-html-implementation-history.md)
contains intermediate failures and later repairs. In particular, the recorded
`26ab0f1d` installed/browser evidence and `73dd5809` all-15-check hosted result,
merged through PR #398, supersede earlier health-rendering Windows timeout notes.
They do not establish acceptance of a different view merely because it uses the
same renderer. The original graph journey is recorded in the
[archived Phase 0/1 plan](../history/roadmap/observe-html-phase-0-1-plan.md).

The current installed harness exercises the authority graph, version health, lock
health and verification health. It does not select `performance-history`,
`workflow-routing` or the multi-project dashboard command. No retained browser
record covering those three later paths was identified in the inspected repository
records. Those claims remain unproven rather than being marked failed or completed.
The smaller current test suite also does not make archived test-count claims current.

## Remaining acceptance, in dependency order

1. Bind active and completed build/test history to existing durable run/step evidence.
   Preserve exact source identity and outcomes, distinguish diagnostic timing from
   verified test receipts, and make absence/interruption visible. Prove that changing
   the underlying run evidence makes the HTML stale.
2. Add project/surface selection to the existing offline shell. Preserve each pane's
   provenance and exact-input refusal behavior, local-file operation, keyboard
   accessibility and the no-script view. Do not introduce a server or live daemon.
3. Extend the real installed-wheel journey to timing, workflow/routing and multiple
   project panes. Retain exact artifacts and exercise current/stale/regenerated,
   tampered and missing-input cases. Inspect desktop/mobile and JavaScript on/off;
   show meaningful workflow nesting, selection behavior, no unexpected requests,
   no console errors and no overflow.
4. Qualify the combined implementation and archive a terminal, scope-specific
   evidence record before `litai work close OBSERVE-HTML-001`. Full release gates
   still belong to the release integration item. Closed historical issues are not
   permission to skip these remaining obligations or to reopen the graph work.

## This audit's verification

Source and historical scope inspection are complete. The focused Make gate
`python-check` with `PYTHON_TEST_PATTERN='test_html*.py'` passed all 11 tests,
along with compilation and host-path policy checks. These are regression checks,
not installed-wheel or browser qualification. Documentation authority review and
`litai project validate` also passed; the reviewed documentation marker was refreshed.
No external issue comments, issue-state changes, hosted jobs or publication were
performed as part of the reconciliation.

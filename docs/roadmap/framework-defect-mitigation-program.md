# Framework defect-mitigation program

- **Status:** active
- **Owning queue item:** [MITIGATION-FW-001](active-work.md#mitigation-fw-001-execute-the-framework-defect-mitigation-program)
- **Completion / archival evidence:** pending while MITIGATION-FW-001 remains open

Independent evaluation of the framework against its own premises identified defects in
five areas: Component-model expressiveness, Flavor taxonomy naming, directory-taxonomy
self-contradiction, `litai init --convert` correctness, and rollout evidence. This
document owns the cross-cutting ordering and design; each phase lands through its own
queue item with local acceptance evidence.

## Phase 1 — convert quarantine + hygiene (low risk)

Owner: `CONVERT-FW-002` (quarantine), `CONVERT-FW-003` (message/hygiene/docs).

- **D1 — Quarantine existing content.** During `init --convert`, after the
  already-initialized guard and before any scaffold creation, move every pre-existing
  entry at the target root except `.git/` into
  `<target>/_legacy/<UTC-timestamp>-<8-hex-content-digest>/`. Use `git mv` for
  git-tracked entries when the target resolves inside a Git work tree; plain `mv` for
  untracked entries and non-Git targets; mixed trees are handled per entry. Record the
  moved manifest (`from`, `to`, `vcs`) as an additive optional key in the
  initialization result. Delete the now-dead preservation branches (`.gitignore`
  merge, agent-shim delegation append, `_ensure_convert_docs_reachable`). Refuse
  symlinks at the target root that escape the tree.
- **D3 — Message.** Reword `project.already_initialized` to state conversion is not
  required and `litai update` may be needed.
- **C2-hygiene.** Ignore working-copy junk (`ap-export/`, agent worktree artifacts).
- **B6-docs.** Align `docs/architecture/component-flavors.md` with the actual init
  default (`build-make`, not `build-bazel`) and add the missing
  `documentation.ecosystem` axis to its axes list.

Evidence: new unit tests cover git-tracked moves (renames visible to `git status`),
non-git moves, mixed trees, hierarchy byte-equality, double-conversion rejection, and
symlink refusal; full non-live suite green.

## Phase 2 — harness inspection + wrapper; catalog single-source

Status: landed.

- **D2 — Harness inventory + wrapper.** `adapters/harness_inventory.py` inspects the
  quarantined legacy tree (build systems, test runners, packaging markers, CI OS
  matrices, release workflows) into `.literate/harness-inventory.json`
  (`urn:literate-ai:schema:v1:harness-inventory`) and generates a top-level
  `litai.harness.mk` wrapper delegating build/test/run/package to the detected legacy
  commands; undetected targets fail closed with `$(error ...)`. `detect_repo_flavors()`
  now prefers `build-cmake` when `CMakeLists.txt` is present. Vertical-slice tests
  execute the generated wrapper through real `make` invocations.
- **B2 — Catalog as source of truth.** `_FLAVOR_ALIAS_AXES`,
  `_FLAVOR_CANONICAL_BY_DIRECTORY`, and `FLAVOR_SELECTOR_CANONICAL_NAMES` are derived
  from the shipped catalog's `flavor.md` frontmatter at import; adding a flavor
  directory requires no Python registry edits. The phantom `platform.service` axis is
  gone (`google-workspace`/`microsoft-365` resolve to their declared
  `documentation.ecosystem` axis).
- **B3 — One physical catalog.** The shipped template flavors are byte-equal to the
  root catalog for every `literate-ai`-namespace flavor (`cpu` stays root-only as a
  sample-namespace fixture); drift was reconciled in favor of the richer root files and
  byte-equality is enforced for all directories, not just packaging.
- **A3 — De-hardcoded topology.** The Rust/JavaScript full-stack prompt special case
  was removed from `prompt()`; the protocol lives in the Component specification
  (`samples/full-stack-rust-js/component.md`, which gained explicit generated-test and
  no-build-file requirements). A boundary test asserts the framework envelope contains
  no domain-topology vocabulary.
- **C1 — openspec scoping.** SKILL.md now states precisely that `openspec/` peers are
  forbidden inside Components while Flavor declarations reserve
  `openspec/spec.md` for WHEN/THEN scenarios.

Deferred from this phase, recorded as queue items:

- **B4 — Per-axis file-shape enforcement, `standard-make-command-profile.json`
  rename, and the `bazel-preferred` → `bazel` target rename.** Deferred because any
  edit to a root flavor's authority bytes changes flavor revision identities and can
  invalidate checked-in component locks and receipts. The `bazel-preferred` rename is
  absorbed into [ADR 0010](../decisions/0010-canonical-flavor-naming-migration.md)'s
  reviewed re-pin. The Make filename repair landed at `ed1c52a4` with focused and
  installed-wheel evidence recorded under MITIGATION-FW-001; per-axis shape
  enforcement remains an ordinary follow-up.
- **B1/B5 — Canonical `<axis-prefix>-<value>` naming migration** for `package.*` and
  bare aliases. Same lock-identity constraint; designed in ADR 0010 with the
  deprecation-alias machinery.

## Phase 3 — flavor naming migration

One canonical scheme `<axis-prefix>-<value>` (dash) for all axes; legacy spellings
(`package.pip`, bare aliases) become deprecated aliases resolved through canonical
flavor selectors with deprecation notes in lock reports; decide flat-alias removal vs
generated coverage. **Designed in
[ADR 0010](../decisions/0010-canonical-flavor-naming-migration.md) (Accepted).** The
catalog migration command, canonical directories/names, Bazel target rename, and
one-release legacy selector aliases are implemented. All affected sample interface,
oracle, lifecycle-driver, runner, and documentation identities were re-pinned through
their normal review boundaries; the complete non-live suite is green.

## Phase 4 — component expressiveness (schema)

Additive optional `component.md` frontmatter behind `literate-markdown@2`:
`data_contracts` (typed schema attachments), `invariants` (verifier-evaluated
predicates), `error_taxonomy`, `performance_budgets`; typed acceptance-contract kinds
(`golden-io`, `exit-code`, `schema-match`, `property`); require ≥1 acceptance contract
for generatable non-sample components. **Designed in
[ADR 0011](../decisions/0011-component-domain-structure-literate-markdown-v2.md)
(Accepted for 1.1, 2026-09-13);** implementation is authorized and remains open. The A3 portion —
removing the hardcoded Rust/JS topology from the prompt builder and moving it into the
sample's authority with a domain-neutrality boundary test — landed in Phase 2;
the published v1 schemas remain untouched throughout.

## Phase 5 — proof sample + composition

Add one stateful/networked/persistent reference sample specified entirely through the
new metadata with zero prompt special-casing; begin per-node generation-cache scoping
so leaf changes revalidate only affected nodes. Full per-component build/link remains a
separate program (framework score-improvement recommendation 4).

## Rejected scope

- Do not rename all `openspec/spec.md` flavor directories in this program; the
  SKILL.md prohibition will be scoped precisely in a documentation-only follow-up
  (TAXONOMY-FW follow-up) to avoid churning 25 flavor declarations without need.
- Do not attempt universal source-disposability claims or enterprise containment here;
  those belong to the score-improvement program items already queued.

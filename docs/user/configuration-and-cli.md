# Configuration and CLI reference

## Current CLI scope

The installed command is `litai`. There is no `literate` compatibility executable.
Literate AI is a release-engineering and SDLC harness: `doctor`, `onboard`, and
`status` are the operator front door; `init` / `init --convert` remain the
lower-level mutators; the `dev` workflow verifies and rebuilds a project; and
`release` cuts a versioned line. Specification-to-source generation remains
available and improves in waves; it is not the front door.

The inventory below is grouped by that flow. It names every public command the
parser currently accepts. It is not a promise that every verb is equally
important, and it is not an invariant against later combinable aliases.

```text
# Adopt / template
litai doctor
litai onboard create
litai onboard adopt
litai onboard orchestrate plan
litai onboard orchestrate check
litai onboard orchestrate initialize
litai onboard orchestrate refresh plan
litai onboard orchestrate refresh check
litai onboard orchestrate refresh apply
litai status
# Flags on init: --convert [--plan], PATH --from URL[#REVISION]
litai init
litai update
litai reparent
litai catalog copy
litai flavor add

# Dev workflow
litai verify
litai lock
litai plan
litai generate
litai rebuild
litai build
litai test
litai run
litai package plan
litai package build
litai package verify
litai orchestrate plan
litai orchestrate run
litai clean
litai really-clean

# Release
litai release contributions sweep
litai release contributions disposition
litai release plan
litai release prepare
litai release check
litai release publish
litai release verify-published
litai release backport
litai release backport-status
litai release advance-default-branch
litai release rc
litai release state
litai release state set
litai release evidence explain
litai release evidence index
litai release evidence show
litai release evidence prune
litai release merge-pr

# Authority / catalog
litai catalog graph
litai catalog migrate-flavor-names
litai graph
litai graph rebalance
litai render html
litai render dashboard
litai project validate
litai project lifecycle rebind-standard
litai project documentation-update
litai project documentation-review
litai project tracker inspect
litai project peer-work
litai project worktree location
litai project parent checkout
litai project ci-plan
litai project guidance
litai project mac-contract
litai project convert-stage show
litai project convert-stage advance
litai project source-intelligence sync
litai project source-intelligence check
litai project retained-scope refresh
litai project retained-harness readmit
litai project retained-cargo check
litai project retained-cargo materialize
litai project retained-cargo admit
litai project test-receipt run-retained
litai project test-receipt update
litai project test-receipt check
litai project test-receipt require-current
litai project test-receipt verify-evidence
litai project test-receipt publish-evidence
litai project test-receipt require-current-evidence
litai project test-checkpoint inspect
litai project test-checkpoint annotate
litai project test-checkpoint revalidate
litai project test-checkpoint clear
litai project test-checkpoint import
litai component migrate
litai learn
litai design refine
litai design explain
litai design accept

# Operator
litai config paths
litai config migrate
litai config mcp show
litai config mcp write
litai config channel-parse
litai work record
litai work close
litai document verify
litai worker verify-model
litai worker resolve-nvidia
litai worker probe
litai worker health
litai worker cleanup plan
litai worker cleanup apply
litai worker wheelhouse
litai worker wheelhouse export
litai worker bootstrap
litai worker execute
litai worker execute-retained
litai worker acknowledge
litai matrix
litai perf show
litai perf chart
litai profile
litai cache publish
litai skills evaluate
litai prompt translate
litai version check
litai help

# Inverse / aspirational
litai spec format
litai spec validate
litai spec explain
litai spec scxml-review
litai spec attest
litai spec skills
litai spec status
litai spec merge
litai spec derive
litai spec audit
litai spec review
litai spec accept
litai spec qualify
litai spec refresh
litai spec conformance
litai spec coverage
litai spec diff
```

`litai run COMPONENT --entrypoint NAME` selects one surface of a previously built
multi-entrypoint Component. Without `--entrypoint`, the first declared entrypoint is
the stable default. Single-entrypoint Components keep the original `litai run
COMPONENT` form and reject a named selector.

Recommended later combinable verbs (not implemented; do not treat as aliases
today): `catalog graph` with `graph`; `project validate` with
`verify --gate=authority`; `really-clean` with `clean --all`;
`release backport-status` with `release backport --status`; dropping the extra
noun on `version check`, `cache publish`, and `worker wheelhouse export`.
`perf` reads recorded spans; `profile` runs a command under timing — related,
not duplicates. `spec skills` and `skills evaluate` collide in naming only.
Do not split `--convert` off `init`. Do not add staging/production CLI verbs;
those remain project workflow catalogs.

`literate.release.json` selects either canonical `semver` or `pep440` version syntax,
declares every mirrored version field, and separates read-only planning, local
preparation, exact gate evidence, and explicitly authorized publication. See
[Project release protocol](../architecture/project-releases.md).

## Repository policy configuration

Per-project repository behavior is centralized under `repository_policy` in
`literate.project.json` and read or atomically changed through the project configuration
store. This is distinct from operator-local MCP/worker files and from
`literate.release.json`, which owns artifact-version and publication mechanics.

| Field | Meaning | Implemented default when omitted |
| --- | --- | --- |
| `default_branch` | Writable trunk and release-cut source | `main` |
| `main_state` | `free` or `pre-release` | `free` |
| `pre_release_version` | Canonical `major.minor` target; required only in Pre-release | `null` |
| `default_component` | Optional default Component path or coordinate | `null` |
| `sample_component_roots` | Project-relative sample catalogs | `samples` |
| `work_queue` | Durable project work queue | `docs/roadmap/active-work.md` |
| `remote` | Git remote used by repository operations | `origin` |
| `merge_method` | Forge merge strategy | `merge` |
| `pull_request_labels` | Labels applied or inspected by tracker workflows | empty |
| `release_engineer_source` | README path and section containing release identities | `README.md#release-engineers` |
| `patch_authority` | `strict` or `loose` release-line patch authority | `strict` |
| `writers` | Logins eligible for loose-mode break-glass patches | empty |
| `branch_gc_minimum_age_days` | Minimum terminal-branch retention policy | `7` |

Repository-varying defaults belong in this policy where implementation supports them,
not hard-coded in agent instructions or duplicated across commands. Pre-release requires
a `pre_release_version`; Free requires it to be null. `main` remains writable for
ordinary work in either state.

The README source must contain an exact `## Release Engineers` heading followed by one or
more exact ``- `github-login` `` bullets. Only those identities can mutate release state,
create RC tags, merge release-line pull requests, and create/publish major or minor
releases. Under `patch_authority: strict`, they also exclusively own patches. Under
`loose`, a listed `writers` login may use patch break-glass only with
`--break-glass-reason` and only after the commit has landed on the trunk; this does not
grant major/minor or RC authority.

Every pull request body carries exactly one `Literate-AI-Release: major.minor` or
`Literate-AI-Release: none` line. The tracker inspection surface classifies an exact
current target separately from `none`, another line, and unknown/malformed declarations.
This classification supports review; it does not silently configure the forge.

This inventory names the public command tree without treating its current size as an
invariant. Run `litai help`, or append `help` after any command path, for the installed
version's exact arguments and nested operations.

Help is a verb at every command depth. Follow the scope you want to understand with
`help`: use `litai help` for the whole CLI, `litai init help` for one command,
`litai project help` for a command group, and `litai project validate help` for one
nested operation. The prefix form (`litai help project validate`) and conventional
`litai project validate --help` form are also accepted. `litai --json project validate
help` returns the same text in a stable `literate-ai/cli-help@1` result envelope.

Commands write structured JSON
when redirected or called through the API. Interactive `litai rebuild` instead shows a
compact topological DAG, per-Component source disposition, build-cache and repair status,
test totals, artifact, and receipt state. Executable Components also show their exact
execution command; importable libraries return typed `library_artifact` export and
import-surface authority without inventing an executable entrypoint. Use
`litai --json rebuild ...` to force the complete stable JSON envelope on a terminal.
The existing `literate-ai/library-rebuild-artifact@1` envelope is validated by
`LibraryArtifactProduct`: only a library-role export and typed import surface are
admitted, and rebuild requires the export to belong to the built Component revision.
Its content identity binds the projection; it does not itself prove acceptance,
authorize importing code, or complete build/test artifact retention.
Local `litai build` and `litai test` now copy an accepted library package before
removing its runtime. They retain the library envelope and package identity instead
of suggesting an executable command. Loading the copy rechecks its bytes against
the accepted directory blob; failed copy/publication leaves the prior export intact.
`litai run` refuses this product with `run.library_not_executable`. Remote build/test
also retain typed library products without suggesting `run`; the SSH worker refuses
library execution independently. Its CAS locator binds package metadata and observed
toolchains. SSH evidence transports the exact sealed package ZIP (including original
file permissions), verifies it against the separately transported artifact files,
and reconstructs the library metadata from the verified evidence manifest. This
preserves package custody; it does not authorize importing code or authenticate a
worker beyond the configured dispatch trust boundary. Combined release qualification
remains a separate gate.
For Cargo-backed libraries, Standard first runs `cargo test --locked --all-targets`
against the exported package with a disposable external target directory, then runs
the attributable `--litai-test` binary. Native target output goes to diagnostic
stderr, leaving the case-result JSON on stdout. Invalid integration-test imports or
failing native tests stop qualification before that binary; native success does not
replace selected-case evidence or independent library acceptance.
Non-interactive usage and workflow failures retain stable JSON envelopes and nonzero exit
status; interactive failures show the same stable code with a concise message.

Use global `-v` or `--verbose` before or after a command verb to observe each
framework-owned child process. Literate AI writes safely quoted argv, cwd, exit status,
and bounded child stdout/stderr to stderr, leaving human or canonical JSON results on
stdout unchanged. The flag follows nested `litai` calls and typed command-worker
requests; it does not change generation, cache, or lifecycle authority. Known secret
flags and values sourced from credential-like environment variables are redacted. Output
is still subject to the normal size limits and appears no later than child completion;
the start record is emitted immediately.

Use global `--debug` or `--debug=FILE` to trace harness SDLC stages (init, convert,
verify, plan, generate, rebuild, release) and redacted child-process argv. Default
destination is stderr; a named
file is truncated for this invocation and written as NDJSON. `--json` (or a
non-TTY stream) uses `literate-ai/debug-event@1` NDJSON; an interactive TTY
without `--json` uses `[litai:stage]` / `[litai:command]` / `[litai:map]` lines. Debug never
contaminates the stdout command result. It is not a `--verbose` superset:
verbose dumps child stdout/stderr, debug traces stages, child argv, and spec↔source maps.
`--debug` on `generate` / `rebuild` also asks generation to place `litai:spec
PATH:LINE` anchors; a framework scanner writes the sidecar and a Python helper
prints maps on stderr when `LITAI_DEBUG` is set (exceptions plus once per
public entrypoint). A tree without anchors still prints stages and one
`spec-map.unavailable` event.

Global `--discover-mcps` is off by default. When on, `litai` probes operator-catalog
`command` hints for reachability and writes a stderr summary. It does not write
tokens, replace `mcps.json`, or run on every command.

## Durable user configuration and state

Linux and macOS resolve durable configuration to absolute `LITAI_CONFIG_DIR`, then
`XDG_CONFIG_HOME/literate-ai`, then `$HOME/.config/literate-ai`. Windows resolves it to
absolute `LITAI_CONFIG_DIR` or the user's Roaming AppData Known Folder plus
`literate-ai`. The root holds reusable `workers.json`, `mcps.json`, and project-scoped
`projects/<project-id>/test.json`. Mutable worker observations and channel events use a
separate platform-resolved state root (`LITAI_STATE_DIR` is its absolute override).
Run `litai config paths` in a canonical project to report every resolved destination
without creating it.

Run `litai config migrate` to inspect recognized 0.8.x project/private assets without
writing. Run `litai config migrate --apply` to validate and move them. Migration rejects
symlinks and differing destinations, never prints file contents, and removes a legacy
source only after the private destination is durable.

### Generated-application settings

This is Literate AI's own configuration. Applications that Literate AI generates or
derives are separate: their runtime settings are user custody, not repository
authority, and never share the `literate-ai/` namespace above. Generated applications
that need their own per-user runtime configuration (named deployment/host profiles,
endpoint settings, credential references) reuse the same resolution/precedence/update
contract through `literate_ai.adapters.application_settings`, resolving to
`$XDG_CONFIG_HOME/<application>/settings.json` (falling back to
`$HOME/.config/<application>/settings.json` on Linux/macOS) or the user's Roaming
AppData Known Folder plus `<application>/settings.json` on Windows. An
application-specific environment variable (`<APPLICATION>_SETTINGS_FILE` by default) or
an explicit CLI value overrides that resolution; precedence is CLI, then environment,
then the settings file, then the specification default.

The versioned `literate-ai/app-settings@1` schema carries named profiles, each with
non-secret `settings` and a separate `credentials` namespace where every entry is
either a `ref` (an OS keychain or external secret-provider reference, preferred) or a
literal `value`. A file containing any literal credential value must have owner-only
permissions; diagnostics never surface a literal value, only a fixed redaction
placeholder (a `ref` pointer is not itself secret and remains visible). Loading an
unversioned legacy settings file upgrades it in place, preserving every existing key
under the default profile, so introducing or evolving this convention never discards a
user's existing settings. This capability does not yet ship a `litai config
app-settings` CLI surface or an OS-keychain writer; those remain future work tracked
against issue #307 and are deliberately out of scope for the initial contract.

Use `litai config mcp show` to read the typed operator catalog and
`litai config mcp write --server ID[:USE]` to replace it atomically after session
discovery. `litai config channel-parse MESSAGE --project PATH` validates an inbound
recipient trailer and project binding without executing or recording its body.

The per-user MCP list (`mcps.json`) is not project authority. An interactive
coding session follows `skills/agent/configure-operator-mcp/SKILL.md` to discover
already-connected session MCPs and write that file. Interactive `litai` without
an agent offers a thin ids-only create when that file is absent; `--json` and
non-TTY sessions skip the offer and do not write `$HOME`. Declining writes an
empty catalog. Mutagenic commands append author envelopes under the state `events/` only
after that catalog file exists, then `litai` fan-outs through listed Jira,
Slack, and Outlook servers when `institutional_channels` names the destination
(ADR 0025). Outage or a missing notify tool is fail-open with a stderr skip.
Optional extra `literate-ai[mcp]` installs the official Python SDK (`mcp`); the
CLI client itself is bounded stdio JSON-RPC and does not require that extra.
There is no drain or discovery daemon: an interactive coding session follows
`skills/agent/configure-operator-mcp/SKILL.md` to author the catalog,
`skills/agent/associate-release-jira/SKILL.md` to associate a ticket (not to
re-post envelopes), and `skills/agent/ingest-channel-work/SKILL.md` for inbound
recipient mail. See
[ADR 0021](../decisions/0021-operator-local-mcp-catalog.md),
[ADR 0022](../decisions/0022-channel-author-recipient-vocabulary.md),
[ADR 0023](../decisions/0023-mutagenic-cli-channel-fan-out.md),
[ADR 0024](../decisions/0024-project-mcp-catalog-hygiene.md), and
[ADR 0025](../decisions/0025-cli-operator-mcp-client.md).

Optional `institutional_channels` on `literate.project.json` may name a Slack
channel, Outlook mailboxes, and a Jira issue id. Tokens stay in the operator
catalog, not git.

The command surface and the newest application core are at different adoption stages.
For a locked Component graph, `litai plan` now selects the configured or first available
coding CLI without invoking it, resolves every node's effective model binding, and
delegates the exact per-Component execution plan to
`StandardProjectApplicationService`. Locked `litai generate` now uses the same plan and
preparation boundary, generates every Component independently into a fresh child
workspace, and returns a versioned custody envelope for those source trees without
building them. Unlocked legacy generation retains its version-9 single-tree report.
`litai rebuild` resolves the manifest-declared lifecycle driver. A Standard-bound
project runs the public in-process filesystem rebuild adapter, while an explicitly
content-pinned external driver remains available as an advanced integration.
`litai rebuild .` with Standard rebuilds every Component that currently has a
committed lock, in catalog path order, under one project lifecycle lock. Unlocked
catalog entries stay skipped. One failed Component fails the command closed and
does not commit `--update-receipt`. `litai rebuild components/NAME` remains the
one-Component entry point. The per-Component `StandardProjectLifecycleService` is
implemented and tested as an application API,
including source-only scheduling and typed artifact assembly. It also consumes
accepted-source memberships, re-indexes and re-authorizes hits, and publishes only newly
generated nodes after acceptance. Its application result proves exact
plan/cache-decision/lifecycle-result set equality, and its typed aggregate receipt can
exist only after project admission over that same membership. The initialized portable
starter uses this complete Standard path. The repository's remaining legacy sample
orchestration has not all migrated to it.

Components are inherited by descendants by default. Set `inheritable: false` in an
individual `component.md` when that Component is local teaching, qualification, or
private composition authority. It remains available to the current project and can be
copied explicitly, but `init` and `update` do not pass it downstream automatically.
This is Component policy, independent of which declared `component_roots` contains it.

The public filesystem composition root requires one provider-neutral
`ComponentCommandContract` per planned Component plus an exact local tool binding. Its
build, test, and execute commands are shell-free argument vectors whose typed path roles
are substituted only after the tool identity and locked build authority are checked.
The framework also exposes strict post-source evidence contracts for build outputs,
selected generated-test cases, execution, and Component acceptance. A public filesystem
composition now joins these contracts to production indexing, CAS-backed
accepted-source publication, and cross-process restoration with current reacceptance.
An optional filesystem Standard checkpoint store also records typed stage and retry
lineage and restores pre-acceptance source across process restart. It deliberately
replays all post-source trust gates; checkpoint evidence is diagnostic custody, not an
authorization or acceptance token.
The ordinary CLI and receipt finalization use this runtime when the project selects the
Standard binding. Automatic project-root lock discovery, link/package actions, and
root-integration acceptance still remain.

| Command | Purpose |
| --- | --- |
| `help [COMMAND ...]` | Show top-level or successively narrower installed CLI help. |
| `version check` | Require package, project, protocol, and release-tag version authority to agree. |
| `doctor` | Inspect platform-resolved user paths, native host dependencies, and coding-CLI authentication without requiring a project or invoking a model. |
| `status [--project PATH]` | Compose project authority, adopted conversion stage, Component locks, the current receipt, tracker state, host preflight, and the next operator verb in one versioned snapshot. |
| `onboard create PATH [--from URL[#REVISION]] [--refine REQUEST] [--apply --acknowledge] [--expect-plan SHA256]` | Plan creation without writes, then revalidate and call the existing init mutator only after explicit acknowledgement. |
| `onboard adopt PATH [--from URL[#REVISION]] [--root-plan SELECTION.json] [--run-baseline] [--apply --acknowledge] [--expect-plan SHA256]` | Plan brownfield conversion with detected Flavors, candidate build roots, quarantine entries and retained runners. Default adoption lands at `wrapped`; explicit multi-root selection is read-only pending per-Component materialization. |
| `init [PATH] [--from URL[#REVISION]] [--repository-fetch-total-seconds N] [--repository-fetch-no-progress-seconds N] [--repository-fetch-connect-seconds N]` | Create a canonical project, optionally inheriting a complete repository ancestor DAG under an explicit bounded fetch policy. |
| `graph [--format text\|json\|mermaid\|dot\|svg] [--output PATH] [--kind KIND] [--provenance PROJECT] [--ownership local\|inherited] [--inheritance inheritable\|private] [--edge-kind KIND]` | Solve effective repository/catalog authority, reject cycles, filter a closed view, and export the provenance DAG. Repeat kind, provenance, and edge-kind filters for unions. |
| `update [PATH] [--apply] [--adopt-added] [--take-upstream PATH] [--keep-local PATH] [--repository-fetch-*-seconds N]` | Plan or safely apply inherited-catalog and framework-scaffold changes while preserving local authority and reporting the selected bounded fetch policy. Repeat resolution flags only for explicitly reviewed inherited-catalog paths. |
| `reparent URL[#REVISION]\|none [--apply] [--repository-fetch-*-seconds N]` | Plan or compare-and-swap an explicit repository-parent change under the selected bounded fetch policy. |
| `cache publish [--project PATH] [--target ID]` | Verify and copy immutable runtime source-cache entries into the configured committable target. |
| `config mcp show` | Read the typed operator-local MCP catalog. |
| `config mcp write --server ID[:USE]` | Atomically replace the operator-local MCP catalog with explicit server selections. |
| `config channel-parse MESSAGE [--project PATH]` | Validate an inbound recipient trailer and project binding without executing it. |
| `work record ID --title TEXT --priority P --owner TEXT --direction TEXT --conclusion TEXT --implementation TEXT --evidence TEXT [--project PATH]` | Append one validated, resumable work item to the active roadmap. Repeat checklist and dependency options as needed. |
| `work close ID [--project PATH]` | Close one work item only after every implementation and evidence checkbox is complete. |
| `worker resolve-nvidia --compatibility FILE --worker-id ID --python-abi ABI [--observations FILE] [--toolkit VERSION] [--package NAME==VERSION]` | Select the oldest compatible exact stack from retained primary-source authority and a typed worker observation; never fetch or infer compatibility. |
| `document verify --manifest FILE --component FILE` | Run the packaged independent document-pair structural, access, credential, geometry, notes, and placeholder acceptance oracle. |
| `skills evaluate CORPUS [--project PATH] [--fire-threshold N] [--fragile-threshold N]` | Run a synthetic prompt corpus against the real skill catalog and classify each outcome as correct/misroute/over_greedy/fragile_pass -- a behavioral check that a skill's own description would distinguish it, distinct from validate's structural checks. |
| `catalog graph [--project PATH]` | Inspect the repository provenance DAG rooted at Literate AI. |
| `catalog copy SOURCE ITEM [ITEM ...] [--project PATH]` | Copy selected Flavor, Skill, or Component catalog items from another Literate AI project. |
| `clean [--project PATH]` | Remove only the marked host-specific `OBJ_DIR`. |
| `really-clean [--project PATH]` | Ensure marked `OBJ_DIR` and generated-source `BUILD_DIR` are absent. This also removes deliberately tracked or uncommitted entries beneath `BUILD_DIR`. |
| `component migrate COMPONENT\|PROJECT [--check\|--diff]` | Non-destructively project legacy `component.json` into readable `component.md`; project mode is one preflighted transaction, preserves every legacy file, and never overwrites authored prose. |
| `lock [COMPONENT\|PROJECT] --target NAME [--flavor=+NAME ...] [--check\|--diff]` | Resolve or verify Component locks and catalog audits; an explicitly bound orchestration root also checks its repository lock, without invoking a model. |
| `lock COMPONENT --large-review start\|status\|acknowledge\|apply\|cleanup [--transaction-id ID] [--page-identity ID]` | Review a genuine lock transition larger than 512 semantic differences in bounded identity-chained pages, then perform one final revalidated atomic replacement. |
| `project validate [PATH]` | Non-mutating validation of the discovered project, catalogs, pins, skills, and generation inputs. |
| `project tracker inspect [PATH]` | Read Git remotes and name `gh` or `glab` plus issue/review/CI/land argv. Unknown hosts skip tracker work fail closed. |
| `project lifecycle rebind-standard [PLAN] [--project PATH] [--output FILE] [--apply --authorize-rebind]` | Plan an exact installed-wheel Standard binding update without mutation, or compare-and-swap an explicitly reviewed plan while changing only lifecycle and derived receipt-policy authority. |
| `project convert-stage show [--project PATH]` | Read the canonical evidence-bound conversion authority state for an adopted project. |
| `project convert-stage advance --to retained\|drafted\|qualified [--project PATH]` | Advance exactly one adjacent stage after independently verifying the current receipt or Component-promotion evidence. No command silently promotes a tree. |
| `project retained-scope refresh [--project PATH] [--apply --acknowledge --expected-plan-identity SHA256]` | Review exact added/removed retained-source paths and input identities, then revalidate and rebind scope and receipt policy without changing source or historical conversion evidence. |
| `project retained-harness readmit [--project PATH] [--retain-stage STAGE_ID ...] [--apply --acknowledge --expected-plan-identity SHA256] [--worker-id ID] [--worker-config PATH] [--timeout-seconds N]` | Re-inspect a retained implementation while original source still owns release authority, including at the `retained` conversion stage. A repeated `--retain-stage` carries the exact named stage from the current admitted inventory when inspection cannot rediscover it; the stage IDs and complete records are part of the reviewed plan and must be repeated unchanged on apply. The command binds the selected worker, timeout, and supported pinned runtime-tool requirements into that plan, then qualifies the corrected direct commands and generated wrapper locally or on one exact POSIX SSH worker before atomically replacing inventory, baseline, parity, wrapper/shim authority, Component lock, and receipt policy. Prior lift-and-shift and conversion evidence and the current conversion stage remain immutable. Specification-authoritative projects fail closed. |
| `project retained-cargo check --binding PATH --plan PATH --gates PATH ...` | Verify reviewed Cargo bindings, current provider authority, an explicit local or HTTPS archive, and any present packages. Report missing packages without provisioning them. See [retained Cargo provisioning](retained-cargo.md) for required review and delivery inputs. |
| `project retained-cargo materialize --binding PATH --plan PATH --gates PATH ...` | Verify the same authority and exclusively publish or reuse exact retained packages under the reviewed disposable paths. This command does not execute consumer gates or admit a receipt. |
| `project retained-cargo admit --binding PATH --plan PATH --gates PATH --test-inventory PATH --retirement PATH --reviewer-review ID --reviewed-at TIMESTAMP --acknowledge-source-retirement --allow-host-execution ...` | Reverify and materialize the reviewed package set, run the exact consumer gates and tests, require an already-absent retired source tree, and return a canonical admission receipt without deleting source. |
| `project peer-work survey --when start\|end [PATH]` | Survey green open PRs/MRs and issues at cycle start, or leftover worktrees and unmerged branches at cycle end. Does not delete Git state. |
| `project peer-work mark --state merged\|dead --branch NAME --actor LOGIN [--reason TEXT] [--push]` | Record an exact-head lifecycle marker; `merged` proves ancestry in the default branch and `dead` requires a reason. |
| `make runner-review` / `make runner-review-record` | Report the exact sample test-runner source-closure drift, then explicitly record its reviewed identity. |
| `project peer-work gc [--apply --authorize-delete]` | Plan safe local branch/worktree collection by default; apply only after exact-marker, PR, ancestry, protected-branch, and cleanliness checks pass. |
| `project worktree location [PATH]` | Resolve the canonical repository-owned `.worktrees/` root from exact Git common-directory and registered-checkout custody. |
| `project parent checkout URL[#REVISION] [--project PATH] [--repository-fetch-*-seconds N]` | Clone or update a parent under `parents/<id>/` with submodules and Git LFS under the shared progress-aware Git deadline policy. Not lineage resolution. |
| `project ci-plan [PATH] [--mode shard\|impact\|compose]` | Detect test frameworks and emit a fail-closed shard and impact-selection plan. Unavailable mechanisms are named; missing impact maps select the full suite. |
| `project source-intelligence sync [PATH] [--binary COMMAND]` | Synchronize and verify the bounded project-local artifact for an explicitly configured external provider. |
| `project source-intelligence check [PATH] [--binary COMMAND]` | Verify the configured external provider, current source binding, and project-local artifact without changing the project. |
| `project documentation-update [PATH] [--apply --allow-model-egress] [--model MODEL]` | Produce a deterministic, read-only documentation-drift plan by default; with both explicit authorizations, request bounded documentation-only replacements from the selected coding CLI. |
| `project documentation-review [PATH] [--record]` | Calculate the exact marker for the current reviewed authority and documentation graph; `--record` atomically replaces the one existing marker and verifies it became current. |
| `spec scxml-review CHART [--trace SIDECAR ...]` | Read-only bounded structural validation and supported-subset trace replay for one exact SCXML 1.0 chart and its explicit JSON sidecars. |
| `project test-receipt run-retained CANDIDATE [--project PATH] [--evidence PATH] [--worker-id ID] [--worker-config PATH] [--timeout-seconds N] [--known-failure-report PATH] [--harness-workspace-link DESTINATION=SOURCE ...]` | Execute a qualified converted project's exact admitted harness in a disposable authored-source copy and emit one outer-finalized candidate plus external run evidence. `local` remains controller-local; every other ID must select an exact configured POSIX SSH worker. Admitted `uv` stages and Ninja declarations in their evidence scripts receive the framework's pinned, digest-verified Linux x86-64 runtime tools without changing the worker installation. External sibling projections remain local-only in this first remote slice. |
| `project test-receipt update CANDIDATE [--project PATH]` | Atomically replace the configured receipt with an exact passing candidate. |
| `project test-receipt check CANDIDATE [--project PATH]` | Require the configured receipt to equal an exact tested candidate. |
| `project test-receipt require-current [--project PATH]` | Require a canonical committed receipt bound to the complete current authority revision. |
| `project test-receipt verify-evidence PLAN --policy POLICY --revocations REVOCATIONS ...` | Authenticate a complete retained graph against independent authority and explicit signed-store mappings, without writing a receipt. |
| `project test-receipt publish-evidence PLAN --policy POLICY --revocations REVOCATIONS --bundle-store PATH --retention ENVELOPE ...` | Retain a complete verified bundle and atomically publish its freshly reverified current map. |
| `project test-receipt require-current-evidence PLAN --policy POLICY --revocations REVOCATIONS --bundle-store PATH ...` | Authenticate the configured current receipt with no unsigned fallback or alternate-map override. |
| `project test-checkpoint inspect --state PATH [--suite ID] [--project PATH]` | List the typed retained-failure annotations in one repair checkpoint. |
| `project test-checkpoint annotate --state PATH --pin ID --test-key KEY ...` | Add or replace one operator-authored exact failure annotation with context, evidence, cause, and revalidation policy. |
| `project test-checkpoint revalidate --state PATH --pin ID --test-key KEY` | Force the next exact repair run to execute an annotated test; a successful result clears that annotation. |
| `project test-checkpoint clear --state PATH --pin ID --test-key KEY` | Explicitly remove one exact retained-failure annotation. |
| `project test-checkpoint import REPORT --state PATH [--release-evidence]` | Reconcile strict external-runner per-test events, persist typed totals, and return nonzero for ordinary or known failures. |
| `plan COMPONENT [--target NAME] [--flavor=+NAME ...] [--model MODEL]` | Validate the current lock and inspect every generation input and lexical model binding without invoking a coding CLI. |
| `generate COMPONENT --output DIR [--target NAME] [--flavor=+NAME ...] [--model MODEL]` | Generate a complete source tree from the current Component lock and asserted Flavor/model selection. |
| `build [COMPONENT] [--target NAME] [--model MODEL] [--worker ID] [--worker-param NAME=VALUE ...] [--worker-timeout-seconds N] [--worker-health-config PATH]` | Run the current specification-led lifecycle and retain a runnable artifact bound to the exact generation target, model-scope set, and execution worker. An explicit health policy performs bounded admission before substantial work and rechecks failures. |
| `test [COMPONENT] [--target NAME] [--model MODEL] [--worker ID] [--worker-param NAME=VALUE ...] [--worker-timeout-seconds N] [--worker-health-config PATH]` | Require the current generated tests and independent acceptance gates through the same build lifecycle and model-scope set, including optional bounded worker-health admission. |
| `run [COMPONENT] [--entrypoint NAME] [--target NAME] [--worker ID] [--flavor SELECTOR ...] [ARGUMENT ...]` | Execute the selected retained entrypoint only when its target profile, worker, and immutable generation authority match the request; omission selects the first declared entrypoint. |
| `worker health --worker-id ID --health-config PATH [--worker-config PATH] [--job-identity SHA256] [--alert-state PATH]` | Bounded storage and optional sustained CPU/memory/paging/GPU inspection with explicit unknowns, deficit alerts and proceed/hold/recheck decisions. Disk incidents automatically run the configured read-only cleanup investigation. An alert-state file only deduplicates reporting; it authorizes neither dispatch nor cleanup. |
| `worker cleanup plan --worker-id ID --health-config PATH [--worker-config PATH]` | Re-run the bounded configured candidate scan and emit only exact proved-inactive target identities, measurements, ownership evidence and recovery cost. Planning is read-only and explicitly reports that deletion is unauthorized. |
| `worker cleanup apply --worker-id ID --health-config PATH --authorization PATH [--worker-config PATH]` | On the exact local worker, require a current authorization bound to worker, policy, proposal, target IDs, supported operation and expiry; revalidate candidates, invoke only the configured exact-target tool, then report measured capacity recovery. Remote apply remains unsupported rather than acting on controller paths. |
| `worker probe (--worker-id ID ... \| --all) [--worker-config PATH] [--output PATH] [--dry-run]` | Probe private workers concurrently for bounded OS, CPU, memory, and GPU observations without changing their authored routing requirements. |
| `verify [PATH]` | Check declared project authority without writing, building, or invoking a model. |
| `learn RUN` | Produce a read-only proposal for placing one evidence-backed lesson in Component, Flavor, Skill, workflow, routing, test, or framework authority. |
| `package plan [COMPONENT] [--target NAME] [--flavor SELECTOR ...] [--model MODEL]` | Project every selected `package.*` Flavor, specification identity, Component lock, and input closure into a content-identified multi-provider declaration. It invokes no model or package tool and authorizes neither construction nor publication. |
| `package build [COMPONENT] ... --allow-host-execution` | Run the complete Standard generation/build/test/execution/independent-acceptance lifecycle, then construct every selected implemented native format from the exact accepted closure beneath `OBJ_DIR`. Pip wheels and Conan cache archives are implemented; no registry write or installation is authorized. |
| `package verify [COMPONENT] ...` | Reopen the retained batch, exact plans, materialized inputs, and native bytes. Wheel metadata and hashes are checked directly; Conan archives are restored into a fresh isolated cache and the exact package reference is required. |
| `release contributions sweep [--project PATH] [--version VERSION] [--current-milestone NAME] [--remote NAME] [--default-branch NAME] [--require-ready]` | Refresh and inventory current tracker items, reviews, unmerged branches, durable exact-head lifecycle markers, and attached worktrees; optionally fail unless all work is dispositioned and included items are closed. |
| `release contributions disposition --kind issue\|review --number N --version VERSION --decision include\|defer --milestone NAME --reason TEXT [--branch NAME ...] --authorize-external-write` | Set one tracker milestone and append the matching machine-readable release disposition; this is the explicitly authorized mutating half of the continuous sweep. |
| `release plan (--bump patch\|minor\|major \| --version VERSION)` | Read the project policy and emit a content-identified release plan without mutation. When the policy names `default_branch`, a plan from that trunk records `release_line.create: true` only if `release/<major>.<minor>.x` for the planned version is absent. |
| `release prepare PLAN` | Update only the plan's declared version and changelog authority; never commit, tag, push, or publish. When `release_line.create` is true, create that branch from the plan revision, check it out, then write. |
| `release check PLAN [--output FILE]` | Require one exact clean prepared commit, constrain its diff, and run the declared gate into a compact prepared-release record. |
| `release publish PREPARED --authorize-external-write` | Revalidate the prepared commit, create and push its tag without force, then invoke the optional provider adapter. |
| `release verify-published PREPARED` | Read-only: require the remote annotated tag, release-line branch, and optional GitHub release to match the prepared identity. Never retags. |
| `release backport COMMIT... --to BRANCH [--from REF]` | Cherry-pick already-landed commit(s) onto a release branch in an isolated worktree, creating the branch from `--from` first if it doesn't exist yet. Never pushes. |
| `release backport-status BRANCH [--against REF]` | List commits on `--against` (default `HEAD`) not yet cherry-picked onto `BRANCH`, using patch-id equivalence so already-backported fixes drop off the list even though cherry-pick gives them a new SHA. |
| `release rc --version VERSION --authorize-external-write` | Create and push an annotated release-candidate tag only from exact green `main` HEAD for the configured Pre-release target. |
| `release state [--project PATH]` | Read Free/Pre-release state, target, branch, writability, release-line lockdown, and patch authorization mode. |
| `release state set --mode free\|pre-release [--pre-release-version MAJOR.MINOR]` | Atomically set the release state as a README Release Engineer; Pre-release requires the target and Free forbids it. |
| `rebuild SPECIFICATION --project PATH --runtime-root DIR --candidate-receipt FILE --allow-host-execution [--build-dir DIR] [--obj-dir DIR] [--flavor=+NAME ...] [--model MODEL]` | Run the project-authorized complete SDLC under one enclosing model default and validate its external passing-receipt candidate. |
| `attest SOURCE --signer ID --machine ID --key FILE` | Sign the exact local source inventory. |
| `skills [--skills-root DIR]` | List an explicit inverse-skill catalog, or packaged reviewed skills by default. |
| `format CORPUS --id-prefix ID [--check\|--write]` | Preview canonical layered-spec formatting by default, check without mutation, or explicitly write canonical Markdown frontmatter and line endings. Supporting JSON remains byte-exact. |

| `validate CORPUS --id-prefix ID` | Non-mutating validation of derived IDs, parents, references, and effective context. |
| `explain CORPUS --id-prefix ID [--node ID]` | Explain the full derived corpus graph or one node's effective context without mutation. |
| `derive CASE_OR_SOURCE [--translator static\|coding-cli]` | Produce a reviewable draft bundle; semantic model translation requires explicit egress consent. |
| `audit CASE_OR_SOURCE --baseline FILE [--translator static\|coding-cli]` | Compare derived behavior with a baseline. |
| `review BUNDLE [--component-graph FILE]` | Record resolutions and optionally sign the review. A standalone static project promotion requires one explicit source-bound Component graph whose identity is covered by the review signature. |
| `accept CASE_OR_SOURCE BUNDLE --review FILE --target DIR [--project-target DIR\|--integrate-project ADOPTED_ROOT] [--qualification-target NAME] [--flavor=+NAME ...]` | Verify and promote reviewed artifacts to intent authority; optionally create a standalone source-free Component project or register one beneath an existing retained adopted root, pin a qualification profile, and resolve the exact target/Flavor Component lock without generating source. |
| `qualify COMPONENT --source DIR --profile FILE --output FILE --key FILE --signer ID --target NAME [--flavor=+NAME ...] --allow-host-execution` | Require the current exact Component lock, then run the historical clean generation/build/test/parity path. Version-1 evidence remains non-authorizing; only complete typed evidence from a trusted lifecycle adapter can enter locked v2 admission. |
| `refresh CASE_OR_SOURCE --previous FILE` | Incrementally rederive, optionally naming changed paths. |
| `conformance CASE` | Run one deterministic descriptor-based conformance case. |
| `coverage BUNDLE` | Summarize coverage and blocking uncertainty. |
| `diff LEFT RIGHT` | Compare two result bundles. |

Ordinary `lock`, `lock --check`, and `lock --diff` retain the 512-difference limit and
write nothing when it is exceeded. For an existing stale lock with a larger genuine
transition, use `--large-review start` with the exact target and ordered Flavor
selectors. A start result returns a transaction identity and the first page. Pass that
page's exact identity to `--large-review acknowledge`; each successful acknowledgement
returns the next page until `ready` is true. `status` resumes the same immutable
transaction, `apply` re-resolves all authority and atomically publishes the complete
lock/audit pair, and `cleanup` explicitly removes abandoned derived state. Status,
acknowledge, apply, and cleanup require `--transaction-id`; acknowledge additionally
requires `--page-identity`. Authority or policy drift, skipped/reordered/replayed pages,
tampering, and incomplete review fail closed. Transaction state lives beneath the
ignored `OBJ_DIR` and no intermediate operation writes a partial lock.

Packaging is deliberately multi-value. Repeat `--flavor` to select every compatible
format needed by the repository's user communities, for example
`--flavor=+package-pip --flavor=+package-conan`. These providers produce independent
package plans; selecting one does not subtract another. Target constraints still fail
before generation: apt is Linux-only, Homebrew is macOS-only, and WinGet and Chocolatey
are Windows-only. Pip wheels and Conan are portable selections.

Planning is intentionally cheap and read-only. Building is intentionally not: it first
requires the same accepted artifact, generated-test, application-execution, independent
acceptance, and resolved-CycloneDX evidence as an ordinary Standard lifecycle.
The package batch embeds the human-authored Component specification and resolved SBOM
beside the accepted products and is content-addressed under
`OBJ_DIR/packages/<batch-digest>`. `package verify` performs no publication and does not
trust the build process's in-memory adapter state.

`--target` always names the generation/Flavor profile; it never chooses a machine.
`--worker` resolves one exact ID from private user `workers.json` (or an absolute
`LITAI_WORKER_CONFIG`/`--worker-config`), and each `--worker-param` must be declared by
that worker. Omitting `--worker` creates the exact implicit local worker. Local and
`command` workers execute through the same typed lifecycle boundary. A command receives
one canonical `execution-dispatch-request` over standard input, or through its one
whole-argument `{request_file}` placeholder, and must return one canonical result on
standard output. It owns scheduling and provisioning; LitAI owns validation and the
bounded timeout. A bounded SSH worker receives the same request plus a verified source
archive. Its fixed one-MiB stdout control result contains only status, request/worker/
artifact/evidence identities, observed facts, canonical manifest and bundle digests and
sizes, the fixed `staged:remote-evidence.tar.gz` transfer handle, and a bounded redacted
summary. It never embeds the file manifest, logs, stage payloads, or opaque compressed
JSON. The coordinator downloads the bundle, verifies the declared bundle and manifest
sizes and digests, independently verifies every listed file, and imports it into
`OBJ_DIR/remote-evidence-cas` before accepting a custody receipt. Worker-local CAS paths
are never coordinator evidence.

A successful command-worker build or test must publish an `artifact-export`
`ContentReference`: a credential-free, query-free durable URI plus exact content
identity. LitAI records that reference under `_build/artifacts/`, and `run` returns it in
the next request together with the application arguments. Build/test/run share an
authority identity over Component, project/source/specification/Flavor/toolchain
requirements, worker, and parameters. If any of those inputs changes, `run`
fails with `artifact_export.authority_stale` and requires a rebuild. Successful results
also report exact observed toolchain identities and bounded stdout/stderr. Secrets may
enter only through worker environment bindings and never enter requests or exports.

SSH lifecycle failure diagnostics follow the same custody path as successful evidence.
The worker records the public error and nested cause under the explicit
`literate-ai-secret-private-paths-v1` redaction policy before returning only its compact
summary; the complete redacted diagnostic remains referenced inside the bundle. Attempt
workspaces remain intact until `worker acknowledge` proves the exact manifest and bundle
identities were imported. Cleanup and acknowledgement are idempotent and bounded; a
failed or disconnected transfer or acknowledgement fails the cell closed and retains
worker custody for an exact retry.

Refresh changing host capacity with `litai worker probe --all`. The command uses
platform-native, non-interactive probes over the declared local or SSH transport and
writes `worker-observations.json` atomically beneath the resolved user state root. Use
`--dry-run` to inspect the complete typed result without writing.
Observed CPU architecture, physical/logical cores, memory, and GPU devices are inventory,
not requested minimums. On Linux and Windows an absent `nvidia-smi` records no
discoverable NVIDIA GPU, while an installed command whose device query fails records a
degraded state and cannot satisfy CUDA eligibility. macOS reports NVIDIA probing as not
applicable and may retain its native Apple GPU inventory. Command dispatchers require a
future explicit observation protocol and fail closed today; Literate AI does not infer
fleet ranking or provisioning policy from these facts.

Before a live qualification run, use `litai worker verify-model` to resolve the
configured coding CLI and model and execute one bounded, non-source-writing preflight.
It fails before generation when the model identifier is unavailable. This command is
explicitly live; ordinary `make python-check` verification does not invoke it.

`BUILD_DIR` defaults to `generated/` beneath the project. `OBJ_DIR` defaults to
`_build/` beneath the project. Export either to place its cache elsewhere; relative values remain
project-relative. Explicit values travel through the lifecycle driver's allowlisted
environment and enter the outer rebuild request binding. They choose storage location,
not derivation validity.

Every `litai` command records a JSON-Lines performance span under
`OBJ_DIR/.litai/perf/<run-id>.jsonl`: schema, run id, stage, target kind/id, coding CLI,
model, ISO 8601 start/end, duration in milliseconds, pass/fail, and a typed error code
when it failed. A `litai release check --target local` fan-out additionally records one
span per dispatched worker (`release.check.worker`, target kind `worker`), so a full
fleet run's per-machine timing lands in the same log as every other command's. This
telemetry is diagnostic, not authority — a read-only or missing `OBJ_DIR` never fails the
run it observes, and unreadable log lines are skipped rather than raised. Inspect it with:

```bash
litai perf show                          # a table aggregated by stage (default)
litai perf show --group-by target_id     # per-Component/per-worker breakdown
litai perf show --group-by coding_cli --stage cli.build
litai perf show --run-id <run-id>        # one recorded run only
litai perf chart --output _build/perf.svg --group-by stage
```

`litai perf chart` renders a dependency-free SVG bar chart (no new third-party graphing
library); each bar shows the group's total duration and span count.

`OBJ_DIR` is disposable and gets wiped by `make clean`/`really-clean`, so telemetry
recorded there does not survive a clean. To keep it, copy `OBJ_DIR/.litai/perf`
somewhere durable before cleaning, then point `litai perf show`/`litai perf chart` at
the copy directly with `--dir`:

```bash
cp -R "$OBJ_DIR/.litai/perf" ~/litai-perf-archive/2026-08-17-run
litai perf show --dir ~/litai-perf-archive/2026-08-17-run --group-by target_id
```

`--dir` names the exact directory holding the `*.jsonl` span files (the archived
copy itself, not a project root) and bypasses `--project`/`OBJ_DIR` resolution
entirely.

`derive` and `audit` accept `--attestation` and `--trust-key`. `review` accepts repeated
`--resolve` values and an optional signing `--key`. `refresh` accepts repeated
`--changed-path` values. Derivation, refresh, conformance, and skill listing accept
`--skills-root` where shown by help; `--skills` remains a compatibility alias.

`derive` and `audit` default to the local deterministic `static` translator.
`--translator coding-cli` fails closed: model-backed inverse translation that
required graph evidence is unavailable. `CODING_CLI`
selects `codex`, `claude`, or
`cursor-agent`, otherwise first-on-`PATH` discovery applies. Case descriptors stay
deterministic and reject model options. The accepted translation journal includes exact
prompts and admitted source evidence, so protect it according to the source's data
classification even though known sensitive files are excluded before egress.

Wire identities are immutable once published. The source-intelligence separation
therefore writes v2 project, lifecycle-driver, source-cache, and rebuild-cache records;
their frozen v1 shapes remain in the catalog with explicit readers that migrate into
the current in-memory contracts. A field or semantic change never silently reuses a
v1 schema URI. `schemas/v2/compatibility.json` is the fail-closed inventory of every
current writer and admitted legacy read path.

Regenerative qualification has typed policy, execution-profile, evidence, decision,
operational-attestation, and local-signature schemas. Current local writers emit v2 wire
records, but `run_regenerative_qualification` still implements the historical v1
measurement model and keeps effective authority at `source-baseline` with the
`qualification-v2-required` blocker. `litai spec qualify` first validates the exact
current Component lock, then wires the concrete local host regenerator,
provider-distinct parity verifier, and HMAC attestor. It accepts a project-contained
execution plan and measures every local result itself; it has no command-line ingress for
precomputed qualification evidence. A parsed decision exposes `claimed_authority` for
audit while its wire-level `effective_authority` remains `source-baseline`. An integrated
trusted lifecycle adapter may provide complete typed version-2 evidence in-process; the
locked admission service rejects missing, partial, stale, or drifted facts before
creating the distinct qualified Component authority projection. Integrators that require
stronger isolation or PKI should configure providers and signing roots they trust.

### Three verbs, three levels of intrusiveness

`verify`, `rebuild`, and `update` are deliberately distinct. Reaching for the wrong one
is the mistake this separation exists to prevent.

| Verb | What it does | Writes | Builds | Reaches a model |
| --- | --- | --- | --- | --- |
| `litai verify` | checks declared project authority | no | no | no |
| `litai rebuild` | runs the project's own gates through its chosen build harness | yes | yes | when generation is needed |
| `litai update` | applies framework-owned files from upstream | yes, opt-in | no | no |

`litai verify [PATH]` answers "is this project's declared state sound?" and changes
nothing. It runs five gates in declared order — `authority` (project validation and the
documentation authority review), `locks` (existing committed Component locks and any
required orchestration repository lock checked, never written),
`source-intelligence` (index currency), `html-observability` (declared HTML currency),
and `receipt` (the committed test receipt) — and
reports each separately so one failure does not hide the others. `--gate NAME` runs a
subset. A failing gate is a verdict, not a CLI error: the result is reported normally and
carried in the exit status, as with `lock --check`.

The HTML gate checks only the optional `html_render_requests` array in
`literate.project.json`; it skips when the array is absent or empty. Declare an
artifact before rendering it, using your project's scope identifier:

```json
{
  "html_render_requests": [
    {
      "schema": "urn:literate-ai:schema:v1:html-observability-render-request",
      "surface_id": "authority-graph",
      "view": {
        "schema": "urn:literate-ai:schema:v1:html-observability-view",
        "view_id": "dag",
        "view_version": "1.0.0",
        "scope_kind": "project",
        "scope_identifier": "my-project"
      },
      "output_path": "graph.html",
      "cache_mode": "read-write",
      "external_asset_policy": "pinned-cdn"
    }
  ]
}
```

Up to 128 declarations are accepted; output paths must be unique ignoring case.
Verification ignores `cache_mode`, never renders to disk, and never repairs a file.
It requires the actual non-editable framework wheel, checks embedded provenance
against current inputs, and reproduces purportedly-current bytes in memory at the
retained timestamp. Stale reports name changed `authority-graph`, `renderer`, or
`view` inputs. Missing, unreadable, and unpinned files fail the gate; unavailable
current inputs also fail without inventing an expected identity. Unrelated HTML
files are not classified as framework artifacts. Regenerate stale output with
`litai render html`; investigate unpinned/edited files before replacing them.

Render `performance-history/history` to inspect retained command, build and test
timings, or `workflow-routing/catalog` to inspect exact workflow dependencies and
routing policies. Both are project-scoped version `1.0.0` views and support
`--external-asset-policy inline-only`.

After each project has declared and rendered its own artifacts, compose an offline
multi-project shell beneath a common directory:

```console
litai render dashboard --root /work \
  --project /work/service-a --project /work/service-b \
  --output observability.html
```

The command refuses missing or edited declared artifacts and projects outside
`--root`. The output contains exact artifact identities and relative iframe paths;
it does not run verification, rebuild projects, or start a server.

Verify never builds. A project's build harness, toolchain, and language are the project's
own choices, and `rebuild` is where those run; verify does not invoke, wrap, or
approximate it. `--gate rebuild` is rejected rather than quietly accepted.

Use `verify` in the inner loop and in CI, `rebuild` when the artifact must actually be
produced and proven, and `update` only when pulling framework changes from upstream.

### Admit generated source before target builds

For coordinator generation followed by target worker fanout, combine generation and
verifier admission:

```console
litai generate component.md --target portable \
  --output "$BUILD_DIR/coordinator-source" \
  --admit --source-portability target-independent \
  --source-test-command '["make","test-generated"]'
```

Each `--source-test-command` is a JSON argv array executed directly by the verifier in
a disposable copy of the generated source root. Relative paths keep the generated
layout; commands such as `make -C source test` can create build output and bytecode
without adding them to accepted source. Commands in one admission share that copy.
The original candidate and every copied source file must remain unchanged, including
source permissions. Absolute paths in argv are not rewritten. At least one command
is required. A nonzero exit, timeout, oversized output, changed source manifest,
missing generated test suite, or identity
drift refuses admission. Successful machine-readable output includes
`source_admissions` with the exact membership, evidence, Component, and source-tree
identities. Admission publishes only source bytes and verifier evidence; it claims no
object, binary, package, acceptance, or final receipt.

Workers continue through the ordinary Standard lifecycle with:

```console
litai build component.md --target macos --from-accepted-source
litai test component.md --target macos --from-accepted-source
litai rebuild component.md --target linux --from-accepted-source
litai rebuild component.md --target windows --from-accepted-source
```

This mode requires one exact verifier-admitted cache member for every planned
Component. Build and test carry the verified cache's exact provider/tool binding into
the local Standard rebuild even after the admitting process and session are gone. The
lifecycle restores and revalidates the member, begins at source indexing and object
construction, and never invokes a coding CLI. Missing or incompatible membership is
`source_cache.runtime_absent`; it is not a regeneration fallback. A final receipt is
possible only after the worker's build, generated tests, execution, independent
acceptance, project assembly, package, and aggregate admission all succeed.

### Full project rebuild

`litai rebuild` is the normal SDLC front door. The project manifest must declare a
`lifecycle_driver` and test-receipt policy. The driver pins a stable ID/version, sorted
project-relative implementation files and their aggregate identity, a shell-free
argument template, allowed environment keys, timeout, specification scope, and the
complete ordered phase list. Its required phases are:

Version 2 also admits a discriminated `binding: standard` authority record containing
the exact installed framework-distribution and policy identities. `litai rebuild` uses
the in-process Standard composition when a project declares that binding. Existing
external driver documents retain their original canonical wire shape; an explicit
`binding: external` is accepted and normalized to that shape.
`--from-accepted-source` is a Standard-only continuation contract; an external driver
request is rejected before driver binding because its current versioned interface has
no accepted-source-only authority field.

```text
compose-and-plan
resolve-source-cache
generate-source-and-tests-or-use-cache
derive-source-intelligence
validate-classify-and-authorize
resolve-and-prepare-dependencies
native-build
verify-resolved-sbom
run-generated-tests
execute-application
independent-acceptance
admit-workspace
write-candidate-receipt
```

This is the **outer rebuild contract**, not a direct invocation of the Standard
per-node service. The outer CLI freezes authority and cache controls, launches the
declared driver, validates its provisional assertion and lifecycle membership, performs
configured publication, and only then emits the finalized receipt candidate. A future
default Standard driver can sit behind the same outer contract without changing the
public command. For Standard, `.` is that outer contract over the committed lock set:
each locked Component still runs the existing one-Component adapter. After every
member is accepted, the CLI rechecks project authority, the complete lock set and
all captured inputs. One finalized candidate and committed receipt bind all member
proofs, all lock identities and the combined test count. A failed member or changed
input prevents publication. The `components` result array preserves each member's
artifact and execution metadata; a multi-Component result has no single top-level
artifact. A one-Component rebuild retains its existing receipt identity.

`package-artifacts`, `publish-source-cache`, `publish-artifacts`, and `deploy` are
optional ordered extensions immediately before receipt production. Declaring a phase
does not implement it: the content-pinned driver must perform it and return the evidence
required by project receipt policy.

When `publish-source-cache` is present, the manifest must also configure a write-enabled
source cache. The driver offers immutable entries only after current acceptance and
workspace admission. `litai` validates that offer against the cache decision and
candidate-receipt identities, then performs publication after the driver exits. The
receipt must bind that operation as `source-cache-publication-result`; a separate
`publish-artifacts` extension binds `artifact-publication-result`, so one result cannot
silently satisfy both phases. A driver cannot publish merely by writing cache files
itself or by declaring the extension.

For a project-scoped driver such as this repository's:

```console
litai rebuild . \
  --project . \
  --runtime-root /tmp/literate-ai-runtime \
  --candidate-receipt /tmp/literate-ai-candidate.json \
  --allow-host-execution
```

Cache custody is an explicit parameter of the run. `--build-dir` selects the
generated-source cache root and `--obj-dir` the object/tool cache root; each overrides
the corresponding `BUILD_DIR`/`OBJ_DIR` environment variable, which in turn overrides
the portable `<project>/generated` and `<project>/_build` defaults. The roots that a
lifecycle actually used are recorded in its result and bound into the candidate
receipt's `cache-directory-custody` evidence, so a warm-cache run and a cold-cache run
of the same revision are distinguishable after the fact:

```console
litai rebuild . \
  --project . \
  --runtime-root /tmp/literate-ai-runtime \
  --candidate-receipt /tmp/literate-ai-candidate.json \
  --build-dir /tmp/literate-ai-generated \
  --obj-dir /tmp/literate-ai-objects \
  --allow-host-execution
```

An explicitly selected root is still subject to every existing custody rule: it may not
resolve inside a protected authority directory, the two roots must differ, and the
generated-source root may not nest beneath the object root. A violation is reported as
`rebuild.cache_root_invalid`. Cache roots are deliberately not workflow authority —
binding host paths into a workflow would make the same workflow non-portable.

For a Component-scoped driver, replace the first `.` with its Component directory and
repeat `--flavor` for ordered `+flavor`, `-flavor`, or `+slot-id:flavor` selections.
The driver must opt into the `{flavor_args}` placeholder before the CLI accepts explicit
selectors.

Flavor names also accept axis-prefixed aliases (`lang-python`, `os-windows`,
`build-make`), an axis/value pair (`implementation.language-ecosystem=python`), or the
canonical coordinate (`flavor://literate-ai/lang-python`). The canonical form resolves flat
alias collisions deterministically. A canonical coordinate begins with `flavor://` and
is never parsed as a Component slot separator.

Cache-related rebuild flags are part of the lifecycle-request identity:

- `--force-regeneration` bypasses lookup without deleting entries;
- repeated `--source-cache-entry sha256:...` selects among several exact candidates and
  is mutually exclusive with forced regeneration; and
- repeated `--source-cache-root BINDING=/absolute/path` supplies every and only
  operator-bound root declared by project configuration.

`--source-cache-entry` and `--source-cache-root` are supported only when the project's
`lifecycle_driver` binds `external`. A Standard driver keeps BUILD_DIR-backed cache
custody and rejects both overrides with `rebuild.standard_cache_override_unsupported`;
its supported equivalent for deterministic reuse of already-accepted source is
`--from-accepted-source`, which requires exact verifier-admitted source-cache
membership for every Component instead of selecting an individual entry.

`litai rebuild` uses one command with a mandatory two-pass driver handshake. First it
invokes the content-pinned driver in `plan-derivations` mode, without host-execution
authority. That pass must not generate source, compile, test, run, publish, or mutate
project authority; it writes only one bounded canonical typed manifest containing every
exact derivation key. The manifest binds the outer planning request, current project
revision, lifecycle driver, and a lifecycle-plan identity. A driver without this
planning protocol fails closed before normal execution.

The outer CLI freezes that manifest into the rebuild request and writes a canonical
cache-control document beneath the external runtime root. The normal driver pass may
resolve only those planned keys and must write exactly one decision item for every key:
neither omissions nor additions are accepted. After it returns, `litai` verifies that
decision and
requires the receipt's `source-cache-decision` evidence to bind it. The driver also
writes `LITAI_SOURCE_CACHE_LIFECYCLE`: exact sorted derivation-key membership joining
the decision and lifecycle plan to the current tree/index, build, test, acceptance,
workspace, provenance, source-SBOM, and resolved-SBOM identities. Receipt aggregates
must be derived from that complete member set and `source-cache-lifecycle` must identify
the exact binding. A hit explicitly
records `current_acceptance_trusted: false` and `generation_skipped: true`; all later
current lifecycle phases still run, and a hit is omitted from a publication offer.

Both output paths must be new external paths, not project authority or a symlink. The
driver writes a typed provisional assertion beneath the runtime root. That assertion
binds the outer request, expanded command, cache control, and exact nested receipt, but
is structurally rejected by `project test-receipt update`. The requested candidate path
remains absent until `litai` has validated the assertion, current lifecycle membership,
and every configured outer publication; it then emits the distinct finalized candidate
envelope as one atomic final step and removes the assertion. A failed outer side effect therefore leaves unpromotable
diagnostics in the runtime root but no candidate at the requested promotion path. The
CLI also verifies
the project revision, driver executable, driver implementation, expanded command, and
runtime root before and after the child process. It passes only a declared,
framework-approved environment subset and requires explicit host-execution
acknowledgement. The result binds the lifecycle request and expanded command identities
and reports `receipt_committed: false`; use the separate receipt update command after
reviewing the run.

This boundary is deliberately modest. The driver is explicit project TCB, not a claim
that every native builder is implemented inside the neutral CLI. The compact receipt
proves that the configured driver returned the policy-required identity bindings; it
does not contain a prompt journal, authenticate an external evidence store, or replace
a stronger enterprise agent ledger.

The outer finalizer pins the runtime and protocol directory device/inode identities,
uses descriptor-relative, no-follow protocol I/O where the host supports it, bounds
documents and publication work, and fails closed on replacement or recursive input.
On Windows, Python does not expose the equivalent directory-handle-relative primitive;
the fallback pins and rechecks names but cannot exclude a same-user replace-and-restore
race. Treat the external runtime/protocol directory as same-user TCB there, or provide a
native handle-backed adapter or stronger filesystem isolation.
The rebuild request binds every non-secret environment value visible to the driver and
the presence of credential-bearing keys; credential values are never placed in request
identity material.

### Source generation

`litai generate` requires a Component directory with a current canonical
`component.lock.json`. Run `litai lock` for the same `--target` and Flavor selectors
first. The generation command reads only exact lock-selected specifications, skills,
public dependency interfaces, workflow, and routing authority. Repeat `--flavor-root` to provide
explicit Flavor catalogs; if omitted, the command uses the `flavor_roots` declared by
the discovered `literate.project.json`. Legacy projects without a manifest retain the
neighboring-catalog fallback. Repeat `--flavor` to apply ordered `+name` and `-name`
operations. When a Component declares multiple slots on the same axis, qualify each
positive selection as `+slot-id:name`; use `-slot-id:name` to remove one role or
unqualified `-name` to remove every binding of that exact Flavor:

```console
litai lock samples/hello-component \
  --target linux-host \
  --flavor=+flavor://literate-ai/lang-cpp \
  --flavor=+flavor://literate-ai/os-linux
litai generate samples/hello-component \
  --output /tmp/literate-ai-hello \
  --target linux-host \
  --flavor=+flavor://literate-ai/lang-cpp \
  --flavor=+flavor://literate-ai/os-linux
```

The output directory is a custody root, not one flattened source tree. Each locked
Component receives a distinct freshly allocated child workspace. The command's
`literate-ai/coding-cli-generation@11` result contains
`standard_source_generation`, whose canonical Component records bind the exact plan,
generation key, workspace identity and locator, node result, source candidate, and
provenance. Consumers must locate a tree through that envelope rather than guessing an
output path. Without `--admit`, this phase performs no indexing, build, test, execution,
acceptance, or publication. With `--admit`, it performs only verifier-owned source
tests and source-only publication; object and later claims remain absent. Cache hits
are consumed only when previously accepted membership exists; new candidates do not
become reusable merely because generation succeeded.

```console
litai lock samples/full-stack-rust-js \
  --target macos-host \
  --flavor=+flavor://literate-ai/os-macos \
  --flavor=+backend-language:rust \
  --flavor=+frontend-language:javascript
litai plan samples/full-stack-rust-js \
  --target macos-host \
  --flavor=+flavor://literate-ai/os-macos \
  --flavor=+backend-language:rust \
  --flavor=+frontend-language:javascript
```

Singleton Flavor slots make two selections on their axis mutually exclusive. Slots
declared `one-or-more`, or `bounded` with a maximum above one, retain multiple explicit
selections; those selections must describe one target value for that axis. Explicit
Flavor conflict declarations are honored even across axes. Replacing a singleton
therefore requires subtraction before addition instead of choosing silently. Output
must be a new or empty directory outside the entire project root.

An unqualified positive selector remains valid when its axis has one Component slot. If
an axis has multiple slots, every positive selector on that axis must name its slot;
selector order is never used to infer roles. An unqualified negative selector is safe:
it removes every binding of that exact Flavor, while a slot-qualified negative removes
only that role. Unknown slots, slot/Flavor axis mismatches, one Flavor bound to an
incompatible axis, two values bound to an exclusive slot, mixed qualified and
unqualified positive selection on one axis, and missing required slot bindings all fail
before generation. One exact Flavor revision may intentionally fill several compatible
role slots; its specification documents and skills are loaded once while each role
binding is proved independently. The exact slot-to-Flavor mapping enters the
target-profile and recipe identities, the plan and generation reports, and the
coding-agent prompt. When the generation-safe acceptance interface declares exact slot
targets, an inverted or otherwise different role binding is rejected during planning.

For a composed graph, prefix an ordinary selector with the exact authored Component
coordinate and `::` to override only that node. Global and coordinate-qualified
selectors may be mixed, and every explicit selector must have an observable effect:

```console
litai lock components/invoice-cli --target host \
  --flavor=+flavor://literate-ai/os-linux \
  --flavor=component://example/pricing::+language:python \
  --flavor=component://example/reporting::+language:javascript
```

An invalid or unreachable coordinate fails before generation. A dependency never
inherits the root Component's language merely because it belongs to the same target.

The optional `default_flavor_selectors` array in `literate.project.json` is applied
first as ordered project preference data. Defaults are considered only for axes the
Component declares and Flavors available in the selected catalog; they do not create a
slot or requirement. Explicit `--flavor` operations then add, subtract, or replace the
result. Projects created by `litai init` use
`["+flavor://literate-ai/build-make"]`, and smart initialization also selects Python
and the host operating-system Flavor for the starter. A Component with a `build.system`
slot therefore inherits Make unless an explicit build-system Flavor replaces it.
Selecting `+flavor://literate-ai/build-bazel` adds Bazel's content-pinned generation
skill; `--flavor=-build-bazel` removes that selection before prompt assembly. For a
Component with several exclusive build-system roles, a slot-qualified selector replaces
only that role, while an unqualified negative removes every binding of that Flavor.
Component specifications and selected Flavor requirements remain authoritative over
skill preferences. Catalog discovery has its own audit identity, while recipe,
effective-set, and generation input-closure identities include only selected Flavor
revisions and skills. A removed or rejected Bazel revision can therefore change the
catalog audit without changing the semantic derivation.

Generation plans use `literate-ai/generation-plan@6`. Their `model_scopes` array records
one exact immutable binding per Component revision, including provider, selected model,
parent-binding identity, owner, decision, and complete resolution trace. Locked Standard
generation reports
use `literate-ai/coding-cli-generation@11`; version 10 is the pre-admission Standard
shape, while the version-9 reader remains relevant only to unlocked legacy single-tree
generation. In both legacy plan/report surfaces,
`selected_flavors` contains each exact
Flavor once (with any `slot_ids`) and `flavor_bindings` records each slot-to-revision
edge separately. A generation report also records `generation_mode` as `major-rebuild`,
the exact `generated_test_suite_identity` for `source/tests/manifest.json`, and the
validated CycloneDX source-SBOM binding for
`source/.literate/sbom.cdx.json`.

`litai plan` accepts the same Component, target, Flavor-root, Flavor-selector, and
recipe-ID arguments but performs no generation. It validates and projects the exact
current Component lock without rerunning composition or Flavor resolution. Use its JSON
result as the preflight view of that lock plus specifications, public dependency
interfaces, Flavors, skills, routing, resolved lexical model scopes, role-qualified
entrypoints, and guarded lifecycle. For canonical locked input,
`standard_component_execution` is the complete typed Standard execution plan rather
than a CLI-owned projection. `coding_cli_selection.selected` records the launcher and
portable tool-binding identities used to resolve its per-node model identities; the
launcher is inspected and guarded but not executed.

### Project initialization and validation

`litai init PATH` creates the canonical catalog directories, a linked illustrated
documentation spine, `literate.project.json`, the provider-neutral root `SKILL.md`, and
thin generic, Codex, Claude, and Cursor agent entrypoints. Omit `--flavor` to keep the
smart defaults (Python, GNU Make, host OS, pip). Optional `--type` persists `project_type`
(default `application`, which installs the portable hello starter; `library` and the
other non-starter types keep taxonomy without that starter). `--type` is not required.
Bazel remains available as an explicit replaceable build-system Flavor. Default
init selects `source_intelligence.provider_id: none`, which never discovers,
installs, or invokes a source-intelligence command. A project may instead select
`--source-intelligence-provider codegraph-cli`; that opt-in records the external
`codegraph` command, minimum version `1.1.1`, and
`.codegraph/codegraph.db` artifact policy. Literate AI does not install or
bootstrap the provider. The operator provisions it, then uses `litai project
source-intelligence sync` or `check`. Required lifecycle stages fail closed on a
missing, stale, malformed, unsafe, or version-incompatible provider observation;
off stages invoke nothing, and preferred stages report bounded unavailability.
The initialized project-only policy keeps repository-source admission off because
the project database is not evidence about an independently quarantined source
tree; Literate AI does not manufacture a per-tree artifact from project-level
status.
The target may be absent, empty, or contain only a regular
`.git` directory and a regular root `README.md`; those repository-bootstrap entries are
preserved byte-for-byte and are not claimed by the initialization baseline. Any other
existing content fails before mutation unless you pass `litai init --convert`
(inspect first with `--convert --plan`). Convert quarantines the live tree and
wraps recorded operations; it is the adoption path, not a silent merge. Inverse
`spec derive` remains a later authority-transfer tool. It creates no Component
source or receipt file. Each direct-baseline and wrapper-parity command uses the same
finite deadline; override the 1,800-second default with
`--baseline-timeout-seconds SECONDS`. Failure evidence hashes each complete file-backed
stdout/stderr stream while retaining bounded reported storage-failure hints,
physical tails, first error contexts and heads. `--baseline-diagnostic-chars CHARS` selects a 512–65,536-character
combined diagnostic budget (default 8,192); CI may supply
`LITAI_CONVERT_DIAGNOSTIC_CHARS`, while an explicit CLI value takes precedence. The
effective timeout and diagnostic budget are recorded in both baseline and parity
evidence. A source file that resolves a required directory outside the repository can
declare that disposable sibling explicitly with repeated
`--harness-workspace-link DESTINATION=SOURCE`. `DESTINATION` is one safe directory name
created beside each disposable `legacy` root; `SOURCE` is an existing disjoint
operator-local directory. Conversion planning validates the binding without writing.
Baseline and both wrapper-parity phases use the same exact destination set, and project
evidence records only those portable names, never the host paths or linked contents.
Disposable Git identity setup and each harness command use the same framework Git
environment: inherited `GIT_*` controls are removed, global/system Git configuration
is disabled, and the disposable author/committer identity is explicit. Both
`CI_PROJECT_DIR` and `OMNI_REPO_ROOT` identify the observed phase root. Non-Git
toolchain environment remains available and the invoking process is unchanged.
This is Git-context isolation, not a sandbox or full credential-environment isolation;
native nested-container behavior still requires its own qualification.
Initialization creates synthetic
`literate.workers.example.json` and `literate.test.example.json` files, reports their
platform-resolved user configuration destinations in its result, and links the
derived-project matrix guide; it never invents or commits SSH
worker identities. The manifest configures
`test_receipt` as `verification/current.json` but deliberately omits a receipt policy.
That `policy-unconfigured` state is valid while onboarding, but no candidate can be
accepted or current until the project declares who may issue one and what it must prove.

Conversion derives repository policy from an explicit `--default-branch` first, then
the current branch's configured upstream, then remote HEAD and bounded local fallbacks.
That ordering also applies to linked and absorbed worktrees whose `.git` boundary is a
pointer file. A Git index containing any mode-`160000` Gitlink returns
`blocked-nested-submodules`: 0.9.0 does not transactionally remap `.gitmodules`, Gitlink
paths, and relative nested-worktree pointers, and `--allow-unready` cannot bypass this
repository-integrity boundary.

`litai init [PATH] --from URL[#REVISION]` initializes from any Literate-AI repository
and its complete declared ancestor DAG. Git resolution is non-interactive and
non-executing, uses isolated `OBJ_DIR` storage, rejects credential-bearing URLs and
unsafe tracked entries, and records the requested parent plus every exact resolved
commit. Components, Flavors, skills, workflows, and routing policies compose
ancestor-first; descendants may override ancestors, while conflicting coordinates from
incomparable parents fail closed. With no `--from`, the installed framework remains the
default parent at its highest published `vX.Y.Z` tag at or below this CLI version, never
`HEAD`. Workflow and routing documents are separate provenance-bound items, so an
inherited Component keeps the global generation authority named by its specification.
Git fetches default to a 3,600-second total deadline, 600 seconds without forced Git
progress, and a 30-second SSH connection attempt. The three
`--repository-fetch-*-seconds` options override those values for `init`, `update`, or
`reparent`, and `project parent checkout` within fixed 30–14,400, 15–1,800,
and 5–120 second bounds. Parent clone, submodule, and LFS transfers force Git progress;
activity resets the no-progress deadline while the total deadline remains a hard safety
ceiling. Connect may not exceed no-progress, and no-progress may not exceed total.
Results identify the canonical policy plus per-field `framework-default`,
`project-policy`, or `cli` provenance; this operational evidence does not change the
exact repository-lineage identity.

`litai update [PATH]` is an initialized-project migration planner. Planning is always
read-only; writing requires an explicit flag. It reads the canonical initialization
origin and baseline, re-resolves the complete recorded repository lineage, composes its
prospective inherited catalogs, and compares those plus the static framework scaffold
with current local bytes. The composite result carries separate, versioned framework
and repository-lineage plans with exact content identities and three-way
classifications. It rejects framework repository substitution and incomplete parent
chains.

Projects created by a pre-lineage Literate AI release must first run an explicit
`litai reparent URL[#REVISION]` (or `litai reparent none`) and review its plan before
applying it. Only the simultaneous absence of both lineage documents is admitted as the
legacy-root starting state. Partial or malformed evidence still fails closed, and
ordinary `update` never infers or manufactures a parent. Applying `reparent none`
materializes explicit root evidence even though the semantic lineage is empty.

Before updating a pre-canonical 0.8.x tree whose Flavor directories still use names
such as `python`, `macos`, or `make`, first inspect
`litai catalog migrate-flavor-names --project PATH`, then run it again with `--record`.
That explicit taxonomy migration preserves local Flavor content while moving it to the
canonical `lang-*`, `os-*`, and `build-*` directories; running update first would
correctly fail duplicate-Flavor validation when it adds the canonical parent paths.

`litai update --apply` writes the mechanically safe subset in both plans:
`upstream-only` files,
which the classifier reached by proving the project's copy still equals the recorded
baseline, so upstream is the sole author and nothing local can be lost. `conflict`,
`local-only`, and `preserved-dynamic` are preserved and reported as refused by default.
`--adopt-added` additionally writes `upstream-added` files; that stays opt-in because
adopting a capability a project never had is a decision, and a new declared document
can leave the documentation graph unreachable.

After reviewing a true inherited-catalog conflict, repeat
`--take-upstream PROJECT-RELATIVE-PATH` with `--apply` to replace exactly those paths
inside the same transaction. A path that is not a conflict in that exact plan is an
error, and the apply receipt lists accepted choices under `taken_upstream`. This is the
execution half of a reviewed decision; `--review-conflicts` itself remains plan-only.
Framework conflicts and every unselected catalog conflict remain untouched.
If validation shows that local authority still depends on an inherited path which the
new parent retired, repeat `--keep-local PROJECT-RELATIVE-PATH` to preserve only that
planned removal as local authority. Its import provenance is removed and the receipt
records it under `kept_local`; the flag cannot retain additions, updates, or arbitrary
paths.

The plan is a snapshot, so every write re-reads both sides first and fails closed if
either moved since planning. Framework writes are atomic per file. Inherited catalog
application additionally treats catalog files, `.literate/imports.json`, and
`.literate/repository-lineage.json` as one rollback unit and validates before commit.
Applying makes the documentation authority review stale by design, and each result
reports `authority_review_required`.

An intentional framework wheel upgrade is separate from inherited-file update. A
Standard-bound project pins the exact installed wheel payload and immutable Standard
policy, so `litai update` never silently changes that executable authority. Use this
review sequence after installing the new non-editable wheel:

```console
litai update PROJECT --apply
litai project lifecycle rebind-standard --project PROJECT --output rebind.json
# Review configured_binding, proposed_binding, embedded origin, versions, and identity.
litai project lifecycle rebind-standard rebind.json --project PROJECT \
  --apply --authorize-rebind
litai rebuild . --project PROJECT --from-accepted-source
```

Planning is read-only. It accepts exactly one discoverable non-editable `literate-ai`
distribution, validates the wheel's embedded repository/revision evidence, and binds
the current project bytes, old/new lifecycle binding, old/new receipt policy, installed
payload identity, and Standard policy identity. Apply requires the reviewed plan and
uses compare-and-swap. It atomically updates only `lifecycle_driver` and the derived
`test_receipt_policy`, re-observes the installed authority, and rolls the project back
if that authority moves during the write. Editable, ambiguous, origin-less, tampered,
or changed installs fail closed.

For a custom (non-Standard) receipt suite, rebind preserves its suite ID/version,
required evidence kinds, and minimum test count. An independent harness runner stays
unchanged; a runner exactly bound to the old Standard driver advances to the new
driver identity. Only the Standard suite adopts the installed Standard receipt policy.
The plan lists only fields that actually change. Re-plan an older proposal that would
replace custom receipt authority; apply rejects it even if its identity is consistent.

The operation does not build, execute generated code, delete a prior receipt, or rewrite
accepted source. Changing the binding makes those earlier identities stale by contract;
the final rebuild is what produces current accepted evidence. A
`rebuild.standard_distribution_mismatch` after an intentional upgrade is therefore a
request for this reviewed rebind, not permission to hand-edit a digest or weaken the
resolver.

Merged-text conflict migration, destructive upstream removal inference, initialization
baseline compaction, and post-apply lifecycle gates remain separate reviewed work.

`litai update --record-work-items` additionally appends advisory entries to the
project's queue (`docs/roadmap/active-work.md` by default, overridable with
`--queue`). Only classifications that represent real upstream movement produce
entries: each `conflict` becomes its own item because it needs individual judgment,
while `upstream-added` and `upstream-only` are grouped into one item each. Entry ids
are content-addressed, so re-running adds nothing and never reintroduces an item you
checked off. The queue is appended to, never rewritten, and must already exist and be
reachable from the documentation spine; recording makes the documentation authority
review stale by design, because a declared document changed. A source checkout obtains this origin
from Git; a wheel carries the URL and exact build commit as generated distribution
metadata, so installing the wheel does not erase an operator fork. Distribution builds
outside a Git checkout must set `LITAI_BUILD_REPOSITORY_URL` and
`LITAI_BUILD_GIT_REVISION` together; missing, partial, credential-bearing, or non-exact
provenance fails the build. `apply_supported` is false in this
milestone: no file, lock, authority marker, or initialization checkpoint is changed.
Dynamic initialized outputs are explicitly preserved for later reviewed migration.

`litai learn RUN` reads one canonical learning-plan input JSON document and emits
exactly one deterministic, content-identified authority proposal. It classifies the
evidence as Component behavior/interface, Flavor variance, reusable Skill technique,
workflow handoff, routing eligibility, framework defect, or candidate-specific
no-change; it binds the narrow authority owner, affected test suites, and rebuild scope.
The command is read-only: it neither invokes a coding CLI nor edits specifications,
Flavors, Skills, workflows, routing, tests, or framework policy.

`litai project validate [PATH]` searches upward for the project manifest. It is
non-mutating: it checks catalog containment and canonical source-intelligence configuration.
When source intelligence is `none` or a stage is `off`, validation reports that
state and continues. Explicit `litai project source-intelligence sync [PATH]`
and `check` remain fail-closed only for an opted-in provider. Validation then checks onboarding files,
every Component and Flavor, pinned workflow and routing bytes, both skill graphs, and
the rule that forward-generated Component source is not retained in the project. It
enumerates Markdown beneath required
`documentation_roots` with content identities and requires `SKILL.md` links to stay
inside that declared set. It also requires exactly one current documentation-authority
review marker. Its result reports the configured test receipt as `unconfigured`,
`policy-unconfigured`, `missing`, `current`, `stale`, or `policy-mismatch`. Any change
to the bound authority graph makes a previous receipt visibly stale without making the
project unreadable.

`litai project tracker inspect [PATH]` reads Git remotes (the same authority as
`.git/config`, including worktrees) and classifies the issue tracker. GitHub remotes
name `gh` for issues, pull requests, hosted CI, and land; GitLab remotes, including self-hosted hosts
whose DNS labels include `gitlab`, name `glab` for issues, merge requests, CI, and land. The
result includes `issue_list`, `review_list`, `ci_status`, `land_create`,
`land_merge`, `review_status`, and `issue_status` argv. Agents wrap this command and then
invoke `gh` or `glab`; they do not re-parse remotes. Conflicting GitHub and GitLab
remotes without `origin` fail closed as `project.tracker_host_ambiguous`. Tokens in
remote URLs never appear in inspect evidence. This is not GitLab release-gate
dispatch: `litai release check --target gitlab` stays unsupported until RELEASE-005.

`litai project peer-work --when start|end [PATH]` surveys other developers' work
around one cycle. Start lists open issues plus open PRs/MRs and classifies
GitHub review CI; only non-draft reviews whose checks are all success are
`green_reviews`. End lists leftover Git worktrees and local branches that are
not the current checkout, not the default branch, and not already ancestors of
HEAD. The command never removes a worktree or deletes a branch.

`litai project parent checkout URL[#REVISION] [--project PATH]
[--repository-fetch-*-seconds N]` clones or updates a
parent working tree under `parents/<id>/` in the *current* project, initializes
submodules, and pulls Git LFS when pointers are present. Use it to search a parent,
edit it, or file issues and review requests against it. Lineage resolution
(`init --from`, `update`, `reparent`) stays non-executing plumbing in
`OBJ_DIR/repository-lineage`. Do not clone parents into `/tmp` or extra Git worktrees.
`parents/` is excluded from the child's index via `.git/info/exclude` and is not catalog
authority. URLs with embedded credentials fail closed.

`litai project ci-plan [PATH] [--mode shard|impact|compose]` detects the project's test
frameworks and emits a fail-closed shard and impact-selection plan. It records which
maintained mechanism exists for pytest, Jest, Go, cargo-nextest, GoogleTest/CTest, and
Swift, or says the mechanism is unavailable. Missing or untrusted impact maps select
the full suite. Compose is impact then shard. This repository's checkpointed
Linux/macOS unittest jobs stay unsharded.

`litai project mac-contract RESOLVED_BOM --platform HOST_FAMILY [...]` is the
interoperability bridge for MAC runners and their OpenShell sandboxes. It accepts one
exact canonical CycloneDX 1.7 post-build BOM and writes `.mac/project.yaml` plus
`.mac/project.contract.json`. The YAML uses MAC's `mac.repository_contract.v1` schema;
the compact JSON sidecar binds those bytes to the source BOM, resolved BOM, and resolved
graph identities. Prerequisite commands are projected only from executable paths in
build, test, packaging, deployment, or toolchain-scoped BOM components, plus the
intrinsic `litai` and `git` commands. The command never inspects source,
manifests, PATH, Components, Flavors, or skills as a fallback. If the selected Flavor or
skill closure is absent, repair the existing CycloneDX creation stage and regenerate the
BOM. MAC remains optional and owns contract validation, runner routing, and OpenShell
policy composition.

### Project source intelligence

`source_intelligence` is a required project-level policy record, not a generated
database hash. It selects a provider independently at each lifecycle stage:

```json
{
  "source_intelligence": {
    "schema": "urn:literate-ai:schema:v1:project-source-intelligence-policy",
    "provider_id": "none",
    "command": null,
    "minimum_version": null,
    "artifact_path": null,
    "stages": {
      "project-maintenance": "off",
      "source-generation": "off",
      "cache-consumption": "off",
      "source-to-specification": "off",
      "repository-source-admission": "off",
      "structural-review": "off"
    },
    "artifact_publication": "metadata-only"
  }
}
```

Each stage is `required`, `preferred`, or `off`. Product
`validate`/`build`/`run`/`rebuild` treat source intelligence as off when the
provider is `none`. `preferred` records an unavailable
reason without turning derived intelligence into authority; `off` performs no
provider work. Provider `none` requires every stage off.
Workspace and repository-cache records carry the stage, mode, state, provider ID, and
stable unavailable reason when applicable. A source-generation stage that is `off`
commits and resolves the exact source tree without creating a provider sidecar;
`required` cannot silently instantiate a default provider.

`sync` creates the index when absent and otherwise performs an incremental refresh;
`check` never writes it. Both reject a symlink/non-file database, another project root,
pending changes, worktree drift, malformed status, or an older runtime. `--binary` is a
deliberate invocation override for repository-pinned tooling and CI; it does not rewrite
the manifest command or weaken the provider/version/path contract. Process stderr stays
in local diagnostics and never enters the stable JSON error envelope.

The SQLite bytes are local derived state and remain ignored by Git. Generated-source
identity covers only the canonical `source/` tree. Generation provenance and the
project's passing receipt instead binds stable `source-intelligence` evidence for that
exact tree. This preserves reproducibility without turning a mutable binary cache
into application or specification authority.

### Documentation authority review

Literate documentation is first-class project state, not an optional explanatory
layer. The sole marker has this exact shape and lives in one declared Markdown file:

```text
<!-- literate-ai:authority-reviewed sha256:... -->
```

Its identity binds the current project definition, root `SKILL.md`, Component and
Flavor revisions, exact forward and inverse skills, workflow and routing catalogs, and
normalized declared documentation plus exact declared assets. The marker text itself is
removed before the document identity is calculated, avoiding a self-reference.

After changing any bound authority or documentation, `project validate` reports the
marker as `missing`, `duplicate`, or `stale`. Start with a deterministic reconciliation
plan; this reads declared Markdown and current validated authority without writing or
invoking a model:

```console
litai project documentation-update .
```

The plan reports bounded drift candidates such as obsolete installed commands and
retired source-intelligence claims. For semantic reconciliation, explicitly authorize
both model egress and documentation writes:

```console
litai project documentation-update . --apply --allow-model-egress
```

The coding CLI runs in an isolated JSON-task workspace and returns complete replacement
content. Literate AI accepts only existing UTF-8 Markdown beneath declared
`documentation_roots`, binds each replacement to the original SHA-256 identity, rejects
review-marker changes and secret-like content, and applies validated replacements
atomically per file with rollback. It cannot edit Component/Flavor/skill authority,
locks, receipts, generated source, assets, or ambient files. Apply never records the
review marker.

Review the resulting diff against the new authority graph, then ask the non-writing
review command for the exact replacement:

```console
litai project documentation-review .
```

Its JSON result includes `state`, `authority_identity`, `expected_marker`, and the
current marker document when exactly one exists. After review, `--record` atomically
replaces the sole marker and verifies that it became current:

```console
litai project documentation-review . --record
```

Then run `litai project validate .` and commit the reviewed documentation. Planning or
applying documentation updates never claims that a person performed this final review.

### Project test receipts

`litai project test-receipt verify-evidence` authenticates a complete retained evidence
graph against an independently prepared plan and explicit pinned-key trust policy:

```bash
litai project test-receipt verify-evidence plan.json --project . \
  --policy trusted-policy.json --revocations current-revocations.json \
  --store local=/absolute/physical/path/to/store \
  --retention subject-retention.dsse.json --retention run-retention.dsse.json
```

Repeat `--retention` for every detached signed locator needed to cover every graph
object. Use `--monorepo-store ID=REPOSITORY` for the repository's
`verification/evidence` CAS or `--https-store ID=HTTPS_URL` for a read-only HTTPS
store. Store IDs must be unique across all three options. Filesystem store paths
must have no symbolic-link components. HTTPS uses verified TLS with no ambient
credentials, proxy routing or redirect following.

For a retained compact current map, replace the detached `--retention` arguments
with `--current-map current-map.json --bundle-store /absolute/physical/path/to/bundle`.
Both options are required together. The canonical map is bounded to 256 KiB and
references the stored plan and detached proofs. Continue to supply the independent
plan, policy, revocations and signed graph store mappings. The bundle cannot replace
availability checks against those graph stores. Verification repeats finalized
receipt validation and rejects a map that changes during reads. Its result includes
`current_map_identity`; it does not install the map as the project's current receipt.
If a map has been installed at the configured receipt path, ordinary receipt inspection
reports `authentication-required` and `authenticated: false`. `require-current`
refuses it until the caller uses fresh evidence verification with independent inputs;
an earlier successful verification does not change that read-only inspection state.

To retain the complete verified bundle and replace the configured receipt with its
compact map, use the explicit publication command:

```bash
litai project test-receipt publish-evidence plan.json --project . \
  --policy trusted-policy.json --revocations current-revocations.json \
  --store local=/absolute/physical/path/to/source-store \
  --bundle-store /absolute/physical/path/to/retained-bundle \
  --retention subject-retention.dsse.json --retention run-retention.dsse.json
```

Provide all detached roots, as for verification. The full matrix must bind a canonical
finalized receipt that satisfies the project's current suite and runner policy.
Publication verifies the graph before retaining bytes, reads each retained object back,
then verifies again under the project lifecycle lock before atomic pointer replacement.
It reloads the independent plan, policy, revocations and project authority during this
flow. Failure preserves the prior receipt, though unreferenced immutable bundle objects
may remain. Success reports the map and verification identities and whether the pointer
changed. Subsequent use still requires fresh verification against the original signed
store mappings. This command grants no execution authority and does not configure
release admission policy.

For a gate that requires the project's configured current receipt to be authenticated,
use `litai project test-receipt require-current-evidence plan.json --project .` with
the same `--policy`, `--revocations`, `--bundle-store` and signed store mappings.
This read-only command selects the receipt path from the project definition; it has
no `--current-map` override and accepts no unsigned fallback. It reloads the configured
path and project authority after full retained-graph verification and reports
`current_receipt_path` on success. Use this operation in an authenticated release gate;
`require-current` retains its local consistency meaning. Local unsigned receipt
inspection explicitly reports `authenticated: false`, even when its state is `current`.

The v2 `evidence-verification.schema.json` defines the plan and compact result.
The plan binds current project authority, the root envelope and the full independently
expected run graph. Requirements are ordered by envelope content identity; each
includes an exact run expectation, child edges and named artifacts. Prepare the plan,
policy and current revocations from trusted execution authority, independently of
producer assertions. Policy and revocation records use `evidence-trust.schema.json`.
The command bounds each JSON input to 4 MiB and each detached DSSE root to 16 MiB;
all evidence shares the graph's 1,024-object and 256 MiB total bounds. It checks the
current clock and reloads revocations after retrieval, then rechecks project authority,
plan and policy. Missing bytes, partial coverage, substitutions and invalid signatures
fail even when the remaining evidence is valid.

Success reports authentication and retention observed during this check, plus content
identities and counts. It does not update `verification/current.json`, promote a
receipt, authorize execution or promise future storage availability. The command
suppresses host updates, MCP setup and event journaling; MCP discovery and debug-file
output are refused. CI identity admission and durable receipt promotion remain
separate from this pinned-key verification operation.


Known failures are repair-checkpoint state, not passing receipts. Manage them through
the explicit `litai project test-checkpoint inspect|annotate|revalidate|clear`
operations. `annotate` requires an exact pin, test key, context or universal-authority
identity, failure fingerprint, evidence run/node, cause, and credential-free reason;
`--expires-at` selects an expiry policy, otherwise revalidation is manual. External
runners submit strict per-test `start`, `passed`, `failed`, `skipped`, or
`known-failure` events through `test-checkpoint import`. Unmatched starts become
failures, and a known-failure event is admitted only when its annotation and original
failure identities match exactly. The ledger's typed `last_report` keeps
passed/failed/known-failed/skipped/executed totals visible while its nonzero result
prevents a passing receipt. `--release-evidence` rejects retained known-failure events.
The built-in Python adapter folds the discovered test-source bytes into its default
suite pin; an external runner is responsible for supplying an equivalently exact pin.

`test_receipt` is an optional, normalized project-relative path. When present, it must
not overlap `literate.project.json`, `SKILL.md`, or any declared catalog root. Canonical
projects use one replaceable file and an explicit admission policy:

```json
{
  "test_receipt": "verification/current.json",
  "test_receipt_policy": {
    "schema": "urn:literate-ai:schema:v1:project-test-receipt-policy",
    "suite_id": "sample-host-e2e",
    "suite_version": "0.1.1",
    "runner_identity": {
      "schema": "urn:literate-ai:schema:v1:content-identity",
      "algorithm": "sha256",
      "digest": "...64 lowercase hex digits..."
    },
    "required_evidence_kinds": [
      "acceptance-result",
      "build-result",
      "generation-provenance",
      "lifecycle-command",
      "lifecycle-plan",
      "lifecycle-request",
      "observation-result",
      "resolved-sbom",
      "security-scan-report",
      "source-cache-decision",
      "source-cache-lifecycle",
      "source-sbom",
      "test-report",
      "test-runner",
      "workspace-admission"
    ],
    "minimum_test_count": 100
  }
}
```

The policy is project authority. It admits exactly one suite ID and semantic version,
requires at least the configured number of passing tests and named evidence roles, and
requires `test-runner` evidence to equal the configured runner content identity. A path
without a policy is storage configuration, not release authority: `update`, `check`,
and `require-current` all fail closed. This is a local, unauthenticated Git assertion
whose allowlist binds claimed identities; it is not by itself cryptographic or remote
attestation that the external artifacts exist. Systems needing that guarantee must
authenticate and retain the content-addressed evidence behind those identities.

`litai rebuild` independently requires the complete base lifecycle role set shown above,
even if a weaker policy is accidentally configured. A driver with package, publication,
or deployment extensions must also return the corresponding extension evidence.

The framework does not infer a passing run from console output. A lifecycle driver
writes a provisional assertion after testing one exact subject. Only `litai rebuild`
may cross the **supported API/TCB boundary** after validating current lifecycle and
outer publication effects. It emits a finalized candidate envelope containing the
provisional, request, command, cache control/decision/lifecycle, and raw-receipt
identities plus the nested receipt. Public `update` and `check` reject both provisional
assertions and extracted raw receipts. This API convention is not a cryptographic trust
anchor against a same-authority filesystem writer; use an authenticated operator ledger
or signature when that threat is in scope.

On successful promotion, Git stores the canonical finalized envelope. The envelope
retains the exact Component-lock set and outer lifecycle/cache identities needed to
reconstruct the qualified project authority. Its nested receipt remains deliberately
compact: `schema`, `project`, `project_revision`, `subject`, `suite` (`id`, `version`,
and `revision`), a positive `tests` count, `result`, and an `evidence` object mapping
each allowed evidence kind to one SHA-256 identity. `project_revision`, `subject`, the
suite `revision`, `result`, and every evidence value are content-identity URIs. Passing
is implicit and structurally enforced: there are no outcome, passed, failed, or skipped
fields to contradict it. Canonical storage is minified JSON and carries no timestamps,
paths, logs, payloads, or other run ephemera.

`source-sbom` identifies the validated pre-build CycloneDX document generated at
`source/.literate/sbom.cdx.json`; `resolved-sbom` identifies the separately verified
post-build document with exact resolved versions. Requiring both roles binds dependency
evidence into the compact receipt without putting either full graph in Git.

`project_revision` is the identity of the same complete authority-review input graph
reported by `project documentation-review`: the project definition, onboarding skill,
Components and their specifications, Flavors, exact skill graphs, workflow and routing
catalogs, and normalized declared documentation and assets. A change to any of those
inputs makes the committed receipt `stale`, even when `literate.project.json` itself is
unchanged.

```console
litai project test-receipt update /tmp/test-candidate.json --project .
litai project test-receipt check /tmp/test-candidate.json --project .
litai project test-receipt require-current --project .
git add verification/current.json
git commit -m 'Record current passing test receipt'
```

`update` bounds and validates the finalized candidate, verifies its nested receipt's
project and policy binding, writes the canonical finalized envelope, and performs an
idempotent atomic replacement. It rejects raw/provisional inputs, symlinks,
invalid existing receipts, concurrent replacement, another project or authority
revision, an unauthorized suite or runner, missing required evidence, too few tests,
and any failed or skipped result. `check` requires both current policy admission and
exact canonical candidate equality. Neither command runs a test suite or invokes Git.
`require-current` needs no candidate: it fails when policy is absent, or when the
configured receipt is absent, invalid, stale, or no longer satisfies policy.
On success it reports both the receipt's and current authority revision identities. It
also does not rerun tests. Failed and skipped runs therefore leave the last passing
receipt untouched; ordinary Git history, rather than an append-only receipt log,
records earlier committed states.

The general generation CLI does not select a project's generated-suite executor;
`update` and `check` consume an outer-finalized candidate. The sample host runner is the
reference adapter: it validates and executes every model-generated case against the
exact built artifact, runs its separate hidden-oracle and entropy cases for acceptance,
and writes only its provisional assertion to the driver-side `--test-receipt PATH`.
`litai rebuild` validates and finalizes it. Other projects must provide the same
manifest → exact run → provisional assertion → outer-finalized candidate seam for their
own build and test systems.

A project created by `litai init --convert` instead receives the exact retained-only
policy derived from its admitted harness inventory. Run it with:

```console
litai project test-receipt run-retained /tmp/retained-candidate.json --project .
litai project test-receipt update /tmp/retained-candidate.json --project .
litai project convert-stage advance --to retained --project .
litai status --project .
```

Failures preserve the harness's 8,192-character combined diagnostic budget through
the CLI, plus a bounded headline allowance. Reported ENOSPC, disk-full and quota
errors are prioritized ahead of ordinary output; both stream tails remain visible.
A report from command output is an investigation hint, not a capacity measurement.
Check free space, quota and inode headroom on the execution worker, including its
workspace, temp and cache volumes. A directly observed local OS ENOSPC/EDQUOT error
uses `project.host_storage_exhausted`; generic exit status 2 does not. Failure never
publishes a passing candidate or authorizes cleanup.

`--worker-id local` keeps this execution on the controller and does not load a worker
catalog, even when `--worker-config` is present. Any other worker ID loads the private
catalog selected by `--worker-config`, `LITAI_WORKER_CONFIG`, or the platform-resolved
user configuration path and requires an exact SSH entry whose declared OS is Linux or
macOS. Unknown IDs, command workers, Windows workers, and workers without an explicit
POSIX OS fail before transport.

The remote path sends only the admitted retained source-scope archive, canonical
harness inventory, selected worker declaration, and identity-bound request. The
Git-visible scope recursively includes tracked and nonignored untracked files from
each initialized Git submodule, including nested initialized submodules. Uninitialized
gitlinks remain absent, ignored child roots remain excluded and accounted for, and
Git administrative state is never flattened into the source archive. All included
child bytes participate in the ordinary scope, tree, and archive identities.
The
controller also sends a deterministic ZIP of its current `literate_ai` package so an
older configured worker launcher can expose the current internal
`worker execute-retained` receiver without changing the worker installation. The
request binds the runtime ZIP's SHA-256 identity and size; the receiver independently
rechecks both. The configured launcher runs with only that exact ZIP on `PYTHONPATH`,
with user-site loading, bytecode writes, and launcher self-update disabled for the
receiver. Receiver-only Python controls are removed before retained commands run, so
they cannot alter a project's interpreter search path or user-site behavior. The
receiver safely extracts and executes the retained projection, returns bounded canonical
sanitized phases plus its actual platform, and removes attempt directories. Cleanup is
mandatory and receives a separate bounded five-minute allowance for large generated
trees.
The controller validates the request, source, inventory, runtime, and worker bindings
before finalizing the candidate. This initial slice does not import or claim the
separate full remote evidence-bundle custody protocol. Qualified external sibling
projections are not yet transported remotely.
Both the admitted source content and compressed source archive are limited to 2 GiB
and 100,000 source entries. This matches the existing finite host-install archive
ceiling, remains below the 8 GiB remote-evidence ceiling, and avoids the earlier
256 MiB cutoff for large retained repositories. Archive capture and digest validation
stream through bounded files; safe extraction validates all members before exposing
the attempt workspace. The separately identity-bound canonical inventory may be at
most 32 MiB; remote control results remain limited to 1 MiB.
The coordinator runtime ZIP admits only portable regular files beneath `literate_ai`,
rejects links and special files, omits `__pycache__`, `.pyc`, and `.pyo` content, and
uses canonical stored ZIP members with fixed modes and timestamps. Its limits are
20,000 files, 32 MiB per file, 192 MiB total package content, and a 256 MiB archive.

The first command copies only the captured authored-source scope to a disposable
directory, executes every admitted build/test/package/CI stage with its recorded working
directory, and writes a new external evidence document plus outer-finalized candidate.
If conversion qualified external sibling destinations, repeat each one with
`--harness-workspace-link DESTINATION=SOURCE`; missing or extra destinations fail before
execution, while the source locator may differ on another host. The portable topology
must match conversion evidence, and the exact operator-local locator set is represented
only by an opaque identity in the runtime request and evidence.
It binds the current authority and Component-lock identities, worker/platform, runner,
commands, source scope, phases, artifacts, logs by digest, and an exact positive test
count. Current retained-source bytes are part of project authority, so an implementation
edit makes the committed receipt stale; a command that mutates authored source in its
disposable copy cannot earn a candidate. The receipt never claims native Component
generation or independent acceptance. Any failed, skipped, expected-failure,
known-failure, empty, or uncounted test phase is ineligible, and neither output may be
inside project authority or overwrite an existing file. The ordinary `update`, `check`,
and `require-current` boundary remains unchanged.

### Inspecting Git-submodule orchestration

`litai onboard orchestrate plan PATH --declaration FILE` is a separate read-only
profile for exact Gitlink inventory and explicit child dependency relationships.
`check` takes the same arguments plus a required `--expected-plan-identity` and
recomputes current inputs. See the [declaration format and boundaries](getting-started.md#plan-a-git-submodule-orchestration-project).
Neither command initializes, flattens or fetches children; a current check is not
proof of child acceptance or publication. Ordinary adoption policy is unchanged.

Add both `--project-id ID` and `--project-version VERSION` to `plan` and `check`
to review an initialization plan, including its exact root-owned file set.
`initialize` requires those same arguments, the initialization plan's
`--expected-plan-identity` and `--acknowledge`. It adds only root orchestration
authority, validates staged files, publishes the manifest last and preserves child
repositories. Existing targets refuse; this is not an overwrite or refresh command.
The [initialization walkthrough](getting-started.md#plan-a-git-submodule-orchestration-project)
describes rollback and unqualified child/release boundaries.

`litai onboard orchestrate refresh plan PATH --request FILE` reads an exact canonical
`literate-ai/orchestration-refresh-request@1`, verifies every requested commit
against the root-declared remote, and reports current/prospective authority plus local
custody and publication identities. `check` recomputes that plan with
`--expected-plan-identity`; both operations are read-only. `apply` adds
`--acknowledge`, recomputes the same plan, and commits the live transaction with the
root manifest last. All operations accept the shared bounded
`--repository-fetch-*-seconds` options. The result reports changed versus no-op
honestly, preserves independent child authority, and does not qualify child
acceptance or crash replay. A changed apply reports that the root documentation
authority requires review; record its new marker before planning another refresh.
See [repository lifecycle](repository-lifecycle.md#review-and-apply-exact-child-pin-refreshes).

For an exact initialized root with `repository_orchestration`, `lock PATH` composes
`.literate/repository.lock.json` with root-owned Component locks. `lock --check` and
`--diff` are read-only, including Component inspection; `plan PATH` refuses missing
or stale repository/Component locks. `verify PATH --gate locks` requires the root
repository lock and checks existing committed root Component locks. Root results
distinguish declared child pins from local checkout observations; neither result
authorizes child execution, publication or re-pinning. These commands do not traverse
child catalogs. An ordinary Component invocation keeps its existing behavior.

Repository-root invocations reject model/recipe/large-review options, ambient
`LITAI_MATRIX_CELL_ROOT`, explicit MCP discovery and debug-file output. Flavor/target
selectors require root-owned Components. Root inspection has no implicit updater,
telemetry or journal side effects. See the [root lock walkthrough](getting-started.md#plan-a-git-submodule-orchestration-project)
for identity and partial-update boundaries.

### Reviewing multiple build roots before adoption

`litai onboard adopt PATH` reports `conversion.build_root_candidates`. A marker
such as `kit/repo.toml` is a candidate, not a selected Component or a proven
independent build. The default continues to use one retained wrapper.

For explicit refinement, provide a selection file outside the source tree, for
example a two-root repository with a shared directory:

```json
{
  "schema": "literate-ai/monorepo-adoption-selection@1",
  "components": [
    {
      "name": "kit", "root": "kit",
      "commands": [
        {"id": "build", "command": "make", "cwd": "kit", "evidence": "kit/Makefile"},
        {"id": "test", "command": "make test", "cwd": "kit", "evidence": "kit/Makefile"}
      ]
    },
    {
      "name": "runtime", "root": "runtime",
      "commands": [
        {"id": "build", "command": "make", "cwd": "runtime", "evidence": "runtime/Makefile"},
        {"id": "test", "command": "make test", "cwd": "runtime", "evidence": "runtime/Makefile"}
      ]
    }
  ],
  "shared_sources": [
    {"path": "shared", "owner": "kit", "consumers": ["runtime"]},
    {"path": "README.md", "owner": "kit", "consumers": []}
  ]
}
```

```console
litai onboard adopt PATH --root-plan SELECTION.json
```

Adapt the example to the real files and commands. Every captured source file outside
selected roots needs an explicit owner, including root-level drivers, configuration
and documentation. Shared consumers are declared usage, not inferred dependency
proof. The plan binds exact bytes and executable bits, and rejects overlaps,
missing ownership, unavailable command evidence and indirect source paths. Inspection
executes no declared command. See [the custody contract](../architecture/monorepo-adoption.md).

Planning remains read-only. To authorize real execution of every selected root's
declared harness, add `--run-baseline`; without it the plan reports
`apply_supported=false`. Then apply with `--apply --acknowledge --expect-plan` and
the exact reviewed onboarding plan identity. The conversion publishes distinct
source-free Components, retained receipts and per-Component `retained-source`
boundary-transfer state while keeping the original source beneath the legacy wrapper.
Source changes invalidate the reviewed identity. To use the existing single-wrapper
conversion instead, omit `--root-plan` and review that separate plan.

### Refreshing retained source membership

When upstream adds or removes files in the retained implementation, inspect:

```console
litai project retained-scope refresh --project PATH
```

Review `added`, `removed`, `source_members`, the old/new inventory, and the authority
review. Apply that exact plan identity explicitly:

```console
litai project retained-scope refresh --project PATH --apply --acknowledge --expected-plan-identity sha256:REVIEWED_DIGEST
```

For a refined monorepo whose Component custody is stale, also pass
`--run-component-baselines`. The apply then revalidates the stored ownership plan,
executes only Components whose exact retained revision is stale, reuses unaffected
receipts, and republishes the complete source-free custody set transactionally.
Without that explicit execution flag the refresh refuses.

This operation reuses Git-visible source capture (including nonignored untracked
files), or the existing pruned-filesystem policy when Git is unavailable. It does
not redetect or replace admitted harness commands, edit source, or advance conversion
stages. The default single-wrapper path executes no harness; refined Component
execution occurs only with `--run-component-baselines`. Indirect source/metadata
paths and already specification-authoritative projects refuse. Inspect a new plan
after any input drift; an unchanged plan applies as a no-op.

The original baseline, wrapper parity, lift-shift evidence and receipt remain on
disk. Reviewed refresh plans are retained beneath `.literate/retained-scope-refresh/`
as history, not execution evidence. The refreshed inventory and receipt policy make
the old receipt stale. Earn a new external candidate with `project test-receipt
run-retained`, publish it with `project test-receipt update`, and verify with
`project test-receipt require-current`. Recorded conversion stages are historical;
later advancement requires the new receipt, not the pre-refresh one.

Refresh, retained execution and receipt publication share the project lifecycle
lock. Ordinary partial-write failures restore owned metadata without reverting source
edits. If a process exits abruptly between metadata writes, mismatched policy or
review blocks qualification; inspect and acknowledge a fresh refresh plan to reconcile
that state. Review or restore any unrelated concurrent edits before retrying.

### Coding CLI selection and generation boundary

Set `CODING_CLI` to exactly `codex`, `claude`, `cursor-agent`, or `opencode`. If it is
unset, the command selects the first executable on `PATH` in that order. An explicit unavailable
selection is an error; there is no fallback. The command invokes the agent
non-interactively and reports the coding CLI, selected model, recipe identity, and
Flavors. Before route compilation it resolves and hashes the executable. The result
records that exact executable and selection, the command and request identities, the
execution plan, requested stage/route metadata, and the resolved
Component/composition/target/Flavor/effective-revision identities. Requested stages are
request evidence, not a claim that the opaque CLI independently acknowledged them.

For OpenCode, executable discovery is followed immediately before model egress by a
bounded, prompt-free `opencode --pure run --help` compatibility probe over the same
exact pinned executable. The advertised surface must include `--pure`, `--dir`,
`--agent`, and `--format`. Missing capabilities or a failed probe produce
`coding_cli.incompatible` with upgrade guidance; LitAI neither retries through another
provider nor drops `--pure` to accommodate an older release.

The subprocess receives only explicit system launch, proxy/certificate, and
provider-auth environment keys; inherited `PWD` is replaced with the output workspace.
Persisted login is scoped to the current machine and user profile. Check it with
`codex login status`, `claude auth status`, `cursor-agent status`, or
`opencode auth list`, or supply the
selected provider's supported environment credential (`OPENAI_API_KEY` through
`codex login --with-api-key` or `CODEX_ACCESS_TOKEN` through
`codex login --with-access-token`; `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or
`ANTHROPIC_AUTH_TOKEN`; `CURSOR_API_KEY`; or an OpenCode-provider credential such as
`OPENCODE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or
`OPENROUTER_API_KEY`). OpenCode's stored provider login is configured with
`opencode auth login`. Literate AI forwards allowed values to the selected process but
never writes them. `CODEX_API_KEY` remains a forwarded
compatibility input for custom Codex provider configurations, but it is not the
documented credential name for the standard Codex login flow.
Every provider is reported as non-hermetic with its actual clean/sandbox profile and
limitations. Generated output is bounded to 4,096 filesystem entries, 1,024 files,
16 MiB total, paths of at most 512 UTF-8 bytes, and 32 path components. Stdout and
JSON-task stderr are independently capped at 1 MiB while the process runs;
source-generation stderr uses the 16 MiB generated-tree budget because supported coding
agents stream file-operation progress there. Exceeding any applicable bound terminates
or rejects the operation instead of returning a partial tree.

Every coding CLI invocation is a clean major rebuild in a new empty output directory.
It must generate the implementation and `source/tests/manifest.json` from the exact
current recipe. The manifest contains 3–256 unique cases, includes example, boundary,
and invariant coverage, cites only current non-acceptance specification documents, and
cannot reuse acceptance-interface argument vectors. A prior generated source or test
tree is never an input and must not be copied into the project.

The Standard node adapter additionally requires that directory to exist already, be
absolute, empty, non-redirected, and bound to the node's exact workspace-allocation
identity. Its successful return is source-only evidence. It deliberately performs no
source indexing, build authorization, compilation, test execution, acceptance,
admission, or cache publication; those belong to later lifecycle ports.

An application specification may include an optional provider map:

```json
{
  "models": {
    "codex": "MODEL_FOR_CODEX",
    "claude": "MODEL_FOR_CLAUDE",
    "cursor-agent": "MODEL_FOR_CURSOR",
    "opencode": "provider/MODEL_FOR_OPENCODE"
  }
}
```

A Flavor may instead include one content-pinned `authoring_inputs` reference with kind
`model-selection`. Its JSON document uses schema
`literate-ai/coding-model-selection@1` and the same `models` map. A selected Flavor's
entry for the active CLI overrides the application entry; conflicting selected Flavor
entries fail closed. A specification-to-source skill may declare the same provider map
as `models` in its typed manifest. Because one forward generation request invokes the
selected skill set as one bounded coding-agent call, explicit selected-skill values must
agree. The resolved value is passed through the active command's model option.

For `plan`, `build`, `test`, `generate`, and `rebuild`, `--model MODEL` establishes the
pipeline scope. Resolution then enters the current Component, each selected Flavor role,
and the selected skill invocation in that order. An omitted inner value inherits its
parent; an explicit inner value overrides it; equally specific conflicting values fail
before model egress. Every binding is immutable and parent-linked, so completing a nested
Component or skill task automatically restores the enclosing model for the next sibling.
Omitting all values retains the coding CLI's configured default and sends no model flag.
`run` accepts no `--model` because it executes an already-retained artifact.

Each coding-CLI session also keeps an explicit model stack in `contextvars`, so every
thread and agent has its own stack. Frame 0 is the session model. Nested Component,
Flavor, skill, and in-spec sectional changes push when the model changes and pop when
leaving that scope; generation uses the top. Push, pop, and depth are part of the
specification language. A Component may author:

```markdown
<!-- literate-ai:model-stack op="push" model="openai/o3" section="algorithm" -->
...
<!-- literate-ai:model-stack op="pop" section="algorithm" -->
```

The spec-to-code converter uses the pushed model until the matching pop. Querying depth
from that language reads the current stack. The stack never reaches depth 0: popping the
only remaining frame is refused, a warning is emitted, and the log records the precise
caller (file, line, function) plus the specification location of the pop. See
[ADR 0018](../decisions/0018-never-empty-per-agent-model-stack.md).

Command-worker requests carry both the caller's pipeline selector and the exact content
identity of the complete resolved model-scope set. That binding participates in dispatch
authority; a remote worker may not silently reinterpret or replace it.

The invocation flags follow the official [Codex CLI reference](https://developers.openai.com/codex/cli/reference/),
[Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage),
[Cursor Agent parameters](https://docs.cursor.com/en/cli/reference/parameters), and
[OpenCode CLI reference](https://opencode.ai/docs/cli/). OpenCode runs through
`opencode --pure run` in a detached temporary workspace. A LitAI-owned configuration
disables project instructions, plugins, sharing, updates, and automatic LSP downloads;
its deny-by-default permission map enables only read/edit/list/glob/grep. That provider
policy is not an operating-system sandbox, so LitAI admits and materializes only the
validated `source/` byte map into the candidate root.

Source-cache resolution is a manifest-configured framework service consumed by a
project lifecycle driver; it is not a separate mutable-cache CLI. Publication,
settings, and security-policy operations likewise remain typed integration services
unless a project's content-pinned driver exposes them as explicit rebuild extensions.

## Settings contracts

The settings registry supports typed sections, schema versions, migrations, validation,
and layered scopes. Integrators may merge defaults, repository, workspace, user,
machine, and run settings while preserving per-value provenance. Secrets should be
references to an external secret provider, never plaintext values smuggled into a
Component definition.

Settings are operational state and do not perturb semantic Component identity. A product
such as OVA renders the same typed registry in its own UI and should keep separate
sections for models, cache, targets/Flavors, security, and publication.

## Environment and paths

The framework core does not prescribe one user cache directory or credential layout.
Products select platform-appropriate locations through settings and adapters. Do not put
machine-local cache paths, tokens, or mutable aliases in `component.md` or its generated
lock.

`LITERATE_AI_SCHEMA_CATALOG_ROOT` is the explicit runtime location for an embedded
schema catalog. It names one absolute, non-symlink directory containing the complete
`v1/` and `v2/` catalog directories and their regular `index.json` files. When set, it
is authoritative: an absent, incomplete, symlinked, malformed, or internally
inconsistent catalog fails closed instead of falling back to another checkout or wheel.
Keep the variable unset for an ordinary source checkout or wheel installation; Literate
AI then resolves the complete repository catalog beside an editable import before the
wheel data directory. Embedding products should copy the versioned catalog as one exact
snapshot and set this variable before invoking the API or `litai` CLI. The path is
operational configuration and does not become Component authority.

## Contributor commands

```console
make help
make validate
make release-check
```

See [Installation](installation.md) for Python and contributor-tool requirements.

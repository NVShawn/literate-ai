# Literate AI — Project Requirements and Completeness

This document is the durable, root-level source of truth for what Literate AI is for
and how complete its implementation is against that purpose. It sits above every file
in `docs/`: a document under `docs/` may detail or justify a decision, but the goals and
completeness state recorded here are authoritative. `docs/roadmap/active-work.md` is the
tactical execution queue beneath this document — every item there should trace back to a
goal recorded here, and a new goal recorded here should either extend that queue or, when
its rationale and acceptance contract cannot stay readable as one queue item, spawn a
detailed plan under `docs/roadmap/` per `skills/agent/record-user-directed-work/SKILL.md`.

Before authoring or changing a Component, Flavor, or skill, read this document first.
If this project declares a blocking dependency below that is not yet satisfied, stop and
say so explicitly (for example: "I need `<repository>` to have its own `PROJECT.md`
goals finalized before I can start this work") rather than proceeding around it.

## Goals

Literate AI is a provider-neutral release-engineering and SDLC harness for
specification-led software: a Component's Markdown specification is the durable
authority for its observable behavior, and every coding-CLI-generated source tree is
disposable output. The framework's own goals are:

1. Template and convert repositories into an evidence-gated SDLC: `litai onboard create`
   plans and scaffolds a new tree with workflow, receipts, and release protocol;
   `litai onboard adopt` wraps an existing tree in the same harness; `verify`,
   `lock`, `plan`, and `rebuild` prove when that tree is valid, and `release`
   cuts a versioned line. Framework and derived-project releases must follow
   Semantic Versioning: each published release has an immutable version tag on
   its `release/<major>.<minor>.x` branch, selecting the exact qualified commit.
   A package version or copied source snapshot alone is not a published release.
2. Let a human-readable Component/Flavor/skill/workflow/routing specification fully
   determine generated application behavior, independent of which coding CLI or model
   performs the generation. This arrives in waves as models improve; it is not the
   current differentiator.
3. Keep generation correct across the full range of supported coding CLIs and model
   quality tiers — see "Generate correctly across every model, not just the strongest
   one" in `SKILL.md`. A defect traced to underspecified authority is fixed in that
   authority, not tolerated as model variance.
4. Give every generated artifact an unbroken, independently verifiable evidence chain
   from specification through lock, generation, build, and acceptance.
5. Support a growing set of first-class implementation-language Flavors
   (currently C++, Go, JavaScript, Python, Rust, Swift, TypeScript, Zig) and
   build-system Flavors (currently Bazel, Make) with the same generation,
   lock, build, and acceptance rigor for each.
6. Let a project inherit and extend another project's Components, Flavors, and skills
   through the repository lineage/DAG mechanism, so framework and derived-project
   authority stay composable rather than duplicated.

## Dependencies

None. Literate AI is the root framework; other repositories depend on its released
authority, not the reverse. A project that adopts Literate AI records its own
`PROJECT.md` with a blocking dependency on this repository's release state when its own
goals require a specific unreleased Literate AI capability.

## Completeness

Authored, inheritable framework Component/Flavor inventory reviewed during `1.1.0`
development (2026-09-10):

| Category | Current count | Notes |
| --- | --- | --- |
| Language Flavors | 8 (`lang-cpp`, `lang-go`, `lang-javascript`, `lang-python`, `lang-rust`, `lang-swift`, `lang-typescript`, `lang-zig`) | Each pairs a language Flavor with a portable-application skill and a native/tree build strategy. Bare selectors (`+cpp`) still resolve. |
| Build-system Flavors | 5 (`build-bazel`, `build-cargo`, `build-cmake`, `build-make`, `build-repo-man`) | Goal 5 treats Bazel and Make as the first-class pair; Cargo, CMake, and repo-man remain cataloged. |
| OS Flavors | 3 (`os-linux`, `os-macos`, `os-windows`) | |
| Packaging Flavors | 9 (`package-apt`, `package-brew`, `package-cargo`, `package-chocolatey`, `package-conan`, `package-npm`, `package-pip`, `package-winget`, `package-zip`) | Canonical axis-prefixed names; do not keep flat alias directories beside them. |
| Toolchain Flavors | 4 (`toolchain-zig-cc`, `toolchain-swift-apple`, `toolchain-swift-linux`, `toolchain-swift-windows`) | |
| UI / deploy / accel / doc | `ui-react`; `deploy-docker`; `accel-nvidia-cuda`, `accelerator-cpu`; `doc-google-workspace`, `doc-microsoft-365` | Cataloged, not all first-class in Goal 5. |
| Framework Components | `backend-application`, `document-pair`, `frontend-application`, `invoice-service`, `literate-ai-overview`, `mcp-application`, `money-calculation` | Reusable, inheritable authority under `components/`. |
| Samples | Non-inheritable demos under `samples/`, except `hello-component` (the inheritable starter `litai init` seeds). | See the open taxonomy question in `docs/roadmap/active-work.md`. |

## Current work

[Accepted ADR 0048](docs/decisions/0048-retained-project-standard-lifecycle.md)
([issue #22](https://github.com/jordanhubbard/literate-ai/issues/22)) records a
retained-project Standard lifecycle admission route under Goals 1, 4 and 6.
The directing human accepted this design on 2026-10-02. Implementation and full
qualification remain open under RETAINED-PROJECT-001; conversion and release-authority
gates remain unchanged.

PACKAGE-ZIP-001 ([issue #17](https://github.com/jordanhubbard/literate-ai/issues/17))
extends Goals 1, 4 and 6 with selectable deterministic ZIP packaging through the
standard package lifecycle. Exact accepted custody, both SBOMs, specifications,
target identities and release authorization remain mandatory. Qualification is pending.

RELEASE-INVARIANTS-001 is a release-blocking repair under Goals 1, 4 and 6:
enforce SemVer release identity and release-branch/tag ownership for the framework
and derived projects, repair the public release baseline, and verify bare
`litai init` from a fresh public installation. Publication remains pending;
no unqualified snapshot may be tagged merely to unblock onboarding.

REFRESH-ABSORBED-001 implements and qualifies attached refresh of absorbed primary
worktrees under Goals 1 and 4, preserving external shared-branch custody. Focused
worktree, application and version-authority tests plus installed-wheel smoke
pass; draft PR #504 retains the separate landing and baseline native-gate limits.

The next release is the 1.2 minor line. RELEASE-INTEGRATION-002 owns the complete
contribution and roadmap reconciliation, truthful completion or explicit deferral of
each remaining obligation, automatic safe parallel execution of ready Component DAG
nodes across compatible private workers, provider-neutral local/LAN cache reuse for
test, Bazel/sccache build, package, and container artifacts, and the exact-main
release branch through published verification. Existing 1.1 entries remain historical
evidence, not proof that their unchecked acceptance or stale release targets are done.

NVIDIA-LIBS-001 extends Goals 2, 4, 5, and 6 with capability-first discovery,
installation, and integration of NVIDIA accelerated libraries across supported
languages. The [initial assessment](docs/architecture/nvidia-library-discovery.md)
records existing CUDA overlap and admission criteria. Full library inventory and
CUDA Python execution qualification remain open; discovery is not support evidence.

Multi-output catalog skill authoring now has public initialized-project validation
proof: backend/frontend trees are accepted and unsafe paths are rejected
(SKILL-ARCH-001). Sample generation failures now expose the lifecycle's bounded,
redacted provider diagnostic; the focused regression also proves transient output
does not change the source derivation key (SAMPLE-DIAG-001). The full release
qualification remains in progress. The hosted macOS retained-Cargo size refusal
now has a bounded streaming native-custody repair; its exact-source/native and
hosted qualification remains pending (RETAINED-LIBRARY-BRIDGE-001). Worker capacity/overload detection and bounded
cleanup investigation from issue #440 are tracked as WORKER-HEALTH-001. The maintainer
accepted ADR 0043 for 1.1 on 2026-09-16 UTC; implementation and qualification are
authorized, while live cleanup still requires exact-target authorization. The first
capacity slice adds versioned private inputs and deterministic volume/quota/inode
admission with freshness and worker/job binding. Bounded local storage collection
now measures actual role directories and retains private paths outside observations.
SSH collection and worker-side watchdogs also pass native Linux/macOS/Windows probes.
Explicit command health receivers now keep lifecycle commands and credentials separate.
Independent byte/inode quota domains now include read-only Linux ext user/group
queries and conservative soft limits. Native disabled-quota applicability passes;
active quota exhaustion, additional Unix filesystems, pressure sampling, dispatch
integration and cleanup qualification remain open. Public `litai worker health`
storage inspection now reports role deficits and explicit unknowns from private
configuration; an explicitly selected bounded alert history reports transitions
and recovery without repeating unchanged findings. Neither authorizes dispatch
or cleanup.

Historical CPP-LIBRARY-001 qualification record: generated C++ static/shared products and exact
Bazel/Conan consumption in accepted ADR 0042. The current implementation establishes
the product and generation foundation, and CONAN-VERIFY-001 (#429) verifies exact
restored payload bytes and revisions. Local macOS and disposable Linux ARM64
static/shared Conan packages now carry exact native metadata, verify restored payload
bytes and feed real Bazel consumers from second fresh caches;
real Bazel measurements now prove zero-action no-op/unrelated edits, three-action
public-header invalidation and incremental restoration, four-action implementation
invalidation and byte-identical seven-action restored-clean products for both kinds.
Fresh-cache Conan resolution also refuses valid alternate target,
build-type, compiler-version and C++ ABI profiles for both kinds, and exact consumer
graph verification rejects a newer same-coordinate recipe revision. Hosted Linux
x86_64 and Windows consumer evidence remains. A Windows hosted clean rebuild exposed
wall-clock timestamps in MSVC static archives; head `57366069` now canonicalizes only
those archive fields while retaining whole-product byte identity, with the affected
module and static/shared consumers green locally. Merging current `main` added its
reviewed whole-project rebuild path to the lifecycle-driver closure; exact-head hosted
run `35071558730` correctly rejected the old closure identity. The merged closure is
now reviewed and pinned as
`sha256:15541885508ed9f922d25068beb06a63300909d7456fe14a797e4461a189cf61`.
Exact-head hosted run `35076380517` completed with 13 successful jobs; Windows
shard two passed 758 tests before exposing two real-product failures, and its sibling
shard was cancelled after that failure. Both failures occurred while the COFF
canonicalizer rewrote the staged `.lib`; diagnostic run `35087913592` proved parsing
completed and Windows rejected reopening the file for output. Run `35088676409`
proved that explicitly closing and verifying the input stream was necessary but not
sufficient: staging had already copied the Bazel action input's permissions onto the
destination before the rewrite. The stager now grants owner-write permission only
for canonicalization and restores the exact source permissions afterward. Its
executable regression uses a read-only source archive, and the real static/shared
Conan-to-Bazel consumer passes locally with all ten subtests. Exact-head run
`35090510043` then completed both Windows products, emitted their full incremental
evidence and reached consumer cleanup; only the fixture's raw `shutil.rmtree` could
not unlink the intentionally read-only Bazel exports. That fixture now uses the
existing bounded Bazel cleanup path. Replacement run `35091851181` passes the
dedicated native-library jobs on both Linux x86_64 and Windows, including exact-tool
preflight, all 11 required JUnit outcomes, the anti-skip verifier and retained
evidence upload. The broader matrix and landing remain required. A separate format hardening accepts the specification-permitted
omission of padding after an odd-sized final member while retaining strict
nonterminal padding validation. The same baseline shard showed that a ten-second
runtime-archive import deadline was below observed Windows startup cost; the finite
deadline is now 60 seconds.
The general hosted matrix does not install Conan beside its test interpreter, so it
cannot by itself prove the real Bazel/Conan consumer path: that test reports a skip
when either native tool is absent. This branch therefore adds a dedicated Linux
x86_64 and Windows native-library job pinned to Bazel 9.2.0 and Conan 2.32.0; it
rejects any skipped, incomplete or failed qualification before retaining its JUnit
evidence. Its first execution in run `35086387730` completed the real Linux path
with one parent test and ten subtests in 230.85 seconds, while the anti-skip check
correctly rejected Windows because the setup-python script location did not satisfy
the test's interpreter-local Conan boundary. The follow-up uses one isolated venv on
each platform and requires all 11 JUnit outcomes. Intermediate run `35087136171`
was rejected before scheduling jobs, so the workflow now exposes each venv through
`GITHUB_PATH` and retains fixed supported shell declarations. Run `35087346634`
therefore reached the real Windows products and reproduced the in-place file-sharing
failure before the diagnostic run isolated it. Replacement exact-head qualification
remains required. The isolated temporary-prefix installed-framework gate passes on
the current implementation.
Current CPP-LIBRARY-001 status: issue #427 implements accepted ADR 0042 with generated static/shared C++
products, exact Bazel/Conan consumption, public-header and ABI authority, independent
source-free consumers, incremental-build evidence and substitution rejection. The
built-in C++ Flavor now selects static libraries by default, matching the initial
downstream adoption target; a reviewed replacement profile selects shared production.
Local macOS and disposable Linux ARM64 qualification passes for both kinds. Exact-head
hosted run
[`35092994515`](https://github.com/NVIDIA-dev/literate-ai/actions/runs/35092994515)
at `558861b5` passed all 17 Linux, macOS and Windows jobs, including dedicated Bazel
9.2.0/Conan 2.32.0 native consumers on Linux x86_64 and Windows with all 11 required
outcomes and no skips. The built-in-profile enablement changes flavor authority and
therefore still requires a new exact-head hosted run before landing.

The current development release is **1.1.0**. The user-directed
[SDLC audit delivery program](docs/roadmap/1.1-sdlc-audit.md) grades architecture,
onboarding, software intelligence and research potential, and orders all audit
findings before release preparation. Standardized Codex/Claude plugin artifacts are
part of this release's engineering cycle (PLUGIN-001). Current readiness remains
partial until the program's implementation and release evidence are complete.
HTML observability now passes the real installed-wheel parent-update journey:
declared artifacts become stale after changed inherited authority, regenerate with
current provenance, and open as one file in desktop/mobile browsers. The compact
agent wrapper and read-only verification gate are implemented under OBSERVE-HTML-001.
Hosted integration, broader observability phases and release qualification remain open.
Git-submodule orchestration now exposes read-only plan/check over exact child pins
and explicit dependency declarations. Its typed modern-manifest binding now separates
persistent child pins/relationships from local checkout observations and rejects
overlapping root catalog/output ownership. Reviewed root-only initialization,
declared-pin graph projection and public root lock/plan/verification now pass full
local and installed-wheel qualification. Corrected candidate `33630a4f` passed all
15 hosted checks and landed through PR #386 as `cf71a4fc`, including #387's history.
Reviewed refresh remains open under SUBMODULE-ORCH-001: its internal commit-only
authority-delta layer passes 87 focused compatibility tests. Root/child custody,
publication checks and the acknowledged filesystem transaction are distinct stages.
Read-only custody now captures exact root/index/lock and clean child observations.
An internal publication verifier proves advertised-remote ancestry in disposable
bare storage, including older commits and tags. Internal refresh preparation now
binds ordered proofs to root-declared URLs and exact requested commits, resolves
relative URLs through Git in disposable storage, and rechecks input custody.
An internal acknowledged reservation layer now binds exact local custody to
exclusive root repository-lock and root/child Git index, HEAD and resolved-ref
writer markers. Live descriptor/node/token ownership permits reobservation;
cleanup preserves foreign or modified markers, and crashed markers require
explicit recovery. The combined reservation path now also owns the existing
manifest and lifecycle advisory locks across observed roots, preserving retained
lock bytes and admitting only exact owned untracked markers during cleanliness
checks. A pre-apply context now renews every reviewed publication proof while
holding these reservations, binds acknowledgement to local and remote evidence,
and rejects stale ownership or input drift. Prospective writes and rollback remain
the next transaction stage; remote observations do not establish child acceptance.
The publication transport can now retain bounded raw commit/tree/blob records from
its verified scratch store, recomputing Git hashes without checkout or filters.
Immutable tree deltas describe file, mode, link and Gitlink changes. The live owner
captures the complete reviewed selection with aggregate entry, blob and metadata
bounds and proof equality checks. Internal physical preflight now retains actual
before-file bytes (including CRLF), modes and node identities, inventories affected
directories and refuses ignored-file collisions, unsafe paths and reserved metadata.
Old object trees bind to observed child HEAD, not an earlier root pin; aggregate
capture limits and revalidation remain bound to live ownership. Exclusive internal
staging now retains rollback inputs, inert prospective blobs and prepared root/child
index bytes in root Git metadata. It refuses retained staging and preserves changed
contents during cleanup. Complete history packs can now be exported from fresh
publication storage, checked in an independent empty object database and installed
additively under live ownership and Git keep markers. Existing object files are
never overwritten; immutable cache additions may remain after failure. Reference
custody now binds every symbolic hop and loose/packed backing file, including
absent loose refs and per-worktree storage, with corresponding writer reservations.
Optional internal metadata staging derives exact ref and manifest transitions,
preserves no-op bytes and records repository-lock invalidation without creating
new acceptance. Conflicting shared refs across observed roots refuse. Bounded
registered-worktree custody now participates in revalidation; metadata preparation
refuses shared-branch effects outside the observed checkouts and active or
unavailable registrations. It preserves unrelated idle worktrees without inspecting
their source or repairing their metadata. Metadata-parent custody now binds existing
directory identities and missing parents; only proved live reservation additions
normalize to prior absence. Internal metadata staging retains bounded original logs
and prospective append records under the repository logging policy, including all
symbolic hops and shared/per-worktree paths. One root committer identity and timestamp
keeps shared-log updates consistent. Changed parent nodes invalidate ownership, and
cleanup retains substituted directories. A live-only transaction authority now admits
only complete object, file, index and metadata staging. It journals applying before
mutation, installs packs additively, commits the manifest last, and validates exact
prospective state or conservative rollback before cleanup. Foreign concurrent changes
retain recovery staging; object packs are never rollback targets. The public reviewed
refresh plan/check/apply front door now binds canonical intent, exact custody and fresh
root-declared publication proof before an acknowledged live apply. It reports exact
no-op without claiming child acceptance or crash replay. Installed/native
cross-platform qualification and crash-replay tooling remain open. Split-index
inspection remains an explicit unsupported mode until its Git timestamp writes
are avoided.
Children keep their independent authority and ordinary adoption behavior is unchanged.
PR #387's import-provenance repair also passes full local and installed qualification
at `46668380`, including repeated updates on the retained migrated-project fixture.
Its corrected combined hosted matrix passed at `33630a4f`; a fresh live upgrade
receipt and the wider RELEASE-017 obligations remain open. The maintainer accepted
ADRs 0040 and 0041 on 2026-09-12: retained Cargo
library consumption and the full production-containment scope now have approved
designs. Their implementation and qualification remain open.
The retained-library bridge now has optional bounded producer capture and immutable
record/package reopening, including exact root-package and project-receipt bindings.
Focused capture and lifecycle checks pass; this draft still lacks the complete
required-evidence verifier, durable publication, importing trust and transactional
Cargo/source-retirement integration. It grants no artifact-consumption authority.
The isolation policy/report schema foundation is implemented and exposed through the
public security API; trusted launch grants, authenticated evidence and actual backend
enforcement remain open under SEC-360.
Disposable conversion phases now share an explicit Git environment for identity
setup and commands, with 143 local harness/initialization/receipt regressions passing.
At `692041a1`, the fresh full suite (4,206 tests, 30 skips), wheel/isolated install,
offline E2E and exact-wheel root-command proof also pass. All 15 hosted checks
passed on that exact candidate; PR #388 landed as `ad92a655` with an identical
tree. Native nested linbuild acceptance remains open under CONVERT-NESTED-GIT-001.
Repository-lineage cache locking now has native Linux/macOS/Windows qualification
for separate-process serialization, independent entries and abrupt-owner recovery;
REPOSITORY-LINEAGE-CACHE-LOCK-001 is complete at the landed `33630a4f` candidate.
The wider framework mitigation program remains incomplete: its remaining Phase 2
follow-ups and Phases 4–5 are explicit checklist obligations. Typed domain metadata
is approved by ADR 0011 (accepted 2026-09-13); its remaining implementation and
qualification belong to MITIGATION-FW-001.
The 2026-09-16 review-coverage reconciliation maps all 17 open issues and 28 open
reviews to queue owners and remaining acceptance. It adds the missing native npm
packaging obligation, restores explicit 1.1 targets to ten omitted open items, and
preserves all 112 unfinished score-program tasks. The strict release projection
initially reported 118 open items. The real model-routing sample now selects the
nested development catalog; live public lock/plan/generate qualification against its
exact shipped workflow and routing bytes closes WORKFLOW-CATALOG-001, leaving 117.
Roadmapped review coverage and source-generation proof do not imply release readiness.
Draft PR #441 carries these slices and the unified CI fail-fast matrix. A controlled
hosted failure cancelled all fourteen sibling checks; the intentional probe is removed
and clean final-head qualification is pending. The review inventory now includes #441.
See the [current review coverage](docs/roadmap/1.1-sdlc-audit.md#review-process-coverage-reconciliation).

The maintainer's full integration directive additionally assigns every unresolved
active-queue item to 1.1.0 (RELEASE-INTEGRATION-001), including historical unfinished
acceptance. Consolidate the current work and outstanding branches/reviews into main,
complete the work, then annotate and close resolved tracker items and stale branches.
Preserve published tags and their history; an old release obligation is not a request
to republish that old release or downgrade current authority.
The integration sweep now recognizes clean, already-merged topic worktrees from
another checkout using exact revision ancestry. It preserves dirty/prunable
blockers and branch-retention policy, and rejects branch/reference drift.
The post-#388 sweep has no open reviews or unclassified contributions; seven
included issues still block release readiness. The refresh/custody and contribution-
sweep changes are consolidated on a descriptive named integration branch with fresh
full-suite, wheel/install and offline E2E evidence. The integration record belongs to
RELEASE-INTEGRATION-001; live refresh and broader 1.1 qualification remain open.
PR #389's first hosted matrix exposed a Windows directory-custody revalidation
failure. Fresh no-follow member metadata replaces cached enumeration records;
disposable publication receivers also disable post-push automatic maintenance
after a macOS snapshot race. The corrected candidate requires renewed full
hosted qualification: clean `9b62593e` now passes the fresh full local suite
(4,481 tests, 30 skips), wheel, isolated install and offline installed-CLI E2E.
Subsequent Windows qualification confirms the repaired custody and lock fixtures,
but exposes case-folded reservation deduplication and local-URL fixture spelling
assumptions. Exact spelling now precedes reservation alias validation; endpoint
tests retain Git's returned bytes and exercise real local publication. Fresh full
local, installed and hosted qualification is required for this further repair.
Future implementation starts from current main on a named topic branch, preserving
qualified worktrees as evidence/recovery snapshots.

### Historical development context

See `docs/roadmap/active-work.md` for the live, evidence-gated queue.

[PUBLIC-EXPORT-001](docs/roadmap/active-work.md#public-export-001-make-the-repository-safe-and-useful-for-public-export)
removes organization-private references, derives release identity from public-export
configuration, and requires a clean-snapshot publication boundary rather than exposing
the private Git object database.

0.11.0 host-install self-update is closed:
[HOST-SELF-UPDATE-001](docs/roadmap/active-work.md#x-host-self-update-001-prefix-installed-litai-self-updates-from-github-releases)
([ADR 0037](docs/decisions/0037-prefix-installed-cli-self-update.md)). Remaining
0.11.0/1.0 operator-surface work is on the queue, including
[ADOPT-CLI-001](docs/roadmap/active-work.md#adopt-cli-001-make-litai-adopt-the-primary-brownfield-entry-point)
and
[APP-SETTINGS-001](docs/roadmap/active-work.md#app-settings-001-define-canonical-per-user-application-settings).

The intended 0.10 headline remains:
[ADOPTION-001](docs/roadmap/active-work.md#x-adoption-001-make-operator-adoption-the-post-090-product-headline)
makes operator adoption the current numbered line — `litai status`/`doctor`,
`litai onboard` create/adopt, a tested first hour from a published wheel, and
explicit convert stages (`wrapped` → `retained` → `drafted` → `qualified`).
Ordered design is in
[the operator adoption program](docs/roadmap/operator-adoption.md).
[QUEUE-HYGIENE-001](docs/roadmap/active-work.md#x-queue-hygiene-001-archive-completed-queue-items-and-reconcile-the-open-tracker)
is closed. `v0.10.0` is tagged, but its premature release-line cut omitted the final
three approved operator-adoption commits; RELEASE-CLOSURE-001 tracks an immutable-tag,
versioned recovery rather than rewriting published history. The 0.10.1 cache and
GitHub-origin fixes have now been reconciled with their exact release-line hosted
checks and terminal issue evidence: UPDATE-ORIGIN-001 / ORIGIN-SSH-HTTPS-001 and
COMMITTED-CACHE-EMPTY-001 / CACHE-EMPTY-RO-001 no longer describe shipped fixes as
future implementation. STANDARD-CACHE-PUBLICATION-001 and
RETRY-DERIVATION-BINDING-001 now also have verified historical hosted closure:
their release-line implementations and tests match the originals, and the exact
0.10.1 candidate and published commit have complete green platform matrices.
CACHE-003 and CACHE-004 also have complete repository-quality evidence for
single-writer cache composition and public deliberate cache promotion on merged
`ad92a655`. DOC-DEPENDENCIES-001 also has complete local and hosted integration
proof with the qualified documentation dependency tree unchanged. Library import
fixtures do not replace the still-needed JavaScript independent function-oracle
proof under ACCEPTANCE-LIBRARY-001. Fresh 1.1 live receipts and consolidated
qualification remain open.
The previously qualified workspace-link boundary and inherited release-skill
reference closure are also reconciled with their exact hosted and terminal issue
evidence (CONVERSION-WORKSPACE-LINKS-001 and RELEASE-SKILL-CLOSURE-001). Their
historical completion does not close the broader mitigation or native build programs.

The 0.10.0 release P0 [RELEASE-CLOSURE-001](docs/roadmap/active-work.md) makes
collateral publication and the issue/review/branch/worktree disposition loop executable
under [ADR 0034](docs/decisions/0034-continuous-evidence-bound-release-closure.md).
[UPSTREAM-ISSUE-FIRST-001](docs/roadmap/active-work.md),
[STANDARD-REBIND-001](docs/roadmap/active-work.md), and
[PORTABLE-MAKE-QUOTING-001](docs/roadmap/active-work.md) address issues #308, #309,
and #310 found in the first live 0.10.0 sweep. [APP-SETTINGS-001](docs/roadmap/active-work.md)
is explicitly deferred to 1.0.0.
Prior 0.9.0 harness P0: [AUTHORITY-FACTORING-001](docs/roadmap/active-work.md) makes skill
taxonomy, deterministic dependency/gate boundaries, native host paths, and Python
connector operations single executable authorities for 0.9.0; its ordered design is
in [the executable-authority roadmap](docs/roadmap/0.9.0-executable-authority.md).
[USER-CONFIG-MIGRATION-001](docs/roadmap/active-work.md) first moves ignored 0.8.x
worker/test/MCP configuration and mutable observations/events from project or legacy
paths into centrally resolved user config/state custody under ADR 0031.
[HOST-INSTALL-SBOM-001](docs/roadmap/active-work.md) makes the remaining native CLI
prerequisites and APT/Homebrew/WinGet commands inspectable by OS/architecture/
accelerator tuple under ADR 0032, with explicit consent before host mutation.
[REPOSITORY-CHECKOUT-001](docs/roadmap/active-work.md) pairs Git with Git LFS on every
supported worker OS and makes shallow exact-revision checkout content-complete across
recursive submodules before build or test execution.
Current harness work: [LOCK-004](docs/roadmap/active-work.md) `make samples` (FLAVOR-005
live generation) after `make wheel-check` passed at `09ce62de`.
[RELEASE-015](docs/roadmap/active-work.md) RelEng nested skills landed.
[INIT-003](docs/roadmap/active-work.md), [CLI-002](docs/roadmap/active-work.md),
[HARNESS-001](docs/roadmap/active-work.md), and
[CLI-DEBUG-001](docs/roadmap/active-work.md) landed on `feat/cli-sdlc-debug`.
Notable open threads at the time of writing:
- Reconciling the `samples/` vs `components/` taxonomy so only genuinely non-inheritable
  demo applications remain under `samples/`.
- Extending coding-CLI support (`opencode`, IDE-vendor CLIs such as GitHub Copilot CLI)
  following the existing `codex`/`claude`/`cursor-agent` pattern.
- Restoring opt-in externally provisioned source intelligence under
  [ADR 0019](docs/decisions/0019-opt-in-external-source-intelligence.md) while
  preserving `provider_id: none` as the dependency-free default established by
  ADR 0016.
- Migrating the existing suite to public-API-first contract and workflow tests under
  [TEST-STRATEGY-001](docs/roadmap/active-work.md), retaining isolated unit coverage
  only for distinct complex or safety-critical logic. `litai design` is the current
  migrated slice.
- Flavor catalog naming: 0.7.2 (FLAVOR-008 / [#186](https://github.com/NVIDIA-dev/literate-ai/issues/186))
  fails closed on duplicate `package-*` / `doc-*` alias directories. FLAVOR-005
  renamed remaining language/OS/build/toolchain directories to axis-qualified
  names (`lang-python`, `os-linux`, `build-make`, `toolchain-swift-apple`). ADR 0010
  dotted selectors fail closed; bare aliases remain.
- WebMCP, the JS/React front-end parent, the Flavor-indexed back-end parent, and
  peer samples `backend-base` / `frontend-base` / `webmcp-page` are cataloged
  ([WEBMCP-001](docs/roadmap/active-work.md) / [#198](https://github.com/NVIDIA-dev/literate-ai/issues/198)
  through [SAMPLE-APP-STACK-001](docs/roadmap/active-work.md) /
  [#201](https://github.com/NVIDIA-dev/literate-ai/issues/201)). Live
  `make samples` for the new peers still needs model-egress acknowledgement and
  SAMPLE-PORTFOLIO-002 harness oracles. FLAVOR-007 generate/build/accept and an
  npm-capable JS axis remain open.
- Frontend browser verification is being strengthened under
  [FRONTEND-002](docs/roadmap/active-work.md): generated pages must expose stable
  browser-observable structure, and authorized desktop/mobile inspection complements
  hash-only render acceptance.
- There is no framework rule against IDEs or complete applications. IDE-001 /
  [#184](https://github.com/NVIDIA-dev/literate-ai/issues/184) withdrew that
  prohibition. Derived projects may ship both. This repository's samples are
  already complete applications; an in-tree IDE sample would be ordinary catalog
  work. SKILL-PROFILE-001 / [#185](https://github.com/NVIDIA-dev/literate-ai/issues/185)
  remains an optional import evaluate (needs an in-tree consumer), not an IDE ban.
- Tracker access is forge-neutral: `litai project tracker inspect` reads Git remotes
  and names `gh` or `glab` ([TRACKER-HOST-001](docs/roadmap/active-work.md) /
  [#188](https://github.com/NVIDIA-dev/literate-ai/issues/188)). Cycle start and
  end survey other developers' green PRs and leftover worktrees via
  `litai project peer-work` ([RELEASE-015](docs/roadmap/active-work.md)). Parent contribution
  checkouts live under `parents/<id>/` in the current project, with submodules and
  LFS ([PARENT-SUBTREE-001](docs/roadmap/active-work.md) /
  [#189](https://github.com/NVIDIA-dev/literate-ai/issues/189)).
- CI shard and impact planning is `litai project ci-plan` plus
  `skills/agent/ci-test-plan` ([CI-SHARD-001](docs/roadmap/active-work.md),
  [CI-IMPACT-001](docs/roadmap/active-work.md) / issues
  [#83](https://github.com/NVIDIA-dev/literate-ai/issues/83)–[#86](https://github.com/NVIDIA-dev/literate-ai/issues/86)).
- Operator MCP opt-in, Jira-when-available mutagenic comments, project MCP
  hygiene, Slack/Outlook institutional channels, and the shared author/recipient
  envelope landed in 0.8.0 Unreleased
  ([USER-MCP-001](docs/roadmap/active-work.md) /
  [#192](https://github.com/NVIDIA-dev/literate-ai/issues/192) through
  [OUTLOOK-MAIL-001](docs/roadmap/active-work.md) /
  [#196](https://github.com/NVIDIA-dev/literate-ai/issues/196);
  Jira consumer [#191](https://github.com/NVIDIA-dev/literate-ai/issues/191);
  CLI client [MCP-CLI-001](docs/roadmap/active-work.md) /
  [#202](https://github.com/NVIDIA-dev/literate-ai/issues/202)).
  Python owns the path, schema, TTY fallback, journal, envelope, and operator-catalog
  MCP fan-out (`--discover-mcps` off by default). Agents author the catalog and
  associate Jira tickets; they do not re-post envelopes `litai` already sent.
  This repository does not publish organization-local channel bindings; downstream
  projects may configure their own `institutional_channels` entries.
- TEST-CACHE-001 is 0.8.0 P0 pin/dispatch; do not land the 2026-08-15 audit-hook WIP
  (`c0439340`).
- **0.8.0 break-glass (2026-08-28, not durable SemVer advice):** no major has shipped;
  this cut is numbered `0.8.0` but every remaining open queue item on `main` is
  in-scope, including breaking changes. See the banner in
  `docs/roadmap/active-work.md`. 0.7.3 stays a patch on `release/0.7.x`.
- CONVERT-FW-009 / [#187](https://github.com/NVIDIA-dev/literate-ai/issues/187) is
  complete: convert omits host-local virtualenvs and `node_modules`.
- SPEC-HIERARCHY-SAMPLES-001 / [#119](https://github.com/NVIDIA-dev/literate-ai/issues/119)
  is complete: Loan Risk Gate (DMN + pinned `assets:`) and Playback Controller
  (SCXML) are portfolio samples. Live generation still needs explicit model-egress
  acknowledgement.
- `v0.7.2` is published; `main` is 0.8.0 Unreleased. 0.7.0 GitHub is green at
  `afe52d09`; local fan-out waits on landing Accepted
  [ADR 0017](docs/decisions/0017-explicit-live-test-coding-cli.md)
  ([GENERATION-011](docs/roadmap/active-work.md)) and Accepted
  [ADR 0018](docs/decisions/0018-never-empty-per-agent-model-stack.md)
  ([GENERATION-010](docs/roadmap/active-work.md)): live tests pin an explicit
  user-configured CLI and model, remote workers require OpenCode plus
  `OPENAI_API_KEY`, a command-line override remains for one-shot runs, and the
  converter keeps a per-agent model stack that never reaches depth 0.
- Previously planned for `0.3.1` and now in the 0.8.0 break-glass scope:
  composing a Flavor into an existing project as a mixin (`FLAVOR-006`), shipping
  literate-ai as a Claude/Codex plugin (`PLUGIN-001`), a Homebrew formula
  (`PACKAGE-002`), and a `lang-javascript-react` Flavor (`FLAVOR-007`).
- Named, nestable workflow catalogs now use the filesystem as the scoping mechanism:
  `workflows/production/workflow.md` is the outer generation workflow, with
  `staging/` and `staging/dev/` nested under it, each paired with `routing.json` in
  the matching directory. Agent development postures nest the same way under
  `skills/agent/develop-in-production-workflow/`. Not yet implemented: composing one
  workflow *document* from another (`staging` adding stages beyond `dev` rather than
  only re-tuning existing ones) — that needs a build-time flattening step or relaxing
  the strict frontmatter field set. Nested skills already inherit by reading ancestor
  `SKILL.md` files.

## Completed work

See `CHANGELOG.md` for the released, user-facing history. Milestones directly tied to
the goals above:
- `v0.6.0`: complete removal of the host source-graph indexer (`provider_id: none`,
  every stage `off`); DMN/SCXML specification providers; Cargo lifecycle; verifier-owned
  persistent-service acceptance; Windows/macOS packaged-child process repairs; and
  current-version release recovery so an already-bumped untagged line can still be cut.
- `v0.3.0`: OpenCode capability preflight, graph-bound generation lock evidence, `litai
  init` prerequisite reporting, sample-taxonomy enforcement, catalog-inheritance
  exclusions for derived evidence, `_build`/`BUILD_DIR` made advisory rather than
  mandatory for load-bearing project self-improvement, root-Makefile skill contradiction
  fixed, transitive skill-dependency closure fixed for arbitrary-source promotion.
- Go added as a sixth first-class language Flavor with its own toolchain discovery,
  authorization-gated builder, and portable-application skill.

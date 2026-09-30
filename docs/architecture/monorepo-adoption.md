# Explicit monorepo adoption boundaries

[ADR 0039](../decisions/0039-1.1-capability-boundaries.md) governs this design.
Build markers are candidate roots, not inferred Components or proof of independent
buildability. The default conversion remains a single retained wrapper.

`litai onboard adopt PATH --root-plan SELECTION.json` reviews an explicit
`literate-ai/monorepo-adoption-selection@1` document. `components` contains unique
portable `name` values, nonoverlapping detected `root` paths and explicit `commands`.
Every command specifies `id`, `command`, project-relative `cwd`, and a source-file
`evidence` path. Commands are operator-reviewed declarations, not qualification;
planning never executes them. Duplicate stage IDs within a Component are invalid.

Files beneath a selected root belong to that Component. Every remaining captured
source file requires exactly one `shared_sources` assignment: `path` (a file or
directory prefix), `owner` (Component name), and explicit `consumers` (other Component
names, possibly empty). Assignments may not overlap one another or selected roots.
This is custody and declared usage, not an inferred dependency graph. Root-level
aggregate drivers do not automatically own their children's source. A command may
cite its own source or explicitly shared input, and may run at the repository root
or beneath its Component root. Cross-root invocation is not inferred.

The read-only result lists exact owned and shared input files per Component and
binds their raw bytes, executable bits, selection and commands into a content
identity. The source universe is the existing retained-source capture policy
(Git-visible tracked plus nonignored untracked files, or pruned filesystem fallback).
Uncaptured/ignored files are not silently admitted. A selection file inside that
universe is itself source and needs ownership. Symlink/reparse source paths and
separate Git histories are unsupported for refinement and refuse explicitly.

Selection, source, bundle and receipt reads bind an opened regular-file descriptor
to the observed named file before consuming bytes and recheck custody afterward.
On platforms that provide them, no-follow and nonblocking open flags prevent a
last-moment symlink or FIFO replacement from being followed or blocking the reader.
Source hashing reads at most the initial observed size plus one detection byte;
documents retain their existing size limits. Replacement, size/mode/time drift or
indirect ancestors refuse without publishing a successful observation. These checks
do not constitute a sandbox against a malicious same-user process. Only a disposable
directory created by the harness is canonicalized through host temporary-directory
aliases; operator-supplied input paths are not silently resolved around these checks.

Candidate discovery and selection review do not authorize movement. A refined plan
is applicable only when the operator also selects `--run-baseline`; this is explicit
consent to execute every declared Component harness in a disposable retained-source
copy before the first conversion mutation. Apply still requires acknowledgement and
the exact reviewed onboarding plan identity. The mutator revalidates the reviewed
identity, requires one passing retained receipt per Component, preserves the shared
source layout and original Git history, and publishes source-free Component
projections after the ordinary retained wrapper lift-and-shift.

Acceptance requires a real multi-root first-change journey with independent
Components, original commands, distinct receipt invalidation and rollback. Planner
unit tests alone cannot close #365 or qualify a 1.1 release.

## Staging and per-Component retained qualification

The internal staging adapter materializes source-free Component projections into a
new external bundle only after acknowledgement and exact plan revalidation. It does
not move source, alter the source repository, or initialize a project. Projections
start as `staged`, not at a conversion authority stage, and declare no generated
product entrypoint. The completion manifest is published last; an interrupted or
partially written bundle is not usable.
Colliding destinations refuse. Failed staging removes only files still owned by that
attempt, preserving concurrently replaced files.

Each Component has its own admitted command inventory and retained receipt policy.
Its current revision binds the Component projection, command/input declaration and
raw bytes/executable bits of owned files plus declared shared inputs. It does not
bind other Components' private source bytes or mutable receipts. Added/deleted files
in the applicable owned/shared prefixes require scope refresh; unrelated additions
do not invalidate this Component. Shared ownership and usage remain explicit.

An explicitly acknowledged local run copies only those admitted files into a fresh
disposable tree while preserving repository-relative paths. The existing strict
retained harness executes the exact commands and requires positive, all-passing test
counts with no source mutation. Evidence is component-scoped retained execution,
not native generation, independent semantic acceptance, conversion parity or a
project-wide release receipt. Source/descriptor drift during execution prevents
publication. These are same-user local execution guarantees, not host containment.

Component evidence and its typed receipt are retained together in one new external
file. Rechecking binds exact evidence, runner/policy and the current Component
revision; a copied receipt cannot be relabeled for another Component. The supported
API is the evidence-producing trust boundary, as for existing retained receipts;
content digests are not signatures or proof against a hostile operator.

The internal bundle-wide check requires the caller's retained bundle identity and
exactly one receipt for every selected Component. It reopens the complete manifest,
reviewed plan and planned projections, then checks all receipts against the current
source. Missing, extra, substituted or stale receipts refuse. Membership outside
every selected boundary also refuses; passing local checks cannot hide newly
unowned files. Changes to source, projections or receipts during inspection prevent
success. Shared changes require new receipts for the owner and every consumer;
private changes require only the affected Component to requalify.
The result remains a read-only `staged` observation under original-source authority,
not a project-wide release receipt, conversion parity or boundary-transfer approval.

Refined conversion consumes this staging API as an internal pre-mutation gate.
Installed custody remains at `retained-source` boundary-transfer state independently
for each Component; it does not mark a Component drafted or qualified and never
retires retained source. Normal project validation reopens the complete bundle,
receipt set, current source and published projections, so tampering or source drift
fails the project-wide gate.

`project retained-scope refresh` detects stale installed Component custody. Its
acknowledged apply additionally requires `--run-component-baselines`, rebuilds the
reviewed source-free bundle from the stored explicit selection, reruns only stale
Component harnesses and reuses receipts whose exact private/shared inputs remain
current. The complete replacement is checked before project authority review is
recorded; ambiguous newly unowned source still refuses and requires a newly reviewed
selection rather than inferred ownership.

The implementation is `adapters/monorepo_components.py`: `stage_monorepo_components`,
`component_retained_revision`, `run_component_retained_harness`, and
`check_component_retained_receipt`, plus `check_monorepo_retained_receipts` for the
complete boundary set. These are internal integration APIs, not new
CLI commands or a substitute for completing #365's adoption transaction.

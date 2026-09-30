# Executing independent child repositories

An orchestration root can delegate retained build commands without converting its
children into root Components. `litai orchestrate plan` reads an explicit lifecycle
declaration and the current Gitlink authority. It neither initializes nor fetches
children. `litai orchestrate run` executes the exact acknowledged plan on the
declared native target.

```sh
litai orchestrate plan build . --declaration lifecycle.json
litai orchestrate run build . --declaration lifecycle.json \
  --expected-plan-identity sha256:REVIEWED_IDENTITY --acknowledge
```

The operations are `build`, `package`, and `containerize`. Each delegates its own
declared child commands; these names do not synthesize a package or an image.
Git/source/framework update remains owned by the separate repository refresh
transaction described below; it is never inferred from lifecycle execution.

## Review and apply exact child pin refreshes

An initialized orchestration root can review published exact child commits and apply
them as one owned local transaction:

```sh
litai onboard orchestrate refresh plan . --request refresh.json
litai onboard orchestrate refresh check . --request refresh.json \
  --expected-plan-identity sha256:REVIEWED_IDENTITY
litai onboard orchestrate refresh apply . --request refresh.json \
  --expected-plan-identity sha256:REVIEWED_IDENTITY --acknowledge
```

The request must be canonical JSON with schema
`literate-ai/orchestration-refresh-request@1` and one or more exact declared paths:

```json
{"schema":"literate-ai/orchestration-refresh-request@1","targets":[{"commit":"0123456789abcdef0123456789abcdef01234567","path":"libraries/core"}]}
```

`plan` and `check` leave the root and children unchanged. They bind current root,
index, lock and child custody to fresh root-declared remote-publication proofs and
the selected repository-fetch deadline policy. Use the same
`--repository-fetch-total-seconds`, `--repository-fetch-no-progress-seconds`, and
`--repository-fetch-connect-seconds` values for all three operations.
Each publication verification, tree capture or object-pack capture may immediately
retry a typed transient Git, transport or deadline failure, at most three total
attempts. Every attempt uses fresh disposable proof storage and the original total
deadline continues to run; retries neither extend it nor change the policy identity
bound into the proof. Semantic failures such as unpublished commits, invalid
advertisements or references changing during proof are not retried.
Disposable proof-storage cleanup never replaces a primary publication refusal.
The primary typed failure remains authoritative for retry classification, with a
simultaneous sanitized cleanup failure retained as its cause. An attempt whose proof
succeeds but whose storage cannot be cleaned still fails safely as a transport error.
When the requested commit is directly advertised by a canonical `refs/heads/*`
branch, publication proof deterministically selects the first exact branch and
fetches only that fully qualified ref, at depth one and without tags, into fresh bare
proof storage. It verifies the exact fetched destination, commit and tree and
requires an equivalent second advertisement. A tag, `HEAD`, peeled tag or other
namespace never selects this shortcut; ancestor and tag publication retain the full
advertised-reference ancestry proof. Source-transition pack capture unshallows only
the selected exact branch and exports it through the same independent empty-object
store connectivity check, so no local or shallow prerequisite can satisfy pack
admission.
Some dumb HTTP transports advertise refs but reject shallow negotiation. An ordinary
Git failure from the depth-one fetch consumes that proof attempt, deletes its
temporary repository, disables the shortcut and retries the full advertised-history
proof in fresh storage. This fallback shares the original absolute total deadline,
remaining no-progress/connect bounds and three-attempt cap. A timeout or no-progress
timeout remains a typed bounded retry of the shallow strategy, while changed refs,
wrong fetched witnesses and other semantic failures after a successful shallow fetch
fail immediately without doing full-fetch work.
Fetch strategy does not choose the witness. Both paths derive it from the same
canonical advertisement and requested commit: the first canonical exact
`refs/heads/*` branch wins even when a generally reachable `refs/changes/*` or tag
sorts earlier. General reachable-ref ordering is used only when no exact branch tip
exists. The full fallback rechecks that preferred branch's exact fetched ref and
resolved commit before issuing the same proof identity.

`apply` requires the exact reviewed plan identity and explicit acknowledgement,
recomputes all local and remote evidence, then stages complete object packs, source,
indexes, references, reflogs and root authority. Verified packs are installed
additively; the root manifest commits last. An exact no-op reports no writes.
Each target reports one identity-bound mode. `source-transition` means the live child
HEAD differs from the reviewed published target and uses the complete bounded
tree/pack and physical-source transaction. `root-pin-only` is available only when a
changed root Gitlink's clean independent child already has that exact published HEAD.
It captures no full local or remote tree, exports and installs no pack, stages no
child bytes, and changes only root index, lock and manifest authority. Mixed requests
apply these rules independently. `source_writes` reports whether any child source or
Git metadata is actually transitioned.

Root-pin-only is not weaker custody. Child root/node, worktree cleanliness, object,
index, HEAD/reference and configuration observations remain exact under live writer
reservations. Root Git and prospective Gitlink URL/branch/configuration binding and
each root-pin-only publication proof are renewed before terminal root commit. Any
child dirtiness, missing object/reference, type or configuration drift, root binding
drift or changed publication proof fails closed and rolls back only still-owned root
changes.
Committed terminal validation repeats exact root and root-pin-only child application
input custody immediately before publishing its journal. A race changing child HEAD,
worktree, index, references, configuration or reachable object therefore cannot be
committed from the earlier proof. Rollback validates restored transaction targets;
it does not incorrectly require the prospective Gitlink inventory or remote proof,
and foreign child bytes are never overwritten to manufacture rollback success.
The same exact custody applies recursively to every observed initialized Gitlink
beneath a root-pin-only top-level child. Nested descendants do not need their own
target mode: path-component ancestry binds their HEAD, index, references,
configuration, worktree and reachable objects through terminal commit. Similar
siblings such as `app2` are not descendants of `app`.
Initialized nested Gitlinks are opaque independent worktrees whose existing custody
is revalidated; uninitialized nested children remain outside the supported refresh
boundary. A previously admitted hydrated LFS file retains its exact physical
payload, node identity and pointer-bound hash when an unrelated path changes.
Canonical `filter.lfs.clean=git-lfs clean -- %f` and
`filter.lfs.process=git-lfs filter-process` configuration is admitted, but
cleanliness status overrides both commands with empty values and sets
`filter.lfs.required=false`, causing raw-byte comparison without any helper process.
The subsection must be spelled exactly `lfs`; Git's case-insensitive section and
terminal variable semantics remain supported. Only an exact regular hydrated
payload whose size and SHA-256 match the indexed canonical pointer is admitted;
pointer bytes stay clean and every malformed, missing, type-changed or unrelated
change still refuses refresh.
Apply also proves the required same-filesystem hardlink primitive before its first
logical mutation.
On Windows, a changed selection containing current or prospective symlinks, or
requiring a directory add/remove/type transition, reports `apply_supported: false`
before acknowledgement. Ordinary file-only changes remain supported. Directory
transitions remain unavailable until native directory-handle custody is qualified.
Children retain independent authority, and the result explicitly leaves child
acceptance unqualified. A changed manifest intentionally makes its previous
documentation-review marker stale; review the new root authority and record a fresh
marker before another validated refresh. A foreign concurrent edit is preserved and
recovery staging is retained for inspection. Saved staging has no replay authority,
and this command does not claim crash-replay support. If a failure is observed after
a committed journal may have been published, the transaction is not rolled back and
the terminal journal/staging evidence is retained for explicit inspection. Terminal
staging remains exact until its marker and all outer refresh reservations release.
If either release or final cleanup fails, the public result reports
`committed-with-cleanup-retained`; inspect `litai-refresh-stage` or a sibling
`.litai-refresh-terminal-*.json` evidence file in the root Git directory. These
records remain evidence only and cannot authorize replay. Cleanup reporting
distinguishes `stage-marker-artifacts` from `refresh-reservation-artifacts`.
Only the recognized reservation-cleanup failure is converted to this committed
result; interruption, resource exhaustion and programming exceptions propagate
after evidence retention. If authority was unchanged but cleanup was retained,
`authority_changed` remains false while `filesystem_writes` and `writes` are true.

A declaration uses schema `literate-ai/repository-lifecycle@1`, a native target
such as `linux-x86_64`, an `inputs` list of root-owned recipe files, and a `children`
list accounting for every indexed Gitlink. Declare root Dockerfiles, resource locks
and helper scripts in `inputs`; their hashes are bound to the execution plan.
Each child declares its exact `path`, `role` (`source`, `sdk`, or `application`),
`dependencies` (provider child paths), and all three `operations`. An unimplemented
operation is explicitly `null`; selecting it fails before any child is started.
A supported operation contains:

```json
{
  "cwd": ".",
  "commands": [["make", "build"]],
  "outputs": ["_build/product"],
  "timeout_seconds": 3600,
  "environment": {}
}
```

Working directories and output paths are relative to that child. Outputs name
actual nonempty files or directories, not glob patterns. Commands are argument
arrays, with no implicit shell expansion. Literal `{root}` and `{child}` tokens
expand to their checkout directories in arguments and explicit environment values.
The same paths are available as `LITAI_ROOT` and `LITAI_CHILD`; `LITAI_TARGET`
names the selected native target. Child recipes own actual dependency wiring;
ordering alone does not make a build consume a provider's artifacts.

All children are selected by default. Repeat `--only PATH` to select an explicit
subset plus its transitive providers. Receipts retain `scope: selected`; subset
success does not qualify the complete stack. The entire declaration must still
cover the inventory and have no dependency cycles.

Execution requires clean selected child worktrees at the exact indexed revisions
and a root binding matching its Gitlinks. Commands run sequentially in dependency
order. A failed command blocks subsequent children. Each run retains a JSON receipt
and per-command stdout/stderr logs under `_build/lifecycle/<run>/`. Timeouts and
handled interruption terminate the owned command process tree. An exclusive root
lock prevents simultaneous runs. Descriptor-backed reservation ownership is checked
before and after commands. Replaced or edited markers are retained and block further
execution; setup failures release unchanged markers owned by the current run. A lock
left by an unhandleable process termination requires explicit inspection and recovery.
Use the retained receipt's PID and plan, when available; another invocation never
silently removes an existing marker.

Use `--resume PATH/receipt.json` with the same current plan to reuse successful
nodes whose complete recorded product hashes, sizes and modes still match.
Changed product bytes trigger execution and invalidate subsequent receipt reuse.
Plan identity binds recipes, pins, target, executor bytes and a digest of the
ambient environment. Environment values are not serialized into receipts.
Tools, downloaded SDK artifacts and runtime acceptance still need the child's own
qualification; command success and output hashes are not independent acceptance.
Every receipt therefore retains `acceptance: not-qualified`.

The current executor runs on one native host. It does not yet dispatch to SSH
workers, schedule parallel children, transport artifacts between workers, or
qualify a fresh full-stack release. Those remain explicit delivery requirements.

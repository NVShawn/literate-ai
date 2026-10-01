# Contributing

## Repository location

The canonical public repository is
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai). Open issues
and pull requests there; release tags, GitHub releases, and the Homebrew formula are
published from it. Its history begins at a sanitized single-root snapshot, so never push
branches or tags whose history predates that root.

Issue, pull request, commit, and CI-run links recorded before 2026-09-30 refer to the
archived `NVIDIA-dev/literate-ai` repository. They remain historical evidence and are
not renumbered.

## Test observable behavior first

Prefer contract and component/integration tests through supported public CLI or Python
APIs. Cover request and response schemas, exit/status codes, documented errors,
backward compatibility, and applicable authentication or authorization behavior. Use
real internal components and realistic persistence; mock only true external boundaries
such as networks, host tools, credentials, clocks, and model providers.

Exercise important stateful behavior as complete scenarios, including relevant
transitions and recovery. Give distinct coverage to malformed input, missing data,
duplicate and idempotent requests, authorization failures, timeouts, retries, partial
failures, boundaries, and concurrency hazards. Use parameterized or property-based
tests when they express related inputs more economically.

Retain method-level unit tests only when isolated logic is complex, safety-critical,
algorithmically subtle, or substantially easier to diagnose independently. Do not add
tests merely for count or line coverage, duplicate the same behavior at multiple
layers, assert internal call sequences, test trivial wrappers/getters, or build
mock-heavy replicas of the implementation. Every test should protect a distinct
contract, failure mode, boundary, or regression.

## Before you open a pull request

Every pull request must contain exactly one release-classification line in its body:

```text
Literate-AI-Release: major.minor
```

Use the active Pre-release target, such as `Literate-AI-Release: 0.8`, when the pull
request belongs on that release line; use `Literate-AI-Release: none` for ordinary work.
Missing, duplicate, malformed, or non-current release values are unknown rather than
silently inferred. Ordinary new work always targets writable `main`. After a release
line is cut, only release-critical fixes belong there, and the fix must land on `main`
first before an authorized backport.

Only identities listed under [`README.md`'s Release Engineers](README.md#release-engineers)
may merge release-line pull requests or create/publish a major or minor release. A
project configured with loose patch authority may additionally permit a listed writer
to perform a break-glass patch operation with an explicit reason; this does not grant
major/minor authority and does not waive the trunk-first rule. LitAI enforces this in its
release commands. Forge branch protection is a separate control and should mirror the
same restrictions; do not assume it is active without verifying the forge settings.

This repository is self-hosting: `litai`, the tool this repository builds, is used
to validate this repository itself. Two of its checks are easy to miss if you don't
already know they exist, and both will fail CI even when your actual change is
correct:

### 1. The lifecycle-driver TCB pin

`literate.project.json` pins a content hash (`driver.pinned_identity`) over every
file under the declared `implementation_paths` — the framework's trusted computing
base. If your change touches anything under `src/literate_ai/` (or another declared
implementation path), that pin goes stale and `make driver-review` (part of
`make python-check`) fails with a message naming the drifted files.

Fix it by re-pinning after you've reviewed what changed:

```
make driver-review-record
```

This updates `literate.project.json`'s pinned identity to match your change. Commit
that file alongside your code change — re-pinning is not optional or automatic in
CI; it has to already be current in your commit.

### 2. The documentation-authority marker

`docs/architecture/design-traceability.md` carries a marker comment:

```
<!-- literate-ai:authority-reviewed sha256:... -->
```

This is a content hash over the project's documentation authority (skill/Component/Flavor
definitions and declared docs other than the tactical `docs/roadmap/` queue). If your
change touches `docs/` outside that queue, `literate.project.json`, or anything else that
feeds this computation, the marker goes stale and `make documentation-review` fails.

Fix it by recording the current reviewed identity after you have reviewed the
changed authority:

```
make documentation-review-record
```

That target runs `litai project documentation-review . --record`, replaces exactly
one placeholder or stale marker, and verifies the result is current. Do not
transcribe a digest by hand. If recording fails because a marker is missing or
duplicated, fix that document first; the command does not write in those cases.
Then re-run `make documentation-review` (read-only) to confirm `"state": "current"`.

## Before every commit

Run the full gate locally so CI doesn't surprise you:

```
make python-check
```

This runs the project's Python test suite plus both checks above (`driver-review`,
`documentation-review`), along with lint and formatting checks. If it passes locally,
CI should pass too.

At cycle boundaries, inspect peer work with `litai project peer-work survey --when
start|end`. Before deleting a local branch, record an exact `merged` or reason-bearing
`dead` lifecycle marker with `litai project peer-work mark`; then run `peer-work gc`
without `--apply` first. Collection rejects stale markers, open pull requests, release or
default branches, unmerged heads, and any dirty worktree or repository state. Actual
deletion additionally requires `--apply --authorize-delete`.

## Why this exists

Literate AI treats its own repository as a project managed by itself — the same
provenance and authority mechanisms it provides to projects built with it apply here
first. See `docs/architecture/design-traceability.md` and
`docs/user/troubleshooting.md` for more on how these checks work and how to recover
from a stale pin or marker mid-development, not just before a commit.

## CI feedback and qualification profiles

Ordinary PRs run one complete Linux/Python 3.11 suite, with repository lint,
OpenSpec and documentation gates in that same conformance job. Three redundant
standalone documentation jobs are removed. Native install/uninstall/reinstall
and sample composition remain on Linux, macOS and Windows; real C++ library
consumption remains on Linux and Windows. Both macOS and Windows also run focused
launcher/update smoke checks. The matrix is declared in `.github/ci-matrix.json`;
`scripts/select_ci_profile.py` selects nine PR cells or all fourteen qualification
cells. The four full macOS partitions are additional qualification jobs.

Dependency download caches are keyed by Python/platform and dependency inputs.
pip uses a 60-second network timeout and three transport retries; failed tests
are never automatically retried. Independent matrix cells finish even if a peer
fails, so the first network failure does not erase other diagnostic results.
The 10–15 minute PR turnaround is a target, not a measured guarantee: the primary
Linux suite and native gates still need hosted runtime measurements.

Ordinary pull requests run the explicitly named macOS smoke check: installation
contracts, launcher/self-update regressions (including a real disposable-prefix
wheel upgrade), and guarded native C++ compilation and execution. The existing
macOS sample-composition check also exercises actual install/uninstall/reinstall.
These checks are **not release qualification** and do not upload qualified wheels.

Pushes to `main` or `release/**`, PRs targeting `release/**`, version-tag pushes,
and manual CI runs retain the complete macOS Python 3.11/3.14 qualification matrix.
The existing independent 3.11 checkpoint partitions are preserved; 3.14 runs all
tests. Full qualification also retains both Linux Python versions, all three
Windows test shards and Windows packaging gates. `make release-check` remains
unchanged. Use a manual CI run on a candidate branch when full platform evidence is needed
before merging. Require full successful exact-commit qualification before release;
a green ordinary PR smoke job is not a substitute.

Conformance test steps have a 120-minute budget, leaving time within the job for
always-run diagnostic uploads. A timeout is a failed qualification, never a pass
or permission to skip a test. CI uses `PYTHON_TEST_VERBOSITY=2` so retained logs
name the running test. Full macOS artifact names include their partition to avoid
collisions. Self-update assertion failures include real pip output and installed
source/bytecode diagnostics. Do not attach credentials or environment dumps.
Keep public PR execution on GitHub-hosted runners, not personal developer Macs.
